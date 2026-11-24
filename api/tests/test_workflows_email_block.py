"""P.1 — the Email block in PipelineWorkflow: the sink loop's tries on the delivery queue, a fixed
Message-ID, the content by reference, D13 once it has gone out, the change summary on a monitor check
(ADR-015 §4-§7; PRD-017 slice 3; PRD-018 R4)."""

import json
import uuid
from datetime import timedelta

from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from app.core.db import AsyncSessionLocal
from app.core.destinations import encrypt_secret
from app.models.destination import Destination
from app.models.pipeline import Pipeline
from app.pipelines.catalog import EMAIL
from app.pipelines.definition import parse_definition
from app.settings import settings
from app.workflows import email_text
from app.workflows.activities.contracts import (
    EMAIL_SEND_ATTEMPT_SECONDS,
    DiffInput,
    DiffOutput,
    DiffSummary,
    PathChange,
    SendEmailOutput,
)
from app.workflows.activities.db import load_destination
from app.workflows.pipeline import (
    LoadDestinationInput,
    MirrorBlockInput,
    MirrorUndoInput,
    PipelineRunInput,
)
from app.workflows.queues import DELIVERY_QUEUE
from tests.test_workflows_monitor_pipeline import (
    BASELINE_ID,
    MONITOR_ID,
    WITH_BASELINE,
    _changed,
    _context,
    _gate,
    _monitored,
)
from tests.test_workflows_pipeline import (
    DEFINITION,
    ENVELOPE,
    LLM_PATH,
    SMTP_ADDRESS,
    Harness,
    _run_rows,
)
from tests.test_workflows_rollback import _failed_run, _hook_refused
from tests.test_workflows_sinks import HOOK, STORE, _patches, _sink_calls, _sinks, _stored

MAIL_DESTINATION = "7c2e3d4f-0000-4000-8000-000000000004"
MAIL = _stored("mail", "email", "extract", {"destination_id": MAIL_DESTINATION})
MAIL_ONLY = _sinks(MAIL)
LOAD = LoadDestinationInput(run_id="r1", destination_id=MAIL_DESTINATION, kind="smtp")
MESSAGE_ID = "<r1.mail@scrapeflow.example.org>"


def _mail(**config) -> dict:
    return _stored("mail", "email", config.pop("input", "extract"), {**MAIL["config"], **config})


def _deferred(times: int):
    calls = {"n": 0}

    async def send(input):
        calls["n"] += 1
        if calls["n"] <= times:
            raise ApplicationError(
                "mail.example.com deferred the message (451)", type="DestinationUnavailable"
            )
        return SendEmailOutput()

    return send


async def _refused(input):
    raise ApplicationError(
        "mail.example.com refused every recipient (550)",
        type="DestinationRefused",
        non_retryable=True,
    )


async def _unanswered(input):
    raise ApplicationError("mail.example.com:587 did not answer in time", type="Unreachable")


def _unanswered_then_refused():
    calls = {"n": 0}

    async def send(input):
        calls["n"] += 1
        if calls["n"] == 1:
            await _unanswered(input)
        await _refused(input)

    return send


async def test_an_email_block_loads_its_smtp_destination_and_sends_once():
    h = Harness()
    result = await h.run(MAIL_ONLY)

    assert result == LLM_PATH
    [sent] = h.email_inputs
    assert (sent.address, sent.encrypted_secret) == (SMTP_ADDRESS, "mail-ciphertext")
    assert sent.message_id == MESSAGE_ID
    assert sent.subject == "[ScrapeFlow] prices: run r1"
    # The content travels by reference: the delivery worker reads ScrapeFlow's copy (ADR-015 §7).
    assert (sent.source_path, sent.run_id) == (LLM_PATH, "r1")
    assert sent.body == (
        "pipeline.result — pipeline prices, run r1\n\n"
        "run_id: r1\nblock_id: mail\npipeline_id: p1\n"
        f"Run: {ENVELOPE.run_url}\n\n"
        "The result — extract's output (extraction):\n\n"
    )
    # No delivery record: nothing to undo (PRD-017 R2).
    assert _sink_calls(h.calls, "mail") == [
        MirrorBlockInput(run_id="r1", block_id="mail", status="running", input_ref=LLM_PATH),
        LOAD,
        MirrorBlockInput(run_id="r1", block_id="mail", status="completed", output_ref=LLM_PATH),
    ]
    assert "sinks" in _patches(h.history)


async def test_one_try_runs_on_the_delivery_queue_with_60_s_and_no_retries():
    h = Harness()
    await h.run(MAIL_ONLY)

    [info] = h.email_info
    timing = EMAIL.timing(MAIL_ONLY.blocks[2].config)
    assert info.task_queue == DELIVERY_QUEUE
    assert info.start_to_close_timeout == timedelta(seconds=EMAIL_SEND_ATTEMPT_SECONDS)
    assert info.schedule_to_close_timeout == timedelta(
        seconds=timing.schedule_to_start + timing.start_to_close
    )
    assert info.attempt == 1


async def test_a_deferred_send_waits_and_the_retry_keeps_the_message_id():
    h = Harness(email=_deferred(1))
    result = await h.run(MAIL_ONLY)

    assert result == LLM_PATH
    assert [e.message_id for e in h.email_inputs] == [MESSAGE_ID, MESSAGE_ID]
    assert [c for c in _sink_calls(h.calls, "mail") if isinstance(c, MirrorBlockInput)][1:3] == [
        MirrorBlockInput(
            run_id="r1",
            block_id="mail",
            status="waiting",
            error="attempt 1: DestinationUnavailable: mail.example.com deferred the message (451)",
        ),
        MirrorBlockInput(run_id="r1", block_id="mail", status="running", input_ref=LLM_PATH),
    ]
    assert h.calls.count(LOAD) == 2


async def test_recipients_refused_beside_an_accepted_one_are_noted_on_the_completed_block():
    async def partly(input):
        return SendEmailOutput(refused=["gone@example.org"])

    h = Harness(email=partly)
    await h.run(MAIL_ONLY)

    assert _sink_calls(h.calls, "mail")[-1] == MirrorBlockInput(
        run_id="r1",
        block_id="mail",
        status="completed",
        output_ref=LLM_PATH,
        error="recipients refused: gone@example.org",
    )


async def test_a_failure_after_the_email_went_out_undoes_nothing_d13():
    mail = _mail(input="store")
    hook = {**HOOK, "input": "mail"}
    h = Harness(webhook=_hook_refused)
    result = await h.run(_sinks(STORE, mail, hook))

    assert isinstance(result, WorkflowFailureError)
    assert h.undo_inputs == []
    assert [c for c in h.calls if isinstance(c, MirrorUndoInput)] == [
        MirrorUndoInput(run_id="r1", undo={"store": "kept", "mail": "not_undoable"})
    ]


async def test_an_email_that_gave_up_announced_nothing_so_the_bucket_is_undone():
    # A try with no answer may have sent it; the block has no undo of its own (it is irreversible).
    h = Harness(email=_unanswered_then_refused())
    result = await h.run(_sinks(STORE, _mail(input="store")))

    assert isinstance(result, WorkflowFailureError)
    assert len(h.email_inputs) == 2
    assert len(h.undo_inputs) == 1 and h.postgres_undo_inputs == []
    undos = [c.undo for c in h.calls if isinstance(c, MirrorUndoInput)]
    assert undos == [{"store": "undoing"}, {"store": "undone"}]
    assert h.calls[-1] == _failed_run(
        "mail", "DestinationRefused: mail.example.com refused every recipient (550)"
    )


async def test_an_alert_only_email_is_skipped_without_an_activity_and_may_precede_a_bucket():
    alert = _mail(events=["pipeline.failed"])
    store = {**STORE, "input": "mail"}
    h = Harness()
    result = await h.run(_sinks(alert, store))

    assert result == LLM_PATH
    assert h.email_inputs == []
    assert LOAD not in h.calls
    assert (
        MirrorBlockInput(run_id="r1", block_id="mail", status="skipped", status_reason="alert_only")
        in h.calls
    )
    assert len(h.bucket_inputs) == 1


def _monitored_email(monitor) -> PipelineRunInput:
    """fetch -> clean -> gate -> extract -> mail."""
    hooked = _monitored(monitor)
    stored = json.loads(json.dumps(DEFINITION))
    fetch, extract = stored["blocks"]
    clean = _stored("clean", "clean", "fetch", {})
    extract["input"] = "gate"
    stored["blocks"] = [fetch, clean, _gate("gate", "clean", on_stop_outputs="delete"), extract]
    stored["blocks"].append(_mail())
    parsed, _ = parse_definition(stored)
    return hooked.model_copy(
        update={
            "blocks": parsed.blocks,
            "time_budget_seconds": parsed.run_budget_seconds,
            "artifact_ids": {b.id: f"a-{b.id}" for b in parsed.blocks},
        }
    )


async def _diffs(input: DiffInput) -> DiffOutput:
    if input.kind == "page":
        summary = DiffSummary(
            status="ok",
            kind="page",
            lines=["-Price: £80", "+Price: £70"],
            lines_added=1,
            lines_removed=1,
        )
    else:
        summary = DiffSummary(
            status="ok",
            kind="extraction",
            changes=[PathChange(path="/price", change="changed", old=80, new=70)],
            paths_changed=1,
        )
    return DiffOutput(summary=summary)


async def test_on_a_monitor_check_the_body_carries_the_change_summary():
    h = Harness(validate=_changed, diff=_diffs)
    await h.run(_monitored_email(WITH_BASELINE))

    [sent] = h.email_inputs
    assert sent.body.split("\n\n")[2:4] == [
        f"monitor_id: {MONITOR_ID}\n"
        f"Change: changed since check {BASELINE_ID}\n"
        "  gate: +1 -1 lines\n"
        "    -Price: £80\n"
        "    +Price: £70\n"
        "  extract: 1 values changed\n"
        "    /price: 80 → 70",
        "The result — extract's output (extraction):",
    ]


async def test_a_first_check_says_there_is_nothing_to_compare():
    h = Harness(validate=_changed)
    await h.run(_monitored_email(_context()))

    [sent] = h.email_inputs
    assert "Change: first check — nothing to compare\n\nThe result" in sent.body


def test_the_change_summary_says_when_there_is_no_diff_and_when_it_was_cut():
    lines = email_text.change_lines(
        {
            "status": "changed",
            "baseline_run_id": "b1",
            "summary": {
                "gate": {"status": "baseline_gone", "kind": "page"},
                "extract": {"status": "unavailable", "kind": "extraction"},
                "again": {
                    "status": "ok",
                    "kind": "extraction",
                    "changes": [
                        {"path": "/a", "change": "added", "new": "x"},
                        {"path": "/b", "change": "removed", "old": None},
                    ],
                    "paths_changed": 2,
                    "truncated": True,
                },
            },
        }
    )
    assert lines == [
        "Change: changed since check b1",
        "  gate: changed; the previous copy is no longer stored, so no diff",
        "  extract: changed; the comparison could not be computed",
        "  again: 2 values changed",
        '    /a: added "x"',
        "    /b: removed (was null)",
        "    (cut; the full comparison is on the run page)",
    ]


def test_a_pipeline_name_is_one_line_in_the_subject():
    assert email_text.subject("price\nwatch\t  x", "r1") == "[ScrapeFlow] price watch x: run r1"


# --- load_destination: an smtp destination comes with what the message names -------------------


async def _smtp_destination(user_id) -> Destination:
    async with AsyncSessionLocal() as db:
        d = Destination(
            user_id=user_id,
            name=f"m-{uuid.uuid4().hex[:6]}",
            kind="smtp",
            address=SMTP_ADDRESS,
            encrypted_secret=encrypt_secret({"user": "u", "password": "p"}),
            display_host="mail.example.com",
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
    return d


async def test_load_destination_reads_the_pipeline_name_run_page_and_message_host(
    db_user, monkeypatch
):
    monkeypatch.setattr(settings, "public_base_url", "https://scrapeflow.example.org")
    run = await _run_rows(db_user.id)
    destination = await _smtp_destination(db_user.id)

    loaded = await load_destination(
        LoadDestinationInput(run_id=str(run.id), destination_id=str(destination.id), kind="smtp")
    )

    async with AsyncSessionLocal() as db:
        name = (await db.get(Pipeline, run.pipeline_id)).name
    assert loaded.address == SMTP_ADDRESS
    assert loaded.email is not None
    assert loaded.email.model_dump() == {
        "pipeline_name": name,
        "run_url": (
            f"https://scrapeflow.example.org/app/dashboard/pipelines/{run.pipeline_id}"
            f"/runs/{run.id}"
        ),
        "message_host": "scrapeflow.example.org",
    }
