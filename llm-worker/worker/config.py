from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )
    nats_url: str = "nats://localhost:4222"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "scrapeflow"
    minio_secret_key: str = "scrapeflow_secret"
    minio_bucket: str = "scrapeflow-results"
    minio_secure: bool = False
    llm_key_encryption_key: str = ""
    llm_request_timeout_seconds: int = 60
    llm_max_content_chars: int = 50_000
    llm_max_workers: int = 3

    @field_validator("llm_key_encryption_key")
    def validate_fernet_key(cls, v):
        if not v:
            raise ValueError("LLM_KEY_ENCRYPTION_KEY must be set to a valid Fernet key")
        try:
            Fernet(v)
        except (ValueError, InvalidToken):
            raise ValueError(
                "LLM_KEY_ENCRYPTION_KEY is not a valid Fernet key"
            ) from None
        return v


settings = Settings()
