import json
import ssl

import pytest
from cryptography.fernet import Fernet
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from worker import checks, egress
from worker.config import settings
from worker.contracts import DestinationTestInput


def _input(url: str, kind: str = "webhook") -> DestinationTestInput:
    token = Fernet(settings.destination_encryption_key).encrypt(
        json.dumps({"url": url, "secret": "s"}).encode()
    )
    return DestinationTestInput(kind=kind, address={}, encrypted_secret=token.decode())


async def _run(inp: DestinationTestInput):
    out = await ActivityEnvironment().run(checks.test_webhook, inp)
    return [(c.check, c.outcome, c.message) for c in out.checks]


@pytest.fixture
def handshake(monkeypatch):
    calls: list[tuple] = []
    outcome: list[BaseException | None] = [None]

    async def fake(host, port, context=None):
        calls.append((host, port))
        if outcome[0] is not None:
            raise outcome[0]
        return "93.184.216.34"

    monkeypatch.setattr(egress, "tls_handshake", fake)
    return calls, outcome


async def test_a_reachable_https_host_passes_both_checks(handshake):
    calls, _ = handshake
    assert await _run(_input("https://hooks.example.com:8443/in?t=1")) == [
        ("address", "passed", None),
        ("tls", "passed", None),
    ]
    assert calls == [("hooks.example.com", 8443)]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (egress.CertificateMismatch("certificate not valid for hooks.example.com"),
         "certificate not valid for hooks.example.com"),
        (ssl.SSLError("handshake failure"), "TLS handshake with hooks.example.com failed (SSLError)"),
        (ConnectionRefusedError(111, "refused"),
         "could not connect to hooks.example.com:443 (ConnectionRefusedError)"),
        (TimeoutError(), "could not connect to hooks.example.com:443 (TimeoutError)"),
    ],
)
async def test_a_tls_failure_is_reported_after_a_passed_address(handshake, error, message):
    handshake[1][0] = error
    assert await _run(_input("https://hooks.example.com/in")) == [
        ("address", "passed", None),
        ("tls", "failed", message),
    ]


async def test_an_address_refusal_ends_the_test(handshake):
    handshake[1][0] = egress.EgressBlocked("URL resolves to a private address: 10.0.0.5")
    assert await _run(_input("https://hooks.example.com/in")) == [
        ("address", "failed", "URL resolves to a private address: 10.0.0.5"),
    ]


@pytest.mark.parametrize("url", ["http://hooks.example.com/in", "https:///nohost"])
async def test_a_url_that_is_not_https_with_a_host_is_refused_without_a_lookup(handshake, url):
    assert await _run(_input(url)) == [("address", "failed", "must be an https URL")]
    assert handshake[0] == []


async def test_the_wrong_kind_is_final():
    with pytest.raises(ApplicationError) as exc:
        await _run(_input("https://hooks.example.com/", kind="bucket"))
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationKind", True)
