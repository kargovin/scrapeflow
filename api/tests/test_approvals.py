"""N.2 — an Approval block's record and messages (ADR-016 §9b, §9c): the three activities, the
answer route, ``GET /approvals`` and the run reads."""

import asyncio
import socket
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from temporalio.exceptions import ApplicationError

from app.auth.api_key import generate_api_key, hash_api_key
from app.core import approvals
from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.api_key import ApiKey
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.models.user import User
from app.models.webhook_delivery import WebhookDelivery
from app.workflows.activities.approvals import remind_approval, request_approval, resolve_approval
from app.workflows.activities.db import mirror_block_status, mirror_run_status
from app.workflows.client import get_temporal
from app.workflows.pipeline import (
    RUN_CANCELLED_ERROR,
    ApprovalOutcome,
    ApprovalRef,
    MirrorBlockInput,
    MirrorRunInput,
    RequestApprovalInput,
    ResolveApprovalInput,
)
from tests.test_pipeline_approval import _destination, _guarded, _save

STORE = {"destination_id": "d1", "kind": "bucket", "location": {"bucket": "exports", "key": "k"}}


@pytest.fixture(autouse=True)
def resolves_to():
    with patch("app.core.security.socket") as fake_socket:
        fake_socket.gaierror = socket.gaierror
        fake_socket.getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 0))]
        yield


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


@pytest.fixture
def temporal():
    client = AsyncMock()
    app.dependency_overrides[get_temporal] = lambda: client
    yield client
    app.dependency_overrides.pop(get_temporal, None)


async def _triggered(client, headers, **config) -> PipelineRun:
    """A run of scrape -> approve -> notify, its rows written, its workflow not started."""
    pipeline = (await _save(client, headers, _guarded(**config), name=uuid.uuid4().hex)).json()
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 202, resp.text
    async with AsyncSessionLocal() as db:
        return await db.get(PipelineRun, uuid.UUID(resp.json()["id"]))


async def _waiting(run: PipelineRun, deadline: datetime | None = None, **request) -> datetime:
    """The workflow's first two steps: the waiting mirror, then the request."""
    deadline = deadline or datetime.now(UTC) + timedelta(hours=24)
    assert (
        await mirror_block_status(
            MirrorBlockInput(
                run_id=str(run.id),
                block_id="approve",
                status="waiting",
                status_reason="awaiting_approval",
                input_ref="scrapeflow-results/history/x/scrape.html",
            )
        )
        == "running"
    )
    await request_approval(
        RequestApprovalInput(run_id=str(run.id), block_id="approve", deadline=deadline, **request)
    )
    return deadline


async def _approval(run_id) -> PipelineRunBlock:
    async with AsyncSessionLocal() as db:
        return await approvals.approval_block(db, run_id)


async def _messages(run_id) -> list[WebhookDelivery]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(WebhookDelivery)
                .where(WebhookDelivery.pipeline_run_id == run_id)
                .order_by(WebhookDelivery.created_at)
            )
        )


REF = {"block_id": "approve"}


# --- the waiting mirror ----------------------------------------------------------------------


async def test_the_waiting_mirror_starts_the_block_with_its_reason(client, headers, temporal):
    run = await _triggered(client, headers)
    await _waiting(run)

    block = await _approval(run.id)
    assert (block.status, block.status_reason) == ("waiting", "awaiting_approval")
    assert block.started_at is not None and block.input_ref is not None


async def test_the_waiting_mirror_refuses_a_run_asked_to_cancel(client, headers, temporal):
    run = await _triggered(client, headers)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run.id)
            .values(cancel_requested_at=datetime.now(UTC))
        )
        await db.commit()

    reply = await mirror_block_status(
        MirrorBlockInput(
            run_id=str(run.id),
            block_id="approve",
            status="waiting",
            status_reason="awaiting_approval",
        )
    )

    assert reply == "cancelled"
    assert (await _approval(run.id)).status == "pending"


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_the_mirror_that_ends_the_block_clears_its_reason(client, headers, temporal, status):
    run = await _triggered(client, headers)
    await _waiting(run)

    await mirror_block_status(
        MirrorBlockInput(run_id=str(run.id), block_id="approve", status=status)
    )

    block = await _approval(run.id)
    assert (block.status, block.status_reason) == (status, None)


# --- request ---------------------------------------------------------------------------------


async def test_the_request_records_the_deadline_and_the_change_so_far(client, headers, temporal):
    run = await _triggered(client, headers)
    summary = {"guard": {"status": "ok", "kind": "page"}}
    deadline = await _waiting(run, change_status="changed", change_summary=summary)

    block = await _approval(run.id)
    assert block.approval_deadline == deadline
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.change_status, row.change_summary) == ("changed", summary)


async def test_without_a_destination_the_request_is_the_spa_only(client, headers, temporal):
    run = await _triggered(client, headers)
    await _waiting(run)
    assert await _messages(run.id) == []


@pytest.mark.parametrize(("kind", "row_kind"), [("webhook", "webhook"), ("smtp", "email")])
async def test_the_request_goes_to_the_blocks_destination_once(
    client, headers, db_user, temporal, kind, row_kind
):
    destination = await _destination(db_user.id, kind)
    run = await _triggered(client, headers, destination_id=str(destination.id))
    deadline = await _waiting(run, change_status="changed", change_summary={"g": {"status": "ok"}})
    # A retried request writes the same deadline and no second row.
    await request_approval(
        RequestApprovalInput(run_id=str(run.id), block_id="approve", deadline=deadline)
    )

    [message] = await _messages(run.id)
    assert (message.event, message.kind, message.destination_id, message.webhook_url) == (
        "pipeline.approval_requested",
        row_kind,
        destination.id,
        None,
    )
    payload = message.payload
    assert payload["deadline"] == deadline.isoformat()
    assert payload["change_status"] == "changed"
    assert payload["change_summary"] == {"g": {"status": "ok"}}
    assert payload["run_url"] == (
        f"http://localhost:8000/app/dashboard/pipelines/{run.pipeline_id}/runs/{run.id}"
    )
    assert (payload["block_id"], payload["pipeline_run_id"]) == ("approve", str(run.id))
    assert payload["deliveries"] == []
    assert "deliveries_note" not in payload


async def test_the_request_lists_what_has_already_been_delivered(
    client, headers, db_user, temporal
):
    destination = await _destination(db_user.id, "webhook")
    run = await _triggered(client, headers, destination_id=str(destination.id))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(PipelineRunBlock.pipeline_run_id == run.id, PipelineRunBlock.block_id == "fetch")
            .values(status="completed", delivery=STORE)
        )
        await db.commit()
    await _waiting(run)

    [message] = await _messages(run.id)
    assert message.payload["deliveries"] == [
        {"block_id": "fetch", "kind": "bucket", "location": STORE["location"]}
    ]
    assert "removed only if this run later fails" in message.payload["deliveries_note"]


async def test_a_request_on_a_cancelled_run_writes_nothing(client, headers, db_user, temporal):
    destination = await _destination(db_user.id, "webhook")
    run = await _triggered(client, headers, destination_id=str(destination.id))
    await mirror_block_status(
        MirrorBlockInput(
            run_id=str(run.id),
            block_id="approve",
            status="waiting",
            status_reason="awaiting_approval",
        )
    )
    await mirror_run_status(MirrorRunInput(run_id=str(run.id), status="cancelled"))

    with pytest.raises(ApplicationError) as exc:
        await request_approval(
            RequestApprovalInput(run_id=str(run.id), block_id="approve", deadline=datetime.now(UTC))
        )

    assert exc.value.type == RUN_CANCELLED_ERROR and exc.value.non_retryable
    assert (await _approval(run.id)).approval_deadline is None
    assert await _messages(run.id) == []


# --- remind ----------------------------------------------------------------------------------


async def test_the_reminder_is_queued_once_while_no_answer_is_recorded(
    client, headers, db_user, temporal
):
    destination = await _destination(db_user.id, "webhook")
    run = await _triggered(client, headers, destination_id=str(destination.id))
    await _waiting(run)
    ref = ApprovalRef(run_id=str(run.id), **REF)

    assert await remind_approval(ref) == ApprovalOutcome()
    assert await remind_approval(ref) == ApprovalOutcome()

    events = [m.event for m in await _messages(run.id)]
    assert events == ["pipeline.approval_requested", "pipeline.approval_expiring"]


async def test_an_answer_recorded_without_its_signal_is_found_by_the_reminder(
    client, headers, db_user, temporal
):
    destination = await _destination(db_user.id, "webhook")
    run = await _triggered(client, headers, destination_id=str(destination.id))
    await _waiting(run)
    await _answer(run, "approved", db_user.id)

    outcome = await remind_approval(ApprovalRef(run_id=str(run.id), **REF))

    assert outcome == ApprovalOutcome(answer="approved")
    assert [m.event for m in await _messages(run.id)] == ["pipeline.approval_requested"]


# --- resolve ---------------------------------------------------------------------------------


async def _answer(run, answer, user_id):
    async with AsyncSessionLocal() as db:
        block = await approvals.approval_block(db, run.id)
        assert await approvals.record_answer(
            db, block, answer, answered_by=user_id, open_until_deadline=True
        )
        await db.commit()


async def test_resolve_returns_nothing_before_an_answer_and_the_answer_after(
    client, headers, db_user, temporal
):
    run = await _triggered(client, headers)
    await _waiting(run)
    resolve = ResolveApprovalInput(run_id=str(run.id), **REF)

    assert await resolve_approval(resolve) == ApprovalOutcome()
    await _answer(run, "rejected", db_user.id)
    assert await resolve_approval(resolve) == ApprovalOutcome(answer="rejected")
    # The deadline then fires: the recorded answer stands.
    assert await resolve_approval(resolve.model_copy(update={"expire": True})) == ApprovalOutcome(
        answer="rejected"
    )
    block = await _approval(run.id)
    assert (block.approval_answer, block.answered_by) == ("rejected", db_user.id)


async def test_at_the_deadline_with_no_answer_it_expires_and_says_so_once(
    client, headers, db_user, temporal
):
    destination = await _destination(db_user.id, "smtp")
    run = await _triggered(client, headers, destination_id=str(destination.id))
    await _waiting(run)
    expire = ResolveApprovalInput(run_id=str(run.id), expire=True, **REF)

    assert await resolve_approval(expire) == ApprovalOutcome(answer="expired")
    assert await resolve_approval(expire) == ApprovalOutcome(answer="expired")

    block = await _approval(run.id)
    assert (block.approval_answer, block.answered_at, block.answered_by) == ("expired", None, None)
    messages = await _messages(run.id)
    assert [(m.event, m.kind) for m in messages] == [
        ("pipeline.approval_requested", "email"),
        ("pipeline.approval_expired", "email"),
    ]
    assert messages[1].payload["answer"] == "expired"


async def test_a_deleted_run_resolves_as_cancelled(client, headers, temporal):
    run = await _triggered(client, headers)
    await _waiting(run)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(PipelineRun).where(PipelineRun.id == run.id))
        await db.commit()

    for call in (
        resolve_approval(ResolveApprovalInput(run_id=str(run.id), expire=True, **REF)),
        remind_approval(ApprovalRef(run_id=str(run.id), **REF)),
    ):
        with pytest.raises(ApplicationError) as exc:
            await call
        assert exc.value.type == RUN_CANCELLED_ERROR


# --- answering -------------------------------------------------------------------------------


async def _key(user, admin=False) -> dict:
    raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKey(user_id=user.id, key_hash=hash_api_key(raw), name="other key"))
        if admin:
            await db.execute(update(User).where(User.id == user.id).values(is_admin=True))
        await db.commit()
    return {"X-API-Key": raw}


@pytest.fixture
def signal(mock_temporal):
    sent = AsyncMock()
    mock_temporal.get_workflow_handle.return_value.signal = sent
    return sent


def _answer_url(run) -> str:
    return f"/pipelines/{run.pipeline_id}/runs/{run.id}/approval"


async def test_the_owner_approves_then_the_workflow_is_woken(
    client, headers, db_user, temporal, signal, mock_temporal
):
    run = await _triggered(client, headers)
    deadline = await _waiting(run)

    resp = await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["state"], body["answered_by"], body["block_id"]) == (
        "approved",
        str(db_user.id),
        "approve",
    )
    assert datetime.fromisoformat(body["deadline"]) == deadline
    block = await _approval(run.id)
    assert (block.approval_answer, block.answered_by) == ("approved", db_user.id)
    assert block.answered_at is not None
    # Still waiting: only the workflow moves the block on.
    assert block.status == "waiting"
    mock_temporal.get_workflow_handle.assert_called_with(run.workflow_id)
    signal.assert_awaited_once_with("approval_answered")


async def test_an_answer_is_never_flipped(client, headers, temporal, signal):
    run = await _triggered(client, headers)
    await _waiting(run)
    await client.post(_answer_url(run), json={"decision": "rejected"}, headers=headers)

    resp = await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["message"] == "The approval is already rejected"
    assert detail["approval"]["state"] == "rejected"
    assert (await _approval(run.id)).approval_answer == "rejected"
    signal.assert_awaited_once()


async def test_after_the_deadline_the_answer_is_refused_with_the_current_state(
    client, headers, temporal, signal
):
    run = await _triggered(client, headers)
    await _waiting(run, deadline=datetime.now(UTC) - timedelta(seconds=1))

    resp = await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["message"] == "The approval's deadline has passed"
    assert (await _approval(run.id)).approval_answer is None
    signal.assert_not_awaited()


async def test_an_expired_approval_cannot_be_answered(client, headers, temporal, signal):
    run = await _triggered(client, headers)
    await _waiting(run)
    await resolve_approval(ResolveApprovalInput(run_id=str(run.id), expire=True, **REF))

    resp = await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["approval"]["state"] == "expired"


async def test_before_the_request_there_is_nothing_to_answer(client, headers, temporal, signal):
    run = await _triggered(client, headers)

    resp = await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["approval"]["state"] == "not_requested"


async def test_a_run_being_cancelled_cannot_be_approved(client, headers, temporal, signal):
    run = await _triggered(client, headers)
    await _waiting(run)
    cancel = await client.post(
        f"/pipelines/{run.pipeline_id}/runs/{run.id}/cancel", headers=headers
    )
    # Nothing executes while an approval waits.
    assert (cancel.json()["still_running"], cancel.json()["upper_bound_seconds"]) == ([], 0)

    resp = await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["message"] == "The run is being cancelled"
    assert (await _approval(run.id)).approval_answer is None


@pytest.mark.parametrize("admin", [False, True])
async def test_anyone_but_the_owner_gets_a_404_admins_included(
    client, headers, other_user, temporal, signal, admin
):
    run = await _triggered(client, headers)
    await _waiting(run)

    resp = await client.post(
        _answer_url(run), json={"decision": "approved"}, headers=await _key(other_user, admin)
    )

    assert resp.status_code == 404, resp.text
    assert (await _approval(run.id)).approval_answer is None
    signal.assert_not_awaited()


async def test_a_run_without_an_approval_block_is_a_404(client, headers, temporal):
    pipeline = (
        await _save(
            client,
            headers,
            {"blocks": [{"id": "fetch", "type": "scrape", "config": {"url": "https://e.com/"}}]},
            name=uuid.uuid4().hex,
        )
    ).json()
    run = (await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)).json()

    resp = await client.post(
        f"/pipelines/{pipeline['id']}/runs/{run['id']}/approval",
        json={"decision": "approved"},
        headers=headers,
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "This run has no approval block"


async def test_a_lost_signal_still_leaves_the_answer_recorded(client, headers, temporal, signal):
    run = await _triggered(client, headers)
    await _waiting(run)
    signal.side_effect = RuntimeError("engine down")

    resp = await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    assert resp.status_code == 200, resp.text
    resolve = ResolveApprovalInput(run_id=str(run.id), expire=True, **REF)
    assert await resolve_approval(resolve) == ApprovalOutcome(answer="approved")


async def test_only_approved_or_rejected_is_an_answer(client, headers, temporal):
    run = await _triggered(client, headers)
    await _waiting(run)
    resp = await client.post(_answer_url(run), json={"decision": "expired"}, headers=headers)
    assert resp.status_code == 422


# --- an answer racing the expiry ---------------------------------------------------------------


async def test_an_answer_committed_first_beats_the_expiry(client, headers, temporal, signal):
    run = await _triggered(client, headers)
    await _waiting(run)
    await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    outcome = await resolve_approval(ResolveApprovalInput(run_id=str(run.id), expire=True, **REF))

    assert outcome == ApprovalOutcome(answer="approved")
    assert [m.event for m in await _messages(run.id)] == []


async def test_an_answer_waits_for_an_expiry_holding_the_run_and_then_sees_it(
    client, headers, temporal, signal
):
    run = await _triggered(client, headers)
    await _waiting(run)
    async with AsyncSessionLocal() as holder:
        # The expiry's transaction, open: it holds the run's lock.
        await holder.scalar(select(PipelineRun).where(PipelineRun.id == run.id).with_for_update())
        block = await approvals.approval_block(holder, run.id)
        assert await approvals.record_answer(holder, block, "expired")
        task = asyncio.create_task(
            client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)
        )
        await asyncio.sleep(1)
        assert not task.done(), "the answer did not wait for the run's lock"
        await holder.commit()
    resp = await asyncio.wait_for(task, timeout=10)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["approval"]["state"] == "expired"
    assert (await _approval(run.id)).approval_answer == "expired"


async def test_an_answer_and_the_expiry_together_one_wins(client, headers, temporal, signal):
    for _ in range(5):
        run = await _triggered(client, headers)
        await _waiting(run)
        resp, outcome = await asyncio.gather(
            client.post(_answer_url(run), json={"decision": "approved"}, headers=headers),
            resolve_approval(ResolveApprovalInput(run_id=str(run.id), expire=True, **REF)),
        )
        recorded = (await _approval(run.id)).approval_answer
        assert outcome.answer == recorded
        assert (resp.status_code == 200) == (recorded == "approved")
        assert recorded in ("approved", "expired")


# --- reads -------------------------------------------------------------------------------------


async def test_get_approvals_lists_the_users_waiting_ones_soonest_first(
    client, headers, other_user, temporal, signal
):
    later, sooner, answered, not_asked = [await _triggered(client, headers) for _ in range(4)]
    now = datetime.now(UTC)
    await _waiting(later, deadline=now + timedelta(hours=5))
    await _waiting(sooner, deadline=now + timedelta(hours=1), change_status="changed")
    await _waiting(answered)
    await client.post(_answer_url(answered), json={"decision": "rejected"}, headers=headers)
    del not_asked

    resp = await client.get("/approvals", headers=headers)

    assert resp.status_code == 200, resp.text
    listed = resp.json()
    assert [a["run_id"] for a in listed] == [str(sooner.id), str(later.id)]
    first = listed[0]
    assert (first["state"], first["change_status"], first["block_id"]) == (
        "waiting",
        "changed",
        "approve",
    )
    assert first["pipeline_name"] and first["requested_at"] and first["deliveries"] == []
    other = await client.get("/approvals", headers=await _key(other_user))
    assert other.json() == []


async def test_a_run_read_shows_the_approval_and_its_messages_apart_from_alerts(
    client, headers, db_user, temporal, signal
):
    destination = await _destination(db_user.id, "webhook")
    run = await _triggered(client, headers, destination_id=str(destination.id))
    await _waiting(run)
    await client.post(_answer_url(run), json={"decision": "approved"}, headers=headers)

    resp = await client.get(f"/pipelines/{run.pipeline_id}/runs/{run.id}", headers=headers)

    body = resp.json()
    approve = next(b for b in body["blocks"] if b["block_id"] == "approve")
    assert (approve["status"], approve["status_reason"], approve["approval_answer"]) == (
        "waiting",
        "awaiting_approval",
        "approved",
    )
    assert approve["approval_deadline"] and approve["answered_by"] == str(db_user.id)
    assert body["alerts"] == []
    [message] = body["approval_messages"]
    assert (message["event"], message["status"], message["destination_id"]) == (
        "pipeline.approval_requested",
        "pending",
        str(destination.id),
    )
