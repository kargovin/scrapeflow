from miniopy_async import Minio

from app.settings import settings

# Module-level client — created once at startup, shared across all requests
_client: Minio | None = None


async def create_client() -> Minio:
    global _client
    _client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    # Auto-create the bucket if it doesn't exist
    if not await _client.bucket_exists(settings.minio_bucket):
        await _client.make_bucket(settings.minio_bucket)
    return _client


async def close_client() -> None:
    global _client
    # miniopy-async uses aiohttp sessions internally — close to release connections
    if _client:
        await _client.close_session()
        _client = None


def get_minio() -> Minio:
    assert _client is not None, "MinIO client not initialized — call create_client() at startup"
    return _client
