import os
from dataclasses import dataclass


@dataclass
class Settings:
    database_url: str
    nats_url: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool
    dispatch_batch_size: int
    dispatch_poll_interval: float  # seconds between dispatch loop iterations
    stale_threshold_minutes: int   # stalled dispatched items reset to pending on startup


settings = Settings(
    database_url=os.environ["DATABASE_URL"],
    nats_url=os.environ.get("NATS_URL", "nats://nats:4222"),
    minio_endpoint=os.environ.get("MINIO_ENDPOINT", "minio:9000"),
    minio_access_key=os.environ.get("MINIO_ACCESS_KEY", "scrapeflow"),
    minio_secret_key=os.environ.get("MINIO_SECRET_KEY", "scrapeflow_secret"),
    minio_bucket=os.environ.get("MINIO_BUCKET", "scrapeflow-results"),
    minio_secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
    dispatch_batch_size=int(os.environ.get("COORDINATOR_DISPATCH_BATCH_SIZE", "10")),
    dispatch_poll_interval=float(os.environ.get("COORDINATOR_DISPATCH_POLL_INTERVAL", "2")),
    stale_threshold_minutes=int(os.environ.get("COORDINATOR_STALE_THRESHOLD_MINUTES", "10")),
)
