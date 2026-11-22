"""N.3 — supersede, and the tick that closes early (ADR-016 §9e; patches "approval" in
PipelineWorkflow and "approval-settled" in MonitorCheckWorkflow).

Time-skipping env, activities mocked (tests/test_workflows_pipeline.py's Harness).
"""

import json
import uuid
from datetime import timedelta

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.pipelines.definition import parse_definition
from app.workflows.activities.contracts import DiffInput, ValidateVerdict
from app.workflows.monitor_check import (
    ADMIT_MONITOR_CHECK_ACTIVITY,
    AdmitMonitorCheckInput,
    MonitorAdmission,
    MonitorCheckInput,
    MonitorCheckWorkflow,
    workflow_id,
)
from app.workflows.pipeline import (
    APPROVAL_SIGNAL,
    GateFingerprints,
    MirrorBlockInput,
    MirrorRunInput,
    PipelineWorkflow,
    RequestApprovalInput,
    SupersedeApprovalInput,
)
from app.workflows.queues import (
    CONTENT_QUEUE,
    DELIVERY_QUEUE,
    LLM_QUEUE,
    SCRAPE_HTTP_QUEUE,
    WORKFLOW_QUEUE,
)
from tests.test_workflows_approval import WAIT_TIMEOUT, _approval_run, answers
from tests.test_workflows_monitor_pipeline import BASELINE_ID, MONITOR_ID, NEW, OLD, _context, _gate
from tests.test_workflows_pipeline import DEFINITION, HOOK_URL, LLM_PATH, RUN_INPUT, Harness

WAITING_ID = "55555555-5555-5555-5555-555555555555"
WAITING = "2" * 64
B_PAGE = "scrapeflow-results/history/b-fetch/scrape.html"

SUPERSEDING = _context(
    baseline_run_id=BASELINE_ID,
    waiting_run_id=WAITING_ID,
    gates={"gate": GateFingerprints(baseline=OLD, waiting=WAITING)},
    baseline_outputs={"fetch": B_PAGE},
)
# fetch -> gate -> approve -> extract -> notify
REACHES_APPROVAL = _approval_run(monitor=SUPERSEDING, gate=True)


def _guarded(monitor):
    """fetch -> gate -> guard (a stop-mode Validate) -> approve -> extract -> notify."""
    stored = json.loads(json.dumps(DEFINITION))
    fetch, extract = stored["blocks"]
    guard = _gate("guard", "gate")
    guard["config"] = {"rules": [{"kind": "contains", "text": "Sold out"}], "when_unmet": "stop"}
    approve = {
        "id": "approve",
        "type": "approval",
        "input": "guard",
        "config_schema_version": 1,
        "config": {"deadline_hours": 24},
        "secrets": {},
    }
    extract["input"] = "approve"
    notify = {
        "id": "notify",
        "type": "webhook",
        "input": "extract",
        "config_schema_version": 1,
        "config": {"url": HOOK_URL, "secret": None},
        "secrets": {},
    }
    stored["blocks"] = [fetch, _gate("gate", "fetch", on_stop_outputs="keep"), guard, approve]
    stored["blocks"] += [extract, notify]
    parsed, _ = parse_definition(stored)
    return RUN_INPUT.model_copy(
        update={
            "blocks": parsed.blocks,
            "time_budget_seconds": parsed.run_budget_seconds,
            "artifact_ids": {b.id: f"a-{b.id}" for b in parsed.blocks},
            "monitor": monitor,
        }
    )


STOPS_AT_GUARD = _guarded(SUPERSEDING)


async def _differs(input):
    if input.rules[0]["kind"] == "changed":
        assert input.known_fingerprints == {"baseline": OLD, "waiting": WAITING}
        return ValidateVerdict(outcome="met", fingerprint=NEW)
    return ValidateVerdict(
        outcome="not_met", rule_index=0, rule={"kind": "contains", "text": "Sold out"}
    )


async def _matches_waiting(input):
    return ValidateVerdict(
        outcome="not_met",
        rule_index=0,
        rule={"kind": "changed", "negate": False},
        fingerprint=WAITING,
        matched="waiting",
    )


async def _run(h, run_input, **kw):
    return await h.run(run_input, timeout=WAIT_TIMEOUT, **kw)


def _run_mirror(h) -> MirrorRunInput:
    [run] = [c for c in h.calls if isinstance(c, MirrorRunInput)]
    return run


def _supersedes(h) -> list[SupersedeApprovalInput]:
    return [c for c in h.calls if isinstance(c, SupersedeApprovalInput)]


SUPERSEDE = SupersedeApprovalInput(run_id=WAITING_ID, superseded_by_run_id="r1")


# --- PipelineWorkflow: the first gate --------------------------------------------------------


async def test_a_check_that_differs_supersedes_the_waiting_one_then_diffs_against_the_baseline():
    h = Harness(validate=_differs)
    result = await _run(h, REACHES_APPROVAL, during=answers("approved")(h))

    assert result == LLM_PATH
    assert _supersedes(h) == [SUPERSEDE]
    supersede = h.calls.index(SUPERSEDE)
    [diff] = [c for c in h.calls if isinstance(c, DiffInput)]
    assert h.calls.index(diff) > supersede
    assert diff.baseline_path == B_PAGE
    gate_done = next(
        c
        for c in h.calls
        if isinstance(c, MirrorBlockInput) and c.block_id == "gate" and c.status == "completed"
    )
    assert h.calls.index(gate_done) > supersede
    # Then it asks for approval itself: the only one waiting.
    [request] = [c for c in h.calls if isinstance(c, RequestApprovalInput)]
    assert request.change_status == "changed"
    run = _run_mirror(h)
    assert (run.status, run.change_status, run.baseline_eligible) == ("completed", "changed", True)


async def test_a_check_that_differs_supersedes_even_when_a_validate_then_stops_it():
    h = Harness(validate=_differs)
    await _run(h, STOPS_AT_GUARD)

    assert _supersedes(h) == [SUPERSEDE]
    assert not any(isinstance(c, RequestApprovalInput) for c in h.calls)
    run = _run_mirror(h)
    assert (run.stopped_at_block_id, run.stop_reason) == ("guard", "rule_not_met")
    assert (run.change_status, run.baseline_eligible) == ("changed", True)


async def test_a_check_matching_the_waiting_content_stops_and_leaves_it_alone():
    h = Harness(validate=_matches_waiting)
    await _run(h, REACHES_APPROVAL)

    assert _supersedes(h) == []
    assert h.extracted == [] and not any(isinstance(c, RequestApprovalInput) for c in h.calls)
    [gate] = [
        c
        for c in h.calls
        if isinstance(c, MirrorBlockInput) and c.block_id == "gate" and c.status == "completed"
    ]
    assert (gate.fingerprint, gate.verdict["matched"]) == (WAITING, "waiting")
    run = _run_mirror(h)
    assert (run.stop_reason, run.change_status, run.baseline_eligible) == (
        "rule_not_met",
        "unchanged",
        False,
    )


async def test_with_no_baseline_a_check_that_differs_from_the_waiting_one_is_a_first_check():
    async def differs(input):
        assert input.known_fingerprints == {"waiting": WAITING}
        return ValidateVerdict(outcome="met", fingerprint=NEW)

    waiting_only = _context(
        waiting_run_id=WAITING_ID, gates={"gate": GateFingerprints(waiting=WAITING)}
    )
    h = Harness(validate=differs)
    await _run(h, _approval_run(monitor=waiting_only, gate=True), during=answers("approved")(h))

    assert _supersedes(h) == [SUPERSEDE]
    assert not any(isinstance(c, DiffInput) for c in h.calls)
    assert _run_mirror(h).change_status == "first_check"


async def test_with_nothing_waiting_nothing_is_superseded():
    async def differs(input):
        return ValidateVerdict(outcome="met", fingerprint=NEW)

    baseline_only = SUPERSEDING.model_copy(
        update={"waiting_run_id": None, "gates": {"gate": GateFingerprints(baseline=OLD)}}
    )
    h = Harness(validate=differs)
    await _run(h, _approval_run(monitor=baseline_only, gate=True), during=answers("approved")(h))

    assert _supersedes(h) == []
    assert _run_mirror(h).change_status == "changed"


# --- the tick closes once its check waits for approval ---------------------------------------


class Settling:
    """The real MonitorCheckWorkflow over a real PipelineWorkflow child, its activities mocked."""

    def __init__(self, run_input, **kw):
        self.h = Harness(**kw)
        self.run_input = run_input

    async def run(self):
        h = self.h
        asked: list[str] = []

        @activity.defn(name=ADMIT_MONITOR_CHECK_ACTIVITY)
        async def admit(input: AdmitMonitorCheckInput) -> MonitorAdmission:
            asked.append(input.run_id)
            run = self.run_input.model_copy(update={"run_id": input.run_id})
            return MonitorAdmission(verdict="admitted", run=run)

        db_activities, scrape, llm, content = h.activities()
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            async with (
                Worker(
                    env.client,
                    task_queue=WORKFLOW_QUEUE,
                    workflows=[MonitorCheckWorkflow, PipelineWorkflow],
                    activities=[*db_activities, admit],
                ),
                Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
                Worker(env.client, task_queue=LLM_QUEUE, activities=[llm]),
                Worker(env.client, task_queue=CONTENT_QUEUE, activities=content),
                Worker(env.client, task_queue=DELIVERY_QUEUE, activities=h.delivery_queue),
            ):
                tick = await env.client.start_workflow(
                    MonitorCheckWorkflow.run,
                    MonitorCheckInput(monitor_id=MONITOR_ID),
                    id=f"{workflow_id(MONITOR_ID)}-{uuid.uuid4()}",
                    task_queue=WORKFLOW_QUEUE,
                    execution_timeout=WAIT_TIMEOUT,
                )
                self.tick_result = await tick.result()
                self.tick_history = await tick.fetch_history()
                check = env.client.get_workflow_handle(f"pipeline-run-{asked[0]}")
                # The check still waits; the owner answers it.
                self.answered_after = await env.get_current_time()
                h.answer = "approved"
                await check.signal(APPROVAL_SIGNAL)
                self.check_result = await check.result()
                self.check_history = await check.fetch_history()


SETTLING = _approval_run(monitor=_context(), gate=True)


async def _first_check(input):
    return ValidateVerdict(outcome="met", fingerprint=NEW)


async def test_the_tick_closes_once_its_check_asks_for_approval_and_the_check_runs_on():
    s = Settling(SETTLING, validate=_first_check)
    await s.run()

    assert s.tick_result == "awaiting_approval"
    assert s.check_result == LLM_PATH
    h = s.h
    assert len(h.extracted) == 1
    # Closed at the request, long before the 24 h deadline.
    (_, requested), *_ = h.approval_times
    assert s.answered_after - requested < timedelta(hours=1)
    assert _run_mirror(h).status == "completed"


async def test_a_hand_run_waiting_for_approval_signals_no_tick():
    h = Harness()
    await _run(h, _approval_run(), during=answers("approved")(h))

    assert not any(
        e.HasField("signal_external_workflow_execution_initiated_event_attributes")
        for e in h.history.events
    )
