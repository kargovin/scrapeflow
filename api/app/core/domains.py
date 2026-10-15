import ipaddress
from urllib.parse import urlsplit

import tldextract

# The suffix list bundled with the pinned package (uv.lock): never fetched, never cached on disk.
_extract = tldextract.TLDExtract(
    cache_dir=None, suffix_list_urls=(), include_psl_private_domains=True
)


def registrable_domain(url: str) -> str:
    """The URL's eTLD+1 (``a.shop.co.uk`` → ``shop.co.uk``), lowercased, IDNs in punycode.

    IPs, single-label hosts and hosts that are themselves a public suffix return the host.
    """
    host = (urlsplit(url).hostname or "").rstrip(".")
    if not host:
        raise ValueError(f"no host in {url!r}")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return _extract(host).top_domain_under_public_suffix or host
