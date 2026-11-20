"""Settings: Temporal, MinIO (a read-only user) and the destinations key — nothing else (ADR-015 §4).

No ``.env`` file is read: every value comes from the process environment, which compose and the
Deployment list explicitly.
"""

from cryptography.fernet import Fernet, InvalidToken
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "scrapeflow"
    # Above one attempt's budget (60 s, ADR-015 §6); the pod's grace period must exceed it.
    delivery_graceful_shutdown_seconds: float = 75.0

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "scrapeflow-results"
    minio_secure: bool = False

    destination_encryption_key: str = ""

    @field_validator("destination_encryption_key")
    @classmethod
    def _fernet(cls, v: str) -> str:
        if not v:
            raise ValueError("DESTINATION_ENCRYPTION_KEY must be set to a valid Fernet key")
        try:
            Fernet(v)
        except (ValueError, InvalidToken):
            raise ValueError("DESTINATION_ENCRYPTION_KEY is not a valid Fernet key") from None
        return v


settings = Settings()
