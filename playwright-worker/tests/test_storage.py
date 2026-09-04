"""
Unit tests for worker/storage.py — upload() and upload_screenshot().

Both are async but their only external dependency is the MinIO client, so the tests
pass a mock_minio fixture and inspect the put_object call arguments.

Key contract being verified (ADR-011 §3, §4):
  - A single write per object — the latest/ mirror is gone
  - Keys are history/{artifact_id}/scrape.{ext} and screenshots/{artifact_id}/{i}.png
  - Return value is the object path prefixed with the bucket name
  - Content-Type header matches the file extension
"""

import pytest

from worker.config import settings
from worker.storage import upload, upload_screenshot


# ---------------------------------------------------------------------------
# Call count and key structure
# ---------------------------------------------------------------------------


async def test_upload_writes_exactly_one_object(mock_minio):
    """No dual write. `latest/` was write-only in the whole codebase (ADR-011 §4)."""
    await upload(mock_minio, "artifact-123", "html", b"<html/>")
    assert mock_minio.put_object.call_count == 1


async def test_upload_key_is_stage_named_under_the_artifact(mock_minio):
    """The key is history/{artifact_id}/scrape.{ext} — no timestamp, no latest/."""
    await upload(mock_minio, "artifact-123", "html", b"<html/>")
    calls = mock_minio.put_object.call_args_list

    # Positional args: (bucket, key, stream, length); content_type is a kwarg
    key: str = calls[0].args[1]
    assert key == "history/artifact-123/scrape.html"
    assert not any(c.args[1].startswith("latest/") for c in calls)


async def test_upload_stage_segment_separates_scrape_from_llm_output(mock_minio):
    """
    The regression ADR-011 §3 exists to prevent: an output_format=json job's scrape and
    its LLM extraction must not resolve to the same key. The LLM worker hardcodes
    ext="json", so a flat history/{artifact_id}.{ext} would collide and the extraction
    would overwrite the page it was derived from.
    """
    await upload(mock_minio, "artifact-123", "json", b"{}")
    scrape_key = mock_minio.put_object.call_args_list[0].args[1]
    assert scrape_key == "history/artifact-123/scrape.json"
    assert scrape_key != "history/artifact-123/llm.json"


async def test_upload_returns_bucket_qualified_path(mock_minio):
    """Return value is '{bucket}/history/{artifact_id}/scrape.{ext}' — job_runs.result_path."""
    result = await upload(mock_minio, "artifact-123", "html", b"<html/>")
    assert result == f"{settings.minio_bucket}/history/artifact-123/scrape.html"


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------


async def test_upload_screenshot_key_has_no_timestamp(mock_minio):
    """
    screenshots/{artifact_id}/{index}.png. Dropping the timestamp makes a redelivery
    overwrite the previous attempt's screenshots instead of orphaning a fresh set.
    """
    result = await upload_screenshot(mock_minio, "artifact-123", 2, b"\x89PNG")
    key = mock_minio.put_object.call_args_list[0].args[1]
    assert key == "screenshots/artifact-123/2.png"
    assert result == f"{settings.minio_bucket}/screenshots/artifact-123/2.png"


async def test_upload_screenshot_is_idempotent_on_redelivery(mock_minio):
    """Two attempts at the same index write the same key, not two objects."""
    await upload_screenshot(mock_minio, "artifact-123", 0, b"\x89PNG")
    await upload_screenshot(mock_minio, "artifact-123", 0, b"\x89PNG")
    keys = {c.args[1] for c in mock_minio.put_object.call_args_list}
    assert keys == {"screenshots/artifact-123/0.png"}


# ---------------------------------------------------------------------------
# Content-Type header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ext, expected_ct",
    [
        ("html", "text/html; charset=utf-8"),
        ("md", "text/markdown; charset=utf-8"),
        ("json", "application/json"),
    ],
)
async def test_upload_content_type_per_extension(mock_minio, ext, expected_ct):
    """Each known extension maps to the correct Content-Type header."""
    await upload(mock_minio, "artifact-123", ext, b"data")
    assert mock_minio.put_object.call_args_list[0].kwargs["content_type"] == expected_ct


async def test_upload_unknown_extension_falls_back_to_octet_stream(mock_minio):
    """An unrecognised extension defaults to application/octet-stream (safe fallback)."""
    await upload(mock_minio, "artifact-123", "bin", b"data")
    assert (
        mock_minio.put_object.call_args_list[0].kwargs["content_type"]
        == "application/octet-stream"
    )
