"""N.2 — the Approval block's wait in PipelineWorkflow (patch "approval"; ADR-016 §9b to §9d).

Time-skipping env, activities mocked (tests/test_workflows_pipeline.py's Harness, whose ``answer``
stands for the block row's recorded answer). Time skips only while the test awaits the result, so
a ``during`` that signals does so before any timer fires.
"""

import json
from datetime import timedelta

import pytest

from app.pipelines.catalog import LLM_EXTRACT
from app.pipelines.definition import parse_definition
from app.workflows.activities.contracts import ValidateVerdict
from app.workflows.pipeline import (
    APPROVAL_SIGNAL,
    REMIND_APPROVAL_ACTIVITY,
    REQUEST_APPROVAL_ACTIVITY,
    RESOLVE_APPROVAL_ACTIVITY,
    ApprovalRef,
    MirrorBlockInput,
    MirrorRunInput,
    RequestApprovalInput,
    ResolveApprovalInput,
    RunRef,
)
from tests.test_workflows_monitor_pipeline import (
    WITH_BASELINE,
    _changed,
    _context,
    _gate,
)
from tests.test_workflows_pipeline import (
    DEFINITION,
    HOOK_URL,
    LLM_PATH,
    RUN_INPUT,
    SCRAPE_PATH,
    Harness,
    _block_calls,
)


def _approval_run(deadline_hours=24, monitor=None, gate=False):
    """fetch [-> gate] -> approve -> extract -> notify."""
    stored = json.loads(json.dumps(DEFINITION))
    fetch, extract = stored["blocks"]
    blocks = [fetch]
    if gate:
        blocks.append(_gate("gate", "fetch", on_stop_outputs="keep"))
    blocks.append(
        {
            "id": "approve",
            "type": "approval",
            "input": blocks[-1]["id"],
            "config_schema_version": 1,
            "config": {"deadline_hours": deadline_hours},
            "secrets": {},
        }
    )
    extract["input"] = "approve"
    blocks.append(extract)
    blocks.append(
        {
            "id": "notify",
            "type": "webhook",
            "input": "extract",
            "config_schema_version": 1,
            "config": {"url": HOOK_URL, "secret": None},
            "secrets": {},
        }
    )
    stored["blocks"] = blocks
    parsed, _ = parse_definition(stored)
    return RUN_INPUT.model_copy(
        update={
            "blocks": parsed.blocks,
            "time_budget_seconds": parsed.run_budget_seconds,
            "artifact_ids": {b.id: f"a-{b.id}" for b in parsed.blocks},
            "monitor": monitor,
        }
    )


APPROVAL_RUN = _approval_run()
SHORT_DEADLINE = _approval_run(deadline_hours=2)
MONITORED = _approval_run(monitor=WITH_BASELINE.model_copy(update={"gates": {}}), gate=True)


def answers(answer):
    """``during``: once the request is written, record ``answer`` on the row, then signal."""

    def setup(h):
        async def during(handle):
            await h.requested.wait()
            h.answer = answer
            await handle.signal(APPROVAL_SIGNAL)

        return during

    return setup


def cancels_while_waiting(h):
    async def during(handle):
        await h.requested.wait()
        await handle.cancel()

    return during


# The Harness's default execution timeout (1 day) is shorter than a wait to the deadline.
WAIT_TIMEOUT = timedelta(days=30)


async def _run(h, run_input, **kw):
    return await h.run(run_input, timeout=WAIT_TIMEOUT, **kw)


def _approval_calls(h):
    return [
        c
        for c in h.calls
        if isinstance(c, RequestApprovalInput | ApprovalRef)
        or (isinstance(c, MirrorBlockInput) and c.block_id == "approve")
    ]


def _run_mirror(h) -> MirrorRunInput:
    [run] = [c for c in h.calls if isinstance(c, MirrorRunInput)]
    return run


def _waited(h) -> list[tuple[str, timedelta]]:
    """Each approval activity after the request, by how long after it it was scheduled."""
    (_, requested), *rest = h.approval_times
    return [(name, at - requested) for name, at in rest]


def _hours(n) -> timedelta:
    return timedelta(hours=n)


async def test_an_approved_run_carries_on_with_its_full_budget_after_the_wait():
    h = Harness()
    result = await _run(h, APPROVAL_RUN, during=answers("approved")(h))

    assert result == LLM_PATH
    [request] = [c for c in h.calls if isinstance(c, RequestApprovalInput)]
    assert (request.run_id, request.block_id) == ("r1", "approve")
    assert (request.change_status, request.change_summary) == (None, None)
    assert _approval_calls(h) == [
        MirrorBlockInput(
            run_id="r1",
            block_id="approve",
            status="waiting",
            status_reason="awaiting_approval",
            input_ref=SCRAPE_PATH,
        ),
        request,
        ResolveApprovalInput(run_id="r1", block_id="approve"),
        MirrorBlockInput(
            run_id="r1", block_id="approve", status="completed", output_ref=SCRAPE_PATH
        ),
    ]
    # Asked once, at the wake-up: no reminder, no expiry.
    assert [name for name, _ in h.approval_times] == [
        REQUEST_APPROVAL_ACTIVITY,
        RESOLVE_APPROVAL_ACTIVITY,
    ]
    assert len(h.extracted) == 1
    assert _run_mirror(h) == MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH)


async def test_the_request_deadline_is_the_blocks_hours_from_the_request():
    h = Harness()
    await _run(h, SHORT_DEADLINE)

    [request] = [c for c in h.calls if isinstance(c, RequestApprovalInput)]
    (_, requested), *_ = h.approval_times
    assert abs(request.deadline - requested - _hours(2)) < timedelta(seconds=5)


async def test_the_wait_is_given_back_to_the_runs_budget():
    h = Harness(answer="approved")
    # No signal: the answer is found at the reminder, 22 h in — far past the run's budget.
    await _run(h, APPROVAL_RUN)

    assert APPROVAL_RUN.time_budget_seconds < _hours(22).total_seconds()
    assert _run_mirror(h).status == "completed"
    budget = LLM_EXTRACT.timing(APPROVAL_RUN.blocks[2].config).budget
    [info] = h.llm_info
    assert info.schedule_to_close_timeout == timedelta(seconds=budget)


async def test_a_rejected_run_stops_completed_at_the_approval_and_keeps_its_copies():
    h = Harness()
    result = await _run(h, APPROVAL_RUN, during=answers("rejected")(h))

    assert result == SCRAPE_PATH
    assert h.extracted == [] and h.delivered == []
    assert not any(isinstance(c, RunRef) for c in h.calls), "a reject never releases outputs"
    assert _block_calls(h.calls)[-1] == ("approve", "completed")
    assert _run_mirror(h) == MirrorRunInput(
        run_id="r1",
        status="completed",
        result_path=SCRAPE_PATH,
        stopped_at_block_id="approve",
        stop_reason="approval_rejected",
    )


async def test_no_answer_sends_the_reminder_two_hours_before_and_expires_at_the_deadline():
    h = Harness()
    result = await _run(h, APPROVAL_RUN)

    assert result == SCRAPE_PATH
    assert [c for c in h.calls if isinstance(c, ApprovalRef)][-2:] == [
        ApprovalRef(run_id="r1", block_id="approve"),
        ResolveApprovalInput(run_id="r1", block_id="approve", expire=True),
    ]
    [(remind, reminded), (resolve, resolved)] = _waited(h)
    assert (remind, resolve) == (REMIND_APPROVAL_ACTIVITY, RESOLVE_APPROVAL_ACTIVITY)
    assert abs(reminded - _hours(22)) < timedelta(seconds=5)
    assert abs(resolved - _hours(24)) < timedelta(seconds=5)
    assert _run_mirror(h) == MirrorRunInput(
        run_id="r1",
        status="completed",
        result_path=SCRAPE_PATH,
        stopped_at_block_id="approve",
        stop_reason="approval_expired",
    )


async def test_a_deadline_of_two_hours_or_less_has_no_reminder():
    h = Harness()
    await _run(h, SHORT_DEADLINE)

    [(resolve, resolved)] = _waited(h)
    assert resolve == RESOLVE_APPROVAL_ACTIVITY
    assert abs(resolved - _hours(2)) < timedelta(seconds=5)
    assert _run_mirror(h).stop_reason == "approval_expired"


@pytest.mark.parametrize(
    ("run_input", "found_by", "after"),
    [(APPROVAL_RUN, REMIND_APPROVAL_ACTIVITY, 22), (SHORT_DEADLINE, RESOLVE_APPROVAL_ACTIVITY, 2)],
)
async def test_an_answer_whose_signal_was_lost_is_found_at_the_reminder_or_the_deadline(
    run_input, found_by, after
):
    h = Harness(answer="approved")
    result = await _run(h, run_input)

    assert result == LLM_PATH
    [(name, at)] = _waited(h)
    assert name == found_by
    assert abs(at - _hours(after)) < timedelta(seconds=5)


async def test_a_cancel_while_waiting_ends_the_run_cancelled_at_once():
    h = Harness()
    await _run(h, APPROVAL_RUN, during=cancels_while_waiting(h))

    assert h.extracted == []
    assert [name for name, _ in h.approval_times] == [REQUEST_APPROVAL_ACTIVITY]
    assert _block_calls(h.calls)[-1] == ("approve", "failed")
    [failed] = [c for c in h.calls if isinstance(c, MirrorBlockInput) and c.status == "failed"]
    assert failed.error == "cancelled while waiting for approval"
    assert _run_mirror(h) == MirrorRunInput(run_id="r1", status="cancelled")


async def test_a_row_cancel_whose_engine_cancel_was_lost_ends_the_run_at_the_reminder():
    h = Harness()
    h.row_cancelled = True
    await _run(h, APPROVAL_RUN)

    assert h.extracted == []
    assert _run_mirror(h) == MirrorRunInput(run_id="r1", status="cancelled")


async def test_a_cancel_seen_by_the_waiting_mirror_asks_nothing():
    h = Harness()
    h.running_reply["approve"] = "cancelled"
    await _run(h, APPROVAL_RUN)

    assert h.approval_times == []
    assert _run_mirror(h) == MirrorRunInput(run_id="r1", status="cancelled")


@pytest.mark.parametrize(
    ("answer", "eligible"),
    [("approved", True), ("rejected", True), ("expired", False), ("superseded", False)],
)
async def test_only_content_someone_looked_at_can_be_the_baseline(answer, eligible):
    h = Harness(validate=_changed)
    await _run(h, MONITORED, during=answers(answer)(h))

    run = _run_mirror(h)
    assert run.status == "completed"
    assert run.baseline_eligible is eligible


async def test_a_checks_request_carries_its_change_so_far():
    gated = _approval_run(monitor=_context(), gate=True)
    h = Harness(validate=_changed)
    await _run(h, gated, during=answers("approved")(h))

    [request] = [c for c in h.calls if isinstance(c, RequestApprovalInput)]
    assert request.change_status == "first_check"


async def _same_gate(input):
    return ValidateVerdict(outcome="not_met", rule_index=0, rule={"kind": "changed"})


async def test_an_unchanged_check_stops_at_its_gate_and_asks_nothing():
    h = Harness(validate=_same_gate)
    await _run(h, _approval_run(monitor=_context(), gate=True))

    assert h.approval_times == []
    assert _run_mirror(h).stop_reason == "rule_not_met"
