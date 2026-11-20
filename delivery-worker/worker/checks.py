"""The destination test's checks that run here (ADR-015 §2d). Nothing is sent and nothing is left behind."""

import ssl
from urllib.parse import urlsplit

import structlog
from temporalio import activity

from . import egress
from .contracts import (
    TEST_WEBHOOK_ACTIVITY,
    CheckResult,
    DestinationTestInput,
    DestinationTestOutput,
    WebhookSecret,
)
from .destinations import final_error, parse

log = structlog.get_logger()


def _passed(check: str) -> CheckResult:
    return CheckResult(check=check, outcome="passed")


def _failed(check: str, message: str) -> CheckResult:
    return CheckResult(check=check, outcome="failed", message=message)


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
