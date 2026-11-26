"""Egress (BUG-010 part 2): everything this worker fetches for a user goes through the egress proxy
(Smokescreen), which resolves each name once, refuses an address that is not public unicast and
connects to the address it checked. The one connection it cannot carry is Chrome's to a job's own
proxy: that address is checked here and handed to Chrome as an IP.

``is_public_address`` is a copy of the api's ``core/security.is_public_address``;
``contracts/test_egress.py`` runs one table against every copy. Imports nothing from this package.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

# How the egress proxy names a refusal on its 407 (lower-case, as Playwright reports headers).
REFUSAL_HEADER = "x-smokescreen-error"
# Chrome's error for a 407 on CONNECT; only the egress proxy sends one when the job has no proxy.
CONNECT_REFUSED = "net::ERR_PROXY_AUTH_UNSUPPORTED"

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Public unicast only (BUG-024); a NAT64 address is judged as its IPv4 address."""
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


class AddressRefused(Exception):
    """A fetch's address is not public. Final: a retry resolves the same name again.

    ``str()`` names the host only; ``reason`` (the proxy's text, which names the resolved internal
    address) is for logs.
    """

    def __init__(self, host: str, reason: str = "") -> None:
        super().__init__(f"address refused: {host} is not a public address")
        self.host = host
        self.reason = reason


async def pinned_proxy_server(proxy_url: str) -> str:
    """The job proxy's ``scheme://host:port`` for Chrome, with the host resolved once and replaced by
    the first address — every address must be public, or AddressRefused. An https proxy keeps its
    name (TLS checks the certificate against it), so it is checked but not pinned.
    """
    parts = urlsplit(proxy_url)
    host = parts.hostname
    if not host:
        raise ValueError("proxy URL has no host")
    port = parts.port
    try:
        addresses = [ipaddress.ip_address(a) for a in await _lookup(host, port)]
    except socket.gaierror as exc:
        raise ValueError(f"proxy host {host} could not be resolved") from exc
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise AddressRefused(
            host, f"proxy {host} resolves to {', '.join(map(str, addresses))}"
        )
    target = host if parts.scheme == "https" else str(addresses[0])
    if ":" in target:
        target = f"[{target}]"
    netloc = f"{target}:{port}" if port else target
    return urlunsplit((parts.scheme, netloc, "", "", ""))


async def _lookup(host: str, port: int | None) -> list[str]:
    answers = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return [a[4][0].split("%", 1)[0] for a in answers]
