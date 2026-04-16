import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException, status


def _validate_no_ssrf(url: str) -> None:
    """Core SSRF check — raises ValueError if the URL targets a private/internal address.

    Used by both the HTTP route layer (wrapped in validate_no_ssrf below) and the
    webhook delivery loop, where HTTPException is not appropriate.
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

    Thin adapter over _validate_no_ssrf. Use _validate_no_ssrf directly in
    non-HTTP contexts (e.g. background tasks) where you handle ValueError yourself.
    """
    try:
        _validate_no_ssrf(url)
    except ValueError as exc:
        msg = str(exc)
        if "no hostname" in msg or "could not be resolved" in msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=msg
            ) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc
