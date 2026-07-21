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

    # ── Q5 option B — transient-failure redelivery ────────────────────────────
    # Total deliveries of one message before the worker gives up and publishes a
    # terminal "failed". The worker enforces this itself via metadata.num_delivered
    # (so the API always gets exactly one failed event); max_deliver on the consumer
    # is the matching backstop for the case where the worker dies before acking.
    # NOTE: like ack_wait, max_deliver does not apply to an existing durable —
    # changing this needs the same out-of-band consumer recreate.
    llm_max_delivery_attempts: int = 3
    # Backoff applied to msg.nak(delay=...) between redeliveries: base * 2^(n-1),
    # capped. Keeps a struggling provider from being hammered.
    llm_retry_base_delay_seconds: float = 5.0
    llm_retry_max_delay_seconds: float = 60.0

    # ── Q5 option C — cold-start warm-up probe ────────────────────────────────
    # Scale-to-zero endpoints (Modal, vLLM, RunPod…) take 90-110s to answer the
    # first request after idle. Rather than spend that inside one long LLM call
    # with an all-or-nothing timeout, poll the OpenAI-compatible /models endpoint
    # until it responds, then make the real call against a warm endpoint.
    # Only runs for provider="openai_compatible" with a base_url — hosted
    # Anthropic/OpenAI have no health endpoint and never cold-start.
    llm_warmup_enabled: bool = True
    # Per-probe timeout. Deliberately short: a booting endpoint should fail fast
    # and be retried, not hold the socket open.
    llm_warmup_probe_timeout_seconds: float = 5.0
    # Total budget for the polling loop before giving up (raises WarmupTimeout,
    # classified transient). Sized above the observed 90-110s Modal cold start.
    llm_warmup_max_wait_seconds: float = 180.0
    # Gap between probes.
    llm_warmup_poll_interval_seconds: float = 2.0
    # Skip the probe entirely if this base_url answered within this many seconds.
    # Avoids paying a round-trip per job on an endpoint that is already hot.
    llm_warm_cache_seconds: float = 60.0

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
