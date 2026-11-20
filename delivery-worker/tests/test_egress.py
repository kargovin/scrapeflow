"""The sinks' egress module: one lookup, every answer checked, the connection pinned to a checked address.

The address rule and the rebinding case are pinned against the api's copy in contracts/test_egress.py.
"""

import asyncio
import re
import socket
import ssl

import pytest

from tests.tls import tls_server
from worker import egress

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"


class Lookups:
    """getaddrinfo that answers each call with the next list in turn."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple] = []

    def __call__(self, host, port, *args, **kwargs):
        self.calls.append((host, port))
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in self.answers.pop(0)
        ]


async def test_vetted_addresses_refuses_a_private_answer_as_egress_blocked(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, "10.0.0.5"]))
    with pytest.raises(egress.EgressBlocked, match=re.escape("private address: 10.0.0.5")):
        await egress.vetted_addresses("hooks.example.com", 443)


async def test_vetted_addresses_returns_each_public_answer_once(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2, PUBLIC]))
    assert await egress.vetted_addresses("hooks.example.com", 443) == [PUBLIC, PUBLIC_2]


# --- a real handshake against a local server ---------------------------------------------------


@pytest.fixture
def loopback_is_vetted(monkeypatch):
    # Loopback is private: stand in for the check so the handshake can reach the local server.
    calls: list[tuple] = []

    def resolve(host, port):
        calls.append((host, port))
        return ["127.0.0.1"]

    monkeypatch.setattr(egress, "resolve_public", resolve)
    return calls


async def test_the_handshake_reaches_the_address_presenting_the_name(loopback_is_vetted):
    async with tls_server("hooks.example.test") as (port, context, seen):
        ip = await egress.tls_handshake("hooks.example.test", port, context)
    assert ip == "127.0.0.1"
    assert seen == ["hooks.example.test"]
    assert loopback_is_vetted == [("hooks.example.test", port)]


async def test_a_certificate_for_another_name_is_a_mismatch(loopback_is_vetted):
    async with tls_server("other.example.test") as (port, context, _):
        with pytest.raises(
            egress.CertificateMismatch, match=re.escape("certificate not valid for hooks.example.test")
        ):
            await egress.tls_handshake("hooks.example.test", port, context)


async def test_an_untrusted_certificate_is_refused_by_the_default_context(loopback_is_vetted):
    async with tls_server("hooks.example.test") as (port, _, _):
        with pytest.raises(egress.CertificateMismatch):
            await egress.tls_handshake("hooks.example.test", port)


async def test_a_server_that_does_not_speak_tls_fails_the_handshake(loopback_is_vetted):
    async def plain(reader, writer):
        writer.write(b"220 not tls\r\n" * 50)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(plain, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        with pytest.raises(ssl.SSLError):
            await egress.tls_handshake("hooks.example.test", port)


# --- several addresses ------------------------------------------------------------------------


def _opener(outcomes):
    tried: list[str] = []

    async def _open(ip, port, host, context, timeout):
        tried.append(ip)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    return _open, tried


@pytest.mark.parametrize("first", [ConnectionRefusedError(111, "refused"), TimeoutError()])
async def test_a_refused_or_timed_out_address_moves_on_to_the_next(monkeypatch, first):
    _open, tried = _opener([first, None])
    monkeypatch.setattr(egress, "_open", _open)
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2]))
    assert await egress.tls_handshake("hooks.example.com", 443) == PUBLIC_2
    assert tried == [PUBLIC, PUBLIC_2]


async def test_the_last_address_refusing_is_the_attempts_error(monkeypatch):
    _open, _ = _opener([ConnectionRefusedError(111, "a"), ConnectionRefusedError(111, "b")])
    monkeypatch.setattr(egress, "_open", _open)
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2]))
    with pytest.raises(ConnectionRefusedError):
        await egress.tls_handshake("hooks.example.com", 443)


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"), egress.CertificateMismatch),
        (ssl.SSLError("handshake failure"), ssl.SSLError),
    ],
)
async def test_a_tls_failure_tries_no_other_address(monkeypatch, error, raised):
    _open, tried = _opener([error, None])
    monkeypatch.setattr(egress, "_open", _open)
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2]))
    with pytest.raises(raised):
        await egress.tls_handshake("hooks.example.com", 443)
    assert tried == [PUBLIC]


async def test_a_private_address_is_refused_before_any_connection(monkeypatch):
    _open, tried = _opener([None])
    monkeypatch.setattr(egress, "_open", _open)
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups(["169.254.169.254"]))
    with pytest.raises(egress.EgressBlocked):
        await egress.tls_handshake("metadata.example", 443)
    assert tried == []
