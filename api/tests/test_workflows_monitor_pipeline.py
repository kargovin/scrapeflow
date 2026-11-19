"""L.8 — a monitor's check inside PipelineWorkflow (patch "monitor-check"; ADR-016 §4a, §5e, §6).

Time-skipping env, activities mocked (tests/test_workflows_pipeline.py's Harness)."""

import json

from temporalio.exceptions import ApplicationError

from app.pipelines.definition import parse_definition
from app.workflows.activities.contracts import (
    CaptureInput,
    DiffInput,
    DiffOutput,
    DiffSummary,
    ValidateVerdict,
)
from app.workflows.activities.webhook import build_payload
from app.workflows.pipeline import (
    MONITOR_CHECK_PATCH,
    CaptureSpec,
    GateFingerprints,
    MirrorBlockInput,
    MirrorRunInput,
    MonitorContext,
    RecordStorageInput,
    RunRef,
)
from tests.test_workflows_pipeline import (
    CLEAN_PATH,
    DEFINITION,
    HOOK_URL,
    LLM_PATH,
    RUN_INPUT,
    Harness,
)

MONITOR_ID = "33333333-3333-3333-3333-333333333333"
BASELINE_ID = "44444444-4444-4444-4444-444444444444"
B_CLEAN = "scrapeflow-results/history/b-clean/clean.md"
B_LLM = "scrapeflow-results/history/b-extract/llm.json"
OLD, NEW = "0" * 64, "1" * 64


def _gate(block_id, input_id, **config):
    return {
        "id": block_id,
        "type": "validate",
        "input": input_id,
        "config_schema_version": 1,
        "config": {"rules": [{"kind": "changed"}], "when_unmet": "stop", **config},
        "secrets": {},
    }


def _monitored(monitor=None, later_gate=False, on_stop_outputs="delete"):
    """fetch -> clean -> gate -> extract [-> again] -> notify."""
    stored = json.loads(json.dumps(DEFINITION))
    fetch, extract = stored["blocks"]
    clean = {
        "id": "clean",
        "type": "clean",
        "input": "fetch",
        "config_schema_version": 1,
        "config": {},
        "secrets": {},
    }
    blocks = [fetch, clean, _gate("gate", "clean", on_stop_outputs=on_stop_outputs)]
    extract["input"] = "gate"
    blocks.append(extract)
    if later_gate:
        blocks.append(_gate("again", "extract", on_stop_outputs="keep"))
    blocks.append(
        {
            "id": "notify",
            "type": "webhook",
            "input": blocks[-1]["id"],
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


def _context(**fields):
    return MonitorContext(monitor_id=MONITOR_ID, **fields)


WITH_BASELINE = _context(
    baseline_run_id=BASELINE_ID,
    gates={"gate": GateFingerprints(baseline=OLD), "again": GateFingerprints(baseline=OLD)},
    baseline_outputs={"clean": B_CLEAN, "extract": B_LLM},
)


async def _changed(input):
    if input.known_fingerprints:
        assert input.known_fingerprints == {"baseline": OLD}
    return ValidateVerdict(outcome="met", fingerprint=NEW)


async def _same(input):
    return ValidateVerdict(
        outcome="not_met",
        rule_index=0,
        rule={"kind": "changed", "negate": False},
        fingerprint=OLD,
        matched="baseline",
    )


def _run_mirror(h) -> MirrorRunInput:
    [run] = [c for c in h.calls if isinstance(c, MirrorRunInput)]
    return run


def _gate_completed(h, block_id="gate") -> MirrorBlockInput:
    [done] = [
        c
        for c in h.calls
        if isinstance(c, MirrorBlockInput) and c.block_id == block_id and c.status == "completed"
    ]
    return done


async def test_a_first_check_records_first_check_and_is_eligible():
    h = Harness(validate=_changed)
    result = await h.run(_monitored(_context()))

    assert result == LLM_PATH
    [validated] = h.validated
    assert validated.known_fingerprints == {}
    assert _gate_completed(h).fingerprint == NEW
    run = _run_mirror(h)
    assert (run.change_status, run.baseline_eligible) == ("first_check", True)
    assert (run.change_summary, run.captured_values) == (None, None)
    assert not any(isinstance(c, DiffInput) for c in h.calls)
    [delivered] = h.delivered
    assert delivered.monitor_id == MONITOR_ID
    assert delivered.change == {"status": "first_check", "baseline_run_id": None, "summary": None}
    assert build_payload(delivered)["diff_detected"] is False


async def test_a_changed_check_diffs_the_page_and_the_extraction_before_completing_them():
    h = Harness(validate=_changed)
    result = await h.run(_monitored(WITH_BASELINE))

    assert result == LLM_PATH
    [validated] = h.validated
    assert validated.known_fingerprints == {"baseline": OLD}
    page, extraction = (c for c in h.calls if isinstance(c, DiffInput))
    assert page == DiffInput(
        artifact_id="a-gate", kind="page", baseline_path=B_CLEAN, current_path=CLEAN_PATH
    )
    assert extraction == DiffInput(
        artifact_id="a-extract", kind="extraction", baseline_path=B_LLM, current_path=LLM_PATH
    )
    # Each diff runs, and is charged to its block, before that block is mirrored completed.
    for diff, block_id in ((page, "gate"), (extraction, "extract")):
        i = h.calls.index(diff)
        record = h.calls[i + 1]
        assert isinstance(record, RecordStorageInput) and record.block_id == block_id
        assert record.objects[0].path.endswith(f"a-{block_id}/diff.json")
        assert h.calls[i + 2] == _gate_completed(h, block_id)
    run = _run_mirror(h)
    assert (run.change_status, run.baseline_eligible) == ("changed", True)
    assert set(run.change_summary) == {"gate", "extract"}
    assert run.change_summary["gate"]["kind"] == "page"
    [delivered] = h.delivered
    assert delivered.change["status"] == "changed"
    assert delivered.change["baseline_run_id"] == BASELINE_ID
    assert delivered.change["summary"] == run.change_summary
    payload = build_payload(delivered)
    assert (payload["diff_detected"], payload["diff_summary"]) == (True, None)
    assert payload["monitor_id"] == MONITOR_ID


async def test_an_unchanged_check_stops_deletes_its_copies_and_is_not_eligible():
    h = Harness(validate=_same)
    result = await h.run(_monitored(WITH_BASELINE))

    assert result is None
    assert h.extracted == [] and h.delivered == []
    assert RunRef(run_id="r1") in h.calls  # on_stop_outputs: delete
    gate = _gate_completed(h)
    assert gate.fingerprint == OLD and gate.verdict["matched"] == "baseline"
    run = _run_mirror(h)
    assert (run.change_status, run.baseline_eligible) == ("unchanged", False)
    assert (run.stopped_at_block_id, run.stop_reason) == ("gate", "rule_not_met")
    assert not any(isinstance(c, DiffInput) for c in h.calls)


async def test_a_stop_at_a_later_gate_leaves_the_check_eligible():
    async def first_changes(input):
        return await (_changed if len(h.validated) == 1 else _same)(input)

    h = Harness(validate=first_changes)
    await h.run(_monitored(WITH_BASELINE, later_gate=True))

    run = _run_mirror(h)
    assert (run.stopped_at_block_id, run.change_status) == ("again", "changed")
    assert run.baseline_eligible is True
    assert _gate_completed(h, "again").fingerprint == OLD
    assert h.validated[1].known_fingerprints == {"baseline": OLD}


async def test_a_baseline_copy_that_is_gone_reads_changed_not_unchanged():
    async def gone(input):
        return DiffOutput(summary=DiffSummary(status="baseline_gone", kind=input.kind))

    h = Harness(validate=_changed, diff=gone)
    await h.run(_monitored(WITH_BASELINE))

    run = _run_mirror(h)
    assert run.change_status == "changed"
    assert run.change_summary["gate"]["status"] == "baseline_gone"
    assert not any(isinstance(c, RecordStorageInput) and c.block_id == "gate" for c in h.calls)


async def test_a_collected_baseline_copy_is_gone_without_asking():
    h = Harness(validate=_changed)
    context = WITH_BASELINE.model_copy(update={"baseline_outputs": {"extract": B_LLM}})
    await h.run(_monitored(context))

    run = _run_mirror(h)
    assert run.change_summary["gate"] == {"status": "baseline_gone", "kind": "page"}
    [diff] = [c for c in h.calls if isinstance(c, DiffInput)]
    assert diff.kind == "extraction"


async def test_a_diff_that_fails_is_unavailable_and_the_check_completes():
    async def broken(input):
        raise ApplicationError("minio down", type="StorageTransient", non_retryable=True)

    h = Harness(validate=_changed, diff=broken)
    result = await h.run(_monitored(WITH_BASELINE))

    assert result == LLM_PATH
    run = _run_mirror(h)
    assert (run.status, run.change_status) == ("completed", "changed")
    assert run.change_summary["gate"]["status"] == "unavailable"
    assert run.change_summary["extract"]["status"] == "unavailable"


async def test_carried_values_are_captured_from_their_block():
    context = _context(
        captures=[
            CaptureSpec(input="last_price", from_block="extract", path="/price", type="number")
        ]
    )
    h = Harness(validate=_changed)
    await h.run(_monitored(context))

    [capture] = [c for c in h.calls if isinstance(c, CaptureInput)]
    assert capture.input_path == LLM_PATH
    assert [(v.input, v.path, v.type) for v in capture.values] == [
        ("last_price", "/price", "number")
    ]
    assert h.calls.index(capture) < h.calls.index(_gate_completed(h, "extract"))
    assert _run_mirror(h).captured_values == {"last_price": {"value": 8}}


async def test_a_capture_that_fails_carries_nothing_and_the_check_completes():
    async def broken(input):
        raise ApplicationError("not JSON", non_retryable=True)

    context = _context(
        captures=[
            CaptureSpec(input="last_price", from_block="extract", path="/price", type="number")
        ]
    )
    h = Harness(validate=_changed, capture=broken)
    await h.run(_monitored(context))

    captured = _run_mirror(h).captured_values
    assert captured["last_price"]["missing"].startswith("could not be read")


async def test_a_hand_run_through_a_gate_reads_no_baseline_and_sends_todays_payload():
    h = Harness(validate=_changed)
    await h.run(_monitored(None))

    [validated] = h.validated
    assert validated.known_fingerprints == {}
    run = _run_mirror(h)
    assert (run.change_status, run.baseline_eligible) == ("no_baseline", False)
    [delivered] = h.delivered
    assert (delivered.monitor_id, delivered.change) == (None, None)
    assert build_payload(delivered) == {
        "event": "pipeline.result",
        "run_id": "r1",
        "pipeline_id": "p1",
        "pipeline_run_id": "r1",
        "result_path": LLM_PATH,
        "diff_detected": False,
        "diff_summary": None,
        "timestamp": delivered.timestamp,
    }


async def test_a_run_without_a_gate_or_a_monitor_takes_no_patch():
    h = Harness()
    await h.run(RUN_INPUT)

    assert _run_mirror(h) == MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH)
    patches = [
        json.loads(e.marker_recorded_event_attributes.details["patch-data"].payloads[0].data)["id"]
        for e in h.history.events
        if e.HasField("marker_recorded_event_attributes")
    ]
    assert "host-slot" in patches and MONITOR_CHECK_PATCH not in patches


async def test_a_check_that_fails_keeps_its_change_status_and_is_never_eligible():
    async def llm_fails(input):
        raise ApplicationError("401 invalid key", non_retryable=True)

    h = Harness(validate=_changed, llm=llm_fails)
    await h.run(_monitored(WITH_BASELINE))

    run = _run_mirror(h)
    assert (run.status, run.change_status, run.baseline_eligible) == ("failed", "changed", False)
