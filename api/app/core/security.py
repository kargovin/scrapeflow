import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException, status


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
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
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
