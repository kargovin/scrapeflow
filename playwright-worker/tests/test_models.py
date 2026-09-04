"""
Unit tests for worker/models.py — Pydantic message schemas.

These tests verify the wire-format contract: what bytes actually get published to
NATS, and what the worker will and will not accept off it. The API's result consumer
parses these bytes, so correctness here is critical.

All tests are synchronous — Pydantic models have no async behavior.
"""

import json

import pytest
from pydantic import ValidationError

from worker.models import JobMessage, PlaywrightOptions, ResultMessage


def _job_payload(**overrides) -> str:
    payload = {
        "schema_version": 3,
        "artifact_id": "artifact-x",
        "run_id": "run-y",
        "url": "https://example.com",
        "output_format": "html",
        "engine": "playwright",
    }
    payload.update(overrides)
    return json.dumps({k: v for k, v in payload.items() if v is not ...})


# ---------------------------------------------------------------------------
# ResultMessage.to_nats_bytes() — exclude_none is the key contract
# ---------------------------------------------------------------------------


def test_to_nats_bytes_excludes_none_fields():
    """
    Fields that are None must be absent from the serialized bytes.
    This mirrors the Go worker's `omitempty` JSON tags — absent fields
    mean 'not applicable', not 'explicitly null'.
    """
    msg = ResultMessage(run_id="run-1", status="running")
    data = json.loads(msg.to_nats_bytes())
    assert "minio_path" not in data
    assert "nats_stream_seq" not in data
    assert "error" not in data


def test_result_message_does_not_carry_job_id():
    """
    job_id has left the wire (ADR-011 §2). The API reads it from the job_runs row it
    already loads, so a stale echo cannot disagree with the row.
    """
    msg = ResultMessage(run_id="run-1", status="completed", minio_path="p")
    assert "job_id" not in json.loads(msg.to_nats_bytes())


def test_result_message_omits_run_id_on_the_crawl_lane():
    """A crawl page has no job_runs row, so there is no run_id to echo back."""
    msg = ResultMessage(status="completed", minio_path="p")
    assert "run_id" not in json.loads(msg.to_nats_bytes())


def test_running_message_includes_nats_stream_seq():
    """
    'running' result includes nats_stream_seq (used by the MaxDeliver advisory
    handler in Step 22 to identify stalled runs by NATS sequence number).
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
        minio_path="scrapeflow-results/history/artifact-x/scrape.html",
    )
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "completed"
    assert data["minio_path"] == "scrapeflow-results/history/artifact-x/scrape.html"
    assert "error" not in data


def test_failed_message_includes_error():
    """'failed' result includes error string; minio_path must be absent."""
    msg = ResultMessage(run_id="run-1", status="failed", error="connection timeout")
    data = json.loads(msg.to_nats_bytes())
    assert data["status"] == "failed"
    assert data["error"] == "connection timeout"
    assert "minio_path" not in data


def test_result_message_warnings_and_screenshots_excluded_when_none():
    """warnings and screenshot_paths must be absent from bytes when not set."""
    msg = ResultMessage(run_id="r", status="completed", minio_path="p")
    data = json.loads(msg.to_nats_bytes())
    assert "warnings" not in data
    assert "screenshot_paths" not in data


def test_result_message_includes_warnings_when_set():
    """warnings must appear in bytes when actions produced failures."""
    msg = ResultMessage(
        run_id="r",
        status="completed",
        minio_path="p",
        warnings=["action click failed: timeout"],
    )
    data = json.loads(msg.to_nats_bytes())
    assert data["warnings"] == ["action click failed: timeout"]


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


# ---------------------------------------------------------------------------
# Schema version 3 parsing (ADR-011)
# ---------------------------------------------------------------------------


def test_schema_version_3_message_parses_all_sub_objects():
    """A full v3 fat message must parse all sub-objects correctly."""
    msg = JobMessage.model_validate_json(
        _job_payload(
            credentials={
                "encrypted_proxy_url": "gAAAAAB_proxy_ciphertext",
                "encrypted_cookies": "gAAAAAB_cookies_ciphertext",
            },
            options={
                "respect_robots": True,
                "actions": [{"type": "wait", "milliseconds": 500}],
            },
            crawl_context={
                "crawl_id": "crawl-1",
                "crawl_page_id": "page-1",
                "depth": 2,
            },
        )
    )

    assert msg.schema_version == 3
    assert msg.artifact_id == "artifact-x"
    assert msg.credentials.encrypted_proxy_url == "gAAAAAB_proxy_ciphertext"
    assert msg.credentials.encrypted_cookies == "gAAAAAB_cookies_ciphertext"
    assert msg.options.respect_robots is True
    assert msg.options.actions[0]["type"] == "wait"
    assert msg.crawl_context.crawl_id == "crawl-1"
    assert msg.crawl_context.depth == 2


def test_minimal_v3_message_parses_with_defaults():
    """Optional sub-objects default to None when the keys are absent."""
    msg = JobMessage.model_validate_json(
        json.dumps(
            {
                "schema_version": 3,
                "artifact_id": "artifact-1",
                "run_id": "run-1",
                "url": "https://example.com",
                "output_format": "html",
            }
        )
    )
    assert msg.credentials is None
    assert msg.options is None
    assert msg.crawl_context is None


# ---------------------------------------------------------------------------
# The BUG-005 class: a missing identifier must fail loudly, never default
# ---------------------------------------------------------------------------


def test_run_id_is_optional_for_the_crawl_lane():
    """
    A crawl page creates no job_runs row, so run_id is absent — declared optional
    rather than filled with a fabricated uuid4() (ADR-011 §2).
    """
    msg = JobMessage.model_validate_json(
        json.dumps(
            {
                "schema_version": 3,
                "artifact_id": "page-1",
                "url": "https://example.com",
                "output_format": "html",
                "crawl_context": {
                    "crawl_id": "crawl-1",
                    "crawl_page_id": "page-1",
                    "depth": 0,
                },
            }
        )
    )
    assert msg.run_id is None
    assert msg.artifact_id == "page-1"


@pytest.mark.parametrize("bad", [None, "", ...])
def test_absent_null_or_empty_artifact_id_is_rejected(bad):
    """
    ADR-011 §5: a missing identifier fails loudly or is explicitly optional — never
    defaulted. artifact_id takes the first half. The empty string is included because
    it is the value Go's encoding/json silently produces from a JSON null, which is
    the mechanism that turned a loud failure here into silent cross-tenant corruption
    on the Go worker (BUG-005 Path B).
    """
    with pytest.raises(ValidationError):
        JobMessage.model_validate_json(_job_payload(artifact_id=bad))


@pytest.mark.parametrize("version", [1, 2, 4])
def test_wrong_schema_version_is_rejected(version):
    """
    v1/v2 keyed artifacts on job_id and carry no artifact_id at all. Pinning the
    version means a mis-ordered deploy reports the version mismatch rather than a
    generic missing field.
    """
    with pytest.raises(ValidationError):
        JobMessage.model_validate_json(_job_payload(schema_version=version))


def test_job_message_has_no_job_id_field():
    """job_id is off the wire — an extra key is ignored, not bound to anything."""
    msg = JobMessage.model_validate_json(_job_payload(job_id="job-legacy"))
    assert not hasattr(msg, "job_id")
