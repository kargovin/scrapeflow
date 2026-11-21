"""M.8 — rollback (ADR-014 §7): the rollback mirrors, the alert's rollback lists, the workflow."""

import asyncio
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import func, select, update
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeoutError

from app.core.db import AsyncSessionLocal
from app.core.pipeline_status import PIPELINE_STATUS_CHANNEL
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.models.quota_views import quota_active_submissions
from app.models.webhook_delivery import WebhookDelivery
from app.pipelines.catalog import BUCKET_UNDO_TIMING
from app.settings import settings
from app.workflows.activities.contracts import (
    BUCKET_UNDO_ATTEMPT_SECONDS,
    NOT_WRITTEN,
    UndoBucketInput,
)
from app.workflows.activities.db import (
    mirror_rollback_started,
    mirror_run_status,
    mirror_undo_status,
)
from app.workflows.pipeline import (
    RUN_CANCELLED_ERROR,
    MirrorBlockInput,
    MirrorRunInput,
    MirrorUndoInput,
    RollbackStartedInput,
    _may_have_written,
)
from app.workflows.queues import DELIVERY_QUEUE
from tests.test_pipeline_alerts import ALERT_URL, _definition, _webhook
from tests.test_pipeline_alerts import _run as _alerting_run
from tests.test_workflows_pipeline import (
    LLM_PATH,
    Harness,
    _block,
    _bucket_ok,
    _load_ok,
    _run_rows,
)
from tests.test_workflows_sinks import (
    BUCKET_ONLY,
    BUCKET_THEN_HOOKS,
    HOOK,
    INLINE_HOOK,
    LOAD,
    STORE,
    STORE_DESTINATION,
    _gone,
    _patches,
    _refused,
    _sinks,
    _stored,
)

LOCATION = {
    "endpoint": "https://s3.example.com/",
    "bucket": "exports",
    "region": None,
    "key": "out/p1/r1/store.json",
}
RECORD = {"destination_id": str(uuid.uuid4()), "kind": "bucket", "location": LOCATION}
DEADLINE = datetime(2026, 11, 21, 14, 20, tzinfo=UTC)
ROLLING_BACK = (
    ("fetch", "scrape", "completed"),
    ("store", "bucket", "completed"),
    ("notify", "webhook", "failed"),
    ("after", "webhook", "pending"),
)


async def _delivered(run_id, *block_ids):
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(
                PipelineRunBlock.pipeline_run_id == run_id,
                PipelineRunBlock.block_id.in_(block_ids),
            )
            .values(delivery=RECORD)
        )
        await db.commit()


def _started(run_id) -> RollbackStartedInput:
    return RollbackStartedInput(
        run_id=str(run_id),
        failed_block_id="notify",
        error="notify: HTTP 503 (gave up after 5 attempts)",
        rollback_deadline=DEADLINE,
    )


async def _listen():
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(PIPELINE_STATUS_CHANNEL, lambda *a: received.put_nowait(a[3]))
    return conn, received


# --- mirror_rollback_started ----------------------------------------------------------------------


async def test_rollback_started_keeps_the_run_running_and_records_what_broke(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK)
    conn, received = await _listen()
    try:
        assert await mirror_rollback_started(_started(run.id)) == "running"
        payload = json.loads(await asyncio.wait_for(received.get(), 5))
    finally:
        await conn.close()

    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.status, row.failed_block_id, row.rollback_deadline, row.error) == (
        "running",
        "notify",
        DEADLINE,
        "notify: HTTP 503 (gave up after 5 attempts)",
    )
    assert (payload["status"], payload["rolling_back"]) == ("running", True)


async def test_rollback_started_is_not_refused_for_a_cancel_requested(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK, cancel_requested_at=datetime.now(UTC))
    assert await mirror_rollback_started(_started(run.id)) == "running"
    async with AsyncSessionLocal() as db:
        assert (await db.get(PipelineRun, run.id)).rollback_deadline == DEADLINE


async def test_rollback_started_on_a_deleted_run_reads_as_cancelled(db_user):
    assert await mirror_rollback_started(_started(uuid.uuid4())) == "cancelled"


async def test_a_run_undoing_holds_no_concurrency_slot(db_user):
    # E2: the failed block is failed, the delivered one completed; the undo lives in undo_status.
    run = await _run_rows(db_user.id, ROLLING_BACK)
    await _delivered(run.id, "store")
    await mirror_rollback_started(_started(run.id))
    await mirror_undo_status(MirrorUndoInput(run_id=str(run.id), undo={"store": "undoing"}))
    async with AsyncSessionLocal() as db:
        held = await db.scalar(
            select(func.count())
            .select_from(quota_active_submissions)
            .where(quota_active_submissions.c.submission_id == run.id)
        )
    assert held == 0


# --- mirror_undo_status ----------------------------------------------------------------------------


async def test_an_undo_moves_undoing_then_undone_and_notifies_each(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK)
    await _delivered(run.id, "store")
    rid = str(run.id)
    conn, received = await _listen()
    try:
        assert await mirror_undo_status(MirrorUndoInput(run_id=rid, undo={"store": "undoing"})) == (
            "running"
        )
        assert (await _block(run.id, "store")).undo_status == "undoing"
        await mirror_undo_status(MirrorUndoInput(run_id=rid, undo={"store": "undone"}))
        payloads = [json.loads(await asyncio.wait_for(received.get(), 5)) for _ in range(2)]
    finally:
        await conn.close()

    block = await _block(run.id, "store")
    assert (block.status, block.undo_status, block.undo_error, block.delivery) == (
        "completed",
        "undone",
        None,
        RECORD,
    )
    assert [(p["block_id"], p["block_status"], p["block_undo_status"]) for p in payloads] == [
        ("store", "completed", "undoing"),
        ("store", "completed", "undone"),
    ]


async def test_an_undo_that_failed_records_why(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK)
    await _delivered(run.id, "store")
    await mirror_undo_status(
        MirrorUndoInput(
            run_id=str(run.id),
            undo={"store": "undo_failed"},
            error="DestinationRefused: the bucket refused the delete (AccessDenied)",
        )
    )
    block = await _block(run.id, "store")
    assert (block.undo_status, block.undo_error) == (
        "undo_failed",
        "DestinationRefused: the bucket refused the delete (AccessDenied)",
    )


async def test_kept_and_not_undoable_are_one_call(db_user):
    blocks = (
        ("store", "bucket", "completed"),
        ("notify", "webhook", "completed"),
        ("check", "validate", "failed"),
    )
    run = await _run_rows(db_user.id, blocks)
    await _delivered(run.id, "store")
    await mirror_undo_status(
        MirrorUndoInput(run_id=str(run.id), undo={"store": "kept", "notify": "not_undoable"})
    )
    assert [(b, (await _block(run.id, b)).undo_status) for b in ("store", "notify", "check")] == [
        ("store", "kept"),
        ("notify", "not_undoable"),
        ("check", None),
    ]


async def test_an_undo_is_written_on_a_run_whose_cancel_was_requested(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK, cancel_requested_at=datetime.now(UTC))
    await _delivered(run.id, "store")
    await mirror_undo_status(MirrorUndoInput(run_id=str(run.id), undo={"store": "undone"}))
    assert (await _block(run.id, "store")).undo_status == "undone"


async def test_only_a_completed_block_takes_an_undo_status(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK)
    with pytest.raises(ApplicationError) as exc:
        await mirror_undo_status(MirrorUndoInput(run_id=str(run.id), undo={"notify": "undone"}))
    assert exc.value.non_retryable
    assert (await _block(run.id, "notify")).undo_status is None


async def test_an_undo_on_a_deleted_run_reads_as_cancelled(db_user):
    assert (
        await mirror_undo_status(MirrorUndoInput(run_id=str(uuid.uuid4()), undo={"s": "undone"}))
        == "cancelled"
    )


# --- the failure alert carries the rollback lists (ADR-014 §8c) ---------------------------------

BUCKET_BLOCK = {
    "type": "bucket",
    "input": "fetch",
    "config_schema_version": 1,
    "config": {"destination_id": RECORD["destination_id"]},
    "secrets": {},
}
ROLLED_BACK = _definition(
    {**BUCKET_BLOCK, "id": "store"},
    {**BUCKET_BLOCK, "id": "store_2", "input": "store"},
    {**BUCKET_BLOCK, "id": "store_3", "input": "store_2"},
    _webhook("alert", ALERT_URL, ["pipeline.failed"]),
)


def _entry(block_id):
    return {"block_id": block_id, "kind": "bucket", "location": LOCATION}


async def test_the_alert_is_queued_after_rollback_and_lists_how_each_delivery_ended(db_user):
    run = await _alerting_run(db_user.id, ROLLED_BACK)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(
                PipelineRunBlock.pipeline_run_id == run.id,
                PipelineRunBlock.block_id.in_(("fetch", "store", "store_2", "store_3")),
            )
            .values(status="completed")
        )
        await db.commit()
    await _delivered(run.id, "store", "store_2", "store_3")
    rid = str(run.id)
    await mirror_rollback_started(_started(run.id))
    await mirror_undo_status(MirrorUndoInput(run_id=rid, undo={"store_3": "undone"}))
    await mirror_undo_status(
        MirrorUndoInput(run_id=rid, undo={"store_2": "undo_failed"}, error="refused")
    )
    await mirror_undo_status(MirrorUndoInput(run_id=rid, undo={"store": "undone"}))
    await mirror_run_status(
        MirrorRunInput(run_id=rid, status="failed", error="store_3: x", failed_block_id="store_3")
    )

    async with AsyncSessionLocal() as db:
        [alert] = (
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        ).all()
    assert alert.payload["rollback"] == {
        "undone": [_entry("store"), _entry("store_3")],
        "left_in_place": [_entry("store_2")],
        "kept": [],
    }


async def test_deliveries_kept_after_an_announcement_are_listed_as_kept(db_user):
    run = await _alerting_run(db_user.id, ROLLED_BACK)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(PipelineRunBlock.pipeline_run_id == run.id, PipelineRunBlock.block_id == "store")
            .values(status="completed", delivery=RECORD, undo_status="kept")
        )
        await db.commit()
    await mirror_run_status(
        MirrorRunInput(run_id=str(run.id), status="failed", error="x: y", failed_block_id="x")
    )
    async with AsyncSessionLocal() as db:
        [alert] = (
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        ).all()
    assert alert.payload["rollback"] == {
        "undone": [],
        "left_in_place": [],
        "kept": [_entry("store")],
    }


async def test_a_failure_with_nothing_delivered_lists_nothing(db_user):
    run = await _alerting_run(db_user.id, ROLLED_BACK)
    await mirror_run_status(
        MirrorRunInput(
            run_id=str(run.id), status="failed", error="fetch: x", failed_block_id="fetch"
        )
    )
    async with AsyncSessionLocal() as db:
        [alert] = (
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        ).all()
    assert alert.payload["rollback"] == {"undone": [], "left_in_place": [], "kept": []}


# --- the workflow (time-skipping env, activities mocked) ----------------------------------------

STORE_2 = _stored("store_2", "bucket", "store", {"destination_id": STORE_DESTINATION})
HOOK_AFTER_TWO = _stored("notify", "webhook", "store_2", {"url": "https://hooks.example.com/in"})
TWO_BUCKETS_THEN_HOOK = _sinks(STORE, STORE_2, HOOK_AFTER_TWO)
HOOKS_ONLY = _sinks({**HOOK, "input": "extract"}, INLINE_HOOK)


def _key(block_id):
    return f"out/p1/r1/{block_id}.json"


def _undo_of(block_id):
    return UndoBucketInput(
        location={
            "endpoint": "https://s3.example.com/",
            "bucket": "exports",
            "region": None,
            "key": _key(block_id),
        },
        encrypted_secret="bucket-ciphertext",
    )


async def _hook_refused(input):
    raise ApplicationError("ssrf_blocked: private", type="WebhookBlocked", non_retryable=True)


def _hook_refused_on(block_id):
    async def webhook(input):
        if input.block_id == block_id:
            await _hook_refused(input)

    return webhook


async def _undo_refused(input):
    raise ApplicationError(
        "the bucket refused the delete (AccessDenied)",
        type="DestinationRefused",
        non_retryable=True,
    )


def _flaky_undo(failures: int):
    seen: list = []

    async def undo(input):
        if len(seen) < failures:
            seen.append(input)
            raise ApplicationError("HTTP 503 from the bucket", type="DestinationUnavailable")

    return undo


def _rollback_calls(calls):
    """Everything from the rollback's first mirror on."""
    first = next(
        i for i, c in enumerate(calls) if isinstance(c, RollbackStartedInput | MirrorUndoInput)
    )
    return calls[first:]


def _failed_run(block_id, message):
    return MirrorRunInput(
        run_id="r1", status="failed", error=f"{block_id}: {message}", failed_block_id=block_id
    )


REFUSED = "WebhookBlocked: ssrf_blocked: private"


async def test_a_failure_after_deliveries_undoes_each_newest_first_then_fails_the_run():
    h = Harness(webhook=_hook_refused)
    result = await h.run(TWO_BUCKETS_THEN_HOOK)

    assert isinstance(result, WorkflowFailureError)
    assert h.undo_inputs == [_undo_of("store_2"), _undo_of("store")]
    [started, *rest] = _rollback_calls(h.calls)
    assert started == RollbackStartedInput(
        run_id="r1",
        failed_block_id="notify",
        error=f"notify: {REFUSED}",
        rollback_deadline=started.rollback_deadline,
    )
    assert rest == [
        MirrorUndoInput(run_id="r1", undo={"store_2": "undoing"}),
        LOAD,
        MirrorUndoInput(run_id="r1", undo={"store_2": "undone"}),
        MirrorUndoInput(run_id="r1", undo={"store": "undoing"}),
        LOAD,
        MirrorUndoInput(run_id="r1", undo={"store": "undone"}),
        _failed_run("notify", REFUSED),
    ]
    # Both undos' own budgets, from the moment rollback started.
    budget = (started.rollback_deadline - h.undo_info[0].scheduled_time).total_seconds()
    assert 2 * BUCKET_UNDO_TIMING.need - 5 <= budget <= 2 * BUCKET_UNDO_TIMING.need
    # The failed block is mirrored before rollback starts: no block is running while undoing.
    statuses = [(c.block_id, c.status) for c in h.calls if isinstance(c, MirrorBlockInput)]
    assert statuses[-1] == ("notify", "failed")
    assert h.calls.index(started) > h.calls.index(
        MirrorBlockInput(run_id="r1", block_id="notify", status="failed", error=REFUSED)
    )
    assert "rollback" in _patches(h.history)


async def test_one_undo_try_runs_on_the_delivery_queue_with_its_own_timeouts_and_no_retries():
    h = Harness(webhook=_hook_refused)
    await h.run(BUCKET_THEN_HOOKS)

    [info] = h.undo_info
    assert info.task_queue == DELIVERY_QUEUE
    assert info.start_to_close_timeout == timedelta(seconds=BUCKET_UNDO_ATTEMPT_SECONDS)
    assert info.schedule_to_close_timeout == timedelta(
        seconds=BUCKET_UNDO_TIMING.schedule_to_start + BUCKET_UNDO_TIMING.start_to_close
    )
    assert info.attempt == 1


async def test_an_undo_refused_is_left_in_place_and_the_older_ones_still_run():
    refused_once: list = []

    async def undo(input):
        if not refused_once:
            refused_once.append(input)
            await _undo_refused(input)

    h = Harness(webhook=_hook_refused, undo=undo)
    result = await h.run(TWO_BUCKETS_THEN_HOOK)

    assert isinstance(result, WorkflowFailureError)
    assert h.undo_inputs == [_undo_of("store_2"), _undo_of("store")]
    undos = [c.undo | {"error": c.error} for c in h.calls if isinstance(c, MirrorUndoInput)]
    assert undos == [
        {"store_2": "undoing", "error": None},
        {
            "store_2": "undo_failed",
            "error": "DestinationRefused: the bucket refused the delete (AccessDenied)",
        },
        {"store": "undoing", "error": None},
        {"store": "undone", "error": None},
    ]
    assert h.calls[-1] == _failed_run("notify", REFUSED)


async def test_a_transient_undo_failure_waits_reloads_and_tries_again_with_no_waiting_mirror():
    h = Harness(webhook=_hook_refused, undo=_flaky_undo(2))
    await h.run(BUCKET_THEN_HOOKS)

    assert len(h.undo_inputs) == 3
    started = [i.started_time for i in h.undo_info]
    assert [round((b - a).total_seconds()) for a, b in itertools.pairwise(started)] == [30, 300]
    assert _rollback_calls(h.calls)[1:] == [
        MirrorUndoInput(run_id="r1", undo={"store": "undoing"}),
        LOAD,
        LOAD,
        LOAD,
        MirrorUndoInput(run_id="r1", undo={"store": "undone"}),
        _failed_run("notify", REFUSED),
    ]


async def test_an_undo_gives_up_after_five_tries_and_is_not_cut_by_the_runs_deadline():
    h = Harness(webhook=_hook_refused, undo=_flaky_undo(99))
    # The run's deadline passes long before the undo's waits end.
    result = await h.run(BUCKET_THEN_HOOKS.model_copy(update={"time_budget_seconds": 200}))

    assert isinstance(result, WorkflowFailureError)
    started = [i.started_time for i in h.undo_info]
    assert [round((b - a).total_seconds()) for a, b in itertools.pairwise(started)] == [
        30,
        300,
        3000,
        7200,
    ]
    assert h.calls[-2:] == [
        MirrorUndoInput(
            run_id="r1",
            undo={"store": "undo_failed"},
            error="DestinationUnavailable: HTTP 503 from the bucket (gave up after 5 attempts)",
        ),
        _failed_run("notify", REFUSED),
    ]


async def test_a_destination_gone_at_the_undos_load_fails_the_undo_without_a_try():
    async def load(input):
        if any(isinstance(c, RollbackStartedInput) for c in h.calls):
            return await _gone(input)
        return await _load_ok(input)

    h = Harness(webhook=_hook_refused, load=load)
    await h.run(BUCKET_THEN_HOOKS)

    assert h.undo_inputs == []
    assert h.calls[-2] == MirrorUndoInput(
        run_id="r1", undo={"store": "undo_failed"}, error="DestinationDeleted: destination deleted"
    )


async def test_a_load_that_could_not_run_is_a_transient_try():
    failed_executions: list = []

    async def load(input):
        if any(isinstance(c, RollbackStartedInput) for c in h.calls):
            if activity.info().attempt == 1:
                failed_executions.append(input)
            if len(failed_executions) == 1:
                raise ApplicationError("database unavailable", type="OperationalError")
        return await _load_ok(input)

    h = Harness(webhook=_hook_refused, load=load)
    await h.run(BUCKET_THEN_HOOKS)

    assert h.undo_inputs == [_undo_of("store")]
    assert h.calls[-2] == MirrorUndoInput(run_id="r1", undo={"store": "undone"})


async def test_a_failure_after_a_webhook_went_out_undoes_nothing():
    h = Harness(webhook=_hook_refused_on("notify_2"))
    result = await h.run(BUCKET_THEN_HOOKS)

    assert isinstance(result, WorkflowFailureError)
    assert h.undo_inputs == []
    assert _rollback_calls(h.calls) == [
        MirrorUndoInput(run_id="r1", undo={"store": "kept", "notify": "not_undoable"}),
        _failed_run("notify_2", REFUSED),
    ]


async def test_a_sent_webhook_on_a_run_that_failed_is_not_undoable():
    h = Harness(webhook=_hook_refused_on("notify_2"))
    await h.run(HOOKS_ONLY)
    assert _rollback_calls(h.calls) == [
        MirrorUndoInput(run_id="r1", undo={"notify": "not_undoable"}),
        _failed_run("notify_2", REFUSED),
    ]


async def test_a_failure_with_nothing_delivered_asks_no_patch_and_rolls_nothing_back():
    h = Harness(bucket=_refused)
    await h.run(BUCKET_THEN_HOOKS)
    assert not [c for c in h.calls if isinstance(c, RollbackStartedInput | MirrorUndoInput)]
    assert "rollback" not in _patches(h.history)


async def test_a_completed_run_rolls_nothing_back():
    h = Harness()
    assert await h.run(BUCKET_THEN_HOOKS) == LLM_PATH
    assert h.undo_inputs == []
    assert "rollback" not in _patches(h.history)


async def test_a_run_deleted_before_rollback_starts_undoes_nothing():
    h = Harness(webhook=_hook_refused)
    h.rollback_reply = "cancelled"
    await h.run(BUCKET_THEN_HOOKS)
    assert h.undo_inputs == []
    assert [type(c) for c in _rollback_calls(h.calls)] == [RollbackStartedInput, MirrorRunInput]


async def test_a_cancel_during_rollback_is_ignored_and_the_run_ends_failed():
    async def cancel_in_undo_wait(handle):
        while not h.undo_inputs:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)  # inside the undo's 30 s wait; nothing skips time here
        await handle.cancel()

    h = Harness(webhook=_hook_refused, undo=_flaky_undo(1))
    result = await h.run(BUCKET_THEN_HOOKS, during=cancel_in_undo_wait)

    # The failure was decided first: the wait and the second try still run, the run fails.
    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, ApplicationError)
    assert len(h.undo_inputs) == 2
    assert h.calls[-2:] == [
        MirrorUndoInput(run_id="r1", undo={"store": "undone"}),
        _failed_run("notify", REFUSED),
    ]


async def test_a_cancel_during_an_undo_try_lets_it_finish_and_rollback_go_on():
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(input):
        started.set()
        await release.wait()

    async def cancel_mid_delete(handle):
        await started.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(webhook=_hook_refused, undo=slow)
    result = await h.run(TWO_BUCKETS_THEN_HOOK, during=cancel_mid_delete)

    assert isinstance(result, WorkflowFailureError)
    assert h.undo_inputs == [_undo_of("store_2"), _undo_of("store")]
    # The delete in flight was never asked to stop: it finished and counts.
    assert [c.undo for c in h.calls if isinstance(c, MirrorUndoInput)] == [
        {"store_2": "undoing"},
        {"store_2": "undone"},
        {"store": "undoing"},
        {"store": "undone"},
    ]
    assert h.calls[-1] == _failed_run("notify", REFUSED)


async def test_a_bucket_failing_after_an_earlier_bucket_delivered_undoes_the_earlier_one():
    async def second_refused(input):
        if input.block_id == "store_2":
            raise ApplicationError(
                "the bucket refused the write (AccessDenied)",
                NOT_WRITTEN,
                type="DestinationRefused",
                non_retryable=True,
            )
        return await _bucket_ok(input)

    h = Harness(bucket=second_refused)
    await h.run(TWO_BUCKETS_THEN_HOOK)
    assert h.undo_inputs == [_undo_of("store")]
    assert h.delivered == []


async def test_a_run_deleted_under_an_undos_load_ends_the_rollback():
    async def load(input):
        if any(isinstance(c, RollbackStartedInput) for c in h.calls):
            raise ApplicationError(
                "the run was deleted", type=RUN_CANCELLED_ERROR, non_retryable=True
            )
        return await _load_ok(input)

    h = Harness(webhook=_hook_refused, load=load)
    await h.run(TWO_BUCKETS_THEN_HOOK)

    assert h.undo_inputs == []
    assert [c for c in h.calls if isinstance(c, MirrorUndoInput)] == [
        MirrorUndoInput(run_id="r1", undo={"store_2": "undoing"})
    ]


# --- a failed bucket block whose try may have written (its reply was lost) -----------------------


def _unanswered(times: int = 99, then=None):
    """The worker's own deadline or a dropped connection: no NOT_WRITTEN, the PUT may have landed."""
    seen: list = []

    async def bucket(input):
        seen.append(input)
        if len(seen) <= times:
            raise ApplicationError(
                "s3.example.com:443 did not answer in time", type="DestinationUnreachable"
            )
        return await (then or _bucket_ok)(input)

    return bucket


def _unanswered_on(block_id: str):
    unanswered = _unanswered()

    async def bucket(input):
        return await (unanswered if input.block_id == block_id else _bucket_ok)(input)

    return bucket


async def _refused_write(input):
    raise ApplicationError(
        "the bucket refused the write (AccessDenied)",
        NOT_WRITTEN,
        type="DestinationRefused",
        non_retryable=True,
    )


# Out of save-time order (a bucket after a webhook): D13 on the workflow side.
HOOK_THEN_BUCKET = _sinks({**HOOK, "input": "extract"}, {**STORE, "input": "notify"})
UNANSWERED = "DestinationUnreachable: s3.example.com:443 did not answer in time"
LOST = (
    "a try whose reply was lost may have written bucket exports, "
    "key out/p1/r1/store.json at https://s3.example.com/"
)


async def test_a_lost_reply_then_the_budget_running_out_undoes_the_blocks_own_key():
    h = Harness(bucket=_unanswered())
    result = await h.run(BUCKET_ONLY.model_copy(update={"time_budget_seconds": 200}))

    assert isinstance(result, WorkflowFailureError)
    assert len(h.bucket_inputs) == 2
    message = f"{UNANSWERED} (the run's time budget ran out after 2 attempts); {LOST}"
    assert h.undo_inputs == [_undo_of("store")]
    [started, *rest] = _rollback_calls(h.calls)
    assert started == RollbackStartedInput(
        run_id="r1",
        failed_block_id="store",
        error=f"store: {message}",
        rollback_deadline=started.rollback_deadline,
    )
    # No undo status: ADR-014 §5a keeps it on completed blocks; the outcome is in the run's error.
    assert rest == [LOAD, _failed_run("store", f"{message}; rollback removed it")]
    # The location is on the failed block before rollback starts.
    assert h.calls.index(
        MirrorBlockInput(run_id="r1", block_id="store", status="failed", error=message)
    ) < h.calls.index(started)
    # Inside the reserve the catalog holds for the block.
    budget = (started.rollback_deadline - h.undo_info[0].scheduled_time).total_seconds()
    assert BUCKET_UNDO_TIMING.need - 5 <= budget <= BUCKET_UNDO_TIMING.need


async def test_a_lost_reply_then_a_final_refusal_still_undoes_the_key():
    h = Harness(bucket=_unanswered(1, then=_refused_write))
    await h.run(BUCKET_ONLY)

    assert len(h.bucket_inputs) == 2
    assert h.undo_inputs == [_undo_of("store")]
    message = f"DestinationRefused: the bucket refused the write (AccessDenied); {LOST}"
    assert h.calls[-1] == _failed_run("store", f"{message}; rollback removed it")


async def test_a_lost_reply_then_the_destination_deleted_is_left_in_place_with_its_location():
    loads: list = []

    async def load(input):
        loads.append(input)
        return await (_gone if len(loads) > 1 else _load_ok)(input)

    h = Harness(bucket=_unanswered(), load=load)
    await h.run(BUCKET_ONLY)

    assert len(h.bucket_inputs) == 1
    assert h.undo_inputs == []
    assert h.calls[-1].error == (
        f"store: DestinationDeleted: destination deleted; {LOST}; "
        "rollback could not remove it, left in place: DestinationDeleted: destination deleted"
    )


async def test_an_undo_that_gives_up_names_the_key_left_in_place():
    h = Harness(bucket=_unanswered(1, then=_refused_write), undo=_flaky_undo(99))
    await h.run(BUCKET_ONLY)

    assert len(h.undo_inputs) == 5
    assert h.calls[-1].error.endswith(
        f"{LOST}; rollback could not remove it, left in place: DestinationUnavailable: "
        "HTTP 503 from the bucket (gave up after 5 attempts)"
    )


async def test_the_lost_reply_is_undone_first_then_the_earlier_deliveries():
    h = Harness(bucket=_unanswered_on("store_2"))
    await h.run(TWO_BUCKETS_THEN_HOOK.model_copy(update={"time_budget_seconds": 300}))

    assert h.undo_inputs == [_undo_of("store_2"), _undo_of("store")]
    started = _rollback_calls(h.calls)[0]
    budget = (started.rollback_deadline - h.undo_info[0].scheduled_time).total_seconds()
    assert 2 * BUCKET_UNDO_TIMING.need - 5 <= budget <= 2 * BUCKET_UNDO_TIMING.need
    assert [c.undo for c in h.calls if isinstance(c, MirrorUndoInput)] == [
        {"store": "undoing"},
        {"store": "undone"},
    ]
    assert h.calls[-1].error.endswith("; rollback removed it")


async def test_a_lost_reply_on_a_try_that_later_delivers_is_not_undone_twice():
    h = Harness(bucket=_unanswered(1), webhook=_hook_refused)
    await h.run(BUCKET_THEN_HOOKS)

    assert len(h.bucket_inputs) == 2
    assert h.undo_inputs == [_undo_of("store")]
    assert h.calls[-1] == _failed_run("notify", REFUSED)


async def test_a_refusal_with_no_lost_reply_before_it_undoes_nothing():
    h = Harness(bucket=_refused_write)
    await h.run(BUCKET_ONLY)
    assert h.undo_inputs == []
    assert not [c for c in h.calls if isinstance(c, RollbackStartedInput | MirrorUndoInput)]
    assert h.calls[-1] == _failed_run(
        "store", "DestinationRefused: the bucket refused the write (AccessDenied)"
    )


async def test_a_lost_reply_after_a_webhook_went_out_is_left_and_said_so():
    h = Harness(bucket=_unanswered())
    await h.run(HOOK_THEN_BUCKET)

    assert h.undo_inputs == []
    rollback = _rollback_calls(h.calls)
    assert rollback[0] == MirrorUndoInput(run_id="r1", undo={"notify": "not_undoable"})
    assert rollback[1].error.endswith(
        f"{LOST}; left in place: a webhook or email had already gone out"
    )


async def test_a_cancel_after_a_lost_reply_names_where_it_may_have_written_and_undoes_nothing():
    async def cancel_in_wait(handle):
        while not h.bucket_inputs:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        await handle.cancel()

    h = Harness(bucket=_unanswered())
    await h.run(BUCKET_ONLY, during=cancel_in_wait)

    assert h.undo_inputs == []
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1",
            block_id="store",
            status="failed",
            error=f"cancelled before it finished; {LOST}",
        ),
        MirrorRunInput(run_id="r1", status="cancelled"),
    ]


@pytest.mark.parametrize(
    ("cause", "written"),
    [
        (ApplicationError("x", type="DestinationUnreachable"), True),
        (ApplicationError("x", type="DeliveryFailed", non_retryable=True), True),
        (ApplicationError("x", NOT_WRITTEN, type="DestinationUnavailable"), False),
        (ApplicationError("x", NOT_WRITTEN, type="DestinationRefused", non_retryable=True), False),
        (
            ActivityTimeoutError("t", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]),
            True,
        ),
        (
            ActivityTimeoutError(
                "t", type=TimeoutType.SCHEDULE_TO_CLOSE, last_heartbeat_details=[]
            ),
            True,
        ),
        (
            ActivityTimeoutError(
                "t", type=TimeoutType.SCHEDULE_TO_START, last_heartbeat_details=[]
            ),
            False,
        ),
        (RuntimeError("?"), True),
    ],
)
def test_a_try_may_have_written_unless_the_worker_says_not_or_no_worker_took_it(cause, written):
    assert _may_have_written(cause) is written
