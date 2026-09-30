import uuid
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.activities.contracts import (
    LLM_EXTRACT_ACTIVITY,
    SCRAPE_ACTIVITY,
    LLMInput,
    LLMOutput,
    ScrapeInput,
    ScrapeOutput,
)
from app.workflows.probe import (
    LLMProbeInput,
    LLMProbeWorkflow,
    ScrapeProbeInput,
    ScrapeProbeWorkflow,
)
from app.workflows.queues import (
    LLM_QUEUE,
    SCRAPE_HTTP_QUEUE,
    SCRAPE_PLAYWRIGHT_QUEUE,
    WORKFLOW_QUEUE,
)

# Each stand-in is registered on one scrape queue only, so a workflow that routed the call
# anywhere else would never reach it.

PROBE_INPUT = ScrapeInput(artifact_id="a1", url="https://example.com", output_format="html")


async def _run(
    env: WorkflowEnvironment, scrape, input: ScrapeInput = PROBE_INPUT, engine: str = "http"
) -> ScrapeOutput:
    queue = SCRAPE_PLAYWRIGHT_QUEUE if engine == "playwright" else SCRAPE_HTTP_QUEUE
    async with (
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[ScrapeProbeWorkflow]),
        Worker(env.client, task_queue=queue, activities=[scrape]),
    ):
        return await env.client.execute_workflow(
            ScrapeProbeWorkflow.run,
            ScrapeProbeInput(scrape=input, engine=engine),
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


async def test_playwright_probe_routes_to_its_queue_with_a_budget_from_the_input():
    seen: list[tuple[timedelta | None, timedelta | None]] = []

    @activity.defn(name=SCRAPE_ACTIVITY)
    async def scrape(input: ScrapeInput) -> ScrapeOutput:
        info = activity.info()
        seen.append((info.start_to_close_timeout, info.heartbeat_timeout))
        return ScrapeOutput.model_validate(
            {
                "result": {"path": "scrapeflow-results/history/a1/scrape.html", "size": 42},
                "content_hash": "00c0ffee12345678",
            }
        )

    slow = PROBE_INPUT.model_copy(update={"playwright_options": {"timeout_seconds": 300}})
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        await _run(env, scrape, slow, engine="playwright")

    assert seen == [(timedelta(seconds=660), timedelta(seconds=90))]


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


# ---------------------------------------------------------------------------
# LLMProbeWorkflow — Scrape, then LLMExtract on the object it wrote
# ---------------------------------------------------------------------------

LLM_PROBE_INPUT = LLMProbeInput(
    scrape=PROBE_INPUT,
    provider="anthropic",
    encrypted_api_key="gAAAAAB_key",
    model="claude-haiku-4-5-20251001",
    output_schema={"type": "object"},
)


@activity.defn(name=SCRAPE_ACTIVITY)
async def _scrape_ok(input: ScrapeInput) -> ScrapeOutput:
    return ScrapeOutput.model_validate(
        {
            "result": {"path": "scrapeflow-results/history/a1/scrape.html", "size": 42},
            "content_hash": "00c0ffee12345678",
        }
    )


async def _run_llm_probe(env: WorkflowEnvironment, llm_extract):
    # Each stand-in is registered on its own queue only, so a misrouted call never reaches it.
    async with (
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[LLMProbeWorkflow]),
        Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[_scrape_ok]),
        Worker(env.client, task_queue=LLM_QUEUE, activities=[llm_extract]),
    ):
        return await env.client.execute_workflow(
            LLMProbeWorkflow.run,
            LLM_PROBE_INPUT,
            id=f"llm-probe-{uuid.uuid4()}",
            task_queue=WORKFLOW_QUEUE,
        )


async def test_llm_probe_extracts_from_the_object_the_scrape_wrote():
    received: list[LLMInput] = []

    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm_extract(input: LLMInput) -> LLMOutput:
        received.append(input)
        return LLMOutput.model_validate(
            {"result": {"path": "scrapeflow-results/history/a1/llm.json", "size": 17}}
        )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        output = await _run_llm_probe(env, llm_extract)

    [sent] = received
    assert sent.artifact_id == "a1"
    assert sent.raw_minio_path == "scrapeflow-results/history/a1/scrape.html"
    assert sent.provider == "anthropic"
    assert sent.encrypted_api_key == "gAAAAAB_key"
    assert output.scrape.result.path == "scrapeflow-results/history/a1/scrape.html"
    assert output.llm.result.path == "scrapeflow-results/history/a1/llm.json"


async def test_llm_probe_retries_a_transient_llm_failure_three_times():
    attempts: list[int] = []

    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm_extract(input: LLMInput) -> LLMOutput:
        attempts.append(activity.info().attempt)
        raise ApplicationError("RateLimitError: slow down", type="LLMTransient")

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await _run_llm_probe(env, llm_extract)

    assert attempts == [1, 2, 3]
    assert exc_info.value.cause.cause.type == "LLMTransient"
