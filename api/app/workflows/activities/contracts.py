"""Producer-side activity contracts."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

# min_length=1 also rejects "", which Go's encoding/json produces from a JSON null.
ArtifactId = Annotated[str, StringConstraints(min_length=1)]


# Registered by the Go worker under this name (internal/activity.ScrapeName); must match exactly.
SCRAPE_ACTIVITY = "Scrape"

# Registered by the LLM worker under this name (worker/contracts.py); must match exactly.
LLM_EXTRACT_ACTIVITY = "LLMExtract"

# Registered by the LLM worker on the content queue (worker/contracts.py); must match exactly.
CLEAN_ACTIVITY = "Clean"
VALIDATE_ACTIVITY = "Validate"

# Registered by the Playwright worker on the crawl queue (worker/contracts.py); must match exactly.
DISCOVER_SITEMAPS_ACTIVITY = "DiscoverSitemaps"
FETCH_SITEMAP_ACTIVITY = "FetchSitemap"
EXTRACT_LINKS_ACTIVITY = "ExtractLinks"


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


class CleanInput(BaseModel):
    artifact_id: ArtifactId
    input_path: Annotated[str, StringConstraints(min_length=1)]


class CleanOutput(BaseModel):
    result: StoredObject


class ValidateInput(BaseModel):
    """A failing rule is raised, not returned."""

    input_path: Annotated[str, StringConstraints(min_length=1)]
    # The Validate block's rules as the catalog dumps them (by alias: "schema").
    rules: list[dict[str, Any]] = Field(min_length=1)


class DiscoverSitemapsInput(BaseModel):
    seed_url: str


class DiscoverSitemapsOutput(BaseModel):
    """The seed's robots.txt ``Sitemap:`` lines, or its ``/sitemap.xml`` when it names none.

    Not fetched yet: the workflow vets each one (SSRF + scope) before it asks for it.
    """

    sitemaps: list[str] = []


class FetchSitemapInput(BaseModel):
    url: str
    max_entries: Annotated[int, Field(ge=1)]


class FetchSitemapOutput(BaseModel):
    """One sitemap file, never a redirect followed: ``location`` is handed back to be vetted."""

    kind: Literal["urlset", "index", "redirect", "unavailable"]
    # Page URLs (urlset) or child sitemaps (index).
    locs: list[str] = []
    location: str | None = None
    # More entries than max_entries or the payload cap; the rest were not read.
    truncated: bool = False


class ExtractLinksInput(BaseModel):
    # The stored page, as {bucket}/{key}.
    path: Annotated[str, StringConstraints(min_length=1)]
    base_url: str


class ExtractLinksOutput(BaseModel):
    """Same-origin links, fragments stripped; the crawl's path filters are admission's."""

    links: list[str] = []
