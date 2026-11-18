"""The Webhook block's activity — workflow-worker queue (it writes the block's status).

The send is ``core/egress.send_signed_post``, shared with the job lane: the wire contract, the
address check and the connection to the checked address. Success = status < 300.

One call is one attempt; the retry policy is the delivery loop (ADR-009 §15). The workflow is
blocked inside one ``execute_activity`` across every retry, so the attempt itself mirrors the
block: ``running`` as it starts (refused on a cancelled run — 15d's boundary re-check), ``waiting``
when it fails and another attempt will follow (15a — a waiting block holds no slot).
"""

import json

import httpx
from cryptography.fernet import Fernet, InvalidToken
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core import egress
from app.settings import settings
from app.workflows.activities.db import mirror_block_status
from app.workflows.pipeline import (
    RUN_CANCELLED_ERROR,
    WEBHOOK_ACTIVITY,
    WEBHOOK_EVENT,
    MirrorBlockInput,
    WebhookInput,
)


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

    body = json.dumps(build_payload(input)).encode()
    secret = _secret(input.encrypted_secret)
    # A refusal is final — retrying hands an attacker hours.
    try:
        status_code = await egress.send_signed_post(input.url, body, secret)
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
