"""aiohttp pinned to a checked address (ADR-015 §3): the bucket client's connection.

One lookup through ``egress``; each request then goes out on a session whose resolver knows only the
host and the one vetted address being tried, and that sends one request. TLS presents the host and
checks the certificate against it. Imports aiohttp and ``.egress`` only:
``contracts/test_egress.py`` loads it.
"""

import socket
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from . import egress

T = TypeVar("T")


class PinnedResolver(AbstractResolver):
    """Answers for one host with one address; any other name is refused."""

    def __init__(self, host: str, ip: str) -> None:
        self._host = host
        self._ip = ip

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host.rstrip(".").lower() != self._host:
            raise OSError(f"{host} is not the checked host")
        return [
            {
                "hostname": host,
                "host": self._ip,
                "port": port,
                "family": socket.AF_INET6 if ":" in self._ip else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        return None


def _send_once() -> aiohttp.ClientMiddlewareType:
    """Lets a session send one request: a second send re-raises the first one's failure.

    aiohttp resends an idempotent request (PUT, DELETE) once after a disconnect, with no option
    to turn it off; the resend goes back through the middlewares.
    """
    failure: Exception | None = None
    sent = False

    async def middleware(
        req: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        nonlocal failure, sent
        if sent:
            raise failure or RuntimeError("a pinned session sends one request")
        sent = True
        try:
            return await handler(req)
        except Exception as exc:
            failure = exc
            raise

    return middleware


def _session(host: str, ip: str, context) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        resolver=PinnedResolver(host, ip),
        ssl=context,
        use_dns_cache=False,
        force_close=True,
    )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=egress.CONNECT_TIMEOUT_SECONDS),
        trust_env=False,
        auto_decompress=False,
        middlewares=(_send_once(),),
    )


async def _at(
    ip: str, host: str, context, attempt: Callable[[aiohttp.ClientSession], Awaitable[T]]
) -> T:
    async with _session(host, ip, context) as session:
        return await attempt(session)


async def pinned(
    host: str,
    port: int,
    attempt: Callable[[aiohttp.ClientSession], Awaitable[T]],
    context=None,
) -> T:
    """Runs ``attempt(session)`` against each vetted address of ``host`` in turn.

    ``attempt`` must send to ``https://{host}:{port}/…`` with ``allow_redirects=False`` and read
    the whole answer before returning. Moves on only after a refused or timed-out connect, never
    after a TLS failure. Raises EgressBlocked or CertificateMismatch (final), aiohttp.ClientSSLError,
    or whatever the last address's attempt raised.
    """
    host = host.rstrip(".").lower()
    addresses = await egress.vetted_addresses(host, port)
    context = context or egress.tls_context()
    for i, ip in enumerate(addresses):
        try:
            return await _at(ip, host, context, attempt)
        except aiohttp.ClientConnectorCertificateError:
            raise egress.CertificateMismatch(f"certificate not valid for {host}") from None
        except (aiohttp.ClientSSLError, aiohttp.ClientConnectorDNSError):
            # Both are ClientConnectorErrors: caught before the move-on below.
            raise
        except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError):
            if i == len(addresses) - 1:
                raise
    raise AssertionError("resolve_public returned no address")
