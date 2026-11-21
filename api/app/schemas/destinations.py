"""Destinations (ADR-015 §2a): what each kind stores, and what any read may show."""

import ipaddress
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

DestinationKind = Literal["bucket", "postgres", "smtp", "webhook"]

_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_EMAIL = re.compile(r"^[^@\s<>,;\"]{1,64}@[^@\s<>,;\"]{1,253}$")
_PEM = "-----BEGIN CERTIFICATE-----"


def _host(v: str) -> str:
    """A DNS name or an IP literal; nothing else (no port, path or userinfo)."""
    v = v.strip().rstrip(".").lower()
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        pass
    if len(v) > 253 or not all(_LABEL.match(label) for label in v.split(".")):
        raise ValueError("must be a host name or an IP address")
    return v


def _https(v: AnyHttpUrl) -> AnyHttpUrl:
    if v.scheme != "https":
        raise ValueError("must be an https URL")
    if v.username or v.password:
        raise ValueError("must not carry a user name or password")
    return v


def _ca(v: str | None) -> str | None:
    if v is not None and _PEM not in v:
        raise ValueError("must be a PEM certificate")
    return v


def _email(v: str) -> str:
    v = v.strip()
    if not _EMAIL.match(v) or "." not in v.rsplit("@", 1)[1]:
        raise ValueError(f"not an email address: {v!r}")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- address: non-secret, returned by every read --------------------------------------------


class BucketAddress(_Strict):
    endpoint: AnyHttpUrl
    bucket: str
    prefix: str = Field(default="", max_length=512)
    region: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, v: AnyHttpUrl) -> AnyHttpUrl:
        _https(v)
        if (v.path or "/") != "/" or v.query or v.fragment:
            raise ValueError("must be the endpoint only, with no path or query")
        return v

    @field_validator("bucket")
    @classmethod
    def _bucket(cls, v: str) -> str:
        if not _BUCKET.match(v) or ".." in v:
            raise ValueError("not a valid bucket name")
        return v

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        return v.strip("/")


class PostgresAddress(_Strict):
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=63)
    db_schema: str = Field(default="public", min_length=1, max_length=63, alias="schema")
    table: str = Field(min_length=1, max_length=63)
    ca_certificate: str | None = Field(default=None, max_length=16384)

    @field_validator("host")
    @classmethod
    def _check_host(cls, v: str) -> str:
        return _host(v)

    @field_validator("ca_certificate")
    @classmethod
    def _check_ca(cls, v: str | None) -> str | None:
        return _ca(v)


class SmtpAddress(_Strict):
    host: str
    port: int = Field(default=587, ge=1, le=65535)
    tls_mode: Literal["starttls", "implicit"] = "starttls"
    sender: str = Field(alias="from", max_length=320)
    to: list[str] = Field(min_length=1, max_length=20)
    ca_certificate: str | None = Field(default=None, max_length=16384)

    @field_validator("host")
    @classmethod
    def _check_host(cls, v: str) -> str:
        return _host(v)

    @field_validator("ca_certificate")
    @classmethod
    def _check_ca(cls, v: str | None) -> str | None:
        return _ca(v)

    @field_validator("sender")
    @classmethod
    def _sender(cls, v: str) -> str:
        return _email(v)

    @field_validator("to")
    @classmethod
    def _to(cls, v: list[str]) -> list[str]:
        return [_email(a) for a in v]


class WebhookAddress(_Strict):
    """Empty: a webhook's URL is a credential (PRD-017 R8)."""


# --- secret: encrypted at save, never returned ----------------------------------------------


class BucketSecret(_Strict):
    access_key: str = Field(min_length=1, max_length=256)
    secret_key: str = Field(min_length=1, max_length=256)


class LoginSecret(_Strict):
    user: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class WebhookSecret(_Strict):
    url: AnyHttpUrl
    # As the inline block's: none signs with b"".
    secret: str | None = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: AnyHttpUrl) -> AnyHttpUrl:
        if len(str(v)) > 2048:
            raise ValueError("must be at most 2048 characters")
        return _https(v)


KIND_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "bucket": (BucketAddress, BucketSecret),
    "postgres": (PostgresAddress, LoginSecret),
    "smtp": (SmtpAddress, LoginSecret),
    "webhook": (WebhookAddress, WebhookSecret),
}


def stored(model: BaseModel) -> dict[str, Any]:
    """The JSON a parsed address or secret is stored and returned as (aliases: ``from``, ``schema``)."""
    return model.model_dump(mode="json", by_alias=True)


def endpoint_url(kind: str, address: BaseModel, secret: BaseModel) -> str:
    """The URL whose host is SSRF-checked at save and shown as ``display_host``."""
    if kind == "bucket":
        return str(address.endpoint)
    if kind == "webhook":
        return str(secret.url)
    host = address.host
    if ":" in host:
        host = f"[{host}]"
    return f"{kind}://{host}:{address.port}"


def display_host(url: str) -> str:
    return urlparse(url).hostname or ""


# --- requests ---------------------------------------------------------------------------------

Name = Annotated[str, Field(min_length=1, max_length=100)]


class BucketForm(_Strict):
    kind: Literal["bucket"]
    address: BucketAddress
    secret: BucketSecret
    # Optional on a test (the SPA sends the form it is about to save); required to create.
    name: Name | None = None


class PostgresForm(_Strict):
    kind: Literal["postgres"]
    address: PostgresAddress
    secret: LoginSecret
    name: Name | None = None


class SmtpForm(_Strict):
    kind: Literal["smtp"]
    address: SmtpAddress
    secret: LoginSecret
    name: Name | None = None


class WebhookForm(_Strict):
    kind: Literal["webhook"]
    address: WebhookAddress = Field(default_factory=WebhookAddress)
    secret: WebhookSecret
    name: Name | None = None


DestinationForm = Annotated[
    BucketForm | PostgresForm | SmtpForm | WebhookForm, Field(discriminator="kind")
]


class BucketCreate(BucketForm):
    name: Name


class PostgresCreate(PostgresForm):
    name: Name


class SmtpCreate(SmtpForm):
    name: Name


class WebhookCreate(WebhookForm):
    name: Name


DestinationCreate = Annotated[
    BucketCreate | PostgresCreate | SmtpCreate | WebhookCreate, Field(discriminator="kind")
]


class DestinationEdit(_Strict):
    """Fields given replace the stored ones; the merged address and secret are checked whole."""

    address: dict[str, Any] | None = None
    secret: dict[str, Any] | None = None


class DestinationUpdate(DestinationEdit):
    """The kind never changes (a pinned version's block needs its kind)."""

    name: Name | None = None


# --- responses --------------------------------------------------------------------------------


class DestinationResponse(BaseModel):
    """Never the secret; a webhook shows its host only (ADR-015 §2a)."""

    id: uuid.UUID
    name: str
    kind: DestinationKind
    address: dict[str, Any]
    display_host: str
    created_at: datetime
    updated_at: datetime


class DestinationCheck(BaseModel):
    check: str
    outcome: Literal["passed", "failed", "warning"]
    message: str | None = None


class DestinationTestResult(BaseModel):
    """ADR-015 §2d: each check run, in order; a failed check ends the test. Never stored."""

    # No check failed: a warning (a bucket that refused the delete) still saves.
    passed: bool
    checks: list[DestinationCheck]


class DestinationSavedResponse(DestinationResponse):
    # None: an edit that changed neither the address nor the secret runs no test.
    test: DestinationTestResult | None = None


class AdminDestinationResponse(DestinationResponse):
    user_id: uuid.UUID
    user_email: str
