from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str
    nats_url: str = "nats://nats:4222"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "scrapeflow"
    minio_secret_key: str = "scrapeflow_secret"
    minio_bucket: str = "scrapeflow-results"
    minio_secure: bool = False
    dispatch_batch_size: int = 10
    dispatch_poll_interval: float = 2.0
    stale_threshold_minutes: int = 10


settings = Settings()
