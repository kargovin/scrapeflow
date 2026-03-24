from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file: api/app/settings.py -> ../../.env (repo root)
_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: str = "development"
    debug: bool = False

    # Postgres
    database_url: str = "postgresql+asyncpg://scrapeflow:scrapeflow@localhost:5432/scrapeflow"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # NATS
    nats_url: str = "nats://localhost:4222"

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "scrapeflow"
    minio_secret_key: str = "scrapeflow"
    minio_bucket: str = "scrapeflow-results"
    minio_secure: bool = False

    # Clerk
    clerk_publishable_key: str = ""
    clerk_secret_key: str = ""
    clerk_jwt_public_key: str = ""  # PEM public key for JWT verification

    # Rate limiting (requests per minute per user)
    rate_limit_rpm: int = 60


settings = Settings()
