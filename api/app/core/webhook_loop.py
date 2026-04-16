"""Webhook delivery background task.

webhook_delivery_loop polls every 15 seconds for pending WebhookDelivery rows
and POSTs each payload to the configured URL with an HMAC-SHA256 signature.

Backoff schedule (BACKOFF_SECONDS) applies on non-2xx responses or network errors.
After settings.webhook_max_attempts failures the delivery is marked exhausted.
"""

import asyncio
import hashlib
import hmac
import json
from asyncio import get_running_loop
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import _validate_no_ssrf
from app.models.job import Job
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings

logger = structlog.get_logger()

BACKOFF_SECONDS: list[int] = [0, 30, 300, 1800, 7200]


async def webhook_delivery_loop(
    db_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    fernet: Fernet,
) -> None:
    """Background task: deliver pending webhooks with exponential backoff."""
    db_error_backoff = 2  # seconds; doubles on consecutive DB errors, capped at 60
    while True:
        await asyncio.sleep(15)  # sleep at top — no immediate trigger on startup
        try:
            async with db_factory() as db:
                stmt = (
                    select(WebhookDelivery)
                    .where(
                        WebhookDelivery.status == "pending",
                        WebhookDelivery.next_attempt_at <= datetime.now(UTC),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(50)
                )
                deliveries = (await db.execute(stmt)).scalars().all()
            # Session closes here — row locks released.
            # _attempt_delivery re-fetches by ID and re-checks status as a race guard.

            db_error_backoff = 2  # reset on successful DB query
            for delivery in deliveries:
                try:
                    await _attempt_delivery(delivery.id, db_factory, http_client, fernet)
                except Exception:
                    logger.exception(
                        "webhook: unhandled error for delivery",
                        delivery_id=str(delivery.id),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "webhook_delivery_loop: unhandled error, backing off",
                backoff=db_error_backoff,
            )
            await asyncio.sleep(db_error_backoff)
            db_error_backoff = min(db_error_backoff * 2, 60)


async def _attempt_delivery(
    delivery_id,
    db_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    fernet: Fernet,
) -> None:
    """Attempt a single webhook delivery. Opens its own DB session."""
    async with db_factory() as db:
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.status != "pending":
            return  # already handled by another replica or deleted

        # Re-validate SSRF on every attempt — DNS rebinding can change what IP
        # a hostname resolves to after the initial check at POST /jobs time.
        # This check runs before incrementing attempts: a rebinding block is a
        # security event, not a real delivery attempt.
        try:
            await get_running_loop().run_in_executor(None, _validate_no_ssrf, delivery.webhook_url)
        except ValueError as exc:
            delivery.status = "exhausted"
            delivery.last_error = f"ssrf_blocked: {exc}"
            logger.warning(
                "webhook: ssrf blocked — delivery marked exhausted",
                delivery_id=str(delivery_id),
                job_id=str(delivery.job_id),
                url=delivery.webhook_url,
                error=str(exc),
            )
            await db.commit()
            return

        delivery.attempts += 1

        job = await db.get(Job, delivery.job_id)
        secret_bytes = b""
        if job and job.webhook_secret:
            secret_bytes = fernet.decrypt(job.webhook_secret.encode())

        payload_bytes = json.dumps(delivery.payload).encode()
        sig = hmac.new(secret_bytes, payload_bytes, hashlib.sha256).hexdigest()

        try:
            resp = await http_client.post(
                delivery.webhook_url,
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-ScrapeFlow-Signature": f"sha256={sig}",
                },
                timeout=10.0,
            )
            if resp.status_code < 300:
                delivery.status = "delivered"
                delivery.delivered_at = datetime.now(UTC)
                logger.info(
                    "webhook: delivered",
                    delivery_id=str(delivery_id),
                    status_code=resp.status_code,
                )
            else:
                _apply_backoff(delivery, error=f"HTTP {resp.status_code}")
                logger.warning(
                    "webhook: delivery failed",
                    delivery_id=str(delivery_id),
                    status_code=resp.status_code,
                    attempts=delivery.attempts,
                )
        except Exception as exc:
            _apply_backoff(delivery, error=str(exc))
            logger.warning(
                "webhook: delivery error",
                delivery_id=str(delivery_id),
                error=str(exc),
                attempts=delivery.attempts,
            )

        await db.commit()


def _apply_backoff(delivery: WebhookDelivery, error: str) -> None:
    """Set delivery.last_error and advance next_attempt_at, or mark exhausted."""
    delivery.last_error = error
    if delivery.attempts >= settings.webhook_max_attempts:
        delivery.status = "exhausted"
    else:
        delay = BACKOFF_SECONDS[min(delivery.attempts, len(BACKOFF_SECONDS) - 1)]
        delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
