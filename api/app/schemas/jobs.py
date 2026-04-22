import enum
import uuid
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from app.models.job import OutputFormat

_VALID_WEBHOOK_EVENTS = frozenset(
    {"job.completed", "job.failed", "crawl.completed", "batch.completed"}
)

_VALID_ACTION_TYPES = frozenset(
    {
        "wait",
        "wait_for_selector",
        "click",
        "type",
        "press",
        "scroll",
        "execute_js",
        "screenshot",
    }
)


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
    respect_robots: bool = False
    proxy_provider: str | None = None
    proxy_url: str | None = None  # write-only; stored encrypted in job_secrets, never returned
    cookies: list[dict] | None = None  # write-only; stored encrypted in job_secrets, never returned
    actions: list[dict] | None = None
    webhook_events: list[str] | None = None

    @field_validator("webhook_events")
    @classmethod
    def validate_webhook_events(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for event in v:
            if event not in _VALID_WEBHOOK_EVENTS:
                raise ValueError(f"unknown webhook event: {event!r}")
        return v

    @field_validator("webhook_url", mode="after")
    @classmethod
    def uri_to_str(cls, v: AnyHttpUrl | None) -> str | None:
        return str(v) if v is not None else None

    @field_validator("actions")
    @classmethod
    def validate_actions(cls, v: list[dict] | None) -> list[dict] | None:
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("max 20 actions per job")
        for action in v:
            action_type = action.get("type")
            if action_type not in _VALID_ACTION_TYPES:
                raise ValueError(f"unknown action type: {action_type!r}")
            if action_type == "wait":
                ms = action.get("milliseconds")
                try:
                    if not (1 <= int(ms) <= 10000):
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "wait.milliseconds must be an integer between 1 and 10000"
                    ) from exc
            elif action_type in {"click", "type", "wait_for_selector"}:
                selector = action.get("selector")
                if not selector or not isinstance(selector, str):
                    raise ValueError(f"{action_type}.selector must be a non-empty string")
        return v


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
    has_proxy: bool = False
    has_cookies: bool = False
    actions: list[dict] | None = None
    webhook_events: list[str] | None = None
    model_config = {"from_attributes": True}


class JobResultResponse(BaseModel):
    content: str
    output_format: str
    result_path: str
    warnings: list[str] | None = None


class CancelJobResponse(BaseModel):
    message: str


class RotateWebhookSecretResponse(BaseModel):
    webhook_secret: str
