from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, field_validator
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
    redis_max_connections: int = 20

    # NATS
    nats_url: str = "nats://localhost:4222"
    nats_max_deliver: int = 3  # max delivery attempts before NATS stops redelivering (ADR-001)

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "scrapeflow"
    minio_secret_key: str = "scrapeflow"
    minio_bucket: str = "scrapeflow-results"
    minio_secure: bool = False

    # Clerk
    clerk_secret_key: str = ""  # used by backend SDK for JWT verification + API calls

    # Rate limiting — per-user sliding window (Redis sorted set + Lua)
    rate_limit_requests: int = 60  # max requests allowed per window
    rate_limit_window_seconds: int = 60  # window size in seconds

    # LLM
    llm_key_encryption_key: str = Field(
        default="", alias="LLM_KEY_ENCRYPTION_KEY"
    )  # symmetric key for encrypting LLM API keys in DB

    # Cron Sheduler
    schedule_min_interval_minutes: int = 5

    # Webhook delivery
    webhook_max_attempts: int = 5

    @field_validator("llm_key_encryption_key")
    def validate_fernet_key(cls, v):
        if not v:
            raise ValueError("LLM_KEY_ENCRYPTION_KEY must be set to a valid Fernet key")
        try:
            Fernet(v)
        except (ValueError, InvalidToken):
            raise ValueError("LLM_KEY_ENCRYPTION_KEY is not a valid Fernet key") from None
        return v

    # Allowed origins - CORS
    allowed_origins_raw: str = Field(
        default="*", alias="ALLOWED_ORIGINS"
    )  # env: ALLOWED_ORIGINS (comma-separated for production)

    @property
    def allowed_origins(self) -> list[str]:
        if self.allowed_origins_raw == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins_raw.split(",")]


settings = Settings()
