"""Dispatch-message builders — the one place a run's NATS message is assembled.

A run is dispatched once (``create_job``, ``create_batch``, ``_dispatch_due_jobs``) and
may be re-published later by stale-pending recovery. Recovery re-sends the run as-is, so
every producer for a lane must build the identical message or a recovered run would not
be the run that was lost. Routing every producer through these two functions is what
makes that hold, rather than merely being true today.

The wire models in ``app.messages`` stay ORM-free; this module is the bridge from rows
to messages.

``dispatch_run`` is the one switch between the engines for a job's run (ADR-009 16d): the run's
``lane``, written when the row was inserted, picks NATS or ``JobWorkflow``. A batch's runs share
one lane; on v2 the batch is one ``BatchWorkflow`` (``start_batch``). A crawl has no lane: it is
always one ``CrawlWorkflow`` (``start_crawl``, E.7).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from nats.js import JetStreamContext
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.constants import NATS_JOBS_RUN_HTTP_SUBJECT, NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
from app.core import batch_progress, crawl_progress
from app.core.webhooks import (
    create_batch_webhook_delivery,
    create_crawl_webhook_delivery,
    create_webhook_delivery,
)
from app.messages import Credentials, MessageOptions, ScrapeMessage
from app.models.batch import Batch, BatchItem
from app.models.crawl import Crawl, CrawlQueueItem
from app.models.job import Job
from app.models.job_runs import JobRun
from app.settings import settings
from app.workflows import batch as batch_workflow
from app.workflows import crawl as crawl_workflow
from app.workflows.activities import contracts
from app.workflows.batch import BatchInput, BatchWorkflow
from app.workflows.crawl import CrawlInput, CrawlWorkflow
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


async def cancel_active_runs(db: AsyncSession, job_id: uuid.UUID) -> list[JobRun]:
    """Cancels the job's active runs, notifies each, commits; returns the runs it cancelled."""
    # The status test is in the UPDATE itself: a run that finished while this waited on its row
    # lock (a v2 activity holds it) stays finished instead of being relabelled cancelled.
    runs = list(
        (
            await db.scalars(
                update(JobRun)
                .where(
                    JobRun.job_id == job_id,
                    JobRun.status.in_(("pending", "running", "processing")),
                )
                .values(status="cancelled")
                .returning(JobRun)
            )
        ).all()
    )
    for run in runs:
        await db.execute(
            text("SELECT pg_notify('job_status', :p)"), {"p": f"{run.job_id}:{run.id}:cancelled"}
        )
    await db.commit()
    return runs


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


async def start_batch(
    batch: Batch, items: list[BatchItem], runs: list[JobRun], temporal: TemporalClient
) -> None:
    """Starts a committed v2 batch's workflow. Raises if the engine refused it."""
    client = await temporal()
    await client.start_workflow(
        BatchWorkflow.run,
        BatchInput(
            batch_id=str(batch.id),
            max_in_flight=settings.batch_max_in_flight,
            runs=[
                JobRunInput(
                    run_id=str(run.id),
                    engine=batch.engine,
                    scrape=build_batch_scrape_input(batch, item, run),
                )
                for item, run in zip(items, runs, strict=True)
            ],
        ),
        id=batch_workflow.workflow_id(batch.id),
        task_queue=WORKFLOW_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )


async def fail_unstarted_batch(
    db: AsyncSession, batch: Batch, runs: list[JobRun], exc: Exception
) -> None:
    """A v2 batch whose workflow never started: every item failed, closed as v1 closes one."""
    now = datetime.now(UTC)
    error = f"could not start workflow: {exc}"
    for run in runs:
        run.status, run.error, run.completed_at = "failed", error, now
    await db.execute(
        update(BatchItem)
        .where(BatchItem.batch_id == batch.id)
        .values(status="failed", error=error, completed_at=now)
    )
    await batch_progress.recount(db, batch)
    batch.status = "partial_failure"
    batch.completed_at = now
    if batch.webhook_url:
        await create_batch_webhook_delivery(db, batch, runs[0].id, event="batch.completed")
    await batch_progress.notify(db, batch)
    await db.commit()


async def cancel_batch(db: AsyncSession, batch_id: uuid.UUID) -> list[JobRun] | None:
    """Cancels the batch, its active runs and their items; notifies, commits. Both lanes.

    Returns the runs it cancelled, or None if the batch closed while this waited on its lock.
    """
    # Lock order run → batch → item, as the v2 mirrors take them. The status test is in the
    # UPDATE, as in cancel_active_runs: a run that just finished stays finished.
    runs = list(
        (
            await db.scalars(
                update(JobRun)
                .where(
                    JobRun.batch_item_id.in_(
                        select(BatchItem.id).where(BatchItem.batch_id == batch_id)
                    ),
                    JobRun.status.in_(("pending", "running", "processing")),
                )
                .values(status="cancelled")
                .returning(JobRun)
            )
        ).all()
    )
    batch = await batch_progress.lock(db, batch_id)
    if batch is None or batch.status in batch_progress.TERMINAL:
        await db.rollback()
        return None
    await db.execute(
        update(BatchItem)
        .where(BatchItem.batch_id == batch_id, BatchItem.status.in_(("pending", "running")))
        .values(status="cancelled")
    )
    batch.status = "cancelled"
    await batch_progress.recount(db, batch)
    await batch_progress.notify(db, batch)
    await db.commit()
    return runs


async def cancel_batch_workflow(
    batch_id: uuid.UUID, runs: list[JobRun], temporal: TemporalClient
) -> None:
    """Best effort, after the rows are cancelled — as ``cancel_workflows``. The batch's workflow
    passes the cancel to its children in flight."""
    if not any(run.lane == "v2" for run in runs):
        return
    try:
        client = await temporal()
        await client.get_workflow_handle(batch_workflow.workflow_id(batch_id)).cancel()
    except Exception as exc:
        logger.warning("batch_workflow_cancel_not_sent", batch_id=str(batch_id), error=str(exc))


def build_crawl_input(crawl: Crawl) -> CrawlInput:
    return CrawlInput(
        crawl_id=str(crawl.id),
        seed_url=crawl.seed_url,
        engine=crawl.engine,
        max_depth=crawl.max_depth,
        max_pages=crawl.max_pages,
        respect_robots=crawl.respect_robots,
        ignore_sitemap=crawl.ignore_sitemap,
        max_in_flight=settings.crawl_max_in_flight,
        pages_per_run=settings.crawl_pages_per_run,
    )


async def start_crawl(crawl: Crawl, temporal: TemporalClient) -> None:
    """Starts a committed crawl's workflow. Raises if the engine refused it."""
    client = await temporal()
    await client.start_workflow(
        CrawlWorkflow.run,
        build_crawl_input(crawl),
        id=crawl_workflow.workflow_id(crawl.id),
        task_queue=WORKFLOW_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )


async def fail_unstarted_crawl(db: AsyncSession, crawl: Crawl, exc: Exception) -> None:
    """A crawl whose workflow never started: failed, with its webhook row, as the workflow would."""
    crawl.status = "failed"
    crawl.error = f"could not start workflow: {exc}"
    crawl.completed_at = datetime.now(UTC)
    if crawl.webhook_url:
        await create_crawl_webhook_delivery(db, crawl, event="crawl.failed")
    await crawl_progress.notify(db, crawl)
    await db.commit()


async def cancel_crawl(db: AsyncSession, crawl_id: uuid.UUID) -> Crawl | None:
    """Cancels the crawl and what is left of its frontier; notifies, commits.

    Returns None if the crawl closed while this waited on its lock. A page in flight is left to
    its workflow, whose record drops what it fetched.
    """
    crawl = await crawl_progress.lock(db, crawl_id)
    if crawl is None or crawl.status in crawl_progress.TERMINAL:
        await db.rollback()
        return None
    crawl.status = "cancelled"
    await db.execute(
        update(CrawlQueueItem)
        .where(CrawlQueueItem.crawl_id == crawl_id, CrawlQueueItem.status == "pending")
        .values(status="skipped")
    )
    await crawl_progress.notify(db, crawl)
    await db.commit()
    return crawl


async def cancel_crawl_workflow(crawl_id: uuid.UUID, temporal: TemporalClient) -> None:
    """Best effort, after the row is cancelled: the workflow's next claim or record refuses."""
    try:
        client = await temporal()
        await client.get_workflow_handle(crawl_workflow.workflow_id(crawl_id)).cancel()
    except Exception as exc:
        logger.warning("crawl_workflow_cancel_not_sent", crawl_id=str(crawl_id), error=str(exc))


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


def build_batch_scrape_input(batch: Batch, item: BatchItem, run: JobRun) -> contracts.ScrapeInput:
    """The v2 twin of ``build_batch_scrape_message``: the same values, the activity's shape."""
    return contracts.ScrapeInput(
        artifact_id=str(run.id),
        url=item.url,
        output_format=batch.output_format,
        options=contracts.ScrapeOptions(respect_robots=batch.respect_robots),
    )
