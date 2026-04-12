"""
MinIO dual-write — mirrors the Go worker's storage.Upload().
Writes latest/{job_id}.{ext} (overwritten each run) and
history/{job_id}/{unix_ts}.{ext} (immutable per-run record).
Returns the history path as the canonical minio_path stored on job_runs.
"""

import io
import time

from miniopy_async import Minio

from .config import settings

_CONTENT_TYPES: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
}


async def upload(minio: Minio, job_id: str, ext: str, data: bytes) -> str:
    """Upload data to MinIO; return the fully-qualified history path."""
    bucket = settings.minio_bucket
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

    latest_key = f"latest/{job_id}.{ext}"
    history_key = f"history/{job_id}/{int(time.time())}.{ext}"

    # Write both paths independently (no copy API call needed — data is already in memory)
    for key in (latest_key, history_key):
        await minio.put_object(
            bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
        )

    # Return bucket-qualified path matching the Go worker's convention:
    # "{bucket}/history/{job_id}/{ts}.{ext}"
    return f"{bucket}/{history_key}"
