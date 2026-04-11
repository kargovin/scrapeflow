import enum
import uuid
from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreate(BaseModel):
    name: str


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    revoked: bool

    model_config = {"from_attributes": True}


class ApiKeyCreatedResponse(ApiKeyResponse):
    # Raw key included only in the creation response — shown once, never stored.
    key: str


class Providers(str, enum.Enum):
    openai_compatible = "openai_compatible"
    anthropic = "anthropic"


class LLMKeyCreate(BaseModel):
    name: str
    provider: Providers
    api_key: str = Field(min_length=8)
    base_url: AnyHttpUrl | None = None

    @field_validator("base_url", mode="after")
    @classmethod
    def uri_to_str(cls, v: AnyHttpUrl | None) -> str | None:
        return str(v) if v is not None else None


class LLMKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    provider: Providers
    base_url: str | None = None
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


class LLMKeyCreatedResponse(LLMKeyResponse):
    api_key: str
