"""The destination test's checks that run here (ADR-015 §2d). Nothing is sent and nothing is left behind."""

import asyncio
import ssl
import uuid
from urllib.parse import urlsplit

import structlog
from temporalio import activity

from . import egress, pinned_tcp, smtp
from .bucket import BucketClient, Endpoint, Refused, describe, endpoint
from .contracts import (
    TEST_BUCKET_ACTIVITY,
    TEST_SMTP_ACTIVITY,
    TEST_WEBHOOK_ACTIVITY,
    BucketAddress,
    BucketSecret,
    CheckResult,
    DestinationTestInput,
    DestinationTestOutput,
    LoginSecret,
    SmtpAddress,
    WebhookSecret,
)
from .destinations import final_error, parse

log = structlog.get_logger()

# The API waits up to 30 s for the whole test (ADR-015 §2d): one deadline for every request.
TEST_SECONDS = 20


def _passed(check: str) -> CheckResult:
    return CheckResult(check=check, outcome="passed")


def _failed(check: str, message: str) -> CheckResult:
    return CheckResult(check=check, outcome="failed", message=message)


def _warning(check: str, message: str) -> CheckResult:
    return CheckResult(check=check, outcome="warning", message=message)


@activity.defn(name=TEST_WEBHOOK_ACTIVITY)
async def test_webhook(inp: DestinationTestInput) -> DestinationTestOutput:
    """Address check, then a TLS handshake with the URL's host. Sends no POST."""
    if inp.kind != "webhook":
        raise final_error("DestinationKind", f"not a webhook destination: {inp.kind}")
    _, secret = parse(inp.kind, inp.address, inp.encrypted_secret)
    assert isinstance(secret, WebhookSecret)

    parts = urlsplit(secret.url)
    host = parts.hostname
    if parts.scheme != "https" or not host:
        return DestinationTestOutput(checks=[_failed("address", "must be an https URL")])
    try:
        port = parts.port or 443
    except ValueError:
        return DestinationTestOutput(checks=[_failed("address", "URL has an invalid port")])

    try:
        async with asyncio.timeout(TEST_SECONDS):
            await egress.tls_handshake(host, port)
    except egress.EgressBlocked as exc:
        return DestinationTestOutput(checks=[_failed("address", str(exc))])
    except egress.CertificateMismatch as exc:
        result = _failed("tls", str(exc))
    except ssl.SSLError as exc:
        result = _failed("tls", f"TLS handshake with {host} failed ({type(exc).__name__})")
    except (OSError, TimeoutError) as exc:
        result = _failed("tls", f"could not connect to {host}:{port} ({type(exc).__name__})")
    else:
        result = _passed("tls")
    log.info("destination_test", kind=inp.kind, host=host, tls=result.outcome)
    return DestinationTestOutput(checks=[_passed("address"), result])


@activity.defn(name=TEST_BUCKET_ACTIVITY)
async def test_bucket(inp: DestinationTestInput) -> DestinationTestOutput:
    """PUT an empty ``{prefix}/.scrapeflow-test-{uuid}``, then DELETE it.

    A refused delete is a warning naming the object left behind: rollback to the bucket will fail.
    """
    if inp.kind != "bucket":
        raise final_error("DestinationKind", f"not a bucket destination: {inp.kind}")
    address, secret = parse(inp.kind, inp.address, inp.encrypted_secret)
    assert isinstance(address, BucketAddress) and isinstance(secret, BucketSecret)
    try:
        where = endpoint(address.endpoint)
    except ValueError as exc:
        return DestinationTestOutput(checks=[_failed("address", str(exc))])

    client = BucketClient(where, address.bucket, address.region, secret)
    key = "/".join(p for p in (address.prefix.strip("/"), f".scrapeflow-test-{uuid.uuid4()}") if p)
    checks = await _bucket_checks(client, where, key)
    log.info(
        "destination_test",
        kind=inp.kind,
        host=where.host,
        **{c.check: c.outcome for c in checks},
    )
    return DestinationTestOutput(checks=checks)


async def _bucket_checks(client: BucketClient, where: Endpoint, key: str) -> list[CheckResult]:
    deadline = asyncio.get_running_loop().time() + TEST_SECONDS
    try:
        async with asyncio.timeout_at(deadline):
            reply = await client.put(key, b"", 0, "application/octet-stream")
    except egress.EgressBlocked as exc:
        return [_failed("address", str(exc))]
    except Exception as exc:
        return [_passed("address"), _failed("tls", describe(exc, where, "write")[1])]
    connected = [_passed("address"), _passed("tls")]
    if not reply.ok:
        return [*connected, _failed("write", describe(Refused(reply), where, "write")[1])]

    try:
        async with asyncio.timeout_at(deadline):
            reply = await client.delete(key)
        if not (reply.ok or reply.missing_key):
            raise Refused(reply)
    except Exception as exc:
        _, message, final = describe(exc, where, "delete")
        if final:
            message = f"delete refused — rollback to this bucket will fail; left `{key}` ({message})"
        else:
            message = f"delete failed — left `{key}` ({message})"
        return [*connected, _passed("write"), _warning("delete", message)]
    return [*connected, _passed("write"), _passed("delete")]


@activity.defn(name=TEST_SMTP_ACTIVITY)
async def test_smtp(inp: DestinationTestInput) -> DestinationTestOutput:
    """Address check, TLS (STARTTLS or implicit), EHLO, AUTH, QUIT. Sends no mail."""
    if inp.kind != "smtp":
        raise final_error("DestinationKind", f"not an smtp destination: {inp.kind}")
    address, secret = parse(inp.kind, inp.address, inp.encrypted_secret)
    assert isinstance(address, SmtpAddress) and isinstance(secret, LoginSecret)
    try:
        smtp.tls_context(address.ca_certificate)
    except ValueError as exc:
        return DestinationTestOutput(checks=[_failed("address", str(exc))])

    passed: list[str] = []

    async def session(sock) -> None:
        s = smtp.Session(sock, address, secret, TEST_SECONDS)
        try:
            await s.connect()
            passed.append("tls")
            await s.login()
            passed.append("login")
        finally:
            await s.close()

    try:
        async with asyncio.timeout(TEST_SECONDS):
            await pinned_tcp.pinned(address.host, address.port, session)
    except egress.EgressBlocked as exc:
        checks = [_failed("address", str(exc))]
    except Exception as exc:
        _, message, _ = smtp.describe(exc, address.host, address.port, "test")
        checks = [_passed("address"), *(_passed(c) for c in passed)]
        checks.append(_failed("login" if passed else "tls", message))
    else:
        checks = [_passed("address"), _passed("tls"), _passed("login")]
    log.info(
        "destination_test", kind=inp.kind, host=address.host, **{c.check: c.outcome for c in checks}
    )
    return DestinationTestOutput(checks=checks)
