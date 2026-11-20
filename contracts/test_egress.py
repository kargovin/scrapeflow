"""One table of egress cases against both copies of the address check (ADR-015 §3, §11).

``api/app/core/egress.py`` (both webhook senders) and ``delivery-worker/worker/egress.py`` (the sinks)
are a deliberate duplicate: the delivery image must not import the api's code. Every row here runs
against both; a change to one copy that the other does not share fails this file.

Run with the contracts suite (docker-api image, repo root mounted) — see test_activity_contract.py.
"""

import asyncio
import importlib
import importlib.util
import ipaddress
import socket
import ssl
import sys
from pathlib import Path

import aiohttp
import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent

# The api's copy imports app.core.security: the working tree's, not the image's baked app.
sys.path.insert(0, str(REPO / "api"))
from app.core import egress as api_egress  # noqa: E402

# The delivery worker's package under another name, so its relative imports resolve without its
# settings: egress and pinned_http import nothing else of it.
_pkg_spec = importlib.util.spec_from_file_location(
    "delivery_worker_egress",
    REPO / "delivery-worker/worker/__init__.py",
    submodule_search_locations=[str(REPO / "delivery-worker/worker")],
)
_pkg = importlib.util.module_from_spec(_pkg_spec)
sys.modules["delivery_worker_egress"] = _pkg
_pkg_spec.loader.exec_module(_pkg)
delivery_egress = importlib.import_module("delivery_worker_egress.egress")
delivery_pinned = importlib.import_module("delivery_worker_egress.pinned_http")

COPIES = {"api": api_egress, "delivery": delivery_egress}

# Each pinned client: the api's webhook POST, the delivery worker's TLS handshake (webhook test)
# and its aiohttp session (bucket sink).
CLIENTS = {"api": api_egress, "delivery": delivery_egress, "delivery-bucket": delivery_egress}

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"

# --- the address rule: public unicast only (BUG-024), NAT64 judged as its IPv4 address -------

REFUSED = [
    "100.64.0.1",  # BUG-024: shared address space (carrier-grade NAT)
    "100.127.255.254",
    "100.100.100.200",
    "224.0.0.1",  # BUG-024: multicast
    "239.1.1.1",
    "ff0e::1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.20",
    "127.0.0.1",
    "169.254.169.254",
    "0.0.0.0",
    "255.255.255.255",
    "198.18.0.1",
    "203.0.113.5",
    "::1",
    "fd00::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:100.64.0.1",
    "::ffff:224.0.0.1",
    "64:ff9b::a00:1",  # NAT64 of 10.0.0.1: is_global alone calls it public
    "64:ff9b::6440:1",  # NAT64 of 100.64.0.1
    "64:ff9b::e000:1",  # NAT64 of 224.0.0.1
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "2002:a00:1::",
]

ADMITTED = [
    "93.184.216.34",
    "8.8.8.8",
    "11.222.33.10",  # the compose gate fixtures' subnet
    "2606:4700::1111",
    "::ffff:8.8.8.8",
    "64:ff9b::808:808",  # NAT64 of 8.8.8.8
]

ADDRESS_CASES = [(a, False) for a in REFUSED] + [(a, True) for a in ADMITTED]


@pytest.mark.parametrize("copy", sorted(COPIES))
@pytest.mark.parametrize(("address", "public"), ADDRESS_CASES)
def test_the_address_rule(copy, address, public):
    assert COPIES[copy].is_public_address(ipaddress.ip_address(address)) is public


# --- one lookup: answers, refusals and their messages -----------------------------------------


class Lookups:
    """getaddrinfo that answers each call with the next list in turn."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple] = []

    def __call__(self, host, port, *args, **kwargs):
        self.calls.append((host, port))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in answer
        ]


LOOKUP_CASES = {
    "each public answer once, in order": ([[PUBLIC, PUBLIC_2, PUBLIC]], [PUBLIC, PUBLIC_2]),
    "one private answer refuses the name": (
        [[PUBLIC, "10.0.0.5"]],
        "URL resolves to a private address: 10.0.0.5",
    ),
    "CGNAT refused": ([["100.64.0.1"]], "URL resolves to a private address: 100.64.0.1"),
    "NAT64 of a private address refused": (
        [["64:ff9b::a00:5"]],
        "URL resolves to a private address: 64:ff9b::a00:5",
    ),
    "NAT64 of a public address admitted": ([["64:ff9b::808:808"]], ["64:ff9b::808:808"]),
    "a name that does not resolve": (
        [socket.gaierror("nope")],
        "URL hostname could not be resolved",
    ),
    # Public on the first lookup, private on any later one: there is no later one.
    "rebinding": ([[PUBLIC], ["10.0.0.5"]], [PUBLIC]),
}


@pytest.mark.parametrize("copy", sorted(COPIES))
@pytest.mark.parametrize("case", sorted(LOOKUP_CASES))
def test_resolve_public(monkeypatch, copy, case):
    answers, expected = LOOKUP_CASES[case]
    module = COPIES[copy]
    lookups = Lookups(*answers)
    monkeypatch.setattr(module.socket, "getaddrinfo", lookups)

    if isinstance(expected, str):
        with pytest.raises(ValueError) as exc:
            module.resolve_public("hooks.example.com", 443)
        assert str(exc.value) == expected
    else:
        assert module.resolve_public("hooks.example.com", 443) == expected
    assert lookups.calls == [("hooks.example.com", 443)]


# --- the connection: pinned to the checked address --------------------------------------------
#
# Each client's connect seam (the api's httpx POST, the delivery worker's TLS open and its aiohttp
# session) is replaced by a recorder that plays one outcome per address tried. Outcomes are named,
# and each client raises its own form of them.


def _api_error(outcome: str) -> BaseException:
    def chained(cause):
        try:
            raise httpx.ConnectError("tls") from cause
        except httpx.ConnectError as exc:
            return exc

    return {
        "refused": httpx.ConnectError("[Errno 111] Connection refused"),
        "timeout": httpx.ConnectTimeout("timed out"),
        "certificate": chained(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")),
        "tls": chained(ssl.SSLError("handshake failure")),
    }[outcome]


def _delivery_error(outcome: str) -> BaseException:
    return {
        "refused": ConnectionRefusedError(111, "Connection refused"),
        "timeout": TimeoutError(),
        "certificate": ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"),
        "tls": ssl.SSLError("handshake failure"),
    }[outcome]


_KEY = aiohttp.client_reqrep.ConnectionKey("hooks.example.com", 443, True, True, None, None, None)


def _bucket_error(outcome: str) -> BaseException:
    return {
        "refused": aiohttp.ClientConnectorError(_KEY, ConnectionRefusedError(111, "refused")),
        "timeout": aiohttp.ConnectionTimeoutError("timed out"),
        "certificate": aiohttp.ClientConnectorCertificateError(
            _KEY, ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")
        ),
        "tls": aiohttp.ClientConnectorSSLError(_KEY, ssl.SSLError("handshake failure")),
    }[outcome]


def _connect(monkeypatch, copy: str, outcomes: list[str]):
    """Patches the copy's seam; returns (connect(host), addresses tried)."""
    tried: list[str] = []
    plays = list(outcomes)

    if copy == "api":

        async def _post(url, body, headers, extensions):
            tried.append(httpx.URL(url).host)
            assert headers["Host"] == "hooks.example.com"
            assert extensions == {"sni_hostname": "hooks.example.com"}
            outcome = plays.pop(0)
            if outcome != "ok":
                raise _api_error(outcome)
            return 204

        monkeypatch.setattr(api_egress, "_post", _post)

        def connect():
            return api_egress.send_signed_post("https://hooks.example.com/in", b"{}", b"")

    elif copy == "delivery-bucket":

        async def _at(ip, host, context, attempt):
            tried.append(ip)
            assert host == "hooks.example.com"
            outcome = plays.pop(0)
            if outcome != "ok":
                raise _bucket_error(outcome)
            return await attempt(None)

        monkeypatch.setattr(delivery_pinned, "_at", _at)

        async def _attempt(session):
            return 204

        def connect():
            return delivery_pinned.pinned("hooks.example.com", 443, _attempt)

    else:

        async def _open(ip, port, host, context, timeout):
            tried.append(ip)
            assert host == "hooks.example.com"
            outcome = plays.pop(0)
            if outcome != "ok":
                raise _delivery_error(outcome)

        monkeypatch.setattr(delivery_egress, "_open", _open)

        def connect():
            return delivery_egress.tls_handshake("hooks.example.com", 443)

    return connect, tried


# (addresses, outcome per address tried) -> (addresses tried, error class name or None)
CONNECT_CASES = {
    "the checked address is the one reached": ([PUBLIC], ["ok"], [PUBLIC], None),
    "refused moves on to the next": ([PUBLIC, PUBLIC_2], ["refused", "ok"], [PUBLIC, PUBLIC_2], None),
    "timed out moves on to the next": (
        [PUBLIC, PUBLIC_2],
        ["timeout", "ok"],
        [PUBLIC, PUBLIC_2],
        None,
    ),
    "the last refusal is the attempt's error": (
        [PUBLIC, PUBLIC_2],
        ["refused", "refused"],
        [PUBLIC, PUBLIC_2],
        "connect",
    ),
    "a certificate for another name is final": (
        [PUBLIC, PUBLIC_2],
        ["certificate"],
        [PUBLIC],
        "CertificateMismatch",
    ),
    "another TLS failure tries no other address": (
        [PUBLIC, PUBLIC_2],
        ["tls"],
        [PUBLIC],
        "tls",
    ),
    "a private answer is never reached": ([PUBLIC, "10.0.0.5"], [], [], "EgressBlocked"),
}


def _is(copy: str, kind: str, exc: BaseException) -> bool:
    module = CLIENTS[copy]
    if kind == "CertificateMismatch":
        return isinstance(exc, module.CertificateMismatch) and (
            str(exc) == "certificate not valid for hooks.example.com"
        )
    if kind == "EgressBlocked":
        return isinstance(exc, module.EgressBlocked) and str(exc) == (
            "URL resolves to a private address: 10.0.0.5"
        )
    if kind == "connect":
        if copy == "delivery-bucket":
            return isinstance(exc, aiohttp.ClientConnectorError) and not isinstance(
                exc, aiohttp.ClientSSLError
            )
        return isinstance(exc, httpx.ConnectError if copy == "api" else ConnectionRefusedError)
    if kind == "tls":
        tls = {"api": httpx.ConnectError, "delivery": ssl.SSLError}.get(copy, aiohttp.ClientSSLError)
        return isinstance(exc, tls) and not isinstance(exc, module.CertificateMismatch)
    raise AssertionError(kind)


@pytest.mark.parametrize("copy", sorted(CLIENTS))
@pytest.mark.parametrize("case", sorted(CONNECT_CASES))
def test_connect(monkeypatch, copy, case):
    answers, outcomes, expected_tried, error = CONNECT_CASES[case]
    monkeypatch.setattr(CLIENTS[copy].socket, "getaddrinfo", Lookups(answers))
    connect, tried = _connect(monkeypatch, copy, outcomes)

    if error is None:
        asyncio.run(connect())
    else:
        with pytest.raises(Exception) as exc:
            asyncio.run(connect())
        assert _is(copy, error, exc.value), repr(exc.value)
    assert tried == expected_tried


@pytest.mark.parametrize("copy", sorted(CLIENTS))
def test_a_name_that_rebinds_to_a_private_address_is_never_reached(monkeypatch, copy):
    # PRD-017's check: public at the first lookup, private at the second — and only one lookup.
    lookups = Lookups([PUBLIC], ["10.0.0.5"])
    monkeypatch.setattr(CLIENTS[copy].socket, "getaddrinfo", lookups)
    connect, tried = _connect(monkeypatch, copy, ["ok"])

    asyncio.run(connect())

    assert tried == [PUBLIC]
    assert len(lookups.calls) == 1
