"""
Unit tests for worker/storage.py — upload().

The LLM worker always writes JSON — unlike the playwright-worker, there is no
ext parameter. These tests verify the dual-write contract and Content-Type.

Key contract being verified:
  - Dual-write: latest/{job_id}.json  AND  history/{job_id}/{ts}.json
  - Return value is the history path prefixed with the bucket name
  - Content-Type is always application/json
"""

from worker.config import settings
from worker.storage import upload


async def test_upload_calls_put_object_twice(mock_minio):
    """Dual-write: put_object must be called exactly twice per upload."""
    await upload(mock_minio, "job-123", b'{"name": "Alice"}')
    assert mock_minio.put_object.call_count == 2


async def test_upload_latest_and_history_keys(mock_minio):
    """First call writes latest/{job_id}.json; second writes history/{job_id}/{ts}.json."""
    await upload(mock_minio, "job-123", b'{"name": "Alice"}')
    calls = mock_minio.put_object.call_args_list

    # Positional args: (bucket, key, stream, length); content_type is a kwarg
    latest_key: str = calls[0].args[1]
    history_key: str = calls[1].args[1]

    assert latest_key == "latest/job-123.json"
    assert history_key.startswith("history/job-123/")
    assert history_key.endswith(".json")


async def test_upload_returns_bucket_qualified_history_path(mock_minio):
    """Return value is '{bucket}/history/{job_id}/{ts}.json' — stored on job_runs.result_path."""
    result = await upload(mock_minio, "job-123", b'{"name": "Alice"}')
    expected_prefix = f"{settings.minio_bucket}/history/job-123/"
    assert result.startswith(expected_prefix)
    assert result.endswith(".json")


async def test_upload_content_type_is_always_json(mock_minio):
    """Both put_object calls must use Content-Type: application/json."""
    await upload(mock_minio, "job-123", b'{"name": "Alice"}')
    for call in mock_minio.put_object.call_args_list:
        assert call.kwargs["content_type"] == "application/json"
