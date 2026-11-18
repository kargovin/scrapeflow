"""Outbound webhook POSTs: resolve once, check every address, connect to a checked address (ADR-015 §3).

The wire contract is the job lane's, byte for byte: HMAC-SHA256 over the raw body,
``X-ScrapeFlow-Signature: sha256=<hex>`` always sent (``b""`` with no secret), 10 s per POST; the caller
reads ``< 300`` as delivered. Both webhook senders call ``send_signed_post`` and keep their own outcome
handling. ``delivery-worker/worker/egress.py`` holds the sinks' copy of the address check (M).
"""

import asyncio
import hashlib
import hmac
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.security import is_public_address

POST_TIMEOUT_SECONDS = 10.0


class EgressBlocked(Exception):
    """The address check refused the URL. Final; not an attempt."""


class CertificateMismatch(Exception):
    """The server's certificate is not valid for the URL's host. Final."""


def sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def resolve_public(host: str, port: int) -> list[str]:
    """One lookup; every answer must be public unicast, or the whole name is refused.

    Raises ValueError with the message the senders record after ``ssrf_blocked: ``.
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


async def _post(url: str, body: bytes, headers: dict[str, str], extensions: dict) -> int:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            content=body,
            headers=headers,
            timeout=POST_TIMEOUT_SECONDS,
            extensions=extensions,
        )
    return response.status_code


def _pinned(parts, ip: str) -> str:
    # Userinfo stays: httpx sends it as Basic auth, as it did before the pin.
    userinfo, at, _ = parts.netloc.rpartition("@")
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{userinfo}{at}{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


def _tls_error(exc: BaseException) -> ssl.SSLError | None:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLError):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


async def send_signed_post(url: str, body: bytes, secret: bytes) -> int:
    """POSTs ``body`` to ``url`` at an address that passed the check; returns the status code.

    Each vetted address is tried in turn after a refused or timed-out connect, never after a TLS
    failure. Raises EgressBlocked or CertificateMismatch (final), or httpx.HTTPError.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise EgressBlocked("URL has no hostname")
    https = parts.scheme == "https"
    port = parts.port or (443 if https else 80)
    try:
        addresses = await asyncio.get_running_loop().run_in_executor(
            None, resolve_public, host, port
        )
    except ValueError as exc:
        raise EgressBlocked(str(exc)) from None

    headers = {
        "Content-Type": "application/json",
        "X-ScrapeFlow-Signature": sign(secret, body),
        # The name, not the address: virtual hosting and the certificate both key on it.
        "Host": parts.netloc.rpartition("@")[2],
    }
    extensions = {"sni_hostname": host} if https else {}
    for i, ip in enumerate(addresses):
        try:
            return await _post(_pinned(parts, ip), body, headers, extensions)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            tls = _tls_error(exc)
            if isinstance(tls, ssl.SSLCertVerificationError):
                raise CertificateMismatch(f"certificate not valid for {host}") from None
            if tls is not None or i == len(addresses) - 1:
                raise
    raise AssertionError("resolve_public returned no address")
