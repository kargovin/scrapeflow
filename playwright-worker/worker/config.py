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
    playwright_max_workers: int = 3
    playwright_default_timeout_seconds: int = 60
    credentials_encryption_key: str = ""
    # NATS pull-consumer ack window (seconds). The default JetStream ack_wait is 30s,
    # which is *shorter than a headed-Chrome scrape of a heavy page* — so NATS redelivered
    # the message mid-scrape, the late ack was a no-op, and the job looped forever
    # (max_deliver is unlimited). This sets an explicit floor; the in-progress heartbeat
    # below additionally resets the timer mid-job so even jobs longer than this never expire.
    playwright_ack_wait_seconds: int = 120
    # How often to send msg.in_progress() while a job runs (must be < ack_wait).
    playwright_heartbeat_seconds: int = 30
    # ── UF-003 3a — transient-failure redelivery (ported from the LLM worker) ──
    # Total deliveries of one message before the worker gives up and publishes a
    # terminal "failed". The worker enforces this itself via metadata.num_delivered
    # (so the API always gets exactly one failed event); max_deliver on the consumer
    # is the matching backstop for the case where the worker dies before acking.
    # NOTE: like ack_wait, max_deliver does not apply to an existing durable —
    # changing this needs the same out-of-band consumer recreate.
    playwright_max_delivery_attempts: int = 3
    # Backoff applied to msg.nak(delay=...) between redeliveries: base * 2^(n-1),
    # capped. Keeps a struggling MinIO from being hammered while it recovers.
    playwright_retry_base_delay_seconds: float = 5.0
    playwright_retry_max_delay_seconds: float = 60.0
    # Stealth launch config (Patchright). Verified against BrowserScan bot-detection.
    # channel="chrome" uses real Google Chrome (installed in the image via
    # `patchright install chrome`) so the UA reports a genuine "Chrome". Set to ""
    # to fall back to Patchright's bundled Chromium.
    playwright_channel: str = "chrome"
    # headless=False runs Chrome *truly headed* under Xvfb (see Dockerfile CMD).
    # This is the only mode that yields a clean UA: every headless mode — including
    # the new `--headless=new` — still emits the "HeadlessChrome" UA token, which
    # bot detectors flag. Headed also matters because headless is itself a signal.
    playwright_headless: bool = False
    # --disable-blink-features=AutomationControlled removes navigator.webdriver
    # (Patchright's driver patches alone don't clear it in launch() mode).
    playwright_disable_automation: bool = True
    # Chrome needs --no-sandbox to launch as a non-root user inside the container.
    playwright_no_sandbox: bool = True
    # --disable-dev-shm-usage: k8s pods default to a 64 MB /dev/shm, which headed
    # Chrome exhausts and crashes on. Routes shared memory to /tmp instead. Neither
    # this nor --no-sandbox is JS-detectable (both are process-level flags).
    playwright_disable_dev_shm: bool = True

    @field_validator("credentials_encryption_key")
    def validate_fernet_key(cls, v):
        if not v:
            raise ValueError(
                "CREDENTIALS_ENCRYPTION_KEY must be set to a valid Fernet key"
            )
        try:
            Fernet(v)
        except (ValueError, InvalidToken):
            raise ValueError(
                "CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key"
            ) from None
        return v


settings = Settings()
