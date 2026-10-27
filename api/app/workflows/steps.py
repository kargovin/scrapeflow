import asyncio
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, FailureError

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
# Scrapes queue behind other runs' fan-out (E.0); 5 min still catches a queue nobody polls.
SCRAPE_SCHEDULE_TO_START = timedelta(minutes=5)


Engine = Literal["http", "playwright"]


SCRAPE_RETRY = RetryPolicy(maximum_attempts=3)


def activity_error(exc: ActivityError) -> str:
    # The worker's own message, as v1 stored it (e.g. "blocked:akamai").
    cause = exc.cause
    if isinstance(cause, FailureError):
        return cause.message
    return str(cause or exc)


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
        schedule_to_start_timeout=SCRAPE_SCHEDULE_TO_START,
        retry_policy=SCRAPE_RETRY,
        **_scrape_timeouts(input, engine),
    )


ACQUIRE_HOST_SLOT_ACTIVITY = "acquire_host_slot"
RELEASE_HOST_SLOT_ACTIVITY = "release_host_slot"
# ADR-012: the per-host slot around a released workflow's scrape (E.9).
HOST_SLOT_PATCH = "host-slot"

# Past this a scrape goes without a slot: the limiter delays runs, it never fails them.
HOST_WAIT_CAP = timedelta(minutes=10)
HOST_LEASE_MARGIN = timedelta(seconds=60)
# Re-asks double from 1 s to 30 s: a wait to the cap is ~25 acquires, not ~600 (history size).
HOST_POLL_FIRST = timedelta(seconds=1)
HOST_POLL_MAX = timedelta(seconds=30)
HOST_POLL_JITTER_MS = 250
# One acquire or release, retries included (2 attempts x 5 s + 1 s backoff).
HOST_SLOT_ACTIVITY_TIMEOUT = timedelta(seconds=15)


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
    per_attempt = (
        SCRAPE_SCHEDULE_TO_START + _scrape_timeouts(input, engine)["start_to_close_timeout"]
    )
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
        schedule_to_close_timeout=HOST_SLOT_ACTIVITY_TIMEOUT,
        retry_policy=RetryPolicy(maximum_attempts=2),
    )


async def _acquire_host_slot(slot_input: HostSlotInput) -> HostSlot:
    try:
        return await _host_slot_activity(ACQUIRE_HOST_SLOT_ACTIVITY, slot_input, HostSlot)
    except ActivityError as exc:
        workflow.logger.warning("Host slot acquire failed, scraping without one: %s", exc.cause)
        return HostSlot()


async def _through_cancel[T](coro: Coroutine[Any, Any, T]) -> tuple[T, bool]:
    """Awaits ``coro`` to completion even if cancelled meanwhile; also says whether it was."""
    task = asyncio.ensure_future(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


def _poll_delay(asks: int, wait_ms: int) -> timedelta:
    # The limiter's own answer first (exact for the rate window), then backing off.
    wait = timedelta(milliseconds=wait_ms)
    if asks == 0:
        return wait
    jitter = timedelta(milliseconds=workflow.random().randint(0, HOST_POLL_JITTER_MS))
    return max(wait, min(HOST_POLL_FIRST * 2**asks, HOST_POLL_MAX) + jitter)


async def wait_for_host_slot(url: str, lease_ttl: timedelta) -> HostSlotRelease | None:
    """Takes ``url``'s per-host slot, waiting on durable timers while the host is busy.

    Returns the lease for ``release_host_slot``; None = nothing to release (failed open, or
    waited past ``HOST_WAIT_CAP``). A cancel ends the wait and is re-raised, the lease released.
    """
    slot_input = HostSlotInput(
        url=url,
        lease_id=str(workflow.uuid4()),
        lease_ttl_ms=int(lease_ttl.total_seconds() * 1000),
    )
    waited_since = workflow.now()
    asks = 0
    while True:
        # A cancel must not land between Redis granting the lease and the workflow learning it.
        slot, cancelled = await _through_cancel(_acquire_host_slot(slot_input))
        held = (
            HostSlotRelease(domain=slot.domain, lease_id=slot_input.lease_id)
            if slot.domain is not None and slot.wait_ms == 0
            else None
        )
        if cancelled:
            await _through_cancel(release_host_slot(held))
            raise asyncio.CancelledError()
        if slot.domain is None or slot.wait_ms == 0:
            return held
        waited = workflow.now() - waited_since
        if waited >= HOST_WAIT_CAP:
            workflow.logger.warning(
                "Host %s busy past %s, scraping without a slot", slot.domain, HOST_WAIT_CAP
            )
            return None
        await workflow.sleep(min(_poll_delay(asks, slot.wait_ms), HOST_WAIT_CAP - waited))
        asks += 1


async def release_host_slot(held: HostSlotRelease | None) -> None:
    if held is None:
        return
    try:
        await _host_slot_activity(RELEASE_HOST_SLOT_ACTIVITY, held, None)
    except ActivityError as exc:
        # The lease expires on its own.
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
