from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.activities.contracts import SCRAPE_ACTIVITY, ScrapeInput, ScrapeOutput
    from app.workflows.queues import SCRAPE_HTTP_QUEUE


@workflow.defn
class ScrapeProbeWorkflow:
    """Operator-only: one Scrape activity, no DB, no ledger row — scripts/probe_scrape.py starts it."""

    @workflow.run
    async def run(self, input: ScrapeInput) -> ScrapeOutput:
        return await workflow.execute_activity(
            SCRAPE_ACTIVITY,
            input,
            task_queue=SCRAPE_HTTP_QUEUE,
            result_type=ScrapeOutput,
            # Not retried: a queue nobody polls fails here instead of waiting forever.
            schedule_to_start_timeout=timedelta(seconds=60),
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
