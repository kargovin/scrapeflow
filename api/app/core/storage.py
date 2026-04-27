import structlog
from miniopy_async import Minio

logger = structlog.get_logger()


async def delete_minio_object(minio: Minio, path: str, label: str = "object") -> bool:
    """Delete a MinIO object by 'bucket/key' path. Returns True on success, False on failure."""
    bucket, _, key = path.partition("/")
    try:
        await minio.remove_object(bucket, key)
        return True
    except Exception:
        logger.warning("minio_delete_failed", label=label, path=path)
        return False


async def stat_minio_size(minio: Minio, path: str) -> int:
    """Return the byte size of an object. Returns 0 on any error (best-effort)."""
    try:
        bucket, _, key = path.partition("/")
        stat = await minio.stat_object(bucket, key)
        size = stat.size
        return int(size) if isinstance(size, int) else 0
    except Exception:
        return 0
