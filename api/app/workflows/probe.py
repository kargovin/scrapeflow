from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.activities.contracts import (
        LLM_EXTRACT_ACTIVITY,
        SCRAPE_ACTIVITY,
        LLMInput,
        LLMOutput,
        ScrapeInput,
        ScrapeOutput,
    )
    from app.workflows.queues import LLM_QUEUE, SCRAPE_HTTP_QUEUE, SCRAPE_PLAYWRIGHT_QUEUE

# Not retried: a queue nobody polls fails here instead of waiting forever.
_SCHEDULE_TO_START = timedelta(seconds=60)


Engine = Literal["http", "playwright"]


async def _scrape(input: ScrapeInput, engine: Engine = "http") -> ScrapeOutput:
    if engine == "playwright":
        # goto and wait_for_load_state each get timeout_seconds (BUG-015), + actions and upload.
        render_s = (input.playwright_options or {}).get("timeout_seconds", 60)
        timeouts = {
            "start_to_close_timeout": timedelta(seconds=2 * render_s + 60),
            # The activity heartbeats every 30 s.
            "heartbeat_timeout": timedelta(seconds=90),
        }
        queue = SCRAPE_PLAYWRIGHT_QUEUE
    else:
        timeouts = {"start_to_close_timeout": timedelta(seconds=90)}
        queue = SCRAPE_HTTP_QUEUE
    return await workflow.execute_activity(
        SCRAPE_ACTIVITY,
        input,
        task_queue=queue,
        result_type=ScrapeOutput,
        schedule_to_start_timeout=_SCHEDULE_TO_START,
        retry_policy=RetryPolicy(maximum_attempts=3),
        **timeouts,
    )


class ScrapeProbeInput(BaseModel):
    scrape: ScrapeInput
    engine: Engine = "http"


@workflow.defn
class ScrapeProbeWorkflow:
    """Operator-only: one Scrape activity, no DB, no ledger row — scripts/probe_scrape.py starts it."""

    @workflow.run
    async def run(self, input: ScrapeProbeInput) -> ScrapeOutput:
        return await _scrape(input.scrape, input.engine)


class LLMProbeInput(BaseModel):
    scrape: ScrapeInput
    provider: Literal["anthropic", "openai_compatible"]
    encrypted_api_key: str
    base_url: str | None = None
    model: str
    output_schema: dict[str, Any]


class LLMProbeOutput(BaseModel):
    scrape: ScrapeOutput
    llm: LLMOutput


@workflow.defn
class LLMProbeWorkflow:
    """Operator-only: Scrape, then LLMExtract on the object it wrote — scripts/probe_llm.py."""

    @workflow.run
    async def run(self, input: LLMProbeInput) -> LLMProbeOutput:
        scraped = await _scrape(input.scrape)
        extracted = await workflow.execute_activity(
            LLM_EXTRACT_ACTIVITY,
            LLMInput(
                artifact_id=input.scrape.artifact_id,
                raw_minio_path=scraped.result.path,
                provider=input.provider,
                encrypted_api_key=input.encrypted_api_key,
                base_url=input.base_url,
                model=input.model,
                output_schema=input.output_schema,
            ),
            task_queue=LLM_QUEUE,
            result_type=LLMOutput,
            schedule_to_start_timeout=_SCHEDULE_TO_START,
            # >= warm-up + request: 180 + 180 s in prod (LLMExtract's docstring).
            start_to_close_timeout=timedelta(seconds=400),
            heartbeat_timeout=timedelta(seconds=90),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                backoff_coefficient=2,
                maximum_interval=timedelta(seconds=60),
                maximum_attempts=3,
            ),
        )
        return LLMProbeOutput(scrape=scraped, llm=extracted)
