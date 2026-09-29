import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.activities.contracts import SCRAPE_ACTIVITY, ScrapeInput, ScrapeOutput
from app.workflows.probe import ScrapeProbeWorkflow
from app.workflows.queues import SCRAPE_HTTP_QUEUE, WORKFLOW_QUEUE

# The stand-in for the Go activity is registered on SCRAPE_HTTP_QUEUE only, so a workflow that
# routed the call anywhere else would never reach it.

PROBE_INPUT = ScrapeInput(artifact_id="a1", url="https://example.com", output_format="html")


async def _run(env: WorkflowEnvironment, scrape) -> ScrapeOutput:
    async with (
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ScrapeProbeWorkflow]),
        Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
    ):
        return await env.client.execute_workflow(
            ScrapeProbeWorkflow.run,
            PROBE_INPUT,
            id=f"scrape-probe-{uuid.uuid4()}",
            task_queue=WORKFLOW_QUEUE,
        )


async def test_probe_returns_the_scrape_output():
    received: list[ScrapeInput] = []

    @activity.defn(name=SCRAPE_ACTIVITY)
    async def scrape(input: ScrapeInput) -> ScrapeOutput:
        received.append(input)
        return ScrapeOutput.model_validate(
            {
                "result": {"path": "scrapeflow-results/history/a1/scrape.html", "size": 42},
                "content_hash": "00c0ffee12345678",
            }
        )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        output = await _run(env, scrape)

    assert received == [PROBE_INPUT]
    assert output.result.path == "scrapeflow-results/history/a1/scrape.html"
    assert output.content_hash == "00c0ffee12345678"


async def test_probe_does_not_retry_a_non_retryable_failure():
    attempts: list[int] = []

    @activity.defn(name=SCRAPE_ACTIVITY)
    async def scrape(input: ScrapeInput) -> ScrapeOutput:
        attempts.append(activity.info().attempt)
        raise ApplicationError("fetch failed", type="ScrapeFailed", non_retryable=True)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await _run(env, scrape)

    assert attempts == [1]
    activity_error = exc_info.value.cause
    assert isinstance(activity_error, ActivityError)
    assert isinstance(activity_error.cause, ApplicationError)
    assert activity_error.cause.type == "ScrapeFailed"
