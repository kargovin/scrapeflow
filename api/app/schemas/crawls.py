import uuid
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.models.job import OutputFormat
from app.schemas.jobs import Engine


class CrawlCreate(BaseModel):
    seed_url: AnyHttpUrl
    max_depth: int = Field(default=3, ge=1, le=10)
    max_pages: int = Field(default=100, ge=1, le=10000)
    include_paths: list[str] | None = None
    exclude_paths: list[str] | None = None
    ignore_sitemap: bool = False
    output_format: OutputFormat = OutputFormat.markdown
    engine: Engine = Engine.http
    webhook_url: AnyHttpUrl | None = None
    respect_robots: bool = True
    schedule_cron: str | None = None

    @field_validator("seed_url", mode="after")
    @classmethod
    def seed_url_to_str(cls, v: AnyHttpUrl) -> str:
        return str(v)

    @field_validator("webhook_url", mode="after")
    @classmethod
    def webhook_url_to_str(cls, v: AnyHttpUrl | None) -> str | None:
        return str(v) if v is not None else None


class CrawlPageResponse(BaseModel):
    id: uuid.UUID
    crawl_id: uuid.UUID
    url: str
    depth: int
    status: str
    result_path: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None = None
    model_config = {"from_attributes": True}


class CrawlResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    seed_url: str
    status: str
    max_depth: int
    max_pages: int
    include_paths: list[str] | None
    exclude_paths: list[str] | None
    ignore_sitemap: bool
    output_format: OutputFormat
    engine: str
    webhook_url: str | None
    respect_robots: bool
    schedule_cron: str | None
    total_queued: int
    total_completed: int
    total_failed: int
    created_at: datetime
    completed_at: datetime | None = None
    model_config = {"from_attributes": True}
