"""
MinIO dual-write — mirrors the Go worker's storage.Upload() and the playwright-worker.
Writes latest/{job_id}.json (overwritten each run) and
history/{job_id}/{unix_ts}.json (immutable per-run record).
Returns the history path as the canonical minio_path stored on job_runs.
"""

import io
import time

from miniopy_async import Minio

from .config import settings


async def upload(minio: Minio, job_id: str, data: bytes) -> str:
    """Upload JSON result to MinIO; return the fully-qualified history path."""
    bucket = settings.minio_bucket
    ext = "json"
    content_type = "application/json"

    latest_key = f"latest/{job_id}.{ext}"
    history_key = f"history/{job_id}/{int(time.time())}.{ext}"

    for key in (latest_key, history_key):
        await minio.put_object(
            bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
        )

    return f"{bucket}/{history_key}"
