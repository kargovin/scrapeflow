"""
Unit tests for worker/models.py — Pydantic message schemas.

These tests verify the wire-format contract: what bytes actually get
published to NATS. The Go result consumer and the API's result consumer
both parse these bytes, so correctness here is critical.

All tests are synchronous — Pydantic models have no async behavior.
"""

import json

from worker.models import PlaywrightOptions, ResultMessage


# ---------------------------------------------------------------------------
# ResultMessage.to_nats_bytes() — exclude_none is the key contract
# ---------------------------------------------------------------------------


def test_to_nats_bytes_excludes_none_fields():
    """
    Fields that are None must be absent from the serialized bytes.
    This mirrors the Go worker's `omitempty` JSON tags — absent fields
    mean 'not applicable', not 'explicitly null'.
    """
    msg = ResultMessage(job_id="job-1", run_id="run-1", status="running")
    data = json.loads(msg.to_nats_bytes())
    assert "minio_path" not in data
    assert "nats_stream_seq" not in data
    assert "error" not in data


def test_running_message_includes_nats_stream_seq():
    """
    'running' result includes nats_stream_seq (used by the MaxDeliver advisory
    handler in Step 22 to identify stalled runs by NATS sequence number).
    """
    msg = ResultMessage(
        job_id="job-1", run_id="run-1", status="running", nats_stream_seq=99
    )
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "running"
    assert data["nats_stream_seq"] == 99
    assert "minio_path" not in data
    assert "error" not in data


def test_completed_message_includes_minio_path():
    """'completed' result includes minio_path; error must be absent."""
    msg = ResultMessage(
        job_id="job-1",
        run_id="run-1",
        status="completed",
        minio_path="scrapeflow-results/history/job-1/1234567890.html",
    )
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "completed"
    assert data["minio_path"] == "scrapeflow-results/history/job-1/1234567890.html"
    assert "error" not in data


def test_failed_message_includes_error():
    """'failed' result includes error string; minio_path must be absent."""
    msg = ResultMessage(
        job_id="job-1", run_id="run-1", status="failed", error="connection timeout"
    )
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "failed"
    assert data["error"] == "connection timeout"
    assert "minio_path" not in data


# ---------------------------------------------------------------------------
# PlaywrightOptions defaults
# ---------------------------------------------------------------------------


def test_playwright_options_defaults():
    """
    Default PlaywrightOptions match what the Go/API side sends when no
    playwright_options block is included in the job message.
    """
    opts = PlaywrightOptions()
    assert opts.wait_strategy == "load"
    assert opts.timeout_seconds == 60
    assert opts.block_images is False
