from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    nats_url: str = "nats://localhost:4222"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "scrapeflow"
    minio_secret_key: str = "scrapeflow_secret"
    minio_bucket: str = "scrapeflow-results"
    minio_secure: bool = False
    playwright_max_workers: int = 3
    playwright_default_timeout_seconds: int = 60


settings = Settings()
