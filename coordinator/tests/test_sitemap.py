"""
Unit tests for coordinator/sitemap.py.

aiohttp.ClientSession is mocked so no network calls are made.
Each test controls the HTTP responses the mock session returns.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from coordinator.sitemap import discover_sitemap_urls

_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page1</loc></url>
  <url><loc>https://example.com/page2</loc></url>
</urlset>"""

_SITEMAP_XML_NO_NS = """<?xml version="1.0"?>
<urlset>
  <url><loc>https://example.com/page3</loc></url>
</urlset>"""


def _make_response(status: int = 200, text: str = "") -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session(*responses) -> MagicMock:
    """Return a mock session whose get() yields responses in order."""
    session = AsyncMock()
    session.get = MagicMock(side_effect=list(responses))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


async def test_discovers_urls_from_sitemap():
    """Full happy path: robots.txt has a Sitemap: directive; sitemap has two <loc> entries."""
    robots_text = "User-agent: *\nDisallow:\nSitemap: https://example.com/sitemap.xml"
    robots_resp = _make_response(200, robots_text)
    sitemap_resp = _make_response(200, _SITEMAP_XML)

    robots_session = _make_session(robots_resp)
    sitemap_session = _make_session(sitemap_resp)

    with patch(
        "coordinator.sitemap.aiohttp.ClientSession",
        side_effect=[robots_session, sitemap_session],
    ):
        urls = await discover_sitemap_urls("https://example.com")

    assert "https://example.com/page1" in urls
    assert "https://example.com/page2" in urls


def test_no_sitemap_directive_returns_empty_list():
    """robots.txt with no Sitemap: line → empty result (synchronous path check)."""
    robots_text = "User-agent: *\nDisallow: /admin"
    robots_resp = _make_response(200, robots_text)
    robots_session = _make_session(robots_resp)
    sitemap_session = _make_session()  # never called

    import asyncio

    async def run():
        with patch(
            "coordinator.sitemap.aiohttp.ClientSession",
            side_effect=[robots_session, sitemap_session],
        ):
            return await discover_sitemap_urls("https://example.com")

    urls = asyncio.get_event_loop().run_until_complete(run())
    assert urls == []


async def test_robots_non_200_returns_empty():
    """A non-200 robots.txt response produces an empty list immediately."""
    robots_resp = _make_response(404)
    robots_session = _make_session(robots_resp)

    with patch(
        "coordinator.sitemap.aiohttp.ClientSession",
        side_effect=[robots_session, _make_session()],
    ):
        urls = await discover_sitemap_urls("https://example.com")

    assert urls == []


async def test_robots_fetch_exception_returns_empty():
    """Network error fetching robots.txt → empty result, no exception raised."""
    session = AsyncMock()
    session.get = MagicMock(side_effect=Exception("connection refused"))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "coordinator.sitemap.aiohttp.ClientSession",
        side_effect=[session, _make_session()],
    ):
        urls = await discover_sitemap_urls("https://example.com")

    assert urls == []


async def test_sitemap_xml_without_namespace_parsed():
    """Sitemaps that omit the xmlns declaration are still parsed via fallback."""
    robots_text = "Sitemap: https://example.com/sitemap.xml"
    robots_resp = _make_response(200, robots_text)
    sitemap_resp = _make_response(200, _SITEMAP_XML_NO_NS)
    robots_session = _make_session(robots_resp)
    sitemap_session = _make_session(sitemap_resp)

    with patch(
        "coordinator.sitemap.aiohttp.ClientSession",
        side_effect=[robots_session, sitemap_session],
    ):
        urls = await discover_sitemap_urls("https://example.com")

    assert "https://example.com/page3" in urls


async def test_sitemap_fetch_error_skipped_gracefully():
    """If the sitemap XML fetch fails, discovery continues without raising."""
    robots_text = "Sitemap: https://example.com/sitemap.xml"
    robots_resp = _make_response(200, robots_text)

    bad_sitemap = AsyncMock()
    bad_sitemap.get = MagicMock(side_effect=Exception("timeout"))
    bad_sitemap.__aenter__ = AsyncMock(return_value=bad_sitemap)
    bad_sitemap.__aexit__ = AsyncMock(return_value=False)

    robots_session = _make_session(robots_resp)

    with patch(
        "coordinator.sitemap.aiohttp.ClientSession",
        side_effect=[robots_session, bad_sitemap],
    ):
        urls = await discover_sitemap_urls("https://example.com")

    assert urls == []
