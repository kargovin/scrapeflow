"""Dispatch-message builders — the one place a run's NATS message is assembled.

A run is dispatched once (``create_job``, ``create_batch``, ``_dispatch_due_jobs``) and
may be re-published later by stale-pending recovery. Recovery re-sends the run as-is, so
every producer for a lane must build the identical message or a recovered run would not
be the run that was lost. Routing every producer through these two functions is what
makes that hold, rather than merely being true today.

The wire models in ``app.messages`` stay ORM-free; this module is the bridge from rows
to messages.

``dispatch_run`` is the one switch between the engines for a job's run (ADR-009 16d): the run's
``lane``, written when the row was inserted, picks NATS or ``JobWorkflow``.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from nats.js import JetStreamContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.constants import NATS_JOBS_RUN_HTTP_SUBJECT, NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
from app.core.webhooks import create_webhook_delivery
from app.messages import Credentials, MessageOptions, ScrapeMessage
from app.models.batch import Batch, BatchItem
from app.models.job import Job
from app.models.job_runs import JobRun
from app.workflows.activities import contracts
from app.workflows.job import JobRunInput, JobWorkflow, workflow_id
from app.workflows.queues import WORKFLOW_QUEUE

logger = structlog.get_logger()

TemporalClient = Callable[[], Awaitable[Client]]


def nats_subject(engine: str) -> str:
    return (
        NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT if engine == "playwright" else NATS_JOBS_RUN_HTTP_SUBJECT
    )


async def dispatch_run(
    job: Job,
    run: JobRun,
    credentials: dict | None,
    js: JetStreamContext,
    temporal: TemporalClient,
) -> None:
    """Sends a committed run to its lane's engine. Raises if the engine refused it.

    v1: a failed publish leaves the run pending for stale-pending recovery. v2 has no recovery:
    the caller fails the run (``fail_unstarted_run``).
    """
    if run.lane == "v1":
        message = build_scrape_message(job, run, credentials)
        await js.publish(nats_subject(job.engine), message.to_nats_bytes())
        return
    client = await temporal()
    await client.start_workflow(
        JobWorkflow.run,
        JobRunInput(
            run_id=str(run.id),
            engine=job.engine,
            scrape=build_scrape_input(job, run, credentials),
        ),
        id=workflow_id(run.id),
        task_queue=WORKFLOW_QUEUE,
        # The default is ALLOW_DUPLICATE.
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )


async def fail_unstarted_run(db: AsyncSession, job: Job, run: JobRun, exc: Exception) -> None:
    """A v2 run whose workflow never started: failed, as a worker failure would leave it."""
    run.status = "failed"
    run.error = f"could not start workflow: {exc}"
    run.completed_at = datetime.now(UTC)
    if job.webhook_url and (not job.webhook_events or "job.failed" in job.webhook_events):
        await create_webhook_delivery(
            db, job, run.id, event="job.failed", minio_path=None, error=run.error
        )
    await db.execute(text("SELECT pg_notify('job_status', :p)"), {"p": f"{job.id}:{run.id}:failed"})
    await db.commit()


async def cancel_workflows(runs: list[JobRun], temporal: TemporalClient) -> None:
    """Best effort, after the rows are cancelled: the row is what stops a v2 run (its next mirror
    refuses), so an engine that cannot be told only delays the stop."""
    v2 = [run for run in runs if run.lane == "v2"]
    if not v2:
        return
    try:
        client = await temporal()
        for run in v2:
            await client.get_workflow_handle(workflow_id(run.id)).cancel()
    except Exception as exc:
        logger.warning("job_workflow_cancel_not_sent", error=str(exc))


def build_scrape_message(job: Job, run: JobRun, credentials: dict | None) -> ScrapeMessage:
    """Build the dispatch message for one run of a job, manual or scheduled.

    ``artifact_id`` is the run's id, not the job's (ADR-011 §1): artifacts are keyed on
    the row that produced them, so each run of a recurring job owns its objects outright
    instead of sharing a job-keyed prefix disambiguated by a timestamp.
    ``playwright_options`` ride only on the playwright engine.
    """
    return ScrapeMessage(
        artifact_id=str(run.id),
        run_id=str(run.id),
        url=job.url,
        output_format=job.output_format.value,
        engine=job.engine,
        credentials=Credentials(**credentials) if credentials else None,
        options=MessageOptions(respect_robots=job.respect_robots, actions=job.playwright_actions),
        playwright_options=job.playwright_options if job.engine == "playwright" else None,
    )


def build_scrape_input(job: Job, run: JobRun, credentials: dict | None) -> contracts.ScrapeInput:
    """The v2 twin of ``build_scrape_message``: the same values, the activity's shape (B.1).

    The engine is not in it — it picks the task queue (``queues.scrape_queue``).
    """
    return contracts.ScrapeInput(
        artifact_id=str(run.id),
        url=job.url,
        output_format=job.output_format.value,
        credentials=contracts.Credentials(**credentials) if credentials else None,
        options=contracts.ScrapeOptions(
            respect_robots=job.respect_robots, actions=job.playwright_actions
        ),
        playwright_options=job.playwright_options if job.engine == "playwright" else None,
    )


def build_batch_scrape_message(batch: Batch, item: BatchItem, run: JobRun) -> ScrapeMessage:
    """Build the dispatch message for one batch item's run.

    A batch item is not a job (ADR-006): the lane sends no credentials, actions or
    playwright_options, and the message carries no batch id — workers are unaware they
    are running a batch. Artifacts key on the item's own run, the row that exists,
    rather than on a null job id (ADR-011 §1).
    """
    return ScrapeMessage(
        artifact_id=str(run.id),
        run_id=str(run.id),
        url=item.url,
        output_format=batch.output_format,
        engine=batch.engine,
        options=MessageOptions(respect_robots=batch.respect_robots),
    )
