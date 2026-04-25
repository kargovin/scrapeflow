import uuid
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.models.job import OutputFormat
from app.schemas.jobs import Engine, LLMJobConfig


class BatchCreate(BaseModel):
    urls: list[AnyHttpUrl] = Field(min_length=1, max_length=500)
    output_format: OutputFormat = OutputFormat.html
    engine: Engine = Engine.http
    webhook_url: AnyHttpUrl | None = None
    respect_robots: bool = False
    llm_config: LLMJobConfig | None = None

    @field_validator("urls", mode="after")
    @classmethod
    def urls_to_str(cls, v: list[AnyHttpUrl]) -> list[str]:
        return [str(u) for u in v]

    @field_validator("webhook_url", mode="after")
    @classmethod
    def webhook_url_to_str(cls, v: AnyHttpUrl | None) -> str | None:
        return str(v) if v is not None else None


class BatchItemResponse(BaseModel):
    id: uuid.UUID
    url: str
    status: str
    result_path: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None = None
    model_config = {"from_attributes": True}


class BatchResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    status: str
    output_format: OutputFormat
    engine: str
    total: int
    completed: int
    failed: int
    webhook_url: str | None
    respect_robots: bool
    llm_config: dict | None = None
    created_at: datetime
    completed_at: datetime | None = None
    model_config = {"from_attributes": True}
