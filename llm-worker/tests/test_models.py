"""
Unit tests for worker/models.py — Pydantic message schemas.

Verifies the wire-format contract: what bytes get published to NATS, and what the
worker will accept off it. The API result consumer parses these bytes.

All tests are synchronous — Pydantic models have no async behavior.
"""

import json

import pytest
from pydantic import ValidationError

from worker.models import JobMessage, ResultMessage


def _job_payload(**overrides) -> bytes:
    payload = {
        "schema_version": 3,
        "artifact_id": "artifact-1",
        "run_id": "run-1",
        "raw_minio_path": "scrapeflow-results/history/artifact-1/scrape.html",
        "provider": "anthropic",
        "encrypted_api_key": "gAAAAAB_ciphertext",
        "model": "claude-3-5-sonnet-20241022",
        "output_schema": {"type": "object"},
    }
    payload.update(overrides)
    return json.dumps({k: v for k, v in payload.items() if v is not ...}).encode()


# ---------------------------------------------------------------------------
# ResultMessage.to_nats_bytes() — exclude_none is the key contract
# ---------------------------------------------------------------------------


def test_to_nats_bytes_excludes_none_fields():
    """
    Fields that are None must be absent from the serialized bytes.
    Mirrors the Go worker's omitempty JSON tags — absent means 'not applicable'.
    """
    msg = ResultMessage(run_id="run-1", status="running")
    data = json.loads(msg.to_nats_bytes())
    assert "minio_path" not in data
    assert "nats_stream_seq" not in data
    assert "error" not in data


def test_result_message_does_not_carry_job_id():
    """job_id has left the wire (ADR-011 §2) — the API reads it from the run row."""
    msg = ResultMessage(run_id="run-1", status="completed", minio_path="p")
    assert "job_id" not in json.loads(msg.to_nats_bytes())


def test_running_message_includes_nats_stream_seq():
    """
    'running' result carries nats_stream_seq — used by the MaxDeliver advisory
    handler (Step 22) to identify stalled runs by NATS sequence number alone.
    """
    msg = ResultMessage(run_id="run-1", status="running", nats_stream_seq=99)
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "running"
    assert data["nats_stream_seq"] == 99
    assert "minio_path" not in data
    assert "error" not in data


def test_completed_message_includes_minio_path():
    """'completed' result includes minio_path; error must be absent."""
    msg = ResultMessage(
        run_id="run-1",
        status="completed",
        minio_path="scrapeflow-results/history/artifact-1/llm.json",
    )
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "completed"
    assert data["minio_path"] == "scrapeflow-results/history/artifact-1/llm.json"
    assert "error" not in data


def test_failed_message_includes_error():
    """'failed' result includes error string; minio_path must be absent."""
    msg = ResultMessage(run_id="run-1", status="failed", error="LLM rate limit")
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "failed"
    assert data["error"] == "LLM rate limit"
    assert "minio_path" not in data


# ---------------------------------------------------------------------------
# JobMessage parsing
# ---------------------------------------------------------------------------


def test_job_message_parses_all_required_fields():
    """JobMessage correctly parses all required LLM job fields."""
    job = JobMessage.model_validate_json(_job_payload())
    assert job.artifact_id == "artifact-1"
    assert job.run_id == "run-1"
    assert job.provider == "anthropic"
    assert job.base_url is None  # optional field defaults to None


def test_job_message_base_url_is_optional():
    """base_url is only required for openai_compatible; must default to None."""
    job = JobMessage.model_validate_json(
        _job_payload(provider="openai_compatible", model="gpt-4o")
    )
    assert job.base_url is None


# ---------------------------------------------------------------------------
# The BUG-005 class: a missing identifier must fail loudly, never default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", ...])
def test_absent_null_or_empty_artifact_id_is_rejected(bad):
    """
    ADR-011 §5. Path C of BUG-005 is what the old contract cost here: the API sent
    job_id=None for every batch run, the required-string field rejected it as
    malformed, and the worker acked and dropped — leaving the run at 'processing'
    forever, with nothing in the system that recovers a run in that state.
    """
    with pytest.raises(ValidationError):
        JobMessage.model_validate_json(_job_payload(artifact_id=bad))


@pytest.mark.parametrize("bad", [None, ...])
def test_run_id_is_required_on_this_lane(bad):
    """
    Unlike the scrape message, run_id is not optional here: the LLM stage is only
    reachable on the job and batch lanes, and both create a job_runs row.
    """
    with pytest.raises(ValidationError):
        JobMessage.model_validate_json(_job_payload(run_id=bad))


@pytest.mark.parametrize("version", [1, 2, 4])
def test_wrong_schema_version_is_rejected(version):
    """
    A stale worker on this lane is the expensive one: it would re-run a billable call
    against the user's own API key. Pinning the version makes the mismatch loud.
    """
    with pytest.raises(ValidationError):
        JobMessage.model_validate_json(_job_payload(schema_version=version))


def test_job_message_has_no_job_id_field():
    """job_id is off the wire — an extra key is ignored, not bound to anything."""
    job = JobMessage.model_validate_json(_job_payload(job_id="job-legacy"))
    assert not hasattr(job, "job_id")
