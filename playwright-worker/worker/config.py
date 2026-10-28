from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "scrapeflow"
    # How long SIGTERM waits for in-flight scrapes before cancelling
    # them; a cancelled scrape is re-rendered on the retry. Covers the 300 s
    # timeout_seconds maximum (goto + wait_for_load_state at 300 s each) + 30 s for
    # upload; page actions are uncapped, so a long action list can still be cut.
    # The pod's terminationGracePeriodSeconds must exceed it.
    playwright_graceful_shutdown_seconds: float = 630.0
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "scrapeflow"
    minio_secret_key: str = "scrapeflow_secret"
    minio_bucket: str = "scrapeflow-results"
    minio_secure: bool = False
    playwright_max_workers: int = 3
    # Crawl-queue activities in flight (robots.txt, sitemaps, link extraction);
    # no browser, so not bounded by playwright_max_workers.
    crawl_max_activities: int = 10
    playwright_default_timeout_seconds: int = 60
    credentials_encryption_key: str = ""
    # How often activity.heartbeat() fires while a scrape runs; must be < the caller's
    # heartbeat_timeout.
    playwright_heartbeat_seconds: int = 30
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
