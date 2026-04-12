"""
Unit tests for worker/storage.py — upload().

upload() is async but its only external dependency is the MinIO client.
We pass a mock_minio fixture and inspect the put_object call arguments.

Key contract being verified:
  - Dual-write: latest/{job_id}.{ext}  AND  history/{job_id}/{ts}.{ext}
  - Return value is the history path prefixed with the bucket name
  - Content-Type header matches the file extension
"""

import pytest

from worker.config import settings
from worker.storage import upload


# ---------------------------------------------------------------------------
# Call count and key structure
# ---------------------------------------------------------------------------


async def test_upload_calls_put_object_twice(mock_minio):
    """Dual-write: put_object must be called exactly twice per upload."""
    await upload(mock_minio, "job-123", "html", b"<html/>")
    assert mock_minio.put_object.call_count == 2


async def test_upload_latest_and_history_keys(mock_minio):
    """First call writes latest/{job_id}.{ext}; second writes history/{job_id}/{ts}.{ext}."""
    await upload(mock_minio, "job-123", "html", b"<html/>")
    calls = mock_minio.put_object.call_args_list

    # Positional args: (bucket, key, stream, length); content_type is a kwarg
    latest_key: str = calls[0].args[1]
    history_key: str = calls[1].args[1]

    assert latest_key == "latest/job-123.html"
    assert history_key.startswith("history/job-123/")
    assert history_key.endswith(".html")


async def test_upload_returns_bucket_qualified_history_path(mock_minio):
    """Return value is '{bucket}/history/{job_id}/{ts}.{ext}' — stored on job_runs.result_path."""
    result = await upload(mock_minio, "job-123", "html", b"<html/>")
    expected_prefix = f"{settings.minio_bucket}/history/job-123/"
    assert result.startswith(expected_prefix)
    assert result.endswith(".html")


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
    """Each known extension maps to the correct Content-Type header on both put_object calls."""
    await upload(mock_minio, "job-123", ext, b"data")
    for call in mock_minio.put_object.call_args_list:
        assert call.kwargs["content_type"] == expected_ct


async def test_upload_unknown_extension_falls_back_to_octet_stream(mock_minio):
    """An unrecognised extension defaults to application/octet-stream (safe fallback)."""
    await upload(mock_minio, "job-123", "bin", b"data")
    for call in mock_minio.put_object.call_args_list:
        assert call.kwargs["content_type"] == "application/octet-stream"
