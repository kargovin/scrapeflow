"""
MinIO upload — mirrors the Go worker's storage.Upload() and the playwright worker's.

    history/{artifact_id}/llm.json

Objects are keyed on the row that produced the execution and named by the producing
stage (ADR-011 §1, §3). The stage segment is what keeps this worker's output from
overwriting the scraped page it was derived from: ext is hardcoded "json" here, so a
flat history/{artifact_id}.{ext} would collide with the scrape of any job whose
output_format is already json. Today only the timestamp prevents that, and only
because LLM calls are slow.

`latest/` is gone (ADR-011 §4) — it was write-only across the whole codebase.
"""

import io

from miniopy_async import Minio

from .config import settings


async def fetch_content(minio: Minio, raw_minio_path: str) -> str:
    """Read raw scrape content from MinIO; return as a UTF-8 string.

    raw_minio_path is bucket-qualified: "{bucket}/history/{artifact_id}/scrape.{ext}"
    """
    bucket = settings.minio_bucket
    object_key = raw_minio_path[len(bucket) + 1 :]
    response = await minio.get_object(bucket, object_key)
    try:
        data = await response.read()
    finally:
        response.close()
        await response.release()
    return data.decode("utf-8")


async def upload(minio: Minio, artifact_id: str, data: bytes) -> str:
    """Upload the structured JSON result; return the fully-qualified object path."""
    bucket = settings.minio_bucket
    key = f"history/{artifact_id}/llm.json"

    await minio.put_object(
        bucket,
        key,
        io.BytesIO(data),
        len(data),
        content_type="application/json",
    )

    return f"{bucket}/{key}"
