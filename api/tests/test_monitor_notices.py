"""M.9 — monitor notices to a destination (ADR-016 §8, §13): a new situation queues a
``monitor.notice`` row in the transaction that records it; the row is delivered like any other."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app.core import monitor_events, webhook_delivery
from app.core.db import AsyncSessionLocal
from app.core.destinations import encrypt_secret
from app.core.email_deliveries import email_for
from app.core.monitor_checks import monitor_check_ended
from app.core.quota import lock_user
from app.main import app
from app.models.destination import Destination
from app.models.monitor import Monitor, MonitorEvent
from app.models.pipeline import PipelineRun
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities.monitor_checks import admit_monitor_check
from app.workflows.activities.webhook_deliveries import _secret, attempt_webhook_delivery
from app.workflows.monitor_check import AdmitMonitorCheckInput
from app.workflows.webhook_delivery import DeliveryRef
from tests.test_admin import admin_api_key, admin_headers, admin_user  # noqa: F401
from tests.test_email_deliveries import SMTP_ADDRESS
from tests.test_monitor_admission import (  # noqa: F401
    _admit,
    _quota,
    _units,
    no_engine,
    resolves_to,
)
from tests.test_monitor_admission import _monitor as _admission_monitor
from tests.test_monitors import (  # noqa: F401
    _body,
    _pipeline,
    gated,
    headers,
    key,
    store,
    temporal,
)
from tests.test_webhook_deliveries import Receiver, receiver, stand_in  # noqa: F401

HOOK_URL = "https://notices.example.com/in?token=t0p"
HOOK_SECRET = b"notice-secret"


async def _destination(user_id, kind="webhook") -> Destination:
    secrets = {
        "webhook": {"url": HOOK_URL, "secret": HOOK_SECRET.decode()},
        "smtp": {"user": "u", "password": "p"},
        "bucket": {"access_key": "a", "secret_key": "s"},
    }
    addresses = {
        "webhook": {},
        "smtp": SMTP_ADDRESS,
        "bucket": {
            "endpoint": "https://s3.example.com/",
            "bucket": "b",
            "prefix": "",
            "region": None,
        },
    }
    async with AsyncSessionLocal() as db:
        d = Destination(
            user_id=user_id,
            name=f"{kind}-{uuid.uuid4().hex[:6]}",
            kind=kind,
            address=addresses[kind],
            encrypted_secret=encrypt_secret(secrets[kind]),
            display_host="notices.example.com",
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
    return d


async def _monitor(user_id, destination=None) -> Monitor:
    monitor = await _admission_monitor(user_id)
    if destination is not None:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Monitor)
                .where(Monitor.id == monitor.id)
                .values(notice_destination_id=destination.id)
            )
            await db.commit()
    return monitor


async def _notices(monitor_id) -> list[WebhookDelivery]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(WebhookDelivery)
                .where(WebhookDelivery.monitor_id == monitor_id)
                .order_by(WebhookDelivery.created_at)
            )
        )


async def _events(monitor_id) -> list[MonitorEvent]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(MonitorEvent)
                .where(MonitorEvent.monitor_id == monitor_id)
                .order_by(MonitorEvent.first_at)
            )
        )


async def _get(model, id_):
    async with AsyncSessionLocal() as db:
        return await db.get(model, id_)


async def _used_up(user_id):
    await _quota(user_id, monthly_runs_limit=await _units(user_id))


# --- recorded and queued together ---------------------------------------------------------


async def test_a_month_of_skipped_firings_queues_one_notice(db_user):
    destination = await _destination(db_user.id)
    monitor = await _monitor(db_user.id, destination)
    await _used_up(db_user.id)

    for _ in range(5):
        assert (await _admit(monitor))[0].verdict == "skipped"

    [event] = await _events(monitor.id)
    assert (event.kind, event.count) == ("skipped_monthly_runs", 5)
    [notice] = await _notices(monitor.id)
    assert (
        notice.event,
        notice.kind,
        notice.monitor_event_id,
        notice.destination_id,
        notice.webhook_url,
        notice.status,
    ) == ("monitor.notice", "webhook", event.id, destination.id, None, "pending")
    payload = dict(notice.payload)
    assert datetime.fromisoformat(payload.pop("timestamp"))
    assert payload == {
        "event": "monitor.notice",
        "monitor_id": str(monitor.id),
        "notice": "skipped_monthly_runs",
        "detail": event.detail,
        "first_at": event.first_at.isoformat(),
    }


async def test_a_new_situation_after_another_sends_again(db_user, monkeypatch):
    monitor = await _monitor(db_user.id, await _destination(db_user.id))
    await _used_up(db_user.id)
    await _admit(monitor)
    monkeypatch.setattr(settings, "pipelines_enabled", False)
    await _admit(monitor)
    monkeypatch.setattr(settings, "pipelines_enabled", True)
    await _admit(monitor)

    assert [n.payload["notice"] for n in await _notices(monitor.id)] == [
        "skipped_monthly_runs",
        "skipped_pipelines_disabled",
        "skipped_monthly_runs",
    ]


async def test_history_only_situations_send_nothing(db_user):
    monitor = await _monitor(db_user.id, await _destination(db_user.id))
    await _admit(monitor)
    # A check in progress, then waiting for a slot: both history only (§7b).
    assert (await _admit(monitor))[0].verdict == "skipped"
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRun).where(PipelineRun.monitor_id == monitor.id).values(status="failed")
        )
        await db.commit()
    await _quota(db_user.id, concurrent_jobs_limit=0)
    run_id = str(uuid.uuid4())
    assert (await _admit(monitor, run_id=run_id))[0].verdict == "waiting"

    assert [e.kind for e in await _events(monitor.id)] == [
        "skipped_check_in_progress",
        "waiting_for_slot",
    ]
    assert await _notices(monitor.id) == []

    assert (await _admit(monitor, run_id=run_id, last_chance=True))[0].verdict == "skipped"
    [notice] = await _notices(monitor.id)
    assert notice.payload["notice"] == "gave_up_waiting"


async def test_a_monitor_with_no_notice_destination_records_only(db_user):
    monitor = await _monitor(db_user.id)
    await _used_up(db_user.id)
    await _admit(monitor)

    assert len(await _events(monitor.id)) == 1
    assert await _notices(monitor.id) == []


async def test_an_smtp_destination_queues_an_email_row(db_user):
    destination = await _destination(db_user.id, "smtp")
    monitor = await _monitor(db_user.id, destination)
    await _used_up(db_user.id)
    await _admit(monitor)

    [notice] = await _notices(monitor.id)
    assert (notice.kind, notice.destination_id, notice.webhook_url) == (
        "email",
        destination.id,
        None,
    )
    async with AsyncSessionLocal() as db:
        message = await email_for(db, notice)
    assert message.message_id == f"<{notice.monitor_event_id}@{settings.public_host}>"
    assert message.run_id is None
    assert message.subject == f"[ScrapeFlow] monitor {monitor.name}: skipped_monthly_runs"
    assert message.address == SMTP_ADDRESS
    assert json.dumps(notice.payload, indent=2) in message.body


async def test_paused_itself_queues_its_notice_with_the_counter(db_user):
    monitor = await _monitor(db_user.id, await _destination(db_user.id))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Monitor).where(Monitor.id == monitor.id).values(consecutive_failures=4)
        )
        run = PipelineRun(
            pipeline_id=monitor.pipeline_id,
            pipeline_version_id=monitor.pipeline_version_id,
            user_id=monitor.user_id,
            status="failed",
            error="fetch: blocked",
            workflow_id=f"pipeline-run-{uuid.uuid4()}",
            monitor_id=monitor.id,
        )
        db.add(run)
        await db.flush()
        await lock_user(monitor.user_id, db)
        ended = await monitor_check_ended(db, run)
        await db.commit()

    [event] = await _events(monitor.id)
    [notice] = await _notices(monitor.id)
    assert ended.notice_event_id == event.id == notice.monitor_event_id
    assert (notice.payload["notice"], notice.payload["detail"]["error"]) == (
        "paused_itself",
        "fetch: blocked",
    )


async def test_the_writer_starts_its_notice_after_the_commit(db_user):
    monitor = await _monitor(db_user.id, await _destination(db_user.id))
    await _used_up(db_user.id)
    client = MagicMock()
    client.start_workflow = AsyncMock()

    await ActivityEnvironment(client=client).run(
        admit_monitor_check,
        AdmitMonitorCheckInput(monitor_id=str(monitor.id), run_id=str(uuid.uuid4())),
    )

    [notice] = await _notices(monitor.id)
    [call] = client.start_workflow.call_args_list
    assert call.kwargs["id"] == f"webhook-delivery-{notice.id}"


async def test_one_notice_per_situation_whatever_retries_its_writer(db_user):
    monitor = await _monitor(db_user.id, await _destination(db_user.id))
    await _used_up(db_user.id)
    await _admit(monitor)
    [event] = await _events(monitor.id)

    async with AsyncSessionLocal() as db:
        assert await monitor_events.queue_notice(db, await db.get(Monitor, monitor.id), event)
        await db.commit()
    [notice] = await _notices(monitor.id)
    async with AsyncSessionLocal() as db:
        assert await webhook_delivery.pending(db, monitor_event_id=event.id) == [notice.id]


async def test_pending_needs_an_owner():
    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError, match="needs an owner"):
            await webhook_delivery.pending(db)


# --- the owner CHECKs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"webhook_url": "https://x.example.com/"}, id="a notice with a URL"),
        pytest.param({"monitor_event_id": None}, id="a monitor without its event"),
        pytest.param({"run_id": "run"}, id="a monitor and a job run"),
    ],
)
async def test_the_constraints_refuse_a_malformed_notice(db_user, job, fields):
    monitor = await _monitor(db_user.id, await _destination(db_user.id))
    await _used_up(db_user.id)
    await _admit(monitor)
    [event] = await _events(monitor.id)
    values = {
        "monitor_id": monitor.id,
        "monitor_event_id": event.id,
        "event": "monitor.notice",
        "payload": {},
        **fields,
    }
    async with AsyncSessionLocal() as db:
        if values.get("run_id") == "run":
            values["run_id"] = (await job_run(db, job)).id
        db.add(WebhookDelivery(**values))
        with pytest.raises(IntegrityError):
            await db.commit()


async def job_run(db, job):
    from app.models.job_runs import JobRun

    run = JobRun(job_id=job.id, status="completed", lane="v2")
    db.add(run)
    await db.flush()
    return run


@pytest_asyncio.fixture
async def job(db_user):
    from app.models.job import Job

    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.commit()
        await db.refresh(job)
    return job


# --- delivered like any other row ---------------------------------------------------------


async def _queued(db_user, kind="webhook") -> tuple[Monitor, WebhookDelivery]:
    monitor = await _monitor(db_user.id, await _destination(db_user.id, kind))
    await _used_up(db_user.id)
    await _admit(monitor)
    [notice] = await _notices(monitor.id)
    return monitor, notice


def _attempt(row):
    return attempt_webhook_delivery(DeliveryRef(delivery_id=str(row.id)))


async def test_a_notice_is_signed_with_its_destination_s_secret(db_user, receiver):  # noqa: F811
    _, notice = await _queued(db_user)

    assert (await _attempt(notice)).status == "delivered"

    [(url, body, sent)] = receiver.posts
    assert (url, sent["Host"]) == ("https://93.184.216.34/in?token=t0p", "notices.example.com")
    assert json.loads(body)["event"] == "monitor.notice"
    assert sent["X-ScrapeFlow-Signature"] == (
        "sha256=" + hmac.new(HOOK_SECRET, body, hashlib.sha256).hexdigest()
    )


async def test_an_undeliverable_notice_changes_nothing_about_the_monitor(db_user, monkeypatch):
    monitor, notice = await _queued(db_user)
    failing = stand_in(monkeypatch, Receiver(status=500))
    before = await _get(Monitor, monitor.id)

    for _ in range(settings.webhook_max_attempts):
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == notice.id)
                .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await db.commit()
        await _attempt(notice)

    row = await _get(WebhookDelivery, notice.id)
    assert (row.status, row.attempts, row.last_error) == (
        "exhausted",
        settings.webhook_max_attempts,
        "HTTP 500",
    )
    assert len(failing.posts) == settings.webhook_max_attempts
    after = await _get(Monitor, monitor.id)
    assert (after.state, after.consecutive_failures, after.updated_at) == (
        before.state,
        before.consecutive_failures,
        before.updated_at,
    )
    [event] = await _events(monitor.id)
    assert event.ended_at is None


async def test_a_notice_never_uses_another_user_s_destination(
    db_user,
    other_user,
    receiver,  # noqa: F811
):
    monitor = await _monitor(db_user.id, await _destination(other_user.id))
    await _used_up(db_user.id)
    await _admit(monitor)
    [notice] = await _notices(monitor.id)

    assert (await _attempt(notice)).status == "exhausted"
    assert receiver.posts == []
    assert (await _get(WebhookDelivery, notice.id)).last_error == "destination deleted"
    await _forget(monitor)


async def test_an_email_notice_to_another_user_s_destination_is_never_sent(db_user, other_user):
    monitor = await _monitor(db_user.id, await _destination(other_user.id, "smtp"))
    await _used_up(db_user.id)
    await _admit(monitor)
    [notice] = await _notices(monitor.id)

    assert (await _attempt(notice)).status == "exhausted"
    assert (await _get(WebhookDelivery, notice.id)).last_error == "destination deleted"
    await _forget(monitor)


async def _forget(monitor):
    # Only a test can point a monitor at another user's destination; the FK would hold it.
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Monitor).where(Monitor.id == monitor.id))
        await db.commit()


async def test_the_secret_lookup_refuses_a_notice_with_a_url():
    stray = WebhookDelivery(
        id=uuid.uuid4(),
        monitor_id=uuid.uuid4(),
        monitor_event_id=uuid.uuid4(),
        event="monitor.notice",
        webhook_url="https://x.example.com/",
        payload={},
    )
    async with AsyncSessionLocal() as db:
        with pytest.raises(ApplicationError, match="sent to its destination only"):
            await _secret(db, stray)


async def test_a_monitor_delete_takes_its_notices(db_user, client, headers, temporal):  # noqa: F811
    monitor, notice = await _queued(db_user)
    resp = await client.delete(f"/monitors/{monitor.id}", headers=headers)
    assert resp.status_code == 204
    assert await _get(WebhookDelivery, notice.id) is None


# --- the job lane's meters do not count notices (BUG-028's class) ------------------------


async def test_the_job_lane_webhook_meters_leave_notices_out(
    client,
    admin_headers,  # noqa: F811
    db_user,
):
    from app.core.minio import get_minio
    from app.core.redis import get_redis
    from tests.test_admin import _make_mock_minio, _make_mock_redis

    async def stats():
        app.dependency_overrides[get_redis] = lambda: _make_mock_redis()
        app.dependency_overrides[get_minio] = lambda: _make_mock_minio()
        try:
            resp = await client.get("/admin/stats", headers=admin_headers)
        finally:
            app.dependency_overrides.pop(get_redis, None)
            app.dependency_overrides.pop(get_minio, None)
        data = resp.json()
        return (
            data["operational"]["job_lane_webhooks_pending"],
            data["operational"]["job_lane_webhooks_exhausted"],
            data["historical"]["job_lane_webhook_success_rate_7d"],
        )

    before = await stats()
    _, pending = await _queued(db_user)
    for status in ("exhausted", "delivered"):
        _, notice = await _queued_for_another_monitor(db_user)
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(WebhookDelivery).where(WebhookDelivery.id == notice.id).values(status=status)
            )
            await db.commit()

    assert pending.status == "pending"
    assert await stats() == before


async def _queued_for_another_monitor(db_user):
    monitor = await _monitor(db_user.id, await _destination(db_user.id))
    await _admit(monitor)
    [notice] = await _notices(monitor.id)
    return monitor, notice


# --- choosing the notice destination (PATCH, create) and the delete that names it --------------


async def _api_monitor(client, headers, key, **fields):  # noqa: F811
    pipeline = await _pipeline(
        client, headers, gated(key.id, failure_route=False), name=f"p-{uuid.uuid4().hex[:8]}"
    )
    resp = await client.post("/monitors", json=_body(pipeline, **fields), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("kind", ["webhook", "smtp"])
async def test_patch_sets_and_clears_the_notice_destination(
    client,
    headers,  # noqa: F811
    key,  # noqa: F811
    temporal,  # noqa: F811
    db_user,
    kind,
):
    monitor = await _api_monitor(client, headers, key)
    assert monitor["notice_destination_id"] is None
    destination = await _destination(db_user.id, kind)
    url = f"/monitors/{monitor['id']}"

    resp = await client.patch(
        url, json={"notice_destination_id": str(destination.id)}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["notice_destination_id"] == str(destination.id)
    # A rename leaves it as it is; an explicit null clears it.
    resp = await client.patch(url, json={"name": "renamed"}, headers=headers)
    assert resp.json()["notice_destination_id"] == str(destination.id)
    resp = await client.patch(url, json={"notice_destination_id": None}, headers=headers)
    assert resp.status_code == 200
    assert (await _get(Monitor, uuid.UUID(monitor["id"]))).notice_destination_id is None


async def test_a_bucket_is_not_a_notice_destination(
    client,
    headers,  # noqa: F811
    key,  # noqa: F811
    temporal,  # noqa: F811
    db_user,
):
    monitor = await _api_monitor(client, headers, key)
    bucket = await _destination(db_user.id, "bucket")

    resp = await client.patch(
        f"/monitors/{monitor['id']}",
        json={"notice_destination_id": str(bucket.id)},
        headers=headers,
    )

    assert resp.status_code == 422
    [problem] = resp.json()["detail"]
    assert problem["message"].startswith("notice_destination_id: destination ")
    assert "must be a webhook or smtp destination" in problem["message"]
    assert (await _get(Monitor, uuid.UUID(monitor["id"]))).notice_destination_id is None


async def test_another_user_s_destination_is_not_found(
    client,
    headers,  # noqa: F811
    key,  # noqa: F811
    temporal,  # noqa: F811
    other_user,
):
    monitor = await _api_monitor(client, headers, key)
    theirs = await _destination(other_user.id)

    for target in (theirs.id, uuid.uuid4()):
        resp = await client.patch(
            f"/monitors/{monitor['id']}",
            json={"notice_destination_id": str(target)},
            headers=headers,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Destination not found"
    resp = await client.post(
        "/monitors",
        json=_body(
            await _pipeline(client, headers, gated(key.id), name="other"),
            name="second",
            notice_destination_id=str(theirs.id),
        ),
        headers=headers,
    )
    assert resp.status_code == 404


async def test_a_notice_destination_counts_as_a_failure_route(
    client,
    headers,  # noqa: F811
    key,  # noqa: F811
    temporal,  # noqa: F811
    db_user,
):
    without = await _api_monitor(client, headers, key, name="without")
    destination = await _destination(db_user.id)
    with_one = await _api_monitor(
        client, headers, key, name="with", notice_destination_id=str(destination.id)
    )

    assert any(w.startswith("nothing is sent when a check fails") for w in without["warnings"])
    assert with_one["warnings"] == []
    assert with_one["notice_destination_id"] == str(destination.id)


async def test_a_destination_delete_names_the_monitors_using_it(
    client,
    headers,  # noqa: F811
    key,  # noqa: F811
    temporal,  # noqa: F811
    db_user,
):
    destination = await _destination(db_user.id)
    monitor = await _api_monitor(
        client, headers, key, name="watcher", notice_destination_id=str(destination.id)
    )
    await client.post(f"/monitors/{monitor['id']}/pause", headers=headers)

    resp = await client.delete(f"/destinations/{destination.id}", headers=headers)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert (detail["error"], detail["pipelines"], detail["runs"]) == ("destination_in_use", [], [])
    assert detail["monitors"] == [{"id": monitor["id"], "name": "watcher"}]
    assert "watcher" in detail["message"]

    await client.patch(
        f"/monitors/{monitor['id']}", json={"notice_destination_id": None}, headers=headers
    )
    assert (
        await client.delete(f"/destinations/{destination.id}", headers=headers)
    ).status_code == 204


async def test_an_admin_user_delete_takes_a_monitor_and_its_notice_destination(
    client,
    headers,  # noqa: F811
    key,  # noqa: F811
    temporal,  # noqa: F811
    store,  # noqa: F811
    db_user,
):
    from app.auth.dependencies import get_current_admin_user

    destination = await _destination(db_user.id)
    monitor = await _api_monitor(client, headers, key, notice_destination_id=str(destination.id))
    app.dependency_overrides[get_current_admin_user] = lambda: MagicMock(id=uuid.uuid4())
    try:
        resp = await client.delete(f"/admin/users/{db_user.id}")
    finally:
        app.dependency_overrides.pop(get_current_admin_user, None)

    assert resp.status_code == 204
    assert await _get(Monitor, uuid.UUID(monitor["id"])) is None
    assert await _get(Destination, destination.id) is None
