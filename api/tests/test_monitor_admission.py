"""L.7 — admit_monitor_check: a firing's admission (ADR-016 §3b, §4, §6b, §6c, §7)."""

import socket
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, update

from app.core.db import AsyncSessionLocal
from app.core.monitors import allowance_seconds
from app.models.llm_keys import UserLLMKey
from app.models.monitor import Monitor, MonitorEvent
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.quota_views import quota_run_units
from app.models.user_quota import UserQuota
from app.models.webhook_delivery import WebhookDelivery
from app.pipelines.definition import parse_definition
from app.pipelines.validator import validate
from app.settings import settings
from app.workflows.activities.approvals import supersede_approval
from app.workflows.activities.monitor_checks import STORAGE_FULL, admit_monitor_check
from app.workflows.monitor_check import AdmitMonitorCheckInput
from app.workflows.pipeline import CaptureSpec, GateFingerprints, SupersedeApprovalInput

SCHEMA = {"type": "object", "properties": {"price": {"type": "number"}}}
PAGE = "https://shop.example.com/item"


APPROVE = {"id": "approve", "type": "approval", "config": {"deadline_hours": 24}}


def definition(key_id, approval=None):
    """fetch -> clean -> gate -> extract -> moved -> notify; ``approval``: an Approval block
    ``after_gate`` (before notify) or ``before_gate`` (after fetch)."""
    raw = [
        {"id": "fetch", "type": "scrape", "config": {"url": {"$input": "page"}}},
        {"id": "clean", "type": "clean", "input": "fetch"},
        {
            "id": "gate",
            "type": "validate",
            "input": "clean",
            "config": {"rules": [{"kind": "changed"}], "when_unmet": "stop"},
        },
        {
            "id": "extract",
            "type": "llm_extract",
            "input": "gate",
            "config": {"llm_key_id": str(key_id), "model": "m", "output_schema": SCHEMA},
        },
        {
            "id": "moved",
            "type": "validate",
            "input": "extract",
            "config": {
                "rules": [
                    {
                        "kind": "compare",
                        "path": "/price",
                        "op": "ne",
                        "value": {"$input": "last_price"},
                    }
                ],
                "when_unmet": "stop",
            },
        },
        {
            "id": "notify",
            "type": "webhook",
            "input": "moved",
            "config": {
                "url": "https://example.com/hook",
                "events": ["pipeline.result", "pipeline.failed"],
            },
        },
    ]
    if approval is not None:
        after = "moved" if approval == "after_gate" else "fetch"
        at = next(i for i, b in enumerate(raw) if b["id"] == after) + 1
        raw[at]["input"] = "approve"
        raw.insert(at, APPROVE | {"input": after})
    inputs = {"page": {"type": "url"}, "last_price": {"type": "number"}}
    return validate({"inputs": inputs, "blocks": raw}).definition


CARRIED = [{"input": "last_price", "from_block": "extract", "path": "/price", "start": 10}]


@pytest.fixture(autouse=True)
def resolves_to():
    with patch("app.core.security.socket") as fake_socket:
        fake_socket.gaierror = socket.gaierror

        def set_ip(ip: str) -> None:
            fake_socket.getaddrinfo.return_value = [(None, None, None, None, (ip, 0))]

        set_ip("93.184.216.34")
        yield set_ip


@pytest.fixture(autouse=True)
def no_engine():
    # Outside an activity there is no client: the after-commit starts and pauses only log.
    with patch("app.core.monitor_checks.monitor_schedules.pause", new=AsyncMock()) as pause:
        yield pause


async def _monitor(user_id, state="active", approval=None) -> Monitor:
    async with AsyncSessionLocal() as db:
        key = UserLLMKey(user_id=user_id, name="k", provider="anthropic", encrypted_api_key="x")
        db.add(key)
        await db.flush()
        pipeline = Pipeline(user_id=user_id, name=f"m-{uuid.uuid4().hex[:8]}")
        db.add(pipeline)
        await db.flush()
        version = PipelineVersion(
            pipeline_id=pipeline.id, version=1, definition=definition(key.id, approval)
        )
        db.add(version)
        await db.flush()
        monitor = Monitor(
            user_id=user_id,
            name=pipeline.name,
            pipeline_id=pipeline.id,
            pipeline_version_id=version.id,
            inputs={"page": PAGE},
            carried=CARRIED,
            schedule_cron="*/5 * * * *",
            state=state,
        )
        db.add(monitor)
        await db.commit()
        await db.refresh(monitor)
        return monitor


async def _admit(monitor, run_id=None, last_chance=False):
    run_id = run_id or str(uuid.uuid4())
    admission = await admit_monitor_check(
        AdmitMonitorCheckInput(monitor_id=str(monitor.id), run_id=run_id, last_chance=last_chance)
    )
    return admission, run_id


async def _runs(monitor) -> list[PipelineRun]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(PipelineRun)
                .where(PipelineRun.monitor_id == monitor.id)
                .order_by(PipelineRun.created_at)
            )
        )


async def _events(monitor) -> list[MonitorEvent]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(MonitorEvent)
                .where(MonitorEvent.monitor_id == monitor.id)
                .order_by(MonitorEvent.first_at)
            )
        )


async def _baseline(monitor, created_at, *, fingerprint="a" * 64, captured=None, eligible=True):
    """A completed check: its gate's fingerprint and its content blocks' stored outputs."""
    run_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(
            PipelineRun(
                id=run_id,
                pipeline_id=monitor.pipeline_id,
                pipeline_version_id=monitor.pipeline_version_id,
                user_id=monitor.user_id,
                status="completed",
                workflow_id=f"pipeline-run-{run_id}",
                monitor_id=monitor.id,
                baseline_eligible=eligible,
                captured_values=captured,
                created_at=created_at,
            )
        )
        rows = [
            ("fetch", "scrape", f"b/{run_id}/scrape.html", None, None),
            ("clean", "clean", f"b/{run_id}/clean.md", None, None),
            ("gate", "validate", f"b/{run_id}/clean.md", None, fingerprint),
            ("extract", "llm_extract", f"b/{run_id}/llm.json", None, None),
        ]
        db.add_all(
            PipelineRunBlock(
                pipeline_run_id=run_id,
                block_id=b,
                position=i,
                type=t,
                status="completed",
                output_ref=ref,
                collected_at=collected,
                fingerprint=fp,
            )
            for i, (b, t, ref, collected, fp) in enumerate(rows)
        )
        await db.commit()
    return run_id


async def _quota(user_id, **limits):
    async with AsyncSessionLocal() as db:
        quota = await db.get(UserQuota, user_id)
        if quota is None:
            db.add(UserQuota(user_id=user_id, **limits))
        else:
            for name, value in limits.items():
                setattr(quota, name, value)
        await db.commit()


async def _units(user_id) -> int:
    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(func.count())
            .select_from(quota_run_units)
            .where(quota_run_units.c.user_id == user_id)
        )


# --- admitted -----------------------------------------------------------------------------


async def test_a_first_check_is_admitted_with_its_start_values_and_the_allowance(db_user):
    monitor = await _monitor(db_user.id)

    admission, run_id = await _admit(monitor)

    assert admission.verdict == "admitted"
    [run] = await _runs(monitor)
    assert (str(run.id), run.status, run.baseline_run_id) == (run_id, "running", None)
    assert run.inputs == {"page": PAGE, "last_price": 10}
    assert run.carried_sources == {
        "last_price": {"from": "start", "why": "no earlier check to carry from"}
    }
    sent = admission.run
    assert sent.run_id == run_id and sent.inputs == run.inputs
    assert sent.monitor.monitor_id == str(monitor.id)
    assert (sent.monitor.baseline_run_id, sent.monitor.gates, sent.monitor.baseline_outputs) == (
        None,
        {},
        {},
    )
    assert sent.monitor.captures == [
        CaptureSpec(input="last_price", from_block="extract", path="/price", type="number")
    ]
    async with AsyncSessionLocal() as db:
        version = await db.get(PipelineVersion, monitor.pipeline_version_id)
    parsed, _ = parse_definition(version.definition)
    assert sent.time_budget_seconds == parsed.run_budget_seconds + allowance_seconds(
        parsed, CARRIED
    )


async def test_the_newest_eligible_check_is_the_baseline(db_user):
    monitor = await _monitor(db_user.id)
    now = datetime.now(UTC)
    await _baseline(monitor, now - timedelta(hours=2), fingerprint="1" * 64)
    newest = await _baseline(
        monitor,
        now - timedelta(hours=1),
        fingerprint="2" * 64,
        captured={"last_price": {"value": 8}},
    )
    await _baseline(monitor, now - timedelta(minutes=5), fingerprint="3" * 64, eligible=False)

    admission, run_id = await _admit(monitor)

    context = admission.run.monitor
    assert context.baseline_run_id == str(newest)
    assert context.gates == {"gate": GateFingerprints(baseline="2" * 64)}
    # Content blocks only: the gate passes its input on and stores nothing.
    assert context.baseline_outputs == {
        "fetch": f"b/{newest}/scrape.html",
        "clean": f"b/{newest}/clean.md",
        "extract": f"b/{newest}/llm.json",
    }
    run = next(r for r in await _runs(monitor) if str(r.id) == run_id)
    assert (run.baseline_run_id, run.inputs["last_price"]) == (newest, 8)
    assert run.carried_sources == {"last_price": {"from": "baseline", "run_id": str(newest)}}


async def test_a_collected_copy_is_not_handed_over(db_user):
    monitor = await _monitor(db_user.id)
    baseline = await _baseline(monitor, datetime.now(UTC) - timedelta(hours=1))
    async with AsyncSessionLocal() as db:
        block = await db.scalar(
            select(PipelineRunBlock).where(
                PipelineRunBlock.pipeline_run_id == baseline, PipelineRunBlock.block_id == "extract"
            )
        )
        block.collected_at = datetime.now(UTC)
        await db.commit()

    admission, _ = await _admit(monitor)

    assert "extract" not in admission.run.monitor.baseline_outputs


@pytest.mark.parametrize(
    ("captured", "why"),
    [
        (
            {"last_price": {"missing": "no value at /price"}},
            "the baseline's value: no value at /price",
        ),
        ({"last_price": {"value": "8"}}, "the baseline's value is not a valid number"),
        ({"last_price": {"value": True}}, "the baseline's value is not a valid number"),
        (None, "the baseline captured no value"),
    ],
)
async def test_a_value_the_baseline_cannot_give_falls_back_to_start(db_user, captured, why):
    monitor = await _monitor(db_user.id)
    await _baseline(monitor, datetime.now(UTC) - timedelta(hours=1), captured=captured)

    admission, _ = await _admit(monitor)

    assert admission.run.inputs["last_price"] == 10
    run = (await _runs(monitor))[-1]
    assert run.carried_sources == {"last_price": {"from": "start", "why": why}}


async def test_a_retried_admission_finds_its_check_and_rebuilds_the_same_input(db_user):
    monitor = await _monitor(db_user.id)
    await _baseline(
        monitor, datetime.now(UTC) - timedelta(hours=1), captured={"last_price": {"value": 8}}
    )
    first, run_id = await _admit(monitor)

    again, _ = await _admit(monitor, run_id=run_id)

    assert again == first
    assert len([r for r in await _runs(monitor) if r.status == "running"]) == 1


async def test_an_admitted_check_ends_the_open_situation(db_user):
    monitor = await _monitor(db_user.id)
    await _quota(db_user.id, concurrent_jobs_limit=0)
    await _admit(monitor)
    await _quota(db_user.id, concurrent_jobs_limit=5)

    await _admit(monitor)

    [waiting] = await _events(monitor)
    assert (waiting.kind, waiting.ended_at is not None) == ("waiting_for_slot", True)


# --- not admitted -------------------------------------------------------------------------


async def test_a_paused_or_deleted_monitor_stops_the_firing(db_user):
    paused = await _monitor(db_user.id, state="paused_itself")
    assert (await _admit(paused))[0].verdict == "stopped"
    gone = AdmitMonitorCheckInput(monitor_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()))
    assert (await admit_monitor_check(gone)).verdict == "stopped"
    assert await _runs(paused) == [] and await _events(paused) == []


async def test_switched_off_is_a_skip_recorded_once_per_situation(db_user, monkeypatch):
    monitor = await _monitor(db_user.id)
    monkeypatch.setattr(settings, "pipelines_enabled", False)

    assert (await _admit(monitor))[0].verdict == "skipped"
    assert (await _admit(monitor))[0].verdict == "skipped"

    assert await _runs(monitor) == []
    [event] = await _events(monitor)
    assert (event.kind, event.count) == ("skipped_pipelines_disabled", 2)


async def test_a_firing_while_a_check_runs_is_skipped(db_user):
    monitor = await _monitor(db_user.id)
    await _admit(monitor)

    assert (await _admit(monitor))[0].verdict == "skipped"

    assert len(await _runs(monitor)) == 1
    [event] = await _events(monitor)
    assert event.kind == "skipped_check_in_progress"


# --- a check waiting for approval (ADR-016 §9e) ---------------------------------------------


async def _waits_for_approval(run_id, fingerprint="f" * 64, answer=None):
    """The check passed its first gate and its Approval block waits (the workflow's mirrors)."""
    async with AsyncSessionLocal() as db:
        blocks = {
            b.block_id: b
            for b in await db.scalars(
                select(PipelineRunBlock).where(
                    PipelineRunBlock.pipeline_run_id == uuid.UUID(run_id)
                )
            )
        }
        blocks["gate"].status, blocks["gate"].fingerprint = "completed", fingerprint
        approve = blocks["approve"]
        approve.status, approve.status_reason = "waiting", "awaiting_approval"
        approve.approval_deadline = datetime.now(UTC) + timedelta(hours=24)
        approve.approval_answer = answer
        await db.commit()


async def _unanswered_waits(monitor) -> list[uuid.UUID]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(PipelineRunBlock.pipeline_run_id)
                .join(PipelineRun, PipelineRun.id == PipelineRunBlock.pipeline_run_id)
                .where(
                    PipelineRun.monitor_id == monitor.id,
                    PipelineRun.status == "running",
                    PipelineRunBlock.status == "waiting",
                    PipelineRunBlock.approval_answer.is_(None),
                )
            )
        )


async def test_a_check_waiting_for_approval_is_not_in_flight_and_is_handed_over(db_user):
    monitor = await _monitor(db_user.id, approval="after_gate")
    await _baseline(monitor, datetime.now(UTC) - timedelta(hours=1))
    _, waiting = await _admit(monitor)
    await _waits_for_approval(waiting, fingerprint="b" * 64)

    admission, _ = await _admit(monitor)

    assert admission.verdict == "admitted"
    context = admission.run.monitor
    assert context.waiting_run_id == waiting
    # The first gate only; diffs and carried values still come from the baseline.
    assert context.gates == {"gate": GateFingerprints(baseline="a" * 64, waiting="b" * 64)}
    assert context.baseline_run_id != waiting
    assert await _events(monitor) == []


async def test_an_answered_approval_is_not_in_flight_and_not_compared_with(db_user):
    monitor = await _monitor(db_user.id, approval="after_gate")
    _, approved = await _admit(monitor)
    # Answered; its workflow not woken yet.
    await _waits_for_approval(approved, answer="approved")

    admission, _ = await _admit(monitor)

    assert admission.verdict == "admitted"
    assert (admission.run.monitor.waiting_run_id, admission.run.monitor.gates) == (None, {})


async def test_where_no_later_check_can_supersede_a_waiting_check_stays_in_flight(db_user):
    monitor = await _monitor(db_user.id, approval="before_gate")
    _, waiting = await _admit(monitor)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(
                PipelineRunBlock.pipeline_run_id == uuid.UUID(waiting),
                PipelineRunBlock.block_id == "approve",
            )
            .values(status="waiting", status_reason="awaiting_approval")
        )
        await db.commit()

    assert (await _admit(monitor))[0].verdict == "skipped"
    [event] = await _events(monitor)
    assert event.kind == "skipped_check_in_progress"


async def test_at_most_one_approval_waits_per_monitor(db_user):
    monitor = await _monitor(db_user.id, approval="after_gate")
    _, first = await _admit(monitor)
    await _waits_for_approval(first)
    assert await _unanswered_waits(monitor) == [uuid.UUID(first)]

    # The next check's content differs: at its first gate it supersedes the waiting one...
    admission, second = await _admit(monitor)
    assert admission.run.monitor.waiting_run_id == first
    with patch("app.workflows.activities.approvals.activity.client"):
        outcome = await supersede_approval(
            SupersedeApprovalInput(run_id=first, superseded_by_run_id=second)
        )
    assert outcome.answer == "superseded"
    # ...before it asks for approval itself.
    await _waits_for_approval(second)
    assert await _unanswered_waits(monitor) == [uuid.UUID(second)]

    # The superseded one has not woken yet: still not in flight, and no longer compared with.
    third, _ = await _admit(monitor)
    assert third.verdict == "admitted"
    assert third.run.monitor.waiting_run_id == second


async def test_monthly_runs_used_up_is_a_skip_with_no_run_and_no_unit(db_user):
    monitor = await _monitor(db_user.id)
    await _quota(db_user.id, monthly_runs_limit=await _units(db_user.id))
    units = await _units(db_user.id)

    assert (await _admit(monitor))[0].verdict == "skipped"
    assert (await _admit(monitor))[0].verdict == "skipped"

    assert await _units(db_user.id) == units and await _runs(monitor) == []
    [event] = await _events(monitor)
    assert (event.kind, event.count) == ("skipped_monthly_runs", 2)
    assert event.detail["resets_at"].startswith(f"{datetime.now(UTC).year}")


async def test_storage_full_is_a_failed_check_that_alerts_and_counts(db_user):
    monitor = await _monitor(db_user.id)
    await _quota(db_user.id, storage_bytes_limit=0)
    units = await _units(db_user.id)

    admission, run_id = await _admit(monitor)

    assert admission.verdict == "failed"
    [run] = await _runs(monitor)
    assert (str(run.id), run.status, run.error) == (run_id, "failed", STORAGE_FULL)
    assert await _units(db_user.id) == units + 1
    async with AsyncSessionLocal() as db:
        blocks = (
            await db.execute(
                select(PipelineRunBlock.status, PipelineRunBlock.status_reason).where(
                    PipelineRunBlock.pipeline_run_id == run.id
                )
            )
        ).all()
        [alert] = list(
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        )
        failures = (await db.get(Monitor, monitor.id)).consecutive_failures
    assert blocks == [("skipped", "not_started")] * 6
    assert (alert.event, alert.payload["error"]) == ("pipeline.failed", STORAGE_FULL)
    assert failures == 1
    # A retry finds the failed check.
    assert (await _admit(monitor, run_id=run_id))[0].verdict == "failed"
    assert len(await _runs(monitor)) == 1


async def test_a_full_concurrency_meter_waits_as_one_situation_then_gives_up(db_user):
    monitor = await _monitor(db_user.id)
    await _quota(db_user.id, concurrent_jobs_limit=0)
    run_id = str(uuid.uuid4())

    for _ in range(3):
        assert (await _admit(monitor, run_id=run_id))[0].verdict == "waiting"
    [waiting] = await _events(monitor)
    assert (waiting.kind, waiting.count, waiting.ended_at) == ("waiting_for_slot", 1, None)

    assert (await _admit(monitor, run_id=run_id, last_chance=True))[0].verdict == "skipped"
    waiting, gave_up = await _events(monitor)
    assert waiting.ended_at is not None
    assert (gave_up.kind, gave_up.ended_at) == ("gave_up_waiting", None)
    assert await _runs(monitor) == []


async def test_a_deleted_llm_key_is_a_failed_check_never_a_skip(db_user):
    monitor = await _monitor(db_user.id)
    async with AsyncSessionLocal() as db:
        version = await db.get(PipelineVersion, monitor.pipeline_version_id)
        key_id = version.definition["blocks"][3]["config"]["llm_key_id"]
        await db.delete(await db.get(UserLLMKey, uuid.UUID(key_id)))
        await db.commit()

    assert (await _admit(monitor))[0].verdict == "failed"

    [run] = await _runs(monitor)
    assert run.error == "extract: LLM key no longer exists"


async def test_a_destination_deleted_anyway_is_a_failed_check(db_user):
    from app.models.destination import Destination

    monitor = await _monitor(db_user.id)
    async with AsyncSessionLocal() as db:
        destination = Destination(
            user_id=db_user.id, name="b", kind="bucket", encrypted_secret="x", display_host="h"
        )
        db.add(destination)
        await db.flush()
        version = await db.get(PipelineVersion, monitor.pipeline_version_id)
        blocks = [dict(b) for b in version.definition["blocks"]]
        store = {
            "id": "store",
            "type": "bucket",
            "input": "moved",
            "config_schema_version": 1,
            "config": {"destination_id": str(destination.id)},
        }
        blocks[-1] = {**blocks[-1], "input": "store"}
        version.definition = {**version.definition, "blocks": [*blocks[:-1], store, blocks[-1]]}
        await db.commit()
        # Outside the API's interlock: nothing refers to it in pipeline_version_destinations.
        await db.delete(destination)
        await db.commit()

    assert (await _admit(monitor))[0].verdict == "failed"

    [run] = await _runs(monitor)
    assert run.error == "store: destination no longer exists"


async def test_an_alerting_destination_deleted_anyway_fails_the_check_and_alerts_nowhere(db_user):
    from app.models.destination import Destination

    monitor = await _monitor(db_user.id)
    async with AsyncSessionLocal() as db:
        destination = Destination(
            user_id=db_user.id, name="h", kind="webhook", encrypted_secret="x", display_host="h"
        )
        db.add(destination)
        await db.flush()
        version = await db.get(PipelineVersion, monitor.pipeline_version_id)
        notify = version.definition["blocks"][-1]
        notify = {
            **notify,
            "config": {**notify["config"], "url": None, "destination_id": str(destination.id)},
        }
        version.definition = {
            **version.definition,
            "blocks": [*version.definition["blocks"][:-1], notify],
        }
        await db.commit()
        await db.delete(destination)
        await db.commit()

    assert (await _admit(monitor))[0].verdict == "failed"

    [run] = await _runs(monitor)
    assert run.error == "notify: destination no longer exists"
    async with AsyncSessionLocal() as db:
        [alert] = await db.scalars(
            select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
        )
    assert (alert.destination_id, alert.webhook_url) == (None, None)


async def test_a_url_now_private_is_a_failed_check(db_user, resolves_to):
    monitor = await _monitor(db_user.id)
    resolves_to("10.0.0.5")

    assert (await _admit(monitor))[0].verdict == "failed"

    [run] = await _runs(monitor)
    assert run.error.startswith(f"{PAGE}: ") and "10.0.0.5" in run.error
