"""Consumer-side twin of the API's delivery contracts.

Activity inputs and outputs: ``api/app/workflows/activities/contracts.py``. A destination's stored
address and secret: ``api/app/schemas/destinations.py`` (``stored()``) — parsed here, not re-validated:
the API checked them at save. Pinned by ``contracts/test_activity_contract.py``.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]

# Must equal the API's DELIVERY_QUEUE and activity names — a mismatch raises no error; the task waits
# on a queue nobody polls until its timeout fires.
DELIVERY_QUEUE = "delivery"
TEST_WEBHOOK_ACTIVITY = "TestWebhook"
TEST_BUCKET_ACTIVITY = "TestBucket"
DELIVER_BUCKET_ACTIVITY = "DeliverBucket"
UNDO_BUCKET_ACTIVITY = "UndoBucket"
TEST_SMTP_ACTIVITY = "TestSmtp"
TEST_POSTGRES_ACTIVITY = "TestPostgres"
DELIVER_POSTGRES_ACTIVITY = "DeliverPostgres"
UNDO_POSTGRES_ACTIVITY = "UndoPostgres"
SEND_EMAIL_ACTIVITY = "SendEmail"

# One attempt's start_to_close (ADR-015 §6); the activity gives up a few seconds sooner.
BUCKET_DELIVER_ATTEMPT_SECONDS = 60
BUCKET_UNDO_ATTEMPT_SECONDS = 30
EMAIL_SEND_ATTEMPT_SECONDS = 60
POSTGRES_DELIVER_ATTEMPT_SECONDS = 30
POSTGRES_UNDO_ATTEMPT_SECONDS = 30

# A DeliverBucket or DeliverPostgres failure's detail when its request cannot have written: the
# destination answered with a refusal, or nothing was sent. Without it the workflow treats the try as maybe written.
NOT_WRITTEN = "not_written"

DestinationKind = Literal["bucket", "postgres", "smtp", "webhook"]


def object_key(prefix: str, pipeline_id: str, run_id: str, block_id: str, ext: str) -> str:
    """``{prefix}/{pipeline_id}/{run_id}/{block_id}.{ext}`` (ADR-015 §5): a retry overwrites."""
    return "/".join(p for p in (prefix.strip("/"), pipeline_id, run_id, f"{block_id}.{ext}") if p)


# --- a destination as stored ----------------------------------------------------------------


class BucketAddress(BaseModel):
    endpoint: NonEmptyStr
    bucket: NonEmptyStr
    prefix: str = ""
    region: str | None = None


class PostgresAddress(BaseModel):
    host: NonEmptyStr
    port: int
    database: NonEmptyStr
    db_schema: NonEmptyStr = Field(alias="schema")
    table: NonEmptyStr
    ca_certificate: str | None = None


class SmtpAddress(BaseModel):
    host: NonEmptyStr
    port: int
    tls_mode: Literal["starttls", "implicit"]
    sender: NonEmptyStr = Field(alias="from")
    to: list[NonEmptyStr] = Field(min_length=1)
    ca_certificate: str | None = None


class WebhookAddress(BaseModel):
    pass


class BucketSecret(BaseModel):
    access_key: NonEmptyStr
    secret_key: NonEmptyStr


class LoginSecret(BaseModel):
    user: NonEmptyStr
    password: NonEmptyStr


class WebhookSecret(BaseModel):
    url: NonEmptyStr
    secret: str | None = None


KIND_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "bucket": (BucketAddress, BucketSecret),
    "postgres": (PostgresAddress, LoginSecret),
    "smtp": (SmtpAddress, LoginSecret),
    "webhook": (WebhookAddress, WebhookSecret),
}


# --- the destination test (ADR-015 §2d) -----------------------------------------------------


class DestinationTestInput(BaseModel):
    """A saved destination or an unsaved form; the secret is always ciphertext."""

    kind: DestinationKind
    address: dict[str, Any]
    encrypted_secret: NonEmptyStr


class CheckResult(BaseModel):
    check: NonEmptyStr
    outcome: Literal["passed", "failed", "warning"]
    message: str | None = None


class DestinationTestOutput(BaseModel):
    """Each check run, in order; a failed check ends the test."""

    checks: list[CheckResult]


# --- the bucket sink (ADR-015 §5, §7) -------------------------------------------------------
#
# Single attempt each: a failure raises ApplicationError, non_retryable = final. The workflow
# retries a transient one (ADR-015 §4).


class BucketLocation(BaseModel):
    """Where a delivery landed: names only, never an address or a credential (ADR-015 §3)."""

    endpoint: NonEmptyStr
    bucket: NonEmptyStr
    region: str | None = None
    key: NonEmptyStr


class DeliverBucketInput(BaseModel):
    # The destination as it is now (load_destination); the secret is ciphertext.
    address: dict[str, Any]
    encrypted_secret: NonEmptyStr
    # ScrapeFlow's copy, as {bucket}/{key}.
    source_path: NonEmptyStr
    pipeline_id: NonEmptyStr
    run_id: NonEmptyStr
    block_id: NonEmptyStr
    ext: Literal["html", "md", "json"]


class DeliverBucketOutput(BaseModel):
    location: BucketLocation
    size: int


class UndoBucketInput(BaseModel):
    """The location recorded at delivery, with the destination's current secret."""

    location: BucketLocation
    encrypted_secret: NonEmptyStr


class UndoBucketOutput(BaseModel):
    # The server said the object was missing; most answer a delete the same either way.
    already_gone: bool = False


# --- the Postgres sink (ADR-015 §5, §7) -------------------------------------------------------
#
# Single attempt each, as the bucket sink. NOT_WRITTEN on a DeliverPostgres failure: the INSERT
# cannot have committed.


class PostgresLocation(BaseModel):
    """The row a delivery wrote, by its key: names only, never an address or a credential."""

    model_config = ConfigDict(populate_by_name=True)

    host: NonEmptyStr
    port: int
    database: NonEmptyStr
    db_schema: NonEmptyStr = Field(alias="schema")
    table: NonEmptyStr
    run_id: NonEmptyStr
    block_id: NonEmptyStr


def row_location(address: dict[str, Any], run_id: str, block_id: str) -> PostgresLocation:
    """The row a try against ``address`` writes: a retry updates it (ADR-015 §5)."""
    return PostgresLocation(
        host=address["host"],
        port=address["port"],
        database=address["database"],
        db_schema=address["schema"],
        table=address["table"],
        run_id=run_id,
        block_id=block_id,
    )


class DeliverPostgresInput(BaseModel):
    # The destination as it is now (load_destination); the secret is ciphertext.
    address: dict[str, Any]
    encrypted_secret: NonEmptyStr
    # ScrapeFlow's copy, as {bucket}/{key}: one JSON document.
    source_path: NonEmptyStr
    pipeline_id: NonEmptyStr
    run_id: NonEmptyStr
    block_id: NonEmptyStr


class DeliverPostgresOutput(BaseModel):
    location: PostgresLocation
    size: int


class UndoPostgresInput(BaseModel):
    """The location recorded at delivery, with the destination's current login and CA."""

    location: PostgresLocation
    encrypted_secret: NonEmptyStr
    ca_certificate: str | None = None


class UndoPostgresOutput(BaseModel):
    # The DELETE found no row.
    already_gone: bool = False


# --- email (ADR-015 §4, §5, §7) --------------------------------------------------------------
#
# Single attempt, as the bucket sink: non_retryable on the ApplicationError is the final/transient answer.


class SendEmailInput(BaseModel):
    # The smtp destination as it is now; the secret is ciphertext.
    address: dict[str, Any]
    encrypted_secret: NonEmptyStr
    # "<…@host>", the same on every attempt (ADR-015 §5).
    message_id: NonEmptyStr
    subject: NonEmptyStr
    body: str
    # Named by the "too large" note; None when the message belongs to no run.
    run_id: str | None = None
    # An Email block's input, {bucket}/{key}: its content follows the body, inline up to the cap.
    source_path: str | None = None
    # The destination's recipients this try sends to (those an earlier try left deferred); None = all.
    recipients: list[str] | None = None


class SendEmailOutput(BaseModel):
    # Recipients the server refused (5xx) while it accepted others.
    refused: list[str] = []
    # Recipients the server deferred (4xx) while it accepted others: a later try sends to them only.
    deferred: list[str] = []
