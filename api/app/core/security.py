import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException, status

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally reachable unicast address.

    A NAT64 address (``64:ff9b::a.b.c.d``) is judged as its IPv4 address: ``is_global`` alone calls
    ``64:ff9b::10.0.0.1`` public.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def validate_no_ssrf_core(url: str) -> None:
    """Core SSRF check — raises ValueError if the URL targets a private/internal address.

    Use this in non-HTTP contexts (e.g. background tasks) where ValueError is handled
    by the caller. HTTP route callers should use validate_no_ssrf instead.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError("URL hostname could not be resolved") from exc

    for _family, _type, _proto, _canonname, sockaddr in results:
        if not is_public_address(ipaddress.ip_address(sockaddr[0])):
            raise ValueError(f"URL resolves to a private address: {sockaddr[0]}")


def validate_no_ssrf(url: str) -> None:
    """HTTP route SSRF check — raises HTTPException for API callers.

    Thin adapter over validate_no_ssrf_core for use in FastAPI route handlers.
    """
    try:
        validate_no_ssrf_core(url)
    except ValueError as exc:
        msg = str(exc)
        if "no hostname" in msg or "could not be resolved" in msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=msg
            ) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc


def validate_proxy_url(url: str) -> None:
    """validate_no_ssrf for a job's or Scrape block's proxy, with the field named in the detail.

    A worker connects to the proxy itself, not through the egress proxy (BUG-010 part 2).
    """
    try:
        validate_no_ssrf(url)
    except HTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=f"proxy_url: {exc.detail}") from exc
