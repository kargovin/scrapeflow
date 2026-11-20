"""Consumer-side twin of the API's delivery contracts.

Activity inputs and outputs: ``api/app/workflows/activities/contracts.py``. A destination's stored
address and secret: ``api/app/schemas/destinations.py`` (``stored()``) — parsed here, not re-validated:
the API checked them at save. Pinned by ``contracts/test_activity_contract.py``.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]

# Must equal the API's DELIVERY_QUEUE and activity names — a mismatch raises no error; the task waits
# on a queue nobody polls until its timeout fires.
DELIVERY_QUEUE = "delivery"
TEST_WEBHOOK_ACTIVITY = "TestWebhook"

DestinationKind = Literal["bucket", "postgres", "smtp", "webhook"]


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
