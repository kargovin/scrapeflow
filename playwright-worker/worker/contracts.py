"""Consumer-side twin of the API's Scrape activity contracts (api/app/workflows/activities/contracts.py)."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
SCRAPE_ACTIVITY = "Scrape"
SCRAPE_PLAYWRIGHT_QUEUE = "scrape-playwright"


class PlaywrightOptions(BaseModel):
    wait_strategy: str = "load"
    timeout_seconds: int = 60
    block_images: bool = False


class Credentials(BaseModel):
    encrypted_proxy_url: str | None = None
    encrypted_cookies: str | None = None


class ScrapeOptions(BaseModel):
    respect_robots: bool = False
    actions: list[dict[str, Any]] | None = None


class ScrapeInput(BaseModel):
    artifact_id: NonEmptyStr
    url: str
    output_format: Literal["html", "markdown", "json"]
    credentials: Credentials | None = None
    options: ScrapeOptions | None = None
    playwright_options: PlaywrightOptions | None = None


class StoredObject(BaseModel):
    path: NonEmptyStr
    size: Annotated[int, Field(ge=0)]


class ScrapeOutput(BaseModel):
    result: StoredObject
    content_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
    warnings: list[str] = []
    screenshots: list[StoredObject] = []
