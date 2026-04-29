from pydantic import BaseModel


class PlaywrightOptions(BaseModel):
    wait_strategy: str = "load"
    timeout_seconds: int = 60
    block_images: bool = False


# ── Schema version 2 sub-objects ────────────────────────────────────────────


class Credentials(BaseModel):
    encrypted_proxy_url: str | None = None
    encrypted_cookies: str | None = None


class Options(BaseModel):
    respect_robots: bool = False
    # Actions kept as raw dicts — the actions executor handles type dispatch.
    actions: list[dict] | None = None


class CrawlContext(BaseModel):
    crawl_id: str
    crawl_page_id: str
    depth: int


class JobMessage(BaseModel):
    # ── Core fields (v1 + v2) ────────────────────────────────────────────────
    job_id: str
    run_id: str
    url: str
    output_format: str
    # ── Playwright-specific settings (top-level, not nested in options) ──────
    playwright_options: PlaywrightOptions | None = None
    # ── Schema version 2 additions (all optional — v1 messages still parse) ──
    schema_version: int = 1
    engine: str = "playwright"
    credentials: Credentials | None = None
    options: Options | None = None
    crawl_context: CrawlContext | None = None


class ResultMessage(BaseModel):
    job_id: str
    run_id: str
    status: str
    source: str = "scrape"
    minio_path: str | None = None
    nats_stream_seq: int | None = None
    error: str | None = None
    warnings: list[str] | None = None
    screenshot_paths: list[str] | None = None
    crawl_context: CrawlContext | None = None

    def to_nats_bytes(self) -> bytes:
        # exclude_none omits fields with no value — matches the Go worker's omitempty tags
        return self.model_dump_json(exclude_none=True).encode()
