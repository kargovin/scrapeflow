"""N.2 — an Approval block's record and messages (ADR-016 §9b, §9c): the three activities, the
answer route, ``GET /approvals`` and the run reads."""

import socket
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from temporalio.exceptions import ApplicationError

from app.core import approvals
from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.pipeline import PipelineRun, PipelineRunBlock
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
