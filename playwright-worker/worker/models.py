from typing import Literal

from pydantic import BaseModel, Field


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
    # ── Wire version (ADR-011) ───────────────────────────────────────────────
    # Pinned rather than defaulted. Versions 1 and 2 keyed artifacts on `job_id`,
    # which two of the three dispatch lanes could not honestly supply; v3 replaces it
    # with `artifact_id`. The two are incompatible in both directions, so the version
    # is what lets a worker say *which* mismatch it hit rather than reporting a
    # generic missing field. The cutover is a hard cut against a drained stream.
    schema_version: Literal[3]

    # ── Core fields ──────────────────────────────────────────────────────────
    # artifact_id names what this execution's artifacts are keyed on: job_runs.id on
    # the job and batch lanes, crawl_pages.id on the crawl lane. The worker uses it
    # verbatim and never interprets it — it does not know which lane it is on, which
    # is the light-worker rule (ADR-011 §2).
    #
    # min_length=1 is the loud failure ADR-011 §5 requires. It is never legitimately
    # absent, and the empty string is what Go's encoding/json silently produces from a
    # JSON null — the behaviour that turned a hard failure on this worker into silent
    # cross-tenant corruption on the Go one.
    artifact_id: str = Field(min_length=1)
    # Absent on the crawl lane, which creates no job_runs row (ADR-011 §2). Optional
    # by declaration, so an absent value parses and an unroutable result is impossible
    # rather than merely unlikely.
    run_id: str | None = None
    url: str
    output_format: str
    # ── Playwright-specific settings (top-level, not nested in options) ──────
    playwright_options: PlaywrightOptions | None = None
    # ── Optional sub-objects ─────────────────────────────────────────────────
    engine: str = "playwright"
    credentials: Credentials | None = None
    options: Options | None = None
    crawl_context: CrawlContext | None = None


class ResultMessage(BaseModel):
    # job_id has left the wire (ADR-011 §2). The API reads it from the job_runs row it
    # already loads, so there is one source for it instead of two that could disagree.
    run_id: str | None = None
    status: str
    source: str = "scrape"
    minio_path: str | None = None
    nats_stream_seq: int | None = None
    error: str | None = None
    warnings: list[str] | None = None
    screenshot_paths: list[str] | None = None
    crawl_context: CrawlContext | None = None

    def to_nats_bytes(self) -> bytes:
        # exclude_none omits fields with no value — matches the Go worker's omitempty
        # tags, and is what makes run_id *absent* rather than null on the crawl lane.
        return self.model_dump_json(exclude_none=True).encode()
