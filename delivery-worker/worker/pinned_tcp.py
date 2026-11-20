"""A TCP connection pinned to a checked address (ADR-015 §3): the SMTP client's socket.

One lookup through ``egress``; the socket is connected to a vetted address, and the session on it does
TLS with the host as server name. Stdlib and ``.egress`` only: ``contracts/test_egress.py`` loads it.
"""

import asyncio
import socket
import ssl
from collections.abc import Awaitable, Callable
from typing import TypeVar

from . import egress

T = TypeVar("T")


async def _open(ip: str, port: int, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, (ip, port)), timeout)
    except BaseException:
        sock.close()
        raise
    return sock


async def pinned(host: str, port: int, session: Callable[[socket.socket], Awaitable[T]]) -> T:
    """Runs ``session(sock)`` on a socket connected to a vetted address of ``host``.

    Moves on to the next address only when the connect is refused or times out; anything raised by
    the session ends the attempt there (a session may already have sent). The session must surface
    a TLS failure as ``ssl.SSLError``. Raises EgressBlocked or CertificateMismatch (final),
    ssl.SSLError, OSError / TimeoutError from the last address, or what the session raised.
    """
    host = host.rstrip(".").lower()
    addresses = await egress.vetted_addresses(host, port)
    for i, ip in enumerate(addresses):
        try:
            sock = await _open(ip, port, egress.CONNECT_TIMEOUT_SECONDS)
        except (OSError, TimeoutError):
            if i == len(addresses) - 1:
                raise
            continue
        try:
            return await session(sock)
        except ssl.SSLCertVerificationError:
            raise egress.CertificateMismatch(f"certificate not valid for {host}") from None
        finally:
            sock.close()
    raise AssertionError("resolve_public returned no address")
