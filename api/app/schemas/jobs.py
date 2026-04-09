import enum
import uuid

from pydantic import BaseModel, Field


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
