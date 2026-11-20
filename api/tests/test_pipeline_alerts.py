"""J.7 — a pipeline run's failure alerts: rows written beside the failed status, delivered by the
job lane's WebhookDeliveryWorkflow, signed with the sending block's secret (ADR-014 §8)."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core import egress
from app.core.db import AsyncSessionLocal
from app.core.destinations import encrypt_secret
from app.core.pipeline_alerts import queue_failure_alerts
from app.main import app
from app.models.destination import Destination
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities.db import mirror_run_status
from app.workflows.activities.webhook_deliveries import (
    _destination_target,
    _secret,
    attempt_webhook_delivery,
)
from app.workflows.pipeline import MirrorRunInput
from app.workflows.webhook_delivery import DeliveryRef
from tests.test_admin import admin_api_key, admin_headers, admin_user  # noqa: F401
from tests.test_webhook_deliveries import Receiver, stand_in

ALERT_URL = "https://alerts.example.com/in"
RESULT_URL = "https://hooks.example.com/in"
BLOCK_SECRET = b"block-secret"


def _webhook(block_id, url, events, secret=None):
    return {
        "id": block_id,
        "type": "webhook",
        "input": "fetch",
        "config_schema_version": 1,
        "config": {"url": url, "secret": None, "events": events},
        "secrets": (
            {"secret": Fernet(settings.credentials_encryption_key).encrypt(secret).decode()}
            if secret
            else {}
        ),
    }


def _definition(*webhooks):
    return {
        "inputs": {},
        "time_budget_seconds": None,
        "blocks": [
            {
                "id": "fetch",
                "type": "scrape",
                "input": None,
                "config_schema_version": 1,
                "config": {
                    "url": "https://example.com/",
                    "output_format": "html",
                    "engine": "http",
                    "playwright_options": None,
                    "respect_robots": False,
                    "proxy_url": None,
                    "cookies": None,
                    "actions": None,
                },
                "secrets": {},
            },
            *webhooks,
        ],
    }


ALERTING = _definition(
    _webhook("alert", ALERT_URL, ["pipeline.failed"], secret=BLOCK_SECRET),
    _webhook("both", RESULT_URL, ["pipeline.result", "pipeline.failed"]),
    _webhook("quiet", RESULT_URL, ["pipeline.result"]),
)


async def _run(user_id, definition=ALERTING, status="running", **fields) -> PipelineRun:
    async with AsyncSessionLocal() as db:
        pipeline = Pipeline(user_id=user_id, name=f"p-{uuid.uuid4().hex[:6]}")
        db.add(pipeline)
        await db.flush()
        version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition=definition)
        db.add(version)
        await db.flush()
        run_id = uuid.uuid4()
        run = PipelineRun(
            id=run_id,
            pipeline_id=pipeline.id,
            pipeline_version_id=version.id,
            user_id=user_id,
            status=status,
            workflow_id=f"pipeline-run-{run_id}",
            **fields,
        )
        db.add(run)
        db.add_all(
            PipelineRunBlock(
                pipeline_run_id=run_id,
                block_id=b["id"],
                position=i,
                type=b["type"],
                status="pending",
            )
            for i, b in enumerate(definition["blocks"])
        )
        await db.commit()
    return run


async def _alerts(run_id) -> list[WebhookDelivery]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.scalars(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.pipeline_run_id == run_id)
                    .order_by(WebhookDelivery.block_id)
                )
            ).all()
        )


async def _fail(run, error="fetch: blocked:akamai", block="fetch"):
    return await mirror_run_status(
        MirrorRunInput(run_id=str(run.id), status="failed", error=error, failed_block_id=block)
    )


async def test_a_failed_run_queues_one_alert_per_block_that_sends_pipeline_failed(db_user):
    run = await _run(db_user.id)
    await _fail(run)
    await _fail(run)  # a retried mirror

    rows = await _alerts(run.id)
    assert [(r.block_id, r.event, r.webhook_url, r.status) for r in rows] == [
        ("alert", "pipeline.failed", ALERT_URL, "pending"),
        ("both", "pipeline.failed", RESULT_URL, "pending"),
    ]
    payload = rows[0].payload
    assert set(payload) == {
        "event",
        "run_id",
        "pipeline_id",
        "pipeline_run_id",
        "block_id",
        "failed_block_id",
        "error",
        "result_path",
        "diff_detected",
        "diff_summary",
        "rollback",
        "timestamp",
    }
    assert {k: v for k, v in payload.items() if k != "timestamp"} == {
        "event": "pipeline.failed",
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "pipeline_run_id": str(run.id),
        "block_id": "alert",
        "failed_block_id": "fetch",
        "error": "fetch: blocked:akamai",
        "result_path": None,
        "diff_detected": False,
        "diff_summary": None,
        "rollback": {"undone": [], "left_in_place": [], "kept": []},
    }
    assert (rows[0].run_id, rows[0].job_id, rows[0].attempts) == (None, None, 0)


async def test_the_alert_sending_block_failing_itself_still_alerts(db_user):
    run = await _run(db_user.id)
    await _fail(run, error="both: HTTP 500 (gave up after 5 attempts)", block="both")
    rows = await _alerts(run.id)
    assert {r.block_id: r.payload["failed_block_id"] for r in rows} == {
        "alert": "both",
        "both": "both",
    }


@pytest.mark.parametrize(
    "ending",
    [
        {"status": "completed", "stopped_at_block_id": "fetch", "stop_reason": "rule_not_met"},
        {"status": "completed"},
        {"status": "cancelled"},
    ],
)
async def test_no_alert_on_a_stop_a_success_or_a_cancel(db_user, ending):
    run = await _run(db_user.id)
    await mirror_run_status(MirrorRunInput(run_id=str(run.id), **ending))
    assert await _alerts(run.id) == []


async def test_a_failure_reported_after_a_cancel_queues_nothing(db_user):
    run = await _run(db_user.id, status="cancelled")
    await _fail(run)
    assert await _alerts(run.id) == []


async def test_a_pipeline_without_alerting_blocks_queues_nothing(db_user):
    run = await _run(db_user.id, _definition(_webhook("quiet", RESULT_URL, ["pipeline.result"])))
    await _fail(run)
    assert await _alerts(run.id) == []


async def test_a_definition_saved_before_events_existed_sends_no_alert(db_user):
    old = _definition(_webhook("notify", RESULT_URL, ["pipeline.result"]))
    del old["blocks"][1]["config"]["events"]
    run = await _run(db_user.id, old)
    await _fail(run)
    assert await _alerts(run.id) == []


# --- delivery: the job lane's attempt, the block's secret -------------------------------------


@pytest.fixture
def receiver(monkeypatch):
    return stand_in(monkeypatch, Receiver())


async def test_an_alert_is_signed_with_its_blocks_secret(db_user, receiver):
    run = await _run(db_user.id)
    await _fail(run)
    signed, unsigned = await _alerts(run.id)

    for row in (signed, unsigned):
        outcome = await attempt_webhook_delivery(DeliveryRef(delivery_id=str(row.id)))
        assert outcome.status == "delivered"

    (url_a, body_a, headers_a), (url_b, body_b, headers_b) = receiver.posts
    assert (headers_a["Host"], headers_b["Host"]) == ("alerts.example.com", "hooks.example.com")
    assert url_a == url_b == "https://93.184.216.34/in"
    assert json.loads(body_a) == signed.payload
    assert headers_a["X-ScrapeFlow-Signature"] == (
        "sha256=" + hmac.new(BLOCK_SECRET, body_a, hashlib.sha256).hexdigest()
    )
    # A block with no secret signs with b"", as the Webhook block itself does.
    assert headers_b["X-ScrapeFlow-Signature"] == (
        "sha256=" + hmac.new(b"", body_b, hashlib.sha256).hexdigest()
    )


async def test_a_private_alert_url_is_refused_on_every_attempt(db_user, monkeypatch):
    def private(host, port, type):
        return [(None, None, None, None, ("10.0.0.5", port))]

    receiver = Receiver()
    monkeypatch.setattr(egress, "_post", receiver)
    monkeypatch.setattr(egress.socket, "getaddrinfo", private)
    run = await _run(db_user.id)
    await _fail(run)
    row, _ = await _alerts(run.id)

    outcome = await attempt_webhook_delivery(DeliveryRef(delivery_id=str(row.id)))
    assert outcome.status == "exhausted"
    assert receiver.posts == []
    [after, _] = await _alerts(run.id)
    assert (after.attempts, after.last_error) == (
        0,
        "ssrf_blocked: URL resolves to a private address: 10.0.0.5",
    )


async def test_a_row_with_no_owner_is_refused_rather_than_signed_with_an_empty_key():
    orphan = WebhookDelivery(id=uuid.uuid4(), event="x", webhook_url=RESULT_URL, payload={})
    async with AsyncSessionLocal() as db:
        with pytest.raises(Exception, match="has no owner"):
            await _secret(db, orphan)


async def test_queue_failure_alerts_returns_how_many_blocks_alert(db_user):
    run = await _run(db_user.id, status="failed", failed_block_id="fetch", error="x")
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
        assert await queue_failure_alerts(db, row) == 2
        await db.commit()
    assert len(await _alerts(run.id)) == 2


# --- the meters and the admin reads (§8e) ----------------------------------------------------


async def _platform_meters(client, headers):
    from app.core.minio import get_minio
    from app.core.redis import get_redis
    from tests.test_admin import _make_mock_minio, _make_mock_redis

    app.dependency_overrides[get_redis] = lambda: _make_mock_redis()
    app.dependency_overrides[get_minio] = lambda: _make_mock_minio()
    try:
        data = (await client.get("/admin/stats", headers=headers)).json()
    finally:
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_minio, None)
    return (
        data["operational"]["job_lane_webhooks_pending"],
        data["operational"]["job_lane_webhooks_exhausted"],
        data["historical"]["job_lane_webhook_success_rate_7d"],
    )


async def test_pipeline_alerts_leave_the_job_lane_meters_unchanged(
    client,
    admin_headers,  # noqa: F811
    db_user,
):
    before = await _platform_meters(client, admin_headers)
    run = await _run(db_user.id)
    await _fail(run)
    exhausted, delivered = await _alerts(run.id)
    async with AsyncSessionLocal() as db:
        (await db.get(WebhookDelivery, exhausted.id)).status = "exhausted"
        (await db.get(WebhookDelivery, delivered.id)).status = "delivered"
        await db.commit()
    assert await _platform_meters(client, admin_headers) == before


async def test_admin_reads_show_an_alert_with_its_run_and_block_and_can_retry_it(
    client,
    admin_headers,  # noqa: F811
    db_user,
):
    run = await _run(db_user.id)
    await _fail(run)
    row, _ = await _alerts(run.id)
    async with AsyncSessionLocal() as db:
        stored = await db.get(WebhookDelivery, row.id)
        stored.status, stored.attempts = "exhausted", 5
        stored.next_attempt_at = datetime.now(UTC)
        await db.commit()

    listed = (
        await client.get("/admin/webhooks/deliveries?status=exhausted", headers=admin_headers)
    ).json()
    [mine] = [d for d in listed if d["id"] == str(row.id)]
    assert (mine["pipeline_run_id"], mine["block_id"], mine["job_id"], mine["run_id"]) == (
        str(run.id),
        "alert",
        None,
        None,
    )

    resp = await client.post(f"/admin/webhooks/deliveries/{row.id}/retry", headers=admin_headers)
    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["attempts"]) == ("pending", 0)


# --- M.7: an alert from a block naming a webhook destination -----------------------------------

DESTINATION_URL = "https://dest.example.com/in?token=t0p"


async def _hook_destination(user_id, secret=b"dest-secret") -> Destination:
    async with AsyncSessionLocal() as db:
        d = Destination(
            user_id=user_id,
            name=f"hook-{uuid.uuid4().hex[:6]}",
            kind="webhook",
            address={},
            encrypted_secret=encrypt_secret(
                {"url": DESTINATION_URL, "secret": secret.decode() if secret else None}
            ),
            display_host="dest.example.com",
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
    return d


def _destination_webhook(block_id, destination_id):
    return {
        "id": block_id,
        "type": "webhook",
        "input": "fetch",
        "config_schema_version": 1,
        "config": {
            "url": None,
            "secret": None,
            "destination_id": str(destination_id),
            "events": ["pipeline.failed"],
        },
        "secrets": {},
    }


async def _destination_alert(user_id, destination_id) -> WebhookDelivery:
    run = await _run(user_id, _definition(_destination_webhook("alert", destination_id)))
    await _fail(run)
    [row] = await _alerts(run.id)
    return row


async def test_a_destination_blocks_alert_stores_the_destination_and_no_url(db_user):
    destination = await _hook_destination(db_user.id)
    row = await _destination_alert(db_user.id, destination.id)
    assert (row.kind, row.destination_id, row.webhook_url) == (
        "webhook",
        destination.id,
        None,
    )


async def test_a_destination_alert_is_sent_to_its_url_with_its_secret(db_user, receiver):
    destination = await _hook_destination(db_user.id)
    row = await _destination_alert(db_user.id, destination.id)

    outcome = await attempt_webhook_delivery(DeliveryRef(delivery_id=str(row.id)))

    assert outcome.status == "delivered"
    [(url, body, headers)] = receiver.posts
    assert (url, headers["Host"]) == ("https://93.184.216.34/in?token=t0p", "dest.example.com")
    assert headers["X-ScrapeFlow-Signature"] == (
        "sha256=" + hmac.new(b"dest-secret", body, hashlib.sha256).hexdigest()
    )


async def test_an_alert_whose_destination_was_deleted_ends_without_an_attempt(db_user, receiver):
    destination = await _hook_destination(db_user.id)
    row = await _destination_alert(db_user.id, destination.id)
    async with AsyncSessionLocal() as db:
        await db.delete(await db.get(Destination, destination.id))
        await db.commit()

    outcome = await attempt_webhook_delivery(DeliveryRef(delivery_id=str(row.id)))

    assert outcome.status == "exhausted"
    assert receiver.posts == []
    [after] = await _alerts(row.pipeline_run_id)
    # ON DELETE SET NULL: the row stays the record.
    assert (after.destination_id, after.attempts, after.last_error) == (
        None,
        0,
        "destination deleted",
    )


async def test_an_alert_never_uses_another_users_destination(db_user, other_user, receiver):
    destination = await _hook_destination(other_user.id)
    row = await _destination_alert(db_user.id, destination.id)

    outcome = await attempt_webhook_delivery(DeliveryRef(delivery_id=str(row.id)))

    assert outcome.status == "exhausted"
    assert receiver.posts == []
    [after] = await _alerts(row.pipeline_run_id)
    assert after.last_error == "destination deleted"


async def test_a_destination_row_with_no_owner_it_can_send_for_raises():
    orphan = WebhookDelivery(
        id=uuid.uuid4(), event="x", webhook_url=None, destination_id=uuid.uuid4(), payload={}
    )
    async with AsyncSessionLocal() as db:
        with pytest.raises(Exception, match="no owner it can send for"):
            await _destination_target(db, orphan)
