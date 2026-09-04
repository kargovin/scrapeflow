"""Producer-side NATS message contract for the crawl lane (ADR-002 §3, ADR-011).

A deliberate duplicate of the API's ``app/messages.py``. ADR-011 §6 weighed extracting
these definitions into a shared package and did not take it: a Python package covers
four of the five services but not the Python→Go wire where BUG-005's silent corruption
actually happened, and every service builds from its own context with its own
dependency manifest, so sharing means either moving all build contexts to the repo
root or vendoring copies anyway.

What binds this copy to the API's is the cross-service contract test, not an import.
"""

from typing import Annotated, Any

from pydantic import BaseModel, StringConstraints

SCHEMA_VERSION = 3

ArtifactId = Annotated[str, StringConstraints(min_length=1)]


class Credentials(BaseModel):
    encrypted_proxy_url: str | None = None
    encrypted_cookies: str | None = None


class MessageOptions(BaseModel):
    respect_robots: bool = False
    actions: list[dict[str, Any]] | None = None


class CrawlContext(BaseModel):
    crawl_id: str
    crawl_page_id: str
    depth: int


class ScrapeMessage(BaseModel):
    """The fat message consumed by the Go HTTP worker and the Playwright worker.

    On this lane ``artifact_id`` is ``crawl_pages.id`` — the row that owns the
    execution — and ``run_id`` is **absent**, because a crawl page has no
    ``job_runs`` row to name (ADR-011 §1, §2).
    """

    schema_version: int = SCHEMA_VERSION
    artifact_id: ArtifactId
    run_id: str | None = None
    url: str
    output_format: str
    engine: str
    credentials: Credentials | None = None
    options: MessageOptions | None = None
    crawl_context: CrawlContext | None = None
    playwright_options: dict[str, Any] | None = None

    def to_nats_bytes(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode()
