from app.core.minio import get_minio
from app.settings import settings


async def test_minio_bucket_exists():
    client = get_minio()
    exists = await client.bucket_exists(settings.minio_bucket)
    assert exists is True
