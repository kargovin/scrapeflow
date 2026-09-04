"""
Unit tests for worker/storage.py — upload().

The LLM worker always writes JSON — unlike the playwright worker there is no ext
parameter, which is exactly why the stage segment matters here.

Key contract being verified (ADR-011 §3, §4):
  - A single write per object — the latest/ mirror is gone
  - The key is history/{artifact_id}/llm.json
  - Return value is the object path prefixed with the bucket name
  - Content-Type is always application/json
"""

from worker.config import settings
from worker.storage import upload


async def test_upload_writes_exactly_one_object(mock_minio):
    """No dual write. `latest/` was write-only in the whole codebase (ADR-011 §4)."""
    await upload(mock_minio, "artifact-123", b'{"name": "Alice"}')
    assert mock_minio.put_object.call_count == 1


async def test_upload_key_is_stage_named_under_the_artifact(mock_minio):
    """The key is history/{artifact_id}/llm.json — no timestamp, no latest/."""
    await upload(mock_minio, "artifact-123", b'{"name": "Alice"}')
    calls = mock_minio.put_object.call_args_list

    # Positional args: (bucket, key, stream, length); content_type is a kwarg
    assert calls[0].args[1] == "history/artifact-123/llm.json"
    assert not any(c.args[1].startswith("latest/") for c in calls)


async def test_llm_output_does_not_collide_with_the_scrape_it_came_from(mock_minio):
    """
    The regression ADR-011 §3 exists to prevent. This worker hardcodes a .json
    extension, so under a flat history/{artifact_id}.{ext} convention an
    output_format=json job's extraction would overwrite its own scraped page. The
    stage segment is what separates them; the timestamp used to, and only because
    LLM calls are slow.
    """
    await upload(mock_minio, "artifact-123", b"{}")
    llm_key = mock_minio.put_object.call_args_list[0].args[1]
    assert llm_key == "history/artifact-123/llm.json"
    assert llm_key != "history/artifact-123/scrape.json"


async def test_upload_returns_bucket_qualified_path(mock_minio):
    """Return value is '{bucket}/history/{artifact_id}/llm.json' — job_runs.result_path."""
    result = await upload(mock_minio, "artifact-123", b'{"name": "Alice"}')
    assert result == f"{settings.minio_bucket}/history/artifact-123/llm.json"


async def test_upload_content_type_is_always_json(mock_minio):
    """The put_object call must use Content-Type: application/json."""
    await upload(mock_minio, "artifact-123", b'{"name": "Alice"}')
    assert (
        mock_minio.put_object.call_args_list[0].kwargs["content_type"]
        == "application/json"
    )
