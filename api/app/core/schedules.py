"""A v2 job's Temporal Schedule (F.1) — one per job, ``job-schedule-{job_id}``.

The jobs row says whether a schedule is armed; the Schedule is only its clock. The admission
activity refuses a tick on a job that is paused, removed or not v2's, so a Schedule the API failed
to pause or delete fires into a no-op. Arming is the direction that needs the engine: a write that
arms is undone if the Schedule cannot be put.
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

from app.models.job import Job
from app.workflows import scheduled_job
from app.workflows.queues import WORKFLOW_QUEUE
from app.workflows.scheduled_job import ScheduledJobInput, ScheduledJobWorkflow

logger = structlog.get_logger()

# ADR-013 §5, set explicitly on every Schedule rather than inherited from the SDK.
CATCHUP_WINDOW = timedelta(days=365)


def schedule_id(job_id: object) -> str:
    return f"job-schedule-{job_id}"


def is_v2(job: Job) -> bool:
    return job.schedule_lane == "v2" and job.schedule_cron is not None


def next_run(job: Job, now: datetime | None = None) -> datetime | None:
    """When the job next fires, or None when it is not armed.

    It is the cron's, the Schedule's spec in UTC; a tick the Schedule drops under SKIP still has
    this time, it just makes no run. A v1 schedule has none: nothing fires it since F.6.
    """
    if job.schedule_status != "active" or job.schedule_cron is None:
        return None
    if job.schedule_lane != "v2":
        return None
    return croniter(job.schedule_cron, now or datetime.now(UTC)).get_next(datetime)


def build(job: Job) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ScheduledJobWorkflow.run,
            ScheduledJobInput(job_id=str(job.id)),
            id=scheduled_job.workflow_id(job.id),
            task_queue=WORKFLOW_QUEUE,
        ),
        spec=ScheduleSpec(cron_expressions=[job.schedule_cron]),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=CATCHUP_WINDOW,
            pause_on_failure=False,
        ),
        state=ScheduleState(paused=job.schedule_status != "active"),
    )


async def put(client: Client, job: Job) -> None:
    """Creates the job's Schedule, or replaces it (cron, paused) with what the row says."""
    schedule = build(job)
    try:
        await client.create_schedule(schedule_id(job.id), schedule)
    except ScheduleAlreadyRunningError:

        def replace(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=schedule)

        await client.get_schedule_handle(schedule_id(job.id)).update(replace)


async def delete(client: Client, job_id: uuid.UUID) -> None:
    try:
        await client.get_schedule_handle(schedule_id(job_id)).delete()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise


async def sync(client: Client, job: Job) -> None:
    """Makes the engine match the row: a Schedule for a v2 schedule, paused or not; else none."""
    if is_v2(job) and job.schedule_status in ("active", "paused"):
        await put(client, job)
    else:
        await delete(client, job.id)


async def forget(temporal, job_ids: list[uuid.UUID]) -> None:
    """Best effort, for jobs about to be deleted: a Schedule left behind fires into ``stopped``."""
    if not job_ids:
        return
    try:
        client = await temporal()
        for job_id in job_ids:
            await delete(client, job_id)
    except Exception as exc:
        logger.warning(
            "job_schedule_delete_failed", job_ids=[str(j) for j in job_ids], error=str(exc)
        )
