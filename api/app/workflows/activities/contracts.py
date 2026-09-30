"""Producer-side activity contracts."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

# min_length=1 also rejects "", which Go's encoding/json produces from a JSON null.
ArtifactId = Annotated[str, StringConstraints(min_length=1)]


# Registered by the Go worker under this name (internal/activity.ScrapeName); must match exactly.
SCRAPE_ACTIVITY = "Scrape"

# Registered by the LLM worker under this name (worker/contracts.py); must match exactly.
LLM_EXTRACT_ACTIVITY = "LLMExtract"


class Credentials(BaseModel):
    """Per-job secrets as Fernet ciphertext — workflow history stores inputs in plain JSON."""

    encrypted_proxy_url: str | None = None
    encrypted_cookies: str | None = None


class ScrapeOptions(BaseModel):
    respect_robots: bool = False
    actions: list[dict[str, Any]] | None = None


class ScrapeInput(BaseModel):
    """Input to the scrape activity on both engines."""

    artifact_id: ArtifactId
    url: str
    output_format: Literal["html", "markdown", "json"]
    credentials: Credentials | None = None
    options: ScrapeOptions | None = None
    playwright_options: dict[str, Any] | None = None


class StoredObject(BaseModel):
    """An object the activity wrote to MinIO, as ``{bucket}/{key}``."""

    path: Annotated[str, StringConstraints(min_length=1)]
    size: Annotated[int, Field(ge=0)]


class ScrapeOutput(BaseModel):
    """Result of a successful Scrape; a failure is raised, not returned."""

    result: StoredObject
    # xxh64 of the result object's bytes, zero-padded — Go's %x on a uint64 drops leading zeros.
    content_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
    warnings: list[str] = []
    screenshots: list[StoredObject] = []


class LLMInput(BaseModel):
    artifact_id: ArtifactId
    raw_minio_path: str
    provider: Literal["anthropic", "openai_compatible"]
    # Fernet ciphertext, for the same reason as Credentials.
    encrypted_api_key: str
    base_url: str | None = None
    model: str
    output_schema: dict[str, Any]


class LLMOutput(BaseModel):
    result: StoredObject
