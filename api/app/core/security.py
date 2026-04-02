import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException, status


def _validate_no_ssrf(url: str) -> None:
    """Reject URLs that resolve to private/loopback/link-local addresses.

    Resolves the hostname via DNS so that Docker service names (redis, postgres)
    and DNS-rebinding attacks are also blocked, not just literal IP strings.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="URL has no hostname"
        )

    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="URL hostname could not be resolved",
        ) from None

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="URL resolves to a private address"
            )
