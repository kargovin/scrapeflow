"""A monitor firing's admission (ADR-016 §3b) — ``admit_scheduled_run``'s monitor-shaped twin.

The version parse and the SSRF lookups run before ``lock_user`` (no network calls inside it);
everything after is one transaction under it: the meters, the overlap rule, the baseline lookup,
the insert, and every ``monitor_events`` write.
"""

import uuid
from asyncio import get_running_loop
from datetime import UTC, datetime

import structlog
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from app.core import monitor_events, webhook_delivery
from app.core.db import AsyncSessionLocal
from app.core.destinations import reference_problems
from app.core.monitor_checks import after_commit, monitor_check_ended
from app.core.monitors import find_baseline, prepare_check
from app.core.pipeline_alerts import queue_failure_alerts
from app.core.pipeline_trigger import (
    build_run,
    llm_key_ids,
    owned_llm_keys,
    run_input,
    run_urls,
    workflow_id,
)
from app.core.quota import _first_of_next_month, is_quota_exceeded, lock_user
from app.core.security import validate_no_ssrf_core
from app.models.monitor import Monitor, MonitorEvent
from app.models.pipeline import PipelineRun, PipelineRunBlock, PipelineVersion
from app.pipelines.catalog import destination_refs
from app.pipelines.definition import DefinitionUnreadable, LLMExtractBlock, parse_definition
from app.settings import settings
from app.workflows.monitor_check import (
    ADMIT_MONITOR_CHECK_ACTIVITY,
    AdmitMonitorCheckInput,
    MonitorAdmission,
)

logger = structlog.get_logger()

# ADR-010 §1: the error names the remedy — storage never frees itself.
STORAGE_FULL = (
    "storage_quota_exceeded: storage is full — delete old results to resume this monitor's checks"
)


async def _client():
    return activity.client()


async def _cannot_run(monitor: Monitor, version: PipelineVersion | None, db: AsyncSession):
    """Before the lock: why the pinned version cannot run now, or None (§3b, G3)."""
    if version is None:
        return "the pinned version no longer exists", None
    try:
        parsed, secrets = parse_definition(version.definition)
    except DefinitionUnreadable as exc:
        return f"this version cannot be run: {exc}", None
    keys = await owned_llm_keys(db, monitor.user_id, llm_key_ids(parsed))
    loop = get_running_loop()
    for url in run_urls(parsed, monitor.inputs, keys):
        try:
            await loop.run_in_executor(None, validate_no_ssrf_core, url)
        except ValueError as exc:
            return f"{url}: {exc}", None
    return None, (parsed, secrets)


async def _missing_keys(db: AsyncSession, monitor: Monitor, parsed) -> str | None:
    """§13: a key or destination deleted anyway (outside the API's interlock) is a failed check."""
    keys = await owned_llm_keys(db, monitor.user_id, llm_key_ids(parsed))
    missing = [
        b.id
        for b in parsed.blocks
        if isinstance(b, LLMExtractBlock) and str(b.config.llm_key_id) not in keys
    ]
    if missing:
        return f"{missing[0]}: LLM key no longer exists"
    # Under the lock, as the trigger (ADR-015 §2c).
    gone, wrong_kind = await reference_problems(
        db, monitor.user_id, destination_refs(parsed.blocks)
    )
    if gone:
        return f"{gone[0]}: destination no longer exists"
    if wrong_kind:
        return f"{wrong_kind[0][0]}: {wrong_kind[0][1]}"
    return None


async def _fail_check(
    db: AsyncSession,
    monitor: Monitor,
    version: PipelineVersion | None,
    run_id: uuid.UUID,
    error: str,
    now: datetime,
) -> MonitorAdmission:
    """A check that never fetched: a failed run whose blocks never ran — it costs one monthly
    run and counts towards self-pause (ADR-010 §1, G3). The third writer of ``failed``."""
    run = PipelineRun(
        id=run_id,
        pipeline_id=monitor.pipeline_id,
        pipeline_version_id=monitor.pipeline_version_id,
        user_id=monitor.user_id,
        status="failed",
        inputs=monitor.inputs,
        workflow_id=workflow_id(run_id),
        error=error,
        finished_at=now,
        monitor_id=monitor.id,
    )
    db.add(run)
    definition = version.definition if version is not None else None
    raw = definition.get("blocks") if isinstance(definition, dict) else None
    db.add_all(
        PipelineRunBlock(
            pipeline_run_id=run_id,
            block_id=b["id"],
            position=i,
            type=b["type"],
            status="skipped",
            status_reason="not_started",
        )
        for i, b in enumerate(raw if isinstance(raw, list) else [])
        if isinstance(b, dict) and isinstance(b.get("id"), str) and isinstance(b.get("type"), str)
    )
    await db.flush()
    await monitor_events.close_open(db, monitor.id, now)
    try:
        alerts = await queue_failure_alerts(db, run)
    except DefinitionUnreadable:
        alerts = 0
    ended = await monitor_check_ended(db, run)
    await db.commit()
    if alerts:
        await webhook_delivery.start_pending(activity.client, db, pipeline_run_id=run_id)
    await after_commit(_client, ended)
    logger.warning("monitor_check_failed_at_admission", monitor_id=str(monitor.id), error=error)
    return MonitorAdmission(verdict="failed")


async def _skip(
    db: AsyncSession, monitor: Monitor, kind: str, now: datetime, detail=None
) -> MonitorAdmission:
    await monitor_events.record(db, monitor.id, kind, now, detail)
    await db.commit()
    logger.info("monitor_firing_skipped", monitor_id=str(monitor.id), reason=kind)
    return MonitorAdmission(verdict="skipped")


async def _wait(db: AsyncSession, monitor: Monitor, now: datetime) -> MonitorAdmission:
    """One row per firing that waits: its first ask starts it, a re-ask only moves last_at."""
    open_row = await db.scalar(
        select(MonitorEvent).where(
            MonitorEvent.monitor_id == monitor.id,
            MonitorEvent.kind == "waiting_for_slot",
            MonitorEvent.ended_at.is_(None),
        )
    )
    if open_row is None:
        await monitor_events.record(db, monitor.id, "waiting_for_slot", now)
    else:
        open_row.last_at = now
    await db.commit()
    return MonitorAdmission(verdict="waiting")


async def _rebuilt(db: AsyncSession, monitor: Monitor, run: PipelineRun) -> MonitorAdmission:
    """A retry of a call that committed: the same check, its input rebuilt from its rows."""
    if run.status != "running":
        return MonitorAdmission(verdict="failed" if run.status == "failed" else "stopped")
    version = await db.get(PipelineVersion, run.pipeline_version_id)
    parsed, secrets = parse_definition(version.definition)
    keys = await owned_llm_keys(db, monitor.user_id, llm_key_ids(parsed))
    baseline = await db.get(PipelineRun, run.baseline_run_id) if run.baseline_run_id else None
    _, check = await prepare_check(db, monitor, parsed, baseline)
    blocks = list(
        await db.scalars(
            select(PipelineRunBlock)
            .where(PipelineRunBlock.pipeline_run_id == run.id)
            .order_by(PipelineRunBlock.position)
        )
    )
    return MonitorAdmission(
        verdict="admitted", run=run_input(run, blocks, parsed, secrets, keys, check)
    )


@activity.defn(name=ADMIT_MONITOR_CHECK_ACTIVITY)
async def admit_monitor_check(input: AdmitMonitorCheckInput) -> MonitorAdmission:
    """ADR-016 §3b, in order. A skip is a monitor event, never a ``pipeline_runs`` row — the
    monthly meter counts every row."""
    run_id = uuid.UUID(input.run_id)
    async with AsyncSessionLocal() as db:
        monitor = await db.get(Monitor, uuid.UUID(input.monitor_id))
        if monitor is None:
            return MonitorAdmission(verdict="stopped")
        version = await db.get(PipelineVersion, monitor.pipeline_version_id)
        problem, pinned = await _cannot_run(monitor, version, db)

        await lock_user(monitor.user_id, db)
        if (run := await db.get(PipelineRun, run_id)) is not None:
            return await _rebuilt(db, monitor, run)
        await db.refresh(monitor)
        if monitor.state != "active":
            return MonitorAdmission(verdict="stopped")

        now = datetime.now(UTC)
        if not settings.pipelines_enabled:
            return await _skip(db, monitor, "skipped_pipelines_disabled", now)
        running = await db.scalar(
            select(
                exists().where(
                    PipelineRun.monitor_id == monitor.id, PipelineRun.status == "running"
                )
            )
        )
        if running:
            return await _skip(db, monitor, "skipped_check_in_progress", now)
        if await is_quota_exceeded(monitor.user_id, db, "monthly_runs"):
            detail = {"resets_at": _first_of_next_month(now).isoformat()}
            return await _skip(db, monitor, "skipped_monthly_runs", now, detail)
        if problem is None and pinned is not None:
            problem = await _missing_keys(db, monitor, pinned[0])
        if problem is not None:
            return await _fail_check(db, monitor, version, run_id, problem, now)
        if await is_quota_exceeded(monitor.user_id, db, "storage_bytes"):
            return await _fail_check(db, monitor, version, run_id, STORAGE_FULL, now)
        if await is_quota_exceeded(monitor.user_id, db, "concurrent_jobs"):
            if input.last_chance:
                return await _skip(db, monitor, "gave_up_waiting", now)
            return await _wait(db, monitor, now)

        parsed, secrets = pinned
        keys = await owned_llm_keys(db, monitor.user_id, llm_key_ids(parsed))
        inputs, check = await prepare_check(
            db, monitor, parsed, await find_baseline(db, monitor.id)
        )
        built = build_run(
            run_id=run_id,
            user_id=monitor.user_id,
            version=version,
            parsed=parsed,
            stored_secrets=secrets,
            keys=keys,
            inputs=inputs,
            check=check,
        )
        db.add(built.run)
        db.add_all(built.blocks)
        # A check admitted ends whatever situation was going on (§7b).
        await monitor_events.close_open(db, monitor.id, now)
        await db.commit()
        logger.info("monitor_check_admitted", monitor_id=str(monitor.id), run_id=str(run_id))
        return MonitorAdmission(verdict="admitted", run=built.input)
