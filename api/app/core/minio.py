from fastapi import Request
from miniopy_async import Minio

from app.settings import settings


async def create_client() -> Minio:
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    # Auto-create the bucket if it doesn't exist
    if not await client.bucket_exists(settings.minio_bucket):
        await client.make_bucket(settings.minio_bucket)
    return client


async def close_client(client: Minio) -> None:
    await client.close_session()


def get_minio(request: Request) -> Minio:
    return request.app.state.minio
