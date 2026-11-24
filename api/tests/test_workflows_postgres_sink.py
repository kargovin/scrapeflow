"""O.1 — the postgres block in PipelineWorkflow: the same tries as a bucket, a row for a record, its
undo at the recorded row with the destination's current login and CA, a lost reply's row undone."""

from datetime import timedelta

from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from app.pipelines.catalog import POSTGRES
from app.workflows.activities.contracts import (
    NOT_WRITTEN,
    POSTGRES_DELIVER_ATTEMPT_SECONDS,
    DeliverPostgresInput,
    UndoPostgresInput,
)
from app.workflows.pipeline import LoadDestinationInput, MirrorBlockInput, MirrorUndoInput
from app.workflows.queues import DELIVERY_QUEUE
from tests.test_workflows_pipeline import (
    LLM_PATH,
    POSTGRES_ADDRESS,
    Harness,
    _postgres_ok,
)
from tests.test_workflows_rollback import _failed_run, _hook_refused, _rollback_calls
from tests.test_workflows_sinks import HOOK, STORE, _patches, _sink_calls, _sinks, _stored

DB_DESTINATION = "7c2e3d4f-0000-4000-8000-000000000003"
DB = _stored("db", "postgres", "extract", {"destination_id": DB_DESTINATION})
DB_ONLY = _sinks(DB)
LOAD = LoadDestinationInput(run_id="r1", destination_id=DB_DESTINATION, kind="postgres")
LOCATION = {
    "host": "db.example.com",
    "port": 5432,
    "database": "shop",
    "schema": "sink",
    "table": "rows",
    "run_id": "r1",
    "block_id": "db",
}
RECORD = {"destination_id": DB_DESTINATION, "kind": "postgres", "location": LOCATION}
UNDO = UndoPostgresInput(
    location=LOCATION, encrypted_secret="db-ciphertext", ca_certificate="CA-PEM"
)
LOST = (
    "a try whose reply was lost may have written the row (run_id r1, block_id db) to sink.rows "
    "in database shop at db.example.com"
)


def _failing(*errors: ApplicationError):
    """Raises each error in turn, then delivers."""
    queue = list(errors)

    async def deliver(input):
        if queue:
            raise queue.pop(0)
        return await _postgres_ok(input)

    return deliver


def _unavailable(*details) -> ApplicationError:
    return ApplicationError(
        "db.example.com could not take the write now (TooManyConnectionsError, SQLSTATE 53300)",
        *details,
        type="DestinationUnavailable",
    )


def _refused(*details) -> ApplicationError:
    return ApplicationError(
        "db.example.com refused the write (InsufficientPrivilegeError, SQLSTATE 42501)",
        *details,
        type="DestinationRefused",
        non_retryable=True,
    )


async def test_a_postgres_block_loads_its_destination_writes_once_and_records_the_row():
    h = Harness()
    result = await h.run(DB_ONLY)

    assert result == LLM_PATH
    assert h.postgres_inputs == [
        DeliverPostgresInput(
            address=POSTGRES_ADDRESS,
            encrypted_secret="db-ciphertext",
            source_path=LLM_PATH,
            pipeline_id="p1",
            run_id="r1",
            block_id="db",
        )
    ]
    assert h.bucket_inputs == []
    assert _sink_calls(h.calls, "db") == [
        MirrorBlockInput(run_id="r1", block_id="db", status="running", input_ref=LLM_PATH),
        LOAD,
        MirrorBlockInput(
            run_id="r1", block_id="db", status="completed", output_ref=LLM_PATH, delivery=RECORD
        ),
    ]
    assert "sinks" in _patches(h.history)


async def test_one_try_runs_on_the_delivery_queue_with_30_s_and_no_retries():
    h = Harness()
    await h.run(DB_ONLY)

    [info] = h.postgres_info
    timing = POSTGRES.timing(DB_ONLY.blocks[2].config)
    assert info.task_queue == DELIVERY_QUEUE
    assert info.start_to_close_timeout == timedelta(seconds=POSTGRES_DELIVER_ATTEMPT_SECONDS)
    assert info.schedule_to_close_timeout == timedelta(
        seconds=timing.schedule_to_start + timing.start_to_close
    )
    assert info.attempt == 1


async def test_a_transient_failure_waits_reloads_and_writes_on_the_next_try():
    h = Harness(postgres=_failing(_unavailable(NOT_WRITTEN)))
    result = await h.run(DB_ONLY)

    assert result == LLM_PATH
    assert len(h.postgres_inputs) == 2
    assert [c for c in _sink_calls(h.calls, "db") if isinstance(c, MirrorBlockInput)][1:3] == [
        MirrorBlockInput(
            run_id="r1",
            block_id="db",
            status="waiting",
            error=f"attempt 1: DestinationUnavailable: {_unavailable().message}",
        ),
        MirrorBlockInput(run_id="r1", block_id="db", status="running", input_ref=LLM_PATH),
    ]
    assert h.calls.count(LOAD) == 2


async def test_a_later_failure_undoes_the_row_with_the_current_login_and_ca():
    hook = {**HOOK, "input": "db"}
    h = Harness(webhook=_hook_refused)
    result = await h.run(_sinks(DB, hook))

    assert isinstance(result, WorkflowFailureError)
    assert h.postgres_undo_inputs == [UNDO]
    [_started, *rest] = _rollback_calls(h.calls)
    assert rest[:3] == [
        MirrorUndoInput(run_id="r1", undo={"db": "undoing"}),
        LOAD,
        MirrorUndoInput(run_id="r1", undo={"db": "undone"}),
    ]


async def test_a_bucket_then_a_postgres_block_are_undone_newest_first():
    db = {**DB, "input": "store"}
    hook = {**HOOK, "input": "db"}
    h = Harness(webhook=_hook_refused)
    await h.run(_sinks(STORE, db, hook))

    undos = [c.undo for c in h.calls if isinstance(c, MirrorUndoInput)]
    assert undos == [{"db": "undoing"}, {"db": "undone"}, {"store": "undoing"}, {"store": "undone"}]
    assert [u.location.block_id for u in h.postgres_undo_inputs] == ["db"]
    assert len(h.undo_inputs) == 1


async def test_a_lost_reply_then_a_final_refusal_undoes_the_blocks_own_row():
    h = Harness(postgres=_failing(_unavailable(), _refused(NOT_WRITTEN)))
    result = await h.run(DB_ONLY)

    assert isinstance(result, WorkflowFailureError)
    assert len(h.postgres_inputs) == 2
    assert h.postgres_undo_inputs == [UNDO]
    message = f"DestinationRefused: {_refused().message}; {LOST}"
    assert h.calls[-1] == _failed_run("db", f"{message}; rollback removed it")


async def test_a_failure_that_cannot_have_written_undoes_nothing():
    h = Harness(postgres=_failing(_refused(NOT_WRITTEN)))
    await h.run(DB_ONLY)

    assert h.postgres_undo_inputs == []
    assert h.calls[-1] == _failed_run("db", f"DestinationRefused: {_refused().message}")
