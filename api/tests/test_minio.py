from app.main import app
from app.settings import settings


async def test_minio_bucket_exists():
    exists = await app.state.minio.bucket_exists(settings.minio_bucket)
    assert exists is True
