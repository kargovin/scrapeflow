"""Tests for the MaxDeliver advisory subscriber (Step 22).

Tests call _handle_advisory directly with a mock NATS message and a real DB session,
matching the pattern established in test_scheduler.py and test_webhook_delivery.py.
"""

import json
from unittest.mock import MagicMock

import pytest_asyncio

from app.core.advisory import _handle_advisory
from app.core.db import AsyncSessionLocal
from app.models.job import Job
from app.models.job_runs import JobRun

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def running_run(db_user):
    """A JobRun with status='running' and nats_stream_seq=42."""
    job = Job(user_id=db_user.id, url="https://example.com")
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running", nats_stream_seq=42)
        db.add(run)
        await db.commit()
        await db.refresh(run)
    yield run
    # Cleanup handled by db_user cascade


def _make_msg(payload: bytes) -> MagicMock:
    """Build a minimal mock NATS Msg with the given raw bytes."""
    msg = MagicMock()
    msg.data = payload
    return msg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_advisory_marks_run_failed(running_run):
    """Happy path: advisory with matching stream_seq marks the run as failed."""
    msg = _make_msg(json.dumps({"stream_seq": 42}).encode())

    await _handle_advisory(msg, AsyncSessionLocal)

    async with AsyncSessionLocal() as db:
        updated = await db.get(JobRun, running_run.id)

    assert updated.status == "failed"
    assert updated.error == "Max NATS redeliveries exceeded"
    assert updated.completed_at is not None


async def test_advisory_no_matching_run_does_not_raise():
    """Advisory with an unknown stream_seq logs a warning and returns cleanly."""
    msg = _make_msg(json.dumps({"stream_seq": 99999}).encode())

    # Should not raise — unknown seq is a no-op
    await _handle_advisory(msg, AsyncSessionLocal)


async def test_advisory_malformed_message_does_not_raise():
    """Malformed (non-JSON) advisory payload is discarded without raising."""
    msg = _make_msg(b"not valid json at all")

    # Should not raise — bad advisory is logged and discarded
    await _handle_advisory(msg, AsyncSessionLocal)
