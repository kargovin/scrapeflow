"""Tests for the webhook delivery background task (Step 21).

Tests call _attempt_delivery directly with real DB sessions and a mocked
httpx.AsyncClient — same pattern as result_consumer tests in test_jobs.py.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest_asyncio
from cryptography.fernet import Fernet

from app.core.db import AsyncSessionLocal
from app.core.webhook_loop import BACKOFF_SECONDS, _attempt_delivery
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings

# Use the same Fernet key that settings uses (validated at startup).
_fernet = Fernet(settings.llm_key_encryption_key.encode())

# A known plaintext webhook secret for HMAC tests.
_KNOWN_SECRET = b"test-webhook-secret"
_ENCRYPTED_SECRET = _fernet.encrypt(_KNOWN_SECRET).decode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def job_with_webhook(db_user):
    """A Job with a webhook_url and Fernet-encrypted webhook_secret."""
    job = Job(
        user_id=db_user.id,
        url="https://example.com",
        webhook_url="https://hooks.example.com/receive",
        webhook_secret=_ENCRYPTED_SECRET,
    )
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.commit()
        await db.refresh(job)
    yield job
    # Cleanup via db_user cascade


@pytest_asyncio.fixture
async def pending_delivery(job_with_webhook):
    """A pending WebhookDelivery with attempts=0 linked to job_with_webhook."""
    sample_payload = {
        "event": "job.completed",
        "job_id": str(job_with_webhook.id),
        "run_id": "00000000-0000-0000-0000-000000000001",
        "result_path": None,
        "diff_detected": None,
        "diff_summary": None,
        "timestamp": "2026-04-12T00:00:00+00:00",
    }
    async with AsyncSessionLocal() as db:
        # Need a JobRun to satisfy the FK
        run = JobRun(job_id=job_with_webhook.id, status="completed")
        db.add(run)
        await db.flush()

        delivery = WebhookDelivery(
            job_id=job_with_webhook.id,
            run_id=run.id,
            webhook_url=job_with_webhook.webhook_url,
            payload=sample_payload,
            status="pending",
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
        db.add(delivery)
        await db.commit()
        await db.refresh(delivery)
    yield delivery


@pytest_asyncio.fixture
async def near_exhausted_delivery(job_with_webhook):
    """A pending WebhookDelivery with attempts=4 (one below webhook_max_attempts=5)."""
    async with AsyncSessionLocal() as db:
        run = JobRun(job_id=job_with_webhook.id, status="completed")
        db.add(run)
        await db.flush()

        delivery = WebhookDelivery(
            job_id=job_with_webhook.id,
            run_id=run.id,
            webhook_url=job_with_webhook.webhook_url,
            payload={"event": "job.failed"},
            status="pending",
            attempts=4,
            next_attempt_at=datetime.now(UTC),
        )
        db.add(delivery)
        await db.commit()
        await db.refresh(delivery)
    yield delivery


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_http(status_code: int) -> AsyncMock:
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.post.return_value = MagicMock(status_code=status_code)
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_2xx_marks_delivered(pending_delivery):
    """A 2xx response marks the delivery as delivered and sets delivered_at."""
    mock_http = _mock_http(200)

    await _attempt_delivery(pending_delivery.id, AsyncSessionLocal, mock_http, _fernet)

    async with AsyncSessionLocal() as db:
        d = await db.get(WebhookDelivery, pending_delivery.id)

    assert d.status == "delivered"
    assert d.delivered_at is not None
    assert d.attempts == 1
    mock_http.post.assert_called_once()


async def test_500_increments_attempts_and_backsoff(pending_delivery):
    """A 5xx response increments attempts, stores the error, and advances next_attempt_at."""
    mock_http = _mock_http(500)

    await _attempt_delivery(pending_delivery.id, AsyncSessionLocal, mock_http, _fernet)

    async with AsyncSessionLocal() as db:
        d = await db.get(WebhookDelivery, pending_delivery.id)

    assert d.status == "pending"
    assert d.attempts == 1
    assert d.last_error == "HTTP 500"
    # After 1 attempt, backoff index = min(1, 4) = 1 → 30 seconds
    expected_delay = BACKOFF_SECONDS[1]
    assert d.next_attempt_at > datetime.now(UTC)
    assert d.next_attempt_at <= datetime.now(UTC) + timedelta(seconds=expected_delay + 2)


async def test_exhausted_after_max_attempts(near_exhausted_delivery):
    """After webhook_max_attempts failures the delivery is marked exhausted."""
    mock_http = _mock_http(503)

    await _attempt_delivery(near_exhausted_delivery.id, AsyncSessionLocal, mock_http, _fernet)

    async with AsyncSessionLocal() as db:
        d = await db.get(WebhookDelivery, near_exhausted_delivery.id)

    assert d.attempts == 5  # incremented from 4
    assert d.status == "exhausted"
    assert d.delivered_at is None
    assert d.last_error == "HTTP 503"


async def test_hmac_header_is_correct(pending_delivery):
    """The X-ScrapeFlow-Signature header must be a valid HMAC-SHA256 over the request body."""
    mock_http = _mock_http(200)

    await _attempt_delivery(pending_delivery.id, AsyncSessionLocal, mock_http, _fernet)

    call_kwargs = mock_http.post.call_args.kwargs
    content_bytes: bytes = call_kwargs["content"]
    header_sig: str = call_kwargs["headers"]["X-ScrapeFlow-Signature"]

    # Independently compute the expected HMAC using the known plaintext secret
    expected_sig = hmac.new(_KNOWN_SECRET, content_bytes, hashlib.sha256).hexdigest()
    assert header_sig == f"sha256={expected_sig}"


async def test_network_error_backsoff(pending_delivery):
    """A network exception increments attempts and stores the error without propagating."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post.side_effect = httpx.ConnectError("connection refused")

    # Must not raise
    await _attempt_delivery(pending_delivery.id, AsyncSessionLocal, mock_http, _fernet)

    async with AsyncSessionLocal() as db:
        d = await db.get(WebhookDelivery, pending_delivery.id)

    assert d.attempts == 1
    assert d.status == "pending"
    assert "connection refused" in d.last_error
