"""
MinIO upload — mirrors the Go worker's storage.Upload() and the LLM worker's.

Objects are keyed on the row that produced the execution and named by the producing
stage (ADR-011 §1, §3):

    history/{artifact_id}/scrape.{ext}      the scraped page, in the job's format
    screenshots/{artifact_id}/{index}.png   one per action-driven screenshot

The stage segment is load-bearing, not decoration. A flat history/{artifact_id}.{ext}
would silently reintroduce the collision this convention exists to remove: the LLM
worker hardcodes ext="json", so an output_format=json job's extraction would resolve
to the same key as its own scraped page and overwrite it.

`latest/` is gone (ADR-011 §4). It was write-only in the entire codebase — three
workers wrote it, one route deleted it, and nothing ever read it. Results are served
from job_runs.result_path, which holds the history/ path.
"""

import io

from miniopy_async import Minio

from .config import settings

_CONTENT_TYPES: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "png": "image/png",
}


async def upload(minio: Minio, artifact_id: str, ext: str, data: bytes) -> str:
    """Upload the scraped page; return the fully-qualified object path."""
    bucket = settings.minio_bucket
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

    key = f"history/{artifact_id}/scrape.{ext}"

    await minio.put_object(
        bucket,
        key,
        io.BytesIO(data),
        len(data),
        content_type=content_type,
    )

    # Bucket-qualified, matching the Go worker: "{bucket}/history/{artifact_id}/scrape.{ext}"
    return f"{bucket}/{key}"


async def upload_screenshot(
    minio: Minio, artifact_id: str, index: int, data: bytes
) -> str:
    """
    Upload a screenshot PNG; return the bucket-qualified path.

    The key carries no timestamp: artifact_id is unique per execution, so a NATS
    redelivery overwrites the previous attempt's screenshots in place instead of
    leaving a fresh orphaned set behind on every retry.
    """
    bucket = settings.minio_bucket
    key = f"screenshots/{artifact_id}/{index}.png"

    await minio.put_object(
        bucket,
        key,
        io.BytesIO(data),
        len(data),
        content_type="image/png",
    )

    return f"{bucket}/{key}"
