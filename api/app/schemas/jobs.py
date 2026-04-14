import enum
import uuid
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

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


class _MutableJobFields(BaseModel):
    playwright_options: PlaywrightOptions | None = None
    llm_config: LLMJobConfig | None = None
    schedule_cron: str | None = None
    webhook_url: AnyHttpUrl | None = None

    @field_validator("webhook_url", mode="after")
    @classmethod
    def uri_to_str(cls, v: AnyHttpUrl | None) -> str | None:
        return str(v) if v is not None else None


class JobPatch(_MutableJobFields):
    model_config = ConfigDict(extra="forbid")

    schedule_status: str | None = None


class JobCreate(_MutableJobFields):
    url: AnyHttpUrl
    output_format: OutputFormat = OutputFormat.html
    engine: Engine = Engine.http

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
    webhook_secret: str | None = None
    model_config = {"from_attributes": True}


class CancelJobResponse(BaseModel):
    message: str


class RotateWebhookSecretResponse(BaseModel):
    webhook_secret: str
