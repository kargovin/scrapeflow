"""Job, batch and crawl webhooks — ``webhook_loop.py``'s attempt, one call per attempt (F.4).

The wire contract is unchanged, byte for byte: the row's payload as JSON, HMAC-SHA256 over it with
the job's or batch's secret (``b""`` when there is none — a crawl has none),
``X-ScrapeFlow-Signature: sha256=<hex>``, success = status < 300, 10 s per POST, the same backoff
and attempt cap. SSRF is re-checked on every attempt and a refusal is final without counting as
one. The row stays the record the admin endpoints and the webhook meters read.
"""

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from cryptography.fernet import Fernet
from sqlalchemy import select
from temporalio import activity

from app.core import webhook_delivery
from app.core.db import AsyncSessionLocal
from app.core.security import validate_no_ssrf_core
from app.models.batch import Batch
from app.models.job import Job
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.webhook_delivery import (
    ATTEMPT_WEBHOOK_ACTIVITY,
    SWEEP_WEBHOOKS_ACTIVITY,
    AttemptOutcome,
    DeliveryRef,
)

logger = structlog.get_logger()

BACKOFF_SECONDS: list[int] = [0, 30, 300, 1800, 7200]
POST_TIMEOUT_SECONDS = 10.0
# A pending row this far past its attempt time has no delivery running for it.
SWEEP_GRACE = timedelta(seconds=60)
SWEEP_BATCH = 500


async def _post(url: str, body: bytes, headers: dict[str, str]) -> int:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, content=body, headers=headers, timeout=POST_TIMEOUT_SECONDS
        )
    return response.status_code


def _apply_backoff(delivery: WebhookDelivery, error: str) -> None:
    delivery.last_error = error
    if delivery.attempts >= settings.webhook_max_attempts:
        delivery.status = "exhausted"
    else:
        delay = BACKOFF_SECONDS[min(delivery.attempts, len(BACKOFF_SECONDS) - 1)]
        delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)


async def _secret(db, delivery: WebhookDelivery) -> bytes:
    encrypted = None
    if delivery.job_id:
        job = await db.get(Job, delivery.job_id)
        encrypted = job.webhook_secret if job else None
    elif delivery.batch_id:
        batch = await db.get(Batch, delivery.batch_id)
        encrypted = batch.webhook_secret if batch else None
    if encrypted is None:
        return b""
    return Fernet(settings.llm_key_encryption_key).decrypt(encrypted.encode())


@activity.defn(name=ATTEMPT_WEBHOOK_ACTIVITY)
async def attempt_webhook_delivery(input: DeliveryRef) -> AttemptOutcome:
    delivery_id = uuid.UUID(input.delivery_id)
    async with AsyncSessionLocal() as db:
        delivery = await db.scalar(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update()
        )
        if delivery is None or delivery.status != "pending":
            return AttemptOutcome(status="gone")

        # Every attempt (DNS rebinding). A block is a security event, not an attempt.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, validate_no_ssrf_core, delivery.webhook_url
            )
        except ValueError as exc:
            delivery.status = "exhausted"
            delivery.last_error = f"ssrf_blocked: {exc}"
            await db.commit()
            logger.warning(
                "webhook: ssrf blocked — delivery marked exhausted",
                delivery_id=input.delivery_id,
                url=delivery.webhook_url,
                error=str(exc),
            )
            return AttemptOutcome(status="exhausted")

        delivery.attempts += 1
        body = json.dumps(delivery.payload).encode()
        signature = hmac.new(await _secret(db, delivery), body, hashlib.sha256).hexdigest()
        try:
            status_code = await _post(
                delivery.webhook_url,
                body,
                {
                    "Content-Type": "application/json",
                    "X-ScrapeFlow-Signature": f"sha256={signature}",
                },
            )
            error = None if status_code < 300 else f"HTTP {status_code}"
        except Exception as exc:
            error = str(exc) or type(exc).__name__

        if error is None:
            delivery.status = "delivered"
            delivery.delivered_at = datetime.now(UTC)
        else:
            _apply_backoff(delivery, error)
        outcome = AttemptOutcome(
            status=delivery.status if delivery.status != "pending" else "retry",
            retry_in_seconds=max(
                0, round((delivery.next_attempt_at - datetime.now(UTC)).total_seconds())
            ),
        )
        await db.commit()
        logger.info(
            "webhook: attempt",
            delivery_id=input.delivery_id,
            attempts=delivery.attempts,
            outcome=outcome.status,
            error=error,
        )
        return outcome


@activity.defn(name=SWEEP_WEBHOOKS_ACTIVITY)
async def sweep_pending_webhooks() -> int:
    """Starts a delivery for each pending row overdue — one whose immediate start was lost, a row
    reset by an admin retry, or one the v1 result consumer wrote. A delivery already running for a
    row is left alone (its workflow id is taken). Returns how many were started or found running."""
    async with AsyncSessionLocal() as db:
        ids = list(
            (
                await db.scalars(
                    select(WebhookDelivery.id)
                    .where(
                        WebhookDelivery.status == "pending",
                        WebhookDelivery.next_attempt_at <= datetime.now(UTC) - SWEEP_GRACE,
                    )
                    .order_by(WebhookDelivery.next_attempt_at)
                    .limit(SWEEP_BATCH)
                )
            ).all()
        )
    await webhook_delivery.start(activity.client(), ids)
    if ids:
        logger.info("webhook_sweep_started", count=len(ids))
    return len(ids)
