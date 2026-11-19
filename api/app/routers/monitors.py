"""Monitors (ADR-016 §2c): a pinned pipeline version run on a schedule, each check compared with
the last. Every route is owner-scoped: another user's monitor is a 404."""

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core import monitor_events, monitor_schedules
from app.core.db import get_db
from app.core.monitors import ceiling_problems, gate_problems, input_problems, warnings
from app.core.pipeline_trigger import llm_key_ids, owned_llm_keys, run_urls, ssrf_check
from app.core.quota import check_scheduled_jobs_quota, lock_user
from app.core.rate_limit import check_rate_limit
from app.models.monitor import Monitor
from app.models.pipeline import Pipeline, PipelineRun, PipelineVersion
from app.models.user import User
from app.pipelines.definition import (
    DefinitionUnreadable,
    LLMExtractBlock,
    ParsedDefinition,
    parse_definition,
)
from app.pipelines.validator import Problem
from app.routers.jobs import validate_cron
from app.routers.pipelines import require_pipelines_enabled, unprocessable, version_to_run
from app.schemas.monitors import (
    LastCheck,
    MonitorCreate,
    MonitorCreatedResponse,
    MonitorResponse,
    MonitorUpdate,
)
from app.workflows.client import lazy_client

router = APIRouter(prefix="/monitors", tags=["monitors"])
logger = structlog.get_logger()

ENGINE_REFUSED = "Workflow engine unavailable: the schedule was not changed"


def _temporal(request: Request):
    return lambda: lazy_client(request.app.state)


def parse_pinned(version: PipelineVersion) -> ParsedDefinition:
    try:
        parsed, _ = parse_definition(version.definition)
    except DefinitionUnreadable as exc:
        raise unprocessable(
            [Problem(b, f"this version cannot be run: {m}") for b, m in exc.problems]
        ) from None
    return parsed


def missing_key_problems(parsed: ParsedDefinition, keys: dict) -> list[Problem]:
    return [
        Problem(b.id, "LLM key no longer exists")
        for b in parsed.blocks
        if isinstance(b, LLMExtractBlock) and str(b.config.llm_key_id) not in keys
    ]


async def monitor_responses(db: AsyncSession, monitors: list[Monitor]) -> list[MonitorResponse]:
    """The monitors as the API returns them: pinned and newest version, last check, next check."""
    if not monitors:
        return []
    version_ids = {m.pipeline_version_id for m in monitors}
    pinned = dict(
        (
            await db.execute(
                select(PipelineVersion.id, PipelineVersion.version).where(
                    PipelineVersion.id.in_(version_ids)
                )
            )
        ).all()
    )
    newest = dict(
        (
            await db.execute(
                select(PipelineVersion.pipeline_id, func.max(PipelineVersion.version))
                .where(PipelineVersion.pipeline_id.in_({m.pipeline_id for m in monitors}))
                .group_by(PipelineVersion.pipeline_id)
            )
        ).all()
    )
    last = {
        run.monitor_id: run
        for run in await db.scalars(
            select(PipelineRun)
            .where(PipelineRun.monitor_id.in_([m.id for m in monitors]))
            .order_by(PipelineRun.monitor_id, PipelineRun.created_at.desc())
            .distinct(PipelineRun.monitor_id)
        )
    }
    responses = []
    for m in monitors:
        version = pinned[m.pipeline_version_id]
        run = last.get(m.id)
        responses.append(
            MonitorResponse(
                id=m.id,
                name=m.name,
                pipeline_id=m.pipeline_id,
                pipeline_version_id=m.pipeline_version_id,
                version=version,
                newer_version=n if (n := newest.get(m.pipeline_id, 0)) > version else None,
                inputs=m.inputs,
                carried=m.carried,
                schedule_cron=m.schedule_cron,
                state=m.state,
                consecutive_failures=m.consecutive_failures,
                next_check_at=monitor_schedules.next_check(m),
                last_check=(
                    LastCheck(
                        run_id=run.id,
                        status=run.status,
                        change_status=run.change_status,
                        stop_reason=run.stop_reason,
                        created_at=run.created_at,
                        finished_at=run.finished_at,
                    )
                    if run
                    else None
                ),
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
        )
    return responses


async def owned_monitor(
    db: AsyncSession, user_id: uuid.UUID, monitor_id: uuid.UUID, lock: bool = False
) -> Monitor:
    stmt = select(Monitor).where(Monitor.id == monitor_id, Monitor.user_id == user_id)
    monitor = await db.scalar(stmt.with_for_update() if lock else stmt)
    if monitor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitor not found")
    return monitor


def _name_conflict(exc: IntegrityError) -> HTTPException | None:
    if "uq_monitors_user_id_name" in str(exc.orig):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Monitor name in use")
    return None


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if conflict := _name_conflict(exc):
            raise conflict from None
        raise


@router.post("", response_model=MonitorCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_monitor(
    body: MonitorCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    __: None = Depends(require_pipelines_enabled),
) -> MonitorCreatedResponse:
    """ADR-016 §2d's checks, in order; the network ones before the user's lock."""
    user_id = user.id
    # 1. The pipeline, the version and the version's LLM keys.
    pipeline, version = await version_to_run(db, user_id, body.pipeline_id, body.version)
    parsed = parse_pinned(version)
    keys = await owned_llm_keys(db, user_id, llm_key_ids(parsed))
    carried = [c.model_dump() for c in body.carried]
    for problems in (
        missing_key_problems(parsed, keys),
        # 2. Inputs, fixed or carried.
        input_problems(parsed, body.inputs, carried),
    ):
        if problems:
            raise unprocessable(problems)
    # 3. SSRF on what a check reaches, as the trigger checks it.
    for url in run_urls(parsed, body.inputs, keys):
        await ssrf_check(url)
    # 4. The schedule.
    validate_cron(body.schedule_cron)
    # 5. D4's change gate; 6. the ceiling.
    for problems in (gate_problems(parsed), ceiling_problems(parsed, carried)):
        if problems:
            raise unprocessable(problems)

    await lock_user(user_id, db)
    # Again under the lock: a pipeline delete and an LLM-key delete take it too (§2a, §13).
    if (
        await db.scalar(
            select(Pipeline.id).where(Pipeline.id == pipeline.id, Pipeline.deleted_at.is_(None))
        )
        is None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    if problems := missing_key_problems(
        parsed, await owned_llm_keys(db, user_id, llm_key_ids(parsed))
    ):
        raise unprocessable(problems)
    # 7. The scheduled cap, shared with jobs.
    await check_scheduled_jobs_quota(user_id, db)

    monitor = Monitor(
        id=uuid.uuid4(),
        user_id=user_id,
        name=body.name,
        pipeline_id=pipeline.id,
        pipeline_version_id=version.id,
        inputs=body.inputs,
        carried=carried,
        schedule_cron=body.schedule_cron,
        state="active",
        consecutive_failures=0,
    )
    db.add(monitor)
    await _commit(db)

    try:
        await monitor_schedules.put(await lazy_client(request.app.state), monitor)
    except Exception:
        logger.exception("monitor_schedule_put_failed", monitor_id=str(monitor.id))
        monitor.state = "paused"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "monitor_id": str(monitor.id),
                "error": "the schedule could not be armed: the monitor was created paused",
            },
        ) from None

    logger.info("monitor_created", monitor_id=str(monitor.id), pipeline_id=str(pipeline.id))
    [response] = await monitor_responses(db, [monitor])
    return MonitorCreatedResponse(**response.model_dump(), warnings=warnings(parsed))


@router.get("", response_model=list[MonitorResponse])
async def list_monitors(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MonitorResponse]:
    monitors = await db.scalars(
        select(Monitor)
        .where(Monitor.user_id == user.id)
        .order_by(Monitor.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return await monitor_responses(db, list(monitors))


@router.get("/{monitor_id}", response_model=MonitorResponse)
async def get_monitor(
    monitor_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MonitorResponse:
    [response] = await monitor_responses(db, [await owned_monitor(db, user.id, monitor_id)])
    return response


@router.patch("/{monitor_id}", response_model=MonitorResponse)
async def update_monitor(
    monitor_id: uuid.UUID,
    body: MonitorUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
) -> MonitorResponse:
    """The name and the schedule (D15); a cron change on an active monitor re-arms it."""
    monitor = await owned_monitor(db, user.id, monitor_id, lock=True)
    before = monitor.schedule_cron
    if body.schedule_cron is not None:
        validate_cron(body.schedule_cron)
        monitor.schedule_cron = body.schedule_cron
    if body.name is not None:
        monitor.name = body.name
    monitor.updated_at = datetime.now(UTC)
    await _commit(db)

    if monitor.schedule_cron != before:
        try:
            await monitor_schedules.put(await lazy_client(request.app.state), monitor)
        except Exception as exc:
            logger.warning(
                "monitor_schedule_put_failed", monitor_id=str(monitor_id), error=str(exc)
            )
            if monitor.state == "active":
                # The engine would not fire what the row says: put the schedule back.
                monitor.schedule_cron = before
                await db.commit()
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=ENGINE_REFUSED
                ) from None
    [response] = await monitor_responses(db, [monitor])
    return response


@router.post("/{monitor_id}/pause", response_model=MonitorResponse)
async def pause_monitor(
    monitor_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MonitorResponse:
    """The row first, then the Schedule, best effort. Pausing ends any open situation (§7b)."""
    await lock_user(user.id, db)
    monitor = await owned_monitor(db, user.id, monitor_id, lock=True)
    if monitor.state == "active":
        now = datetime.now(UTC)
        monitor.state = "paused"
        monitor.updated_at = now
        await monitor_events.close_open(db, monitor.id, now)
        await db.commit()
        await monitor_schedules.pause(_temporal(request), monitor)
        logger.info("monitor_paused", monitor_id=str(monitor_id))
    [response] = await monitor_responses(db, [monitor])
    return response


@router.post("/{monitor_id}/resume", response_model=MonitorResponse)
async def resume_monitor(
    monitor_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_pipelines_enabled),
) -> MonitorResponse:
    """Checked against the cap like a create; the failure count starts again (D12)."""
    await lock_user(user.id, db)
    monitor = await owned_monitor(db, user.id, monitor_id, lock=True)
    if monitor.state != "active":
        await check_scheduled_jobs_quota(user.id, db, exclude_monitor_id=monitor.id)
        before = (monitor.state, monitor.consecutive_failures)
        monitor.state, monitor.consecutive_failures = "active", 0
        monitor.updated_at = datetime.now(UTC)
        await db.commit()
        try:
            await monitor_schedules.put(await lazy_client(request.app.state), monitor)
        except Exception as exc:
            logger.warning(
                "monitor_schedule_put_failed", monitor_id=str(monitor_id), error=str(exc)
            )
            monitor.state, monitor.consecutive_failures = before
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=ENGINE_REFUSED
            ) from None
        logger.info("monitor_resumed", monitor_id=str(monitor_id))
    [response] = await monitor_responses(db, [monitor])
    return response
