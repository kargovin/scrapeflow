"""Dispatch-message builders — the one place a run's NATS message is assembled.

A run is dispatched once (``create_job``, ``create_batch``, ``_dispatch_due_jobs``) and
may be re-published later by stale-pending recovery. Recovery re-sends the run as-is, so
every producer for a lane must build the identical message or a recovered run would not
be the run that was lost. Routing every producer through these two functions is what
makes that hold, rather than merely being true today.

The wire models in ``app.messages`` stay ORM-free; this module is the bridge from rows
to messages.
"""

from app.messages import Credentials, MessageOptions, ScrapeMessage
from app.models.batch import Batch, BatchItem
from app.models.job import Job
from app.models.job_runs import JobRun


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
