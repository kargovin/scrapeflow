"""
Sitemap discovery: fetch robots.txt → follow Sitemap: directives → parse <loc> tags.

Called only on the seed URL result (depth == 0) when ignore_sitemap is False.
Failures at any stage are logged and treated as empty (crawl proceeds without sitemap URLs).
"""

import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import aiohttp
import structlog

log = structlog.get_logger()

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


async def discover_sitemap_urls(seed_url: str) -> list[str]:
    """Return all <loc> URLs found in sitemaps referenced by the seed URL's robots.txt."""
    parsed = urlparse(seed_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    sitemap_urls: list[str] = []

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        try:
            async with session.get(robots_url) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
        except Exception as exc:
            log.warning("sitemap_robots_fetch_failed", url=robots_url, error=str(exc))
            return []

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("sitemap:"):
                sitemap_urls.append(stripped.split(":", 1)[1].strip())

    all_locs: list[str] = []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for sitemap_url in sitemap_urls:
            try:
                async with session.get(sitemap_url) as resp:
                    if resp.status != 200:
                        continue
                    xml_text = await resp.text()
                root = ET.fromstring(xml_text)
                # Try namespaced first, then without namespace (some sitemaps omit it).
                locs = root.findall(".//sm:loc", _SITEMAP_NS) or root.findall(".//loc")
                for loc in locs:
                    if loc.text and loc.text.strip() not in all_locs:
                        all_locs.append(loc.text.strip())
            except Exception as exc:
                log.warning("sitemap_xml_parse_failed", url=sitemap_url, error=str(exc))

    return all_locs
