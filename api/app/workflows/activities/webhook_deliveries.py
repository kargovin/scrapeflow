"""Job, batch, crawl and pipeline-alert webhooks — the deleted ``webhook_loop.py``'s attempt, one call per attempt (F.4).

The send is ``core/egress.send_signed_post``, shared with the pipeline Webhook block: the row's
payload as JSON, signed with the owner's secret (``b""`` when there is none — a crawl has none).
Success = status < 300, the same backoff and attempt cap. Every attempt resolves afresh; an address
refusal is final without counting as an attempt. The row stays the record the admin endpoints and the
webhook meters read.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from cryptography.fernet import Fernet
from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core import egress, webhook_delivery
from app.core.db import AsyncSessionLocal
from app.core.pipeline_alerts import alert_secret
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
# A pending row this far past its attempt time has no delivery running for it.
SWEEP_GRACE = timedelta(seconds=60)
SWEEP_BATCH = 500


def _apply_backoff(delivery: WebhookDelivery, error: str) -> None:
    delivery.last_error = error
    if delivery.attempts >= settings.webhook_max_attempts:
        delivery.status = "exhausted"
    else:
        delay = BACKOFF_SECONDS[min(delivery.attempts, len(BACKOFF_SECONDS) - 1)]
        delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)


async def _secret(db, delivery: WebhookDelivery) -> bytes:
    # One arm per owner: a missed arm would sign with b"" silently.
    key = settings.llm_key_encryption_key
    if delivery.job_id:
        job = await db.get(Job, delivery.job_id)
        encrypted = job.webhook_secret if job else None
    elif delivery.batch_id:
        batch = await db.get(Batch, delivery.batch_id)
        encrypted = batch.webhook_secret if batch else None
    elif delivery.crawl_id:
        return b""
    elif delivery.pipeline_run_id:
        # Encrypted by core/pipeline_trigger.encrypt_secret, not with the jobs' key.
        key = settings.credentials_encryption_key
        encrypted = await alert_secret(db, delivery)
    else:
        raise ApplicationError(f"webhook delivery {delivery.id} has no owner", non_retryable=True)
    if encrypted is None:
        return b""
    return Fernet(key).decrypt(encrypted.encode())


@activity.defn(name=ATTEMPT_WEBHOOK_ACTIVITY)
async def attempt_webhook_delivery(input: DeliveryRef) -> AttemptOutcome:
    delivery_id = uuid.UUID(input.delivery_id)
    async with AsyncSessionLocal() as db:
        delivery = await db.scalar(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update()
        )
        if delivery is None or delivery.status != "pending":
            return AttemptOutcome(status="gone")

        body = json.dumps(delivery.payload).encode()
        secret = await _secret(db, delivery)
        try:
            status_code = await egress.send_signed_post(delivery.webhook_url, body, secret)
            error = None if status_code < 300 else f"HTTP {status_code}"
        except egress.EgressBlocked as exc:
            # A security event, not an attempt.
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
        except egress.CertificateMismatch as exc:
            delivery.attempts += 1
            delivery.status = "exhausted"
            delivery.last_error = str(exc)
            await db.commit()
            return AttemptOutcome(status="exhausted")
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        delivery.attempts += 1

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
