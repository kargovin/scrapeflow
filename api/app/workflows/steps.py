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
    from app.workflows.queues import LLM_QUEUE, WORKFLOW_QUEUE, scrape_queue

# Not retried: a queue nobody polls fails here instead of waiting forever.
SCHEDULE_TO_START = timedelta(seconds=60)


Engine = Literal["http", "playwright"]


async def scrape(input: ScrapeInput, engine: Engine = "http") -> ScrapeOutput:
    if engine == "playwright":
        # goto and wait_for_load_state each get timeout_seconds (BUG-015), + actions and upload.
        render_s = (input.playwright_options or {}).get("timeout_seconds", 60)
        timeouts = {
            "start_to_close_timeout": timedelta(seconds=2 * render_s + 60),
            # The activity heartbeats every 30 s.
            "heartbeat_timeout": timedelta(seconds=90),
        }
    else:
        timeouts = {"start_to_close_timeout": timedelta(seconds=90)}
    return await workflow.execute_activity(
        SCRAPE_ACTIVITY,
        input,
        task_queue=scrape_queue(engine),
        result_type=ScrapeOutput,
        schedule_to_start_timeout=SCHEDULE_TO_START,
        retry_policy=RetryPolicy(maximum_attempts=3),
        **timeouts,
    )


async def llm_extract(input: LLMInput) -> LLMOutput:
    return await workflow.execute_activity(
        LLM_EXTRACT_ACTIVITY,
        input,
        task_queue=LLM_QUEUE,
        result_type=LLMOutput,
        schedule_to_start_timeout=SCHEDULE_TO_START,
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


async def db(activity: str, arg: BaseModel, result_type: type) -> Any:
    """An app-DB activity on the workflow worker: short, retried until its window closes."""
    return await workflow.execute_activity(
        activity,
        arg,
        task_queue=WORKFLOW_QUEUE,
        result_type=result_type,
        start_to_close_timeout=timedelta(seconds=10),
        schedule_to_close_timeout=timedelta(seconds=20),
        retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=5)),
    )
