"""Records the paths compose cannot produce live (a webhook that delivers, the storage wall, retries
used up) from the mocked-activity harness into the replay corpus. Writes only missing files.

    docker compose exec api uv run python -m tests.replay.record_harness
"""

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.workflows.job import RECORD_JOB_OBJECTS_ACTIVITY, JobLLM
from app.workflows.pipeline import RUN_CANCELLED_ERROR, StorageUsage
from tests.replay.export_history import HISTORIES
from tests.test_workflows_job import KEY
from tests.test_workflows_job import Harness as JobHarness
from tests.test_workflows_pipeline import CLEAN_GUARD, R6, RUN_INPUT, Harness


async def _webhook_flaky(input):
    if activity.info().attempt < 3:
        raise ApplicationError("HTTP 503", type="WebhookFailed")


async def _webhook_cancelled(input):
    raise ApplicationError("the run was cancelled", type=RUN_CANCELLED_ERROR, non_retryable=True)


async def _llm_overloaded(input):
    raise ApplicationError("503 overloaded", type="LLMTransient")


SCENARIOS = {
    "harness_r6_webhook_delivered_after_retries": ({"webhook": _webhook_flaky}, R6),
    "harness_r6_webhook_run_cancelled_on_retry": ({"webhook": _webhook_cancelled}, R6),
    "harness_clean_validate_llm_completed": ({}, CLEAN_GUARD),
    "harness_llm_retries_exhausted": ({"llm": _llm_overloaded}, RUN_INPUT),
    "harness_storage_wall_completed": ({"usage": StorageUsage(used=150, limit=100)}, RUN_INPUT),
}


async def _scrape_blocked(input):
    raise ApplicationError("blocked:akamai", type="ScrapeFailed", non_retryable=True)


JOB_SCENARIOS = {
    "harness_job_completed": {},
    "harness_job_llm_completed": {"job_llm": KEY},
    "harness_job_scrape_failed": {"scrape": _scrape_blocked},
    "harness_job_llm_key_deleted": {"job_llm": JobLLM(error="LLM key not found or deleted")},
    "harness_job_storage_wall": {
        "job_llm": KEY,
        "replies": {RECORD_JOB_OBJECTS_ACTIVITY: "failed"},
    },
}


async def main() -> None:
    runs = [(name, Harness(**kwargs), run_input) for name, (kwargs, run_input) in SCENARIOS.items()]
    runs += [(name, JobHarness(**kwargs), None) for name, kwargs in JOB_SCENARIOS.items()]
    for name, h, run_input in runs:
        path = HISTORIES / f"{name}.json"
        if path.exists():
            continue
        await (h.run(run_input) if run_input is not None else h.run())
        path.write_text(h.history.to_json() + "\n")
        print(path)


if __name__ == "__main__":
    asyncio.run(main())
