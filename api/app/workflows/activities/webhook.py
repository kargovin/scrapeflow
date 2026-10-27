"""The Webhook block's activity — workflow-worker queue (it writes the block's status).

The wire contract is the job lane's (``activities/webhook_deliveries.py``), byte for byte: HMAC-SHA256 over
the raw body, ``X-ScrapeFlow-Signature: sha256=<hex>`` always sent (``b""`` with no secret),
success = status < 300, 10 s per POST. Changing any of it breaks receivers silently.

One call is one attempt; the retry policy is the delivery loop (ADR-009 §15). The workflow is
blocked inside one ``execute_activity`` across every retry, so the attempt itself mirrors the
block: ``running`` as it starts (refused on a cancelled run — 15d's boundary re-check), ``waiting``
when it fails and another attempt will follow (15a — a waiting block holds no slot).
"""

import asyncio
import hashlib
import hmac
import json

import httpx
from cryptography.fernet import Fernet, InvalidToken
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core.security import validate_no_ssrf_core
from app.settings import settings
from app.workflows.activities.db import mirror_block_status
from app.workflows.pipeline import (
    RUN_CANCELLED_ERROR,
    WEBHOOK_ACTIVITY,
    WEBHOOK_EVENT,
    MirrorBlockInput,
    WebhookInput,
)

POST_TIMEOUT_SECONDS = 10.0


def build_payload(input: WebhookInput) -> dict:
    # The job lane's fields (core/webhooks.py) less job_id, plus the pipeline identifiers
    # (PRD-016 R6's two payload exceptions). No change detection on this lane: constants.
    return {
        "event": WEBHOOK_EVENT,
        "run_id": input.run_id,
        "pipeline_id": input.pipeline_id,
        "pipeline_run_id": input.run_id,
        "result_path": input.result_path,
        "diff_detected": False,
        "diff_summary": None,
        "timestamp": input.timestamp,
    }


def sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _secret(encrypted: str | None) -> bytes:
    if encrypted is None:
        return b""
    try:
        return Fernet(settings.credentials_encryption_key).decrypt(encrypted.encode())
    except InvalidToken:
        raise ApplicationError(
            "the webhook secret cannot be decrypted", type="WebhookSecret", non_retryable=True
        ) from None


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

    # Every attempt (DNS rebinding); a refusal is final — retrying hands an attacker hours.
    try:
        await asyncio.get_running_loop().run_in_executor(None, validate_no_ssrf_core, input.url)
    except ValueError as exc:
        raise ApplicationError(
            f"ssrf_blocked: {exc}", type="WebhookBlocked", non_retryable=True
        ) from None

    body = json.dumps(build_payload(input)).encode()
    headers = {
        "Content-Type": "application/json",
        "X-ScrapeFlow-Signature": sign(_secret(input.encrypted_secret), body),
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                input.url, content=body, headers=headers, timeout=POST_TIMEOUT_SECONDS
            )
        error = None if response.status_code < 300 else f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    if error is None:
        activity.logger.info("webhook delivered", extra={"run_id": input.run_id})
        return

    if info.attempt < input.maximum_attempts:
        await _mirror(input, "waiting", error=f"attempt {info.attempt}: {error}")
    # Every failed POST is retried, as on the job lane — a 4xx included.
    raise ApplicationError(error, type="WebhookFailed")
