"""A monitor's Temporal Schedule (ADR-016 §3a) — one per monitor, ``monitor-schedule-{id}``.

ADR-013's job pattern, copied: the monitors row says whether the monitor is armed; the Schedule
is only its clock. Admission refuses a firing whose monitor is not ``active``, so a Schedule the
API failed to pause or delete fires into a no-op. Arming (create, resume, a cron change while
active) is undone if the Schedule cannot be put.
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from croniter import croniter
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

from app.models.monitor import Monitor
from app.workflows import monitor_check
from app.workflows.monitor_check import MONITOR_CHECK_WORKFLOW, MonitorCheckInput
from app.workflows.queues import WORKFLOW_QUEUE

logger = structlog.get_logger()

# ADR-013 §5's values, set explicitly on every Schedule rather than inherited from the SDK.
CATCHUP_WINDOW = timedelta(days=365)


def schedule_id(monitor_id: object) -> str:
    return f"monitor-schedule-{monitor_id}"


def next_check(monitor: Monitor, now: datetime | None = None) -> datetime | None:
    """When the monitor next fires (the cron, UTC), or None when it is not active."""
    if monitor.state != "active":
        return None
    return croniter(monitor.schedule_cron, now or datetime.now(UTC)).get_next(datetime)


def build(monitor: Monitor) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            MONITOR_CHECK_WORKFLOW,
            MonitorCheckInput(monitor_id=str(monitor.id)),
            id=monitor_check.workflow_id(monitor.id),
            task_queue=WORKFLOW_QUEUE,
        ),
        spec=ScheduleSpec(cron_expressions=[monitor.schedule_cron]),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=CATCHUP_WINDOW,
            # Self-pause is ScrapeFlow's, from the checks' outcomes (§7a).
            pause_on_failure=False,
        ),
        state=ScheduleState(paused=monitor.state != "active"),
    )


async def put(client: Client, monitor: Monitor) -> None:
    """Creates the monitor's Schedule, or replaces it (cron, paused) with what the row says."""
    schedule = build(monitor)
    try:
        await client.create_schedule(schedule_id(monitor.id), schedule)
    except ScheduleAlreadyRunningError:

        def replace(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=schedule)

        await client.get_schedule_handle(schedule_id(monitor.id)).update(replace)


async def delete(client: Client, monitor_id: uuid.UUID) -> None:
    try:
        await client.get_schedule_handle(schedule_id(monitor_id)).delete()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise


async def pause(temporal, monitor: Monitor) -> None:
    """Best effort: the row already stops every firing at admission."""
    try:
        await put(await temporal(), monitor)
    except Exception as exc:
        logger.warning("monitor_schedule_pause_failed", monitor_id=str(monitor.id), error=str(exc))


async def forget(temporal, monitor_ids: list[uuid.UUID]) -> None:
    """Best effort, for monitors deleted or about to be: a Schedule left behind fires into
    ``stopped``."""
    if not monitor_ids:
        return
    try:
        client = await temporal()
        for monitor_id in monitor_ids:
            await delete(client, monitor_id)
    except Exception as exc:
        logger.warning(
            "monitor_schedule_delete_failed",
            monitor_ids=[str(m) for m in monitor_ids],
            error=str(exc),
        )
