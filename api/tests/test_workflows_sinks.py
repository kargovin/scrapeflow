"""M.7 — sinks in PipelineWorkflow: the destination load before each try, the delivery record."""

import asyncio
import itertools
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import update
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError, CancelledError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeoutError

from app.core.db import AsyncSessionLocal
from app.core.destinations import encrypt_secret
from app.models.destination import Destination
from app.models.pipeline import PipelineRunBlock
from app.pipelines.catalog import BUCKET
from app.pipelines.definition import parse_definition
from app.workflows.activities.contracts import BUCKET_DELIVER_ATTEMPT_SECONDS, DeliverBucketInput
from app.workflows.activities.db import load_destination, mirror_block_status
from app.workflows.pipeline import (
    DESTINATION_GONE_ERROR,
    RUN_CANCELLED_ERROR,
    LoadDestinationInput,
    MirrorBlockInput,
    MirrorRunInput,
    PipelineRunInput,
    RecordStorageInput,
    _sink_failure,
)
from app.workflows.queues import DELIVERY_QUEUE
from tests.test_workflows_pipeline import (
    BUCKET_ADDRESS,
    DEFINITION,
    HOOK_URL,
    LLM_PATH,
    RUN_INPUT,
    SCRAPE_PATH,
    Harness,
    _block,
    _bucket_ok,
    _run_rows,
)

STORED_ADDRESS = {
    "endpoint": "https://s3.example.com/",
    "bucket": "exports",
    "prefix": "scrapeflow",
    "region": None,
}
LOCATION = {
    "endpoint": "https://s3.example.com/",
    "bucket": "exports",
    "region": None,
    "key": "scrapeflow/p1/r1/store.json",
}


async def _bucket_destination(user_id, kind="bucket") -> Destination:
    async with AsyncSessionLocal() as db:
        d = Destination(
            user_id=user_id,
            name=f"b-{uuid.uuid4().hex[:6]}",
            kind=kind,
            address=STORED_ADDRESS if kind == "bucket" else {},
            encrypted_secret=encrypt_secret({"access_key": "a", "secret_key": "s"}),
            display_host="s3.example.com",
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
    return d


def _load(run_id, destination_id, kind="bucket"):
    return load_destination(
        LoadDestinationInput(run_id=str(run_id), destination_id=str(destination_id), kind=kind)
    )


# --- load_destination (workflow queue) ---------------------------------------------------------


async def test_load_destination_returns_the_current_address_and_ciphertext(db_user):
    run = await _run_rows(db_user.id)
    destination = await _bucket_destination(db_user.id)

    loaded = await _load(run.id, destination.id)
    assert (loaded.address, loaded.encrypted_secret) == (
        STORED_ADDRESS,
        destination.encrypted_secret,
    )

    # Each try reads it again: an edit takes effect on the next one (ADR-015 §2b).
    moved = {**STORED_ADDRESS, "bucket": "elsewhere"}
    async with AsyncSessionLocal() as db:
        row = await db.get(Destination, destination.id)
        row.address = moved
        await db.commit()
    assert (await _load(run.id, destination.id)).address == moved


async def test_a_deleted_destination_is_final(db_user):
    run = await _run_rows(db_user.id)
    destination = await _bucket_destination(db_user.id)
    async with AsyncSessionLocal() as db:
        await db.delete(await db.get(Destination, destination.id))
        await db.commit()

    with pytest.raises(ApplicationError) as exc:
        await _load(run.id, destination.id)
    assert (exc.value.type, exc.value.non_retryable, exc.value.message) == (
        DESTINATION_GONE_ERROR,
        True,
        "destination deleted",
    )


async def test_another_users_destination_reads_as_deleted(db_user, other_user):
    run = await _run_rows(db_user.id)
    destination = await _bucket_destination(other_user.id)
    with pytest.raises(ApplicationError) as exc:
        await _load(run.id, destination.id)
    assert (exc.value.type, exc.value.message) == (DESTINATION_GONE_ERROR, "destination deleted")


async def test_a_destination_of_another_kind_is_final(db_user):
    run = await _run_rows(db_user.id)
    destination = await _bucket_destination(db_user.id, kind="smtp")
    with pytest.raises(ApplicationError) as exc:
        await _load(run.id, destination.id)
    assert (exc.value.type, exc.value.non_retryable, exc.value.message) == (
        DESTINATION_GONE_ERROR,
        True,
        "destination is a smtp destination, not bucket",
    )


async def test_a_deleted_run_reads_as_cancelled(db_user):
    destination = await _bucket_destination(db_user.id)
    with pytest.raises(ApplicationError) as exc:
        await _load(uuid.uuid4(), destination.id)
    assert (exc.value.type, exc.value.non_retryable) == (RUN_CANCELLED_ERROR, True)


# --- the delivery record (mirror_block_status, completed) --------------------------------------


async def test_a_completed_sink_records_where_it_delivered(db_user):
    run = await _run_rows(db_user.id, (("store", "bucket", "running"),))
    record = {"destination_id": str(uuid.uuid4()), "kind": "bucket", "location": LOCATION}

    await mirror_block_status(
        MirrorBlockInput(
            run_id=str(run.id),
            block_id="store",
            status="completed",
            output_ref="scrapeflow-results/history/a/llm.json",
            delivery=record,
        )
    )
    assert (await _block(run.id, "store")).delivery == record


async def test_a_block_that_delivered_nothing_records_no_delivery(db_user):
    run = await _run_rows(db_user.id, (("store", "bucket", "running"),))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(PipelineRunBlock.pipeline_run_id == run.id)
            .values(delivery={"stale": True})
        )
        await db.commit()
    await mirror_block_status(
        MirrorBlockInput(run_id=str(run.id), block_id="store", status="completed")
    )
    assert (await _block(run.id, "store")).delivery is None


# --- the workflow (time-skipping env, activities mocked) ----------------------------------------

STORE_DESTINATION = "7c2e3d4f-0000-4000-8000-000000000001"
HOOK_DESTINATION = "7c2e3d4f-0000-4000-8000-000000000002"


def _stored(block_id, type, input, config, secrets=None):
    return {
        "id": block_id,
        "type": type,
        "input": input,
        "config_schema_version": 1,
        "config": config,
        "secrets": secrets or {},
    }


STORE = _stored("store", "bucket", "extract", {"destination_id": STORE_DESTINATION})
HOOK = _stored("notify", "webhook", "store", {"destination_id": HOOK_DESTINATION})
INLINE_HOOK = _stored("notify_2", "webhook", "notify", {"url": HOOK_URL, "secret": None})


def _sinks(*blocks, fetch_format="html") -> PipelineRunInput:
    stored = json.loads(json.dumps(DEFINITION))
    stored["blocks"][0]["config"]["output_format"] = fetch_format
    stored["blocks"] += list(blocks)
    parsed, _ = parse_definition(stored)
    return RUN_INPUT.model_copy(
        update={
            "blocks": parsed.blocks,
            "time_budget_seconds": parsed.run_budget_seconds,
            "artifact_ids": {b.id: f"a-{b.id}" for b in parsed.blocks},
        }
    )


BUCKET_THEN_HOOKS = _sinks(STORE, HOOK, INLINE_HOOK)
BUCKET_ONLY = _sinks(STORE)
KEY = "out/p1/r1/store.json"
RECORD = {
    "destination_id": STORE_DESTINATION,
    "kind": "bucket",
    "location": {
        "endpoint": "https://s3.example.com/",
        "bucket": "exports",
        "region": None,
        "key": KEY,
    },
}
LOAD = LoadDestinationInput(run_id="r1", destination_id=STORE_DESTINATION, kind="bucket")


def _flaky_bucket(failures: int):
    seen: list = []

    async def bucket(input):
        if len(seen) < failures:
            seen.append(input)
            raise ApplicationError("HTTP 503 from the bucket", type="DestinationUnavailable")
        return await _bucket_ok(input)

    return bucket


def _patches(history) -> list[str]:
    ids = []
    for event in history.events:
        if event.HasField("marker_recorded_event_attributes"):
            data = event.marker_recorded_event_attributes.details["patch-data"].payloads[0].data
            ids.append(json.loads(data)["id"])
    return ids


async def _refused(input):
    raise ApplicationError(
        "the bucket refused the write (AccessDenied)", type="DestinationRefused", non_retryable=True
    )


async def _gone(input):
    raise ApplicationError("destination deleted", type=DESTINATION_GONE_ERROR, non_retryable=True)


def _sink_calls(calls, block_id="store"):
    return [
        c
        for c in calls
        if isinstance(c, LoadDestinationInput)
        or (isinstance(c, MirrorBlockInput) and c.block_id == block_id)
    ]


async def test_a_bucket_block_loads_its_destination_delivers_once_and_records_where():
    h = Harness()
    result = await h.run(BUCKET_ONLY)

    assert result == LLM_PATH
    [sent] = h.bucket_inputs
    assert sent == DeliverBucketInput(
        address=BUCKET_ADDRESS,
        encrypted_secret="bucket-ciphertext",
        source_path=LLM_PATH,
        pipeline_id="p1",
        run_id="r1",
        block_id="store",
        ext="json",
    )
    assert _sink_calls(h.calls) == [
        MirrorBlockInput(run_id="r1", block_id="store", status="running", input_ref=LLM_PATH),
        LOAD,
        MirrorBlockInput(
            run_id="r1", block_id="store", status="completed", output_ref=LLM_PATH, delivery=RECORD
        ),
    ]
    # An effect block: nothing stored, the run's result stays on the extraction.
    assert not [c for c in h.calls if isinstance(c, RecordStorageInput) and c.block_id == "store"]
    assert h.calls[-1] == MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH)


async def test_one_try_runs_on_the_delivery_queue_with_the_catalogs_timeouts_and_no_retries():
    h = Harness()
    await h.run(BUCKET_ONLY)

    [info] = h.bucket_info
    timing = BUCKET.timing(BUCKET_ONLY.blocks[2].config)
    assert info.task_queue == DELIVERY_QUEUE
    assert info.start_to_close_timeout == timedelta(seconds=BUCKET_DELIVER_ATTEMPT_SECONDS)
    assert info.schedule_to_close_timeout == timedelta(
        seconds=timing.schedule_to_start + timing.start_to_close
    )
    assert (info.attempt, timing.maximum_attempts) == (1, 5)


@pytest.mark.parametrize(
    ("fetch_format", "input_block", "ext"),
    [("html", "fetch", "html"), ("markdown", "fetch", "md"), ("json", "fetch", "json")],
)
async def test_the_object_extension_follows_the_input_it_is_given(fetch_format, input_block, ext):
    store = _stored("store", "bucket", input_block, {"destination_id": STORE_DESTINATION})
    h = Harness()
    await h.run(_sinks(store, fetch_format=fetch_format))
    [sent] = h.bucket_inputs
    assert (sent.source_path, sent.ext) == (SCRAPE_PATH, ext)


async def test_a_transient_failure_waits_reloads_and_tries_again_on_the_delivery_ladder():
    h = Harness(bucket=_flaky_bucket(2))
    result = await h.run(BUCKET_ONLY)

    assert result == LLM_PATH
    assert len(h.bucket_inputs) == 3
    waiting = MirrorBlockInput(
        run_id="r1",
        block_id="store",
        status="waiting",
        error="attempt 1: DestinationUnavailable: HTTP 503 from the bucket",
    )
    running = MirrorBlockInput(run_id="r1", block_id="store", status="running", input_ref=LLM_PATH)
    assert _sink_calls(h.calls) == [
        running,
        LOAD,
        waiting,
        running,
        LOAD,
        waiting.model_copy(
            update={"error": "attempt 2: DestinationUnavailable: HTTP 503 from the bucket"}
        ),
        running,
        LOAD,
        MirrorBlockInput(
            run_id="r1", block_id="store", status="completed", output_ref=LLM_PATH, delivery=RECORD
        ),
    ]
    started = [i.started_time for i in h.bucket_info]
    waits = [round((b - a).total_seconds()) for a, b in itertools.pairwise(started)]
    assert waits == [30, 300]


async def test_five_transient_failures_fail_the_block_and_say_how_many_tries():
    h = Harness(bucket=_flaky_bucket(99))
    result = await h.run(BUCKET_THEN_HOOKS)

    assert isinstance(result, WorkflowFailureError)
    assert len(h.bucket_inputs) == 5
    started = [i.started_time for i in h.bucket_info]
    waits = [round((b - a).total_seconds()) for a, b in itertools.pairwise(started)]
    assert waits == [30, 300, 3000, 7200]
    assert h.delivered == []
    message = "DestinationUnavailable: HTTP 503 from the bucket (gave up after 5 attempts)"
    assert h.calls[-2:] == [
        MirrorBlockInput(run_id="r1", block_id="store", status="failed", error=message),
        MirrorRunInput(
            run_id="r1", status="failed", error=f"store: {message}", failed_block_id="store"
        ),
    ]


async def test_a_final_failure_fails_at_once_and_nothing_after_it_sends():
    h = Harness(bucket=_refused)
    result = await h.run(BUCKET_THEN_HOOKS)

    assert isinstance(result, WorkflowFailureError)
    assert len(h.bucket_inputs) == 1
    assert h.delivered == []
    assert h.calls[-1] == MirrorRunInput(
        run_id="r1",
        status="failed",
        error="store: DestinationRefused: the bucket refused the write (AccessDenied)",
        failed_block_id="store",
    )


async def test_a_destination_gone_at_its_load_fails_the_block_without_a_try():
    h = Harness(load=_gone)
    result = await h.run(BUCKET_ONLY)

    assert isinstance(result, WorkflowFailureError)
    assert h.bucket_inputs == []
    assert h.calls[-1] == MirrorRunInput(
        run_id="r1",
        status="failed",
        error="store: DestinationDeleted: destination deleted",
        failed_block_id="store",
    )


async def test_a_run_deleted_under_the_load_ends_cancelled():
    async def deleted(input):
        raise ApplicationError("the run was deleted", type=RUN_CANCELLED_ERROR, non_retryable=True)

    h = Harness(load=deleted)
    assert await h.run(BUCKET_ONLY) is None
    assert h.calls[-1] == MirrorRunInput(run_id="r1", status="cancelled")


async def test_a_cancel_in_a_sinks_retry_wait_ends_the_wait_and_the_run_with_no_undo():
    async def cancel_in_wait(handle):
        while not h.bucket_inputs:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)  # inside the 30 s wait; nothing skips time here
        await handle.cancel()

    h = Harness(bucket=_flaky_bucket(99))
    result = await h.run(BUCKET_THEN_HOOKS, during=cancel_in_wait)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, CancelledError)
    assert len(h.bucket_inputs) == 1
    assert h.delivered == []
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="store", status="failed", error="cancelled before it finished"
        ),
        MirrorRunInput(run_id="r1", status="cancelled"),
    ]


async def test_a_row_cancel_found_after_the_wait_stops_without_another_try():
    h = Harness(bucket=_flaky_bucket(99))
    h.running_reply["store"] = "running"
    seen = []

    async def bucket(input):
        seen.append(input)
        # The run's cancel is written while the block waits; the next running mirror finds it.
        h.running_reply["store"] = "cancelled"
        raise ApplicationError("HTTP 503 from the bucket", type="DestinationUnavailable")

    h._bucket = bucket
    assert await h.run(BUCKET_ONLY) is None
    assert len(seen) == 1
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="store", status="failed", error="cancelled before it finished"
        ),
        MirrorRunInput(run_id="r1", status="cancelled"),
    ]


async def test_a_cancel_during_a_try_lets_it_finish_and_keeps_what_it_delivered():
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(input):
        started.set()
        await release.wait()
        return await _bucket_ok(input)

    async def cancel_mid_put(handle):
        await started.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(bucket=slow)
    result = await h.run(BUCKET_THEN_HOOKS, during=cancel_mid_put)

    assert isinstance(result, WorkflowFailureError)
    # Cancel never rolls back (ADR-014 §7): delivered, recorded, and nothing after it runs.
    assert h.delivered == []
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="store", status="completed", output_ref=LLM_PATH, delivery=RECORD
        ),
        MirrorRunInput(run_id="r1", status="cancelled"),
    ]


async def test_a_cancel_after_a_transient_failure_does_not_wait():
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_then_down(input):
        started.set()
        await release.wait()
        raise ApplicationError("HTTP 503 from the bucket", type="DestinationUnavailable")

    async def cancel_mid_put(handle):
        await started.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(bucket=slow_then_down)
    result = await h.run(BUCKET_ONLY, during=cancel_mid_put)

    assert isinstance(result, WorkflowFailureError)
    assert len(h.bucket_inputs) == 1
    assert [c.status for c in _sink_calls(h.calls) if isinstance(c, MirrorBlockInput)] == [
        "running",
        "failed",
    ]


async def test_a_wait_past_the_runs_deadline_gives_up_instead_of_sleeping():
    h = Harness(bucket=_flaky_bucket(99))
    # Enough for the blocks before and one try, not for the 300 s wait before the third.
    result = await h.run(BUCKET_ONLY.model_copy(update={"time_budget_seconds": 200}))

    assert isinstance(result, WorkflowFailureError)
    assert len(h.bucket_inputs) == 2
    assert h.calls[-1].error == (
        "store: DestinationUnavailable: HTTP 503 from the bucket "
        "(the run's time budget ran out after 2 attempts)"
    )


async def test_several_webhooks_each_send_their_block_id_and_the_deliveries_before_them():
    h = Harness()
    result = await h.run(BUCKET_THEN_HOOKS)

    assert result == LLM_PATH
    first, second = h.delivered
    entry = {"block_id": "store", "kind": "bucket", "location": RECORD["location"]}
    assert (first.block_id, first.url, first.destination_id, first.deliveries) == (
        "notify",
        None,
        HOOK_DESTINATION,
        [entry],
    )
    assert (second.block_id, second.url, second.destination_id, second.deliveries) == (
        "notify_2",
        HOOK_URL,
        None,
        [entry],
    )
    assert [(c.block_id, c.status) for c in h.calls if isinstance(c, MirrorBlockInput)][-2:] == [
        ("notify_2", "running"),
        ("notify_2", "completed"),
    ]


async def test_only_a_run_with_a_sink_asks_the_patch():
    h = Harness()
    await h.run(RUN_INPUT)
    assert "sinks" not in _patches(h.history)
    h = Harness()
    await h.run(BUCKET_ONLY)
    assert "sinks" in _patches(h.history)


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (
            ApplicationError("x", type="DestinationUnavailable"),
            ("DestinationUnavailable: x", False),
        ),
        (
            ApplicationError("x", type="DestinationRefused", non_retryable=True),
            ("DestinationRefused: x", True),
        ),
        (
            ActivityTimeoutError(
                "t", type=TimeoutType.SCHEDULE_TO_START, last_heartbeat_details=[]
            ),
            ("no delivery worker took the delivery", False),
        ),
        (
            ActivityTimeoutError("t", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]),
            ("the delivery did not finish in time", False),
        ),
        (RuntimeError("?"), ("the delivery failed (RuntimeError)", True)),
    ],
)
def test_a_tries_failure_is_the_workers_answer_a_timeout_transient_else_final(cause, expected):
    assert _sink_failure(cause) == expected
