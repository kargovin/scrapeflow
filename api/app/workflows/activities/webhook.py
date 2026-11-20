"""The Webhook block's activity — workflow-worker queue (it writes the block's status).

The send is ``core/egress.send_signed_post``, shared with the job lane: the wire contract, the
address check and the connection to the checked address. Success = status < 300.

A block naming a webhook destination reads its URL and signing key at each attempt, so an edit takes
effect on the next one (ADR-015 §2b); a destination gone is final.

One call is one attempt; the retry policy is the delivery loop (ADR-009 §15). The workflow is
blocked inside one ``execute_activity`` across every retry, so the attempt itself mirrors the
block: ``running`` as it starts (refused on a cancelled run — 15d's boundary re-check), ``waiting``
when it fails and another attempt will follow (15a — a waiting block holds no slot).
"""

import json
import uuid

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core import egress
from app.core.db import AsyncSessionLocal
from app.core.destinations import DestinationGone, current_destination, webhook_target
from app.models.pipeline import PipelineRun
from app.settings import settings
from app.workflows.activities.db import mirror_block_status
from app.workflows.pipeline import (
    DESTINATION_GONE_ERROR,
    RUN_CANCELLED_ERROR,
    WEBHOOK_ACTIVITY,
    WEBHOOK_EVENT,
    MirrorBlockInput,
    WebhookInput,
)


def build_payload(input: WebhookInput) -> dict:
    # The job lane's fields (core/webhooks.py) less job_id, plus the pipeline identifiers
    # (PRD-016 R6's two payload exceptions). Outside a monitor's check: constants.
    payload = {
        "event": WEBHOOK_EVENT,
        "run_id": input.run_id,
        "pipeline_id": input.pipeline_id,
        "pipeline_run_id": input.run_id,
        "result_path": input.result_path,
        "diff_detected": False,
        "diff_summary": None,
        "timestamp": input.timestamp,
    }
    if input.deliveries is not None:
        # PRD-017 R4: which block sent it, and where the sinks before it wrote.
        payload["block_id"] = input.block_id
        payload["deliveries"] = input.deliveries
    if input.monitor_id is not None:
        # ADR-016 §6a, G7: a receiver reading the old field is never told "no change" about one.
        change = input.change or {}
        payload["diff_detected"] = change.get("status") == "changed"
        payload["monitor_id"] = input.monitor_id
        payload["change"] = input.change
    return payload


def _unreadable_secret() -> ApplicationError:
    return ApplicationError(
        "the webhook secret cannot be decrypted", type="WebhookSecret", non_retryable=True
    )


def _secret(encrypted: str | None) -> bytes:
    if encrypted is None:
        return b""
    try:
        return Fernet(settings.credentials_encryption_key).decrypt(encrypted.encode())
    except InvalidToken:
        raise _unreadable_secret() from None


async def _target(input: WebhookInput) -> tuple[str, bytes]:
    """The URL and signing key for this attempt."""
    if input.destination_id is None:
        assert input.url is not None
        return input.url, _secret(input.encrypted_secret)
    async with AsyncSessionLocal() as db:
        user_id = await db.scalar(
            select(PipelineRun.user_id).where(PipelineRun.id == uuid.UUID(input.run_id))
        )
        if user_id is None:
            raise ApplicationError(
                "the run was deleted", type=RUN_CANCELLED_ERROR, non_retryable=True
            )
        try:
            destination = await current_destination(
                db, uuid.UUID(input.destination_id), user_id, "webhook"
            )
            return webhook_target(destination)
        except DestinationGone as exc:
            raise ApplicationError(
                str(exc), type=DESTINATION_GONE_ERROR, non_retryable=True
            ) from None
        except InvalidToken:
            raise _unreadable_secret() from None


async def _mirror(input: WebhookInput, status: str, error: str | None = None) -> str:
    return await mirror_block_status(
        MirrorBlockInput(
            run_id=input.run_id,
            block_id=input.block_id,
            status=status,
            input_ref=input.result_path if status == "running" else None,
            error=error,
        )
    )


@activity.defn(name=WEBHOOK_ACTIVITY)
async def deliver_webhook(input: WebhookInput) -> None:
    info = activity.info()
    # Attempt 1: the workflow mirrored running a moment ago, cancel check included.
    if info.attempt > 1 and await _mirror(input, "running") != "running":
        raise ApplicationError(
            "the run was cancelled", type=RUN_CANCELLED_ERROR, non_retryable=True
        )

    body = json.dumps(build_payload(input)).encode()
    url, secret = await _target(input)
    # A refusal is final — retrying hands an attacker hours.
    try:
        status_code = await egress.send_signed_post(url, body, secret)
        error = None if status_code < 300 else f"HTTP {status_code}"
    except egress.EgressBlocked as exc:
        raise ApplicationError(
            f"ssrf_blocked: {exc}", type="WebhookBlocked", non_retryable=True
        ) from None
    except egress.CertificateMismatch as exc:
        raise ApplicationError(str(exc), type="WebhookCertificate", non_retryable=True) from None
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    if error is None:
        activity.logger.info("webhook delivered", extra={"run_id": input.run_id})
        return

    if info.attempt < input.maximum_attempts:
        await _mirror(input, "waiting", error=f"attempt {info.attempt}: {error}")
    # Every failed POST is retried, as on the job lane — a 4xx included.
    raise ApplicationError(error, type="WebhookFailed")
