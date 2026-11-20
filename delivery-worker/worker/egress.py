"""Outbound connections from the sinks: resolve once, check every address, connect to a checked one.

The delivery worker's copy of ``api/app/core/egress.py`` (ADR-015 §3), kept separate on purpose: this
image must not import the api's code. ``contracts/test_egress.py`` runs one table of cases against both
copies — change them together. Self-contained (stdlib only): the contracts test loads it by path.
"""

import asyncio
import contextlib
import ipaddress
import socket
import ssl

CONNECT_TIMEOUT_SECONDS = 10.0

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class EgressBlocked(Exception):
    """The address check refused the host. Final; not an attempt."""


class CertificateMismatch(Exception):
    """The server's certificate is not valid for the host. Final."""


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally reachable unicast address.

    A NAT64 address (``64:ff9b::a.b.c.d``) is judged as its IPv4 address: ``is_global`` alone calls
    ``64:ff9b::10.0.0.1`` public.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int) -> list[str]:
    """One lookup; every answer must be public unicast, or the whole name is refused.

    Raises ValueError with the same messages as the api's copy.
    """
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("URL hostname could not be resolved") from exc
    addresses: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in results:
        ip = sockaddr[0]
        if not is_public_address(ipaddress.ip_address(ip)):
            raise ValueError(f"URL resolves to a private address: {ip}")
        if ip not in addresses:
            addresses.append(ip)
    return addresses


async def vetted_addresses(host: str, port: int) -> list[str]:
    """``resolve_public`` off the event loop; a refusal raises EgressBlocked."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, resolve_public, host, port)
    except ValueError as exc:
        raise EgressBlocked(str(exc)) from None


def tls_context() -> ssl.SSLContext:
    """Verifies the chain and the name. Clients take this, never an unverified context."""
    return ssl.create_default_context()


async def _open(ip: str, port: int, host: str, context: ssl.SSLContext, timeout: float) -> None:
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(
            ip, port, ssl=context, server_hostname=host, ssl_handshake_timeout=timeout
        ),
        timeout,
    )
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def tls_handshake(host: str, port: int, context: ssl.SSLContext | None = None) -> str:
    """Completes a TLS handshake for ``host`` at an address that passed the check; sends nothing.

    Returns the address reached. Each vetted address is tried in turn after a refused or timed-out
    connect, never after a TLS failure. Raises EgressBlocked or CertificateMismatch (final),
    ssl.SSLError, or OSError / TimeoutError from the last address.
    """
    addresses = await vetted_addresses(host, port)
    context = context or tls_context()
    for i, ip in enumerate(addresses):
        try:
            await _open(ip, port, host, context, CONNECT_TIMEOUT_SECONDS)
            return ip
        except ssl.SSLCertVerificationError:
            raise CertificateMismatch(f"certificate not valid for {host}") from None
        except ssl.SSLError:
            raise
        except (OSError, TimeoutError):
            # ssl.SSLError is an OSError: it must be caught above.
            if i == len(addresses) - 1:
                raise
    raise AssertionError("resolve_public returned no address")
