import enum
import uuid
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.models.job import OutputFormat


class Engine(str, enum.Enum):
    http = "http"
    playwright = "playwright"


class WaitStrategy(str, enum.Enum):
    load = "load"
    domcontentloaded = "domcontentloaded"
    networkidle = "networkidle"


class PlaywrightOptions(BaseModel):
    wait_strategy: WaitStrategy = WaitStrategy.load
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    block_images: bool = False


class LLMJobConfig(BaseModel):
    llm_key_id: uuid.UUID
    model: str
    output_schema: dict


class JobCreate(BaseModel):
    url: AnyHttpUrl
    output_format: OutputFormat = OutputFormat.html
    engine: Engine = Engine.http
    playwight_options: PlaywrightOptions | None = None
    llm_config: LLMJobConfig | None = None
    schedule_cron: str | None = None
    webhook_url: AnyHttpUrl | None = None

    @field_validator("url", "webhook_url", mode="after")
    @classmethod
    def uri_to_str(cls, v: AnyHttpUrl) -> str:
        return str(v)


class JobResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    url: str
    status: str
    output_format: OutputFormat
    result_path: str | None
    error: str | None
    run_id: uuid.UUID
    diff_detected: bool | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class JobCreateResponse(JobResponse):
    webhook_secret: str | None = None


class CancelJobResponse(BaseModel):
    message: str
