"""Producer-side NATS message contracts (ADR-002 §3, ADR-011).

These models are the **only** definitions the API publishes through. Every dispatcher
builds one of them and calls ``to_nats_bytes()``; no route or background loop
constructs a raw payload dict.

Why this module exists (ADR-011 §6). Before it there were ten publish sites building
raw dicts and **zero** producer-side schemas, while three workers each declared a
consumer-side one. BUG-005 is what that asymmetry cost: the worker models were not
wrong and had not drifted from each other — each said ``job_id`` was a required
string, which is a reasonable thing to say. They disagreed with a producer that had
no definition to disagree in, and the disagreement surfaced as silent cross-tenant
corruption on the Go wire rather than as an error anywhere.

Live in ``app/`` beside ``constants.py`` for the same reason the subject names do:
this is the worker contract, not an HTTP schema and not a service.
"""

from typing import Annotated, Any

from pydantic import BaseModel, StringConstraints

# ── Schema version ───────────────────────────────────────────────────────────
#
# 3 is the ADR-011 wire: `artifact_id` replaces `job_id`, objects are named by
# producing stage, and `run_id` is optional (absent on the crawl lane).
#
# The bump is load-bearing rather than cosmetic. v2 and v3 have incompatible
# required-field sets in both directions — a v2 worker reading a v3 message finds
# no `job_id`, and a v3 worker reading a v2 message finds no `artifact_id` — so the
# version is what lets each side say *which* mismatch it hit instead of reporting a
# generic missing field. The cutover is a hard cut with a drained stream; this is
# what makes a mis-ordered deploy legible.
SCHEMA_VERSION = 3

# `artifact_id` is never legitimately absent, so it is the one field that must fail
# loudly rather than default (ADR-011 §5). min_length=1 also rejects the empty
# string, which is the value Go's encoding/json silently produces from a JSON null.
ArtifactId = Annotated[str, StringConstraints(min_length=1)]


class Credentials(BaseModel):
    """Per-job secrets, as Fernet ciphertext under CREDENTIALS_ENCRYPTION_KEY."""

    encrypted_proxy_url: str | None = None
    encrypted_cookies: str | None = None


class MessageOptions(BaseModel):
    """Per-job behavioural flags.

    ``actions`` stays a list of raw dicts — the Playwright worker's actions executor
    owns type dispatch, and mirroring that union here would put a second copy of it
    on the producing side.
    """

    respect_robots: bool = False
    actions: list[dict[str, Any]] | None = None


class CrawlContext(BaseModel):
    """Set only when the BFS coordinator dispatched this run (ADR-005)."""

    crawl_id: str
    crawl_page_id: str
    depth: int


class ScrapeMessage(BaseModel):
    """The fat message consumed by the Go HTTP worker and the Playwright worker.

    Built in one place on the API side — ``core/dispatch.py``, which every scrape
    dispatcher (``routers/jobs.py``, ``routers/batch.py``, ``core/scheduler.py`` for
    both dispatch and stale-pending recovery) calls — plus the coordinator's own copy
    of this shape.

    ``artifact_id`` is the primary key of the row that owns the execution —
    ``job_runs.id`` on the job, scheduled and batch lanes, ``crawl_pages.id`` on the
    crawl lane (ADR-011 §1). The worker uses it verbatim to build its object paths and
    never interprets it, which is what keeps the light-worker rule intact: a worker
    does not know which lane it is on.

    ``run_id`` is the result-routing key and is **absent on the crawl lane**, which
    creates no ``job_runs`` row. It used to carry a ``uuid4()`` fabricated for that
    lane — a made-up identifier that satisfied a required field by lying, and then
    read as fact to everything downstream (ADR-011 §2).
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
        # exclude_none omits unset fields, which is how `run_id` becomes *absent*
        # rather than null on the crawl lane. Matches the workers' own result-message
        # serialization and the Go struct's omitempty tags.
        return self.model_dump_json(exclude_none=True).encode()


class LLMMessage(BaseModel):
    """The message consumed by the LLM worker, dispatched from the result consumer.

    ``run_id`` is required here, unlike on ``ScrapeMessage``: the LLM stage is only
    reachable on the job and batch lanes, and both create a ``job_runs`` row. On both,
    ``artifact_id`` and ``run_id`` hold the same value — they are set from the same
    row. They are still two fields, set explicitly from the run, because the identity
    of *what the artifacts are named after* and the identity of *what routes the
    result* are different questions that happen to share an answer here. Collapsing
    them is the move that put a ``crawl_pages`` id in a field named ``job_id``.
    """

    schema_version: int = SCHEMA_VERSION
    artifact_id: ArtifactId
    run_id: str
    raw_minio_path: str
    provider: str
    encrypted_api_key: str
    base_url: str | None = None
    model: str
    output_schema: dict[str, Any]

    def to_nats_bytes(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode()
