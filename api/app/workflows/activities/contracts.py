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


FingerprintOf = Literal["baseline", "waiting"]


class ValidateInput(BaseModel):
    """Fail mode raises naming every rule not met; stop mode returns a ValidateVerdict.

    Either mode raises, non-retryable, on a rule that cannot be checked.
    """

    input_path: Annotated[str, StringConstraints(min_length=1)]
    # The Validate block's rules as the catalog dumps them (by alias: "schema").
    rules: list[dict[str, Any]] = Field(min_length=1)
    when_unmet: Literal["fail", "stop"] = "fail"
    # SHA-256 hex of earlier checks' gate inputs; read by a `changed` rule only (ADR-016 §5d).
    known_fingerprints: dict[FingerprintOf, str] = {}


class ValidateVerdict(BaseModel):
    """Stop mode's answer; fail mode returns None."""

    outcome: Literal["met", "not_met"]
    # The first rule not met, as saved (by alias); None when every rule is met.
    rule_index: int | None = None
    rule: dict[str, Any] | None = None
    found: Any = None
    expected: Any = None
    # A `changed` rule's: SHA-256 hex of the input bytes, and which known one it equals.
    fingerprint: str | None = None
    matched: FingerprintOf | None = None


DIFF_ACTIVITY = "Diff"
CAPTURE_ACTIVITY = "Capture"


class DiffInput(BaseModel):
    """Compare the baseline's object with this check's (ADR-016 §6a). Content queue."""

    # The block that triggered the diff: diff.json is written beside its output.
    artifact_id: ArtifactId
    kind: Literal["page", "extraction"]
    baseline_path: Annotated[str, StringConstraints(min_length=1)]
    current_path: Annotated[str, StringConstraints(min_length=1)]


class PathChange(BaseModel):
    path: str
    change: Literal["added", "removed", "changed"]
    old: Any = None
    new: Any = None


class DiffSummary(BaseModel):
    """At most 16 KiB of JSON; what ``change_summary`` holds."""

    status: Literal["ok", "baseline_gone", "unavailable"]
    kind: Literal["page", "extraction"]
    reason: str | None = None
    lines: list[str] = []
    lines_added: int = 0
    lines_removed: int = 0
    changes: list[PathChange] = []
    paths_changed: int = 0
    truncated: bool = False


class DiffOutput(BaseModel):
    summary: DiffSummary
    # The full comparison, for the workflow to record on the block's ledger row.
    diff: StoredObject | None = None


class CarriedValue(BaseModel):
    input: Annotated[str, StringConstraints(min_length=1)]
    # RFC 6901, as Validate reads it.
    path: str
    type: Literal["number", "text"]


class CaptureInput(BaseModel):
    input_path: Annotated[str, StringConstraints(min_length=1)]
    values: list[CarriedValue] = Field(min_length=1)


class CapturedValue(BaseModel):
    """Exactly one of the two is set (ADR-014 §4's strict types: a bool is not a number)."""

    value: int | float | str | None = None
    missing: str | None = None


class CaptureOutput(BaseModel):
    values: dict[str, CapturedValue]


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


# --- delivery queue (ADR-015 §4) --------------------------------------------------------------

# Registered by the delivery worker under these names (delivery-worker/worker/contracts.py).
TEST_WEBHOOK_ACTIVITY = "TestWebhook"
TEST_BUCKET_ACTIVITY = "TestBucket"
DELIVER_BUCKET_ACTIVITY = "DeliverBucket"
UNDO_BUCKET_ACTIVITY = "UndoBucket"
TEST_SMTP_ACTIVITY = "TestSmtp"
SEND_EMAIL_ACTIVITY = "SendEmail"

# One attempt's start_to_close (ADR-015 §6); the worker gives up a few seconds sooner.
BUCKET_DELIVER_ATTEMPT_SECONDS = 60
BUCKET_UNDO_ATTEMPT_SECONDS = 30
EMAIL_SEND_ATTEMPT_SECONDS = 60


class DestinationTestInput(BaseModel):
    """A saved destination or an unsaved form (ADR-015 §2d); the secret is always ciphertext."""

    kind: Literal["bucket", "postgres", "smtp", "webhook"]
    # As stored: schemas.destinations.stored(address).
    address: dict[str, Any]
    encrypted_secret: Annotated[str, StringConstraints(min_length=1)]


class CheckResult(BaseModel):
    check: str
    outcome: Literal["passed", "failed", "warning"]
    message: str | None = None


class DestinationTestOutput(BaseModel):
    """Each check run, in order; a failed check ends the test."""

    checks: list[CheckResult]


# --- the bucket sink (ADR-015 §5, §7) --------------------------------------------------------
#
# Single attempt each, retry policy maximum_attempts=1: a failure is an ApplicationError whose
# non_retryable says final; the workflow retries a transient one (ADR-015 §4).


class BucketLocation(BaseModel):
    """Where a delivery landed: names only, never an address or a credential (ADR-015 §3)."""

    endpoint: Annotated[str, StringConstraints(min_length=1)]
    bucket: Annotated[str, StringConstraints(min_length=1)]
    region: str | None = None
    key: Annotated[str, StringConstraints(min_length=1)]


class DeliverBucketInput(BaseModel):
    # The destination as it is now (load_destination); the secret is ciphertext.
    address: dict[str, Any]
    encrypted_secret: Annotated[str, StringConstraints(min_length=1)]
    # ScrapeFlow's copy, as {bucket}/{key}.
    source_path: Annotated[str, StringConstraints(min_length=1)]
    pipeline_id: Annotated[str, StringConstraints(min_length=1)]
    run_id: Annotated[str, StringConstraints(min_length=1)]
    block_id: Annotated[str, StringConstraints(min_length=1)]
    ext: Literal["html", "md", "json"]


class DeliverBucketOutput(BaseModel):
    location: BucketLocation
    size: int


class UndoBucketInput(BaseModel):
    """The location recorded at delivery, with the destination's current secret."""

    location: BucketLocation
    encrypted_secret: Annotated[str, StringConstraints(min_length=1)]


class UndoBucketOutput(BaseModel):
    # The server said the object was missing; most answer a delete the same either way.
    already_gone: bool = False


# --- email (ADR-015 §4, §5, §7) --------------------------------------------------------------
#
# Single attempt, as the bucket sink: non_retryable on the ApplicationError is the final/transient answer.


class SendEmailInput(BaseModel):
    # The smtp destination as it is now; the secret is ciphertext.
    address: dict[str, Any]
    encrypted_secret: Annotated[str, StringConstraints(min_length=1)]
    # "<…@host>", the same on every attempt (ADR-015 §5).
    message_id: Annotated[str, StringConstraints(min_length=1)]
    subject: Annotated[str, StringConstraints(min_length=1)]
    body: str
    # Named by the "too large" note; None when the message belongs to no run.
    run_id: str | None = None


class SendEmailOutput(BaseModel):
    # Recipients the server refused while it accepted others.
    refused: list[str] = []
