"""
Unit tests for worker/models.py — Pydantic message schemas.

Verifies the wire-format contract: what bytes get published to NATS.
The API result consumer parses these bytes, so correctness here matters.

All tests are synchronous — Pydantic models have no async behavior.
"""

import json

from worker.models import JobMessage, ResultMessage


# ---------------------------------------------------------------------------
# ResultMessage.to_nats_bytes() — exclude_none is the key contract
# ---------------------------------------------------------------------------


def test_to_nats_bytes_excludes_none_fields():
    """
    Fields that are None must be absent from the serialized bytes.
    Mirrors the Go worker's omitempty JSON tags — absent means 'not applicable'.
    """
    msg = ResultMessage(job_id="job-1", run_id="run-1", status="running")
    data = json.loads(msg.to_nats_bytes())
    assert "minio_path" not in data
    assert "nats_stream_seq" not in data
    assert "error" not in data


def test_running_message_includes_nats_stream_seq():
    """
    'running' result carries nats_stream_seq — used by the MaxDeliver advisory
    handler (Step 22) to identify stalled runs by NATS sequence number alone.
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
        minio_path="scrapeflow-results/history/job-1/1234567890.json",
    )
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "completed"
    assert data["minio_path"] == "scrapeflow-results/history/job-1/1234567890.json"
    assert "error" not in data


def test_failed_message_includes_error():
    """'failed' result includes error string; minio_path must be absent."""
    msg = ResultMessage(
        job_id="job-1", run_id="run-1", status="failed", error="LLM rate limit"
    )
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "failed"
    assert data["error"] == "LLM rate limit"
    assert "minio_path" not in data


# ---------------------------------------------------------------------------
# JobMessage parsing
# ---------------------------------------------------------------------------


def test_job_message_parses_all_required_fields():
    """JobMessage correctly parses all required LLM job fields."""
    raw = json.dumps(
        {
            "job_id": "job-1",
            "run_id": "run-1",
            "raw_minio_path": "scrapeflow-results/history/job-1/123.html",
            "provider": "anthropic",
            "encrypted_api_key": "gAAAAAB_ciphertext",
            "model": "claude-3-5-sonnet-20241022",
            "output_schema": {"type": "object"},
        }
    ).encode()
    job = JobMessage.model_validate_json(raw)
    assert job.job_id == "job-1"
    assert job.provider == "anthropic"
    assert job.base_url is None  # optional field defaults to None


def test_job_message_base_url_is_optional():
    """base_url is only required for openai_compatible; must default to None."""
    raw = json.dumps(
        {
            "job_id": "job-1",
            "run_id": "run-1",
            "raw_minio_path": "scrapeflow-results/history/job-1/123.html",
            "provider": "openai_compatible",
            "encrypted_api_key": "gAAAAAB_ciphertext",
            "model": "gpt-4o",
            "output_schema": {"type": "object"},
        }
    ).encode()
    job = JobMessage.model_validate_json(raw)
    assert job.base_url is None
