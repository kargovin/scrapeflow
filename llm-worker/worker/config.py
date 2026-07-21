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
    # Provider-SDK retry count. Both the anthropic and openai clients default to
    # max_retries=2, so one call_llm() could make three billable attempts — invisible
    # in our logs and multiplying the wall-clock ceiling to ~3x. Pinned to 0 so retry
    # lives in exactly one visible layer (today: NATS redelivery; Phase 4: Temporal
    # RetryPolicy). Raising this re-hides retries *underneath* whatever retries above it.
    llm_max_retries: int = 0
    # NATS pull-consumer ack window (seconds). The JetStream default is 30s — shorter
    # than llm_request_timeout_seconds (60), so a slow LLM call was redelivered mid-flight,
    # the late ack was a no-op, and with max_deliver unlimited the job re-billed the user's
    # own API key in a loop. Unlike the playwright worker, this value is NOT sized to cover
    # a whole job: the heartbeat below does that. This is the orphan-recovery window — how
    # long a message sits before redelivery when a worker dies mid-job.
    llm_ack_wait_seconds: int = 120
    # How often to send msg.in_progress() while a job runs (must be < ack_wait).
    # This, not ack_wait, is what keeps a long LLM call from being redelivered.
    llm_heartbeat_seconds: int = 30

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
