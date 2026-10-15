from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

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


SCRAPE_RETRY = RetryPolicy(maximum_attempts=3)


def _scrape_timeouts(input: ScrapeInput, engine: Engine) -> dict[str, timedelta]:
    if engine == "playwright":
        # goto and wait_for_load_state each get timeout_seconds (BUG-015), + actions and upload.
        render_s = (input.playwright_options or {}).get("timeout_seconds", 60)
        return {
            "start_to_close_timeout": timedelta(seconds=2 * render_s + 60),
            # The activity heartbeats every 30 s.
            "heartbeat_timeout": timedelta(seconds=90),
        }
    return {"start_to_close_timeout": timedelta(seconds=90)}


async def scrape(input: ScrapeInput, engine: Engine = "http") -> ScrapeOutput:
    return await workflow.execute_activity(
        SCRAPE_ACTIVITY,
        input,
        task_queue=scrape_queue(engine),
        result_type=ScrapeOutput,
        schedule_to_start_timeout=SCHEDULE_TO_START,
        retry_policy=SCRAPE_RETRY,
        **_scrape_timeouts(input, engine),
    )


ACQUIRE_HOST_SLOT_ACTIVITY = "acquire_host_slot"
RELEASE_HOST_SLOT_ACTIVITY = "release_host_slot"

# Past this a scrape goes without a slot: the limiter delays runs, it never fails them.
HOST_WAIT_CAP = timedelta(minutes=10)
HOST_LEASE_MARGIN = timedelta(seconds=60)


class HostSlotInput(BaseModel):
    url: str
    lease_id: str
    lease_ttl_ms: int


class HostSlot(BaseModel):
    # None: no limiter for this scrape (failed open).
    domain: str | None = None
    wait_ms: int = 0


class HostSlotRelease(BaseModel):
    domain: str
    lease_id: str


def host_lease_ttl(input: ScrapeInput, engine: Engine) -> timedelta:
    """The longest ``scrape()`` can hold a slot: every attempt queued and run to its limit."""
    attempts = SCRAPE_RETRY.maximum_attempts
    per_attempt = SCHEDULE_TO_START + _scrape_timeouts(input, engine)["start_to_close_timeout"]
    backoff = sum(
        (
            SCRAPE_RETRY.initial_interval * SCRAPE_RETRY.backoff_coefficient**i
            for i in range(attempts - 1)
        ),
        timedelta(),
    )
    return attempts * per_attempt + backoff + HOST_LEASE_MARGIN


async def _host_slot_activity(name: str, arg: BaseModel, result_type: type | None) -> Any:
    return await workflow.execute_activity(
        name,
        arg,
        task_queue=WORKFLOW_QUEUE,
        result_type=result_type,
        start_to_close_timeout=timedelta(seconds=5),
        retry_policy=RetryPolicy(maximum_attempts=2),
    )


async def _acquire_host_slot(slot_input: HostSlotInput) -> HostSlot:
    try:
        return await _host_slot_activity(ACQUIRE_HOST_SLOT_ACTIVITY, slot_input, HostSlot)
    except ActivityError as exc:
        workflow.logger.warning("Host slot acquire failed, scraping without one: %s", exc.cause)
        return HostSlot()


async def scrape_with_host_slot(input: ScrapeInput, engine: Engine = "http") -> ScrapeOutput:
    """``scrape()`` inside a per-host slot: waits on a durable timer while the host is busy."""
    slot_input = HostSlotInput(
        url=input.url,
        lease_id=str(workflow.uuid4()),
        lease_ttl_ms=int(host_lease_ttl(input, engine).total_seconds() * 1000),
    )
    slot = await _acquire_host_slot(slot_input)
    waited_since = workflow.now()
    while slot.wait_ms > 0:
        if workflow.now() - waited_since >= HOST_WAIT_CAP:
            workflow.logger.warning(
                "Host %s busy past %s, scraping without a slot", slot.domain, HOST_WAIT_CAP
            )
            break
        await workflow.sleep(timedelta(milliseconds=slot.wait_ms))
        slot = await _acquire_host_slot(slot_input)
    try:
        return await scrape(input, engine)
    finally:
        if slot.domain is not None:
            try:
                await _host_slot_activity(
                    RELEASE_HOST_SLOT_ACTIVITY,
                    HostSlotRelease(domain=slot.domain, lease_id=slot_input.lease_id),
                    None,
                )
            except ActivityError as exc:
                workflow.logger.warning("Host slot release failed: %s", exc.cause)


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
