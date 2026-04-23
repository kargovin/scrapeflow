from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    scrapeflow_api_url: str = "http://localhost:8000"
    scrapeflow_api_key: str = ""

    mcp_poll_interval_seconds: float = 2.0
    mcp_max_wait_seconds: float = 120.0
    mcp_max_content_bytes: int = 51200  # 50 KB


settings = Settings()
