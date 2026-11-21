"""Monitors (ADR-016 §2c): a pinned pipeline version run on a schedule, each check compared with
the last. Every route is owner-scoped: another user's monitor is a 404."""

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from miniopy_async import Minio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.auth.dependencies import get_current_admin_user, get_current_user
from app.core import monitor_events, monitor_schedules
from app.core.db import get_db
from app.core.destinations import notice_destination_problem
from app.core.minio import get_minio
from app.core.monitors import (
    ceiling_problems,
    find_baseline,
    gate_problems,
    input_problems,
    prepare_check,
    warnings,
)
from app.core.pipeline_trigger import (
    build_run,
    llm_key_ids,
    mark_not_started,
    owned_llm_keys,
    run_urls,
    ssrf_check,
)
from app.core.quota import admit, check_scheduled_jobs_quota, lock_user
from app.core.rate_limit import check_rate_limit
from app.models.monitor import Monitor, MonitorEvent
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
from app.routers.pipelines import (
    check_destinations,
    release_runs,
    request_cancel,
    require_pipelines_enabled,
    run_response,
    run_summary,
    runs_select,
    unprocessable,
    version_to_run,
)
from app.schemas.monitors import (
    AdminMonitorDetail,
    AdminMonitorResponse,
    LastCheck,
    MonitorCreate,
    MonitorCreatedResponse,
    MonitorEventResponse,
    MonitorHistoryItem,
    MonitorResponse,
    MonitorUpdate,
)
from app.schemas.pipelines import PipelineRunResponse
from app.settings import settings
from app.workflows.client import get_temporal, get_temporal_or_none, lazy_client
from app.workflows.pipeline import PipelineWorkflow
from app.workflows.queues import WORKFLOW_QUEUE

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
                notice_destination_id=m.notice_destination_id,
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


async def check_notice_destination(
    db: AsyncSession, user_id: uuid.UUID, destination_id: uuid.UUID | None
) -> None:
    """404 for one gone or another user's, 422 for a bucket or postgres one. Under ``lock_user``,
    as a destination delete (ADR-015 §2c)."""
    if destination_id is None:
        return
    try:
        problem = await notice_destination_problem(db, user_id, destination_id)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Destination not found"
        ) from None
    if problem is not None:
        raise unprocessable([Problem(None, f"notice_destination_id: {problem}")])


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
    await check_notice_destination(db, user_id, body.notice_destination_id)
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
        notice_destination_id=body.notice_destination_id,
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
    return MonitorCreatedResponse(
        **response.model_dump(),
        warnings=warnings(parsed, has_notice_destination=monitor.notice_destination_id is not None),
    )


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
    """The name, the schedule and the notice destination (D15); a cron change on an active
    monitor re-arms it."""
    if body.schedule_cron is not None:
        validate_cron(body.schedule_cron)
    # User then monitor, as a destination delete reads the notice setting under the user's lock.
    await lock_user(user.id, db)
    monitor = await owned_monitor(db, user.id, monitor_id, lock=True)
    before = monitor.schedule_cron
    if body.schedule_cron is not None:
        monitor.schedule_cron = body.schedule_cron
    if body.name is not None:
        monitor.name = body.name
    if "notice_destination_id" in body.model_fields_set:
        await check_notice_destination(db, user.id, body.notice_destination_id)
        monitor.notice_destination_id = body.notice_destination_id
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
        now = datetime.now(UTC)
        monitor.state, monitor.consecutive_failures = "active", 0
        monitor.updated_at = now
        # A self-pause's situation ends here; a skip after this starts a fresh one (§7b).
        await monitor_events.close_open(db, monitor.id, now)
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


@router.delete("/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_monitor(
    monitor_id: uuid.UUID,
    request: Request,
    with_checks: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio: Minio = Depends(get_minio),
    temporal: Client | None = Depends(get_temporal_or_none),
) -> Response:
    """ADR-016 §11. A running check is cancelled. Plain: the checks stay as ordinary runs of the
    pipeline. ``with_checks``: every check, its stored objects and rows go too (503 if an object
    cannot be removed: nothing is deleted)."""
    await lock_user(user.id, db)
    monitor = await owned_monitor(db, user.id, monitor_id, lock=True)
    checks = list(
        await db.scalars(
            select(PipelineRun).where(PipelineRun.monitor_id == monitor.id).with_for_update()
        )
    )
    released = None
    if with_checks:
        released = await release_runs(db, minio, temporal, checks)
        await db.execute(delete(PipelineRun).where(PipelineRun.monitor_id == monitor.id))
    else:
        await request_cancel(db, temporal, checks)
    await db.execute(delete(Monitor).where(Monitor.id == monitor.id))
    await db.commit()
    await monitor_schedules.forget(_temporal(request), [monitor_id])
    logger.info(
        "monitor_deleted",
        monitor_id=str(monitor_id),
        checks=len(checks),
        with_checks=with_checks,
        bytes_freed=released.freed_bytes if released else 0,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{monitor_id}/check",
    response_model=PipelineRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def check_now(
    monitor_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    __: None = Depends(require_pipelines_enabled),
    temporal: Client = Depends(get_temporal),
) -> PipelineRunResponse:
    """A check now (ADR-016 §3d): admission's rules, answered while the user waits, and the
    run started directly — no firing, no Schedule ``trigger()`` (``SKIP`` would drop it
    silently while a firing is open)."""
    user_id = user.id
    monitor = await owned_monitor(db, user_id, monitor_id)
    version = await db.get(PipelineVersion, monitor.pipeline_version_id)
    parsed = parse_pinned(version)
    _, secrets = parse_definition(version.definition)
    keys = await owned_llm_keys(db, user_id, llm_key_ids(parsed))
    if problems := missing_key_problems(parsed, keys):
        raise unprocessable(problems)
    # Before the lock: DNS lookups.
    for url in run_urls(parsed, monitor.inputs, keys):
        await ssrf_check(url)

    await lock_user(user_id, db)
    running = await db.scalar(
        select(PipelineRun.id).where(
            PipelineRun.monitor_id == monitor.id, PipelineRun.status == "running"
        )
    )
    if running is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "check_in_progress",
                "message": "A check of this monitor is running; wait for it to end.",
                "run_id": str(running),
            },
        )
    # §8d's headroom, as any trigger.
    await admit(user_id, db, headroom_bytes=settings.storage_headroom_bytes)
    await check_destinations(db, user_id, parsed.blocks)
    await db.refresh(monitor)
    inputs, check = await prepare_check(db, monitor, parsed, await find_baseline(db, monitor.id))
    built = build_run(
        run_id=uuid.uuid4(),
        user_id=user_id,
        version=version,
        parsed=parsed,
        stored_secrets=secrets,
        keys=keys,
        inputs=inputs,
        check=check,
    )
    run, blocks = built.run, built.blocks
    db.add(run)
    db.add_all(blocks)
    # A check started ends whatever situation the monitor was in (§7b).
    await monitor_events.close_open(db, monitor.id, datetime.now(UTC))
    await db.commit()

    try:
        await temporal.start_workflow(
            PipelineWorkflow.run,
            built.input,
            id=run.workflow_id,
            task_queue=WORKFLOW_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except Exception as exc:
        logger.exception("monitor_check_start_failed", run_id=str(run.id))
        # Counted like any failed check, alerts queued, in one transaction (§7a).
        await mark_not_started(db, run, blocks, exc, lambda: temporal)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"run_id": str(run.id), "error": run.error},
        ) from None
    logger.info("monitor_checked_now", monitor_id=str(monitor_id), run_id=str(run.id))
    return run_response(run, version.version, blocks)


async def monitor_history(
    db: AsyncSession, monitor_id: uuid.UUID, limit: int
) -> list[MonitorHistoryItem]:
    """Checks and situations merged by time, newest first (R9); a run of identical skipped
    firings is one counted line (G1)."""
    checks = (
        await db.execute(runs_select().where(PipelineRun.monitor_id == monitor_id).limit(limit))
    ).all()
    events = await db.scalars(
        select(MonitorEvent)
        .where(MonitorEvent.monitor_id == monitor_id)
        .order_by(MonitorEvent.first_at.desc())
        .limit(limit)
    )
    items = [
        MonitorHistoryItem(type="check", at=run.created_at, check=run_summary(run, version))
        for run, version in checks
    ]
    items += [
        MonitorHistoryItem(
            type="event",
            at=e.first_at,
            event=MonitorEventResponse.model_validate(e, from_attributes=True),
        )
        for e in events
    ]
    return sorted(items, key=lambda i: i.at, reverse=True)[:limit]


@router.get("/{monitor_id}/history", response_model=list[MonitorHistoryItem])
async def get_monitor_history(
    monitor_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MonitorHistoryItem]:
    monitor = await owned_monitor(db, user.id, monitor_id)
    return await monitor_history(db, monitor.id, limit)


# ---------------------------------------------------------------------------
# Admin (R9: read-only)
# ---------------------------------------------------------------------------

admin_router = APIRouter(prefix="/admin/monitors", tags=["admin"])


async def _admin_responses(db: AsyncSession, rows) -> list[AdminMonitorResponse]:
    monitors = [m for m, _ in rows]
    emails = {m.id: email for m, email in rows}
    return [
        AdminMonitorResponse(**r.model_dump(), user_id=m.user_id, user_email=emails[m.id])
        for m, r in zip(monitors, await monitor_responses(db, monitors), strict=True)
    ]


@admin_router.get("", response_model=list[AdminMonitorResponse])
async def admin_list_monitors(
    user_id: uuid.UUID | None = Query(default=None),
    state: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> list[AdminMonitorResponse]:
    stmt = select(Monitor, User.email).join(User, User.id == Monitor.user_id)
    if user_id is not None:
        stmt = stmt.where(Monitor.user_id == user_id)
    if state is not None:
        stmt = stmt.where(Monitor.state == state)
    stmt = stmt.order_by(Monitor.created_at.desc()).limit(limit).offset(offset)
    return await _admin_responses(db, (await db.execute(stmt)).all())


@admin_router.get("/{monitor_id}", response_model=AdminMonitorDetail)
async def admin_get_monitor(
    monitor_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> AdminMonitorDetail:
    row = (
        await db.execute(
            select(Monitor, User.email)
            .join(User, User.id == Monitor.user_id)
            .where(Monitor.id == monitor_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitor not found")
    [response] = await _admin_responses(db, [row])
    return AdminMonitorDetail(
        **response.model_dump(), history=await monitor_history(db, monitor_id, limit)
    )
