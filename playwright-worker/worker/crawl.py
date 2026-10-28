"""
The crawl queue's activities (E.4): robots.txt and sitemap fetches, and link
extraction from a stored page. No browser, no DB.

Replaces coordinator/sitemap.py, which fetched with aiohttp (BUG-006) and followed whatever the
target's robots.txt named. Here a fetch never follows a redirect off its host and never decides what
to fetch next: every sitemap URL — a Sitemap: line, an index's child, a redirect's Location — goes
back to the workflow, which vets it (SSRF + scope) first (ADR-009 13d, ADR-010 §2).
"""

import xml.etree.ElementTree as ET
import zlib
from urllib.parse import urljoin, urlsplit

import httpx
import structlog
from miniopy_async import Minio
from temporalio import activity

from .contracts import (
    DISCOVER_SITEMAPS_ACTIVITY,
    EXTRACT_LINKS_ACTIVITY,
    FETCH_SITEMAP_ACTIVITY,
    DiscoverSitemapsInput,
    DiscoverSitemapsOutput,
    ExtractLinksInput,
    ExtractLinksOutput,
    FetchSitemapInput,
    FetchSitemapOutput,
)
from .link_extractor import extract_links

log = structlog.get_logger()

# Direct, never through a job's proxy (robots.py's rule); redirects are this module's to judge.
_CLIENT = httpx.AsyncClient(timeout=10.0, follow_redirects=False)

ROBOTS_MAX_BYTES = 512 * 1024
# The sitemap protocol's own limit is 50 MB uncompressed; this is ours.
SITEMAP_MAX_BYTES = 10 * 1024 * 1024
# Activity results ride in workflow history: keep each well under Temporal's 2 MiB payload limit.
PAYLOAD_MAX_CHARS = 256 * 1024
# robots.txt may move to another scheme on its own host (http → https), nowhere else.
ROBOTS_MAX_REDIRECTS = 2

_REDIRECTS = (301, 302, 303, 307, 308)


class _TooLarge(Exception):
    pass


async def _get(url: str, max_bytes: int) -> tuple[int, str | None, bytes]:
    """(status, absolute Location or None, body up to max_bytes). Raises _TooLarge past it."""
    async with _CLIENT.stream("GET", url) as resp:
        location = resp.headers.get("location")
        if resp.status_code in _REDIRECTS and location:
            return resp.status_code, urljoin(url, location), b""
        if resp.status_code != 200:
            return resp.status_code, None, b""
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise _TooLarge(url)
        return 200, None, bytes(body)


async def _robots_txt(seed_url: str) -> str | None:
    parts = urlsplit(seed_url)
    url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    for _ in range(ROBOTS_MAX_REDIRECTS + 1):
        status, location, body = await _get(url, ROBOTS_MAX_BYTES)
        if status == 200:
            return body.decode("utf-8", errors="replace")
        if location is None or urlsplit(location).hostname != parts.hostname:
            return None
        url = location
    return None


def sitemap_lines(robots_txt: str, robots_url: str) -> list[str]:
    found: list[str] = []
    for line in robots_txt.splitlines():
        field, sep, value = line.split("#", 1)[0].partition(":")
        if sep and field.strip().lower() == "sitemap" and value.strip():
            found.append(urljoin(robots_url, value.strip()))
    return list(dict.fromkeys(found))


@activity.defn(name=DISCOVER_SITEMAPS_ACTIVITY)
async def discover_sitemaps(input: DiscoverSitemapsInput) -> DiscoverSitemapsOutput:
    """The seed's robots.txt Sitemap: lines; its /sitemap.xml when there are none. Not fetched."""
    parts = urlsplit(input.seed_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    try:
        robots = await _robots_txt(input.seed_url)
    except (httpx.HTTPError, _TooLarge) as exc:
        log.warning(
            "sitemap_robots_fetch_failed", seed_url=input.seed_url, error=str(exc)
        )
        robots = None
    found = sitemap_lines(robots, f"{origin}/robots.txt") if robots else []
    return DiscoverSitemapsOutput(sitemaps=found or [f"{origin}/sitemap.xml"])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _decompress(data: bytes) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = inflater.decompress(data, SITEMAP_MAX_BYTES + 1)
    if len(out) > SITEMAP_MAX_BYTES:
        raise _TooLarge("decompressed sitemap")
    return out


def parse_sitemap(data: bytes, max_entries: int) -> FetchSitemapOutput:
    """A urlset's page URLs or an index's child sitemaps — never one read as the other (v1's
    ``.//loc`` crawled index children as pages). A plain-text sitemap is a urlset."""
    if data[:2] == b"\x1f\x8b":
        data = _decompress(data)
    text = data.decode("utf-8", errors="replace").lstrip("﻿ \t\r\n")
    if not text.startswith("<"):
        locs = [line.strip() for line in text.splitlines() if line.strip()]
        return _capped("urlset", locs, max_entries)
    # No DTDs: entity expansion is the XML bomb.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return FetchSitemapOutput(kind="unavailable")
    root = ET.fromstring(text)
    kind, child = {
        "urlset": ("urlset", "url"),
        "sitemapindex": ("index", "sitemap"),
    }.get(_local(root.tag), (None, None))
    if kind is None:
        return FetchSitemapOutput(kind="unavailable")
    locs = [
        loc.text.strip()
        for entry in root
        if _local(entry.tag) == child
        for loc in entry
        if _local(loc.tag) == "loc" and loc.text and loc.text.strip()
    ]
    return _capped(kind, locs, max_entries)


def _capped(kind: str, locs: list[str], max_entries: int) -> FetchSitemapOutput:
    locs = list(dict.fromkeys(locs))
    kept: list[str] = []
    chars = 0
    for loc in locs[:max_entries]:
        chars += len(loc)
        if chars > PAYLOAD_MAX_CHARS:
            break
        kept.append(loc)
    return FetchSitemapOutput(kind=kind, locs=kept, truncated=len(kept) < len(locs))


@activity.defn(name=FETCH_SITEMAP_ACTIVITY)
async def fetch_sitemap(input: FetchSitemapInput) -> FetchSitemapOutput:
    """One sitemap file. A redirect is handed back unfollowed; anything unreadable is
    ``unavailable`` — the crawl goes on without it."""
    try:
        status, location, body = await _get(input.url, SITEMAP_MAX_BYTES)
        if location is not None:
            return FetchSitemapOutput(kind="redirect", location=location)
        if status != 200:
            return FetchSitemapOutput(kind="unavailable")
        out = parse_sitemap(body, input.max_entries)
    except (httpx.HTTPError, _TooLarge, ET.ParseError, zlib.error) as exc:
        log.warning(
            "sitemap_unavailable", url=input.url, error=str(exc) or type(exc).__name__
        )
        return FetchSitemapOutput(kind="unavailable")
    if out.truncated:
        log.info("sitemap_truncated", url=input.url, kept=len(out.locs))
    return out


class CrawlActivities:
    def __init__(self, minio: Minio) -> None:
        self.minio = minio

    @activity.defn(name=EXTRACT_LINKS_ACTIVITY)
    async def extract_links(self, input: ExtractLinksInput) -> ExtractLinksOutput:
        """Same-origin links from a stored page; the crawl's filters are admission's to apply."""
        bucket, _, key = input.path.partition("/")
        response = await self.minio.get_object(bucket, key)
        try:
            html = (await response.read()).decode("utf-8", errors="replace")
        finally:
            response.close()
        out = _capped("urlset", extract_links(html, input.base_url, None, None), 10_000)
        if out.truncated:
            log.info("links_truncated", path=input.path, kept=len(out.locs))
        return ExtractLinksOutput(links=out.locs)
