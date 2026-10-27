"""
Unit tests for worker/crawl.py — the crawl queue's activities (E.4).

Target sites are an httpx.MockTransport: every request the activities make is recorded, so a test
can assert what was *not* fetched as well as what was.
"""

import gzip
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from worker import crawl
from worker.contracts import (
    DiscoverSitemapsInput,
    ExtractLinksInput,
    FetchSitemapInput,
)

SEED = "https://shop.example.com/"
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def _urlset(*locs: str) -> bytes:
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<?xml version="1.0"?><urlset {NS}>{body}</urlset>'.encode()


def _index(*locs: str) -> bytes:
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0"?><sitemapindex {NS}>{body}</sitemapindex>'.encode()


@pytest.fixture
def site(monkeypatch):
    """routes: url -> httpx.Response; anything else is a 404. Returns the requested URLs."""
    routes: dict[str, httpx.Response] = {}
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    monkeypatch.setattr(crawl, "_CLIENT", client)
    return routes, requested


async def _discover(seed=SEED):
    return await ActivityEnvironment().run(
        crawl.discover_sitemaps, DiscoverSitemapsInput(seed_url=seed)
    )


async def _fetch(url, max_entries=1000):
    return await ActivityEnvironment().run(
        crawl.fetch_sitemap, FetchSitemapInput(url=url, max_entries=max_entries)
    )


# ---------------------------------------------------------------------------
# DiscoverSitemaps
# ---------------------------------------------------------------------------


async def test_robots_sitemap_lines_are_returned_unfetched(site):
    routes, requested = site
    routes["https://shop.example.com/robots.txt"] = httpx.Response(
        200,
        text=(
            "User-agent: *\nDisallow: /private/\n"
            "Sitemap: https://shop.example.com/sitemap.xml\n"
            "sitemap: /relative-map.xml  # comment\n"
            "Sitemap: http://169.254.169.254/latest/meta-data/\n"
        ),
    )

    out = await _discover()

    assert out.sitemaps == [
        "https://shop.example.com/sitemap.xml",
        "https://shop.example.com/relative-map.xml",
        # Returned, not fetched: the workflow's vet refuses it (ADR-009 13d).
        "http://169.254.169.254/latest/meta-data/",
    ]
    assert requested == ["https://shop.example.com/robots.txt"]


async def test_no_sitemap_line_falls_back_to_sitemap_xml(site):
    routes, _ = site
    routes["https://shop.example.com/robots.txt"] = httpx.Response(
        200, text="User-agent: *\n"
    )
    assert (await _discover()).sitemaps == ["https://shop.example.com/sitemap.xml"]


async def test_missing_robots_falls_back_to_sitemap_xml(site):
    assert (await _discover()).sitemaps == ["https://shop.example.com/sitemap.xml"]


async def test_robots_redirect_is_followed_on_its_own_host_only(site):
    routes, requested = site
    routes["http://shop.example.com/robots.txt"] = httpx.Response(
        301, headers={"location": "https://shop.example.com/robots.txt"}
    )
    routes["https://shop.example.com/robots.txt"] = httpx.Response(
        200, text="Sitemap: https://shop.example.com/a.xml\n"
    )
    assert (await _discover("http://shop.example.com/")).sitemaps == [
        "https://shop.example.com/a.xml"
    ]

    requested.clear()
    routes["http://shop.example.com/robots.txt"] = httpx.Response(
        302, headers={"location": "http://10.0.0.5/robots.txt"}
    )
    out = await _discover("http://shop.example.com/")
    assert out.sitemaps == ["http://shop.example.com/sitemap.xml"]
    assert requested == ["http://shop.example.com/robots.txt"]


async def test_oversized_robots_is_treated_as_missing(site):
    routes, _ = site
    routes["https://shop.example.com/robots.txt"] = httpx.Response(
        200, content=b"#" * (crawl.ROBOTS_MAX_BYTES + 1)
    )
    assert (await _discover()).sitemaps == ["https://shop.example.com/sitemap.xml"]


async def test_unreachable_robots_is_treated_as_missing(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        crawl, "_CLIENT", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert (await _discover()).sitemaps == ["https://shop.example.com/sitemap.xml"]


# ---------------------------------------------------------------------------
# FetchSitemap
# ---------------------------------------------------------------------------


async def test_urlset_returns_its_page_urls(site):
    routes, _ = site
    routes["https://shop.example.com/sitemap.xml"] = httpx.Response(
        200, content=_urlset("https://shop.example.com/a", "https://shop.example.com/b")
    )
    out = await _fetch("https://shop.example.com/sitemap.xml")
    assert (out.kind, out.locs, out.truncated) == (
        "urlset",
        ["https://shop.example.com/a", "https://shop.example.com/b"],
        False,
    )


async def test_index_children_are_sitemaps_not_pages(site):
    # v1's `.//loc` would have crawled these as pages.
    routes, requested = site
    routes["https://shop.example.com/sitemap.xml"] = httpx.Response(
        200,
        content=_index(
            "https://shop.example.com/s1.xml", "https://cdn.other.net/s2.xml"
        ),
    )
    out = await _fetch("https://shop.example.com/sitemap.xml")
    assert out.kind == "index"
    assert out.locs == [
        "https://shop.example.com/s1.xml",
        "https://cdn.other.net/s2.xml",
    ]
    # Children are the workflow's to vet and ask for.
    assert requested == ["https://shop.example.com/sitemap.xml"]


async def test_redirecting_sitemap_is_not_followed(site):
    routes, requested = site
    routes["https://shop.example.com/sitemap.xml"] = httpx.Response(
        301, headers={"location": "http://127.0.0.1:8080/internal.xml"}
    )
    out = await _fetch("https://shop.example.com/sitemap.xml")
    assert (out.kind, out.location) == (
        "redirect",
        "http://127.0.0.1:8080/internal.xml",
    )
    assert requested == ["https://shop.example.com/sitemap.xml"]


async def test_relative_redirect_is_made_absolute(site):
    routes, _ = site
    routes["https://shop.example.com/sitemap.xml"] = httpx.Response(
        302, headers={"location": "/maps/main.xml"}
    )
    out = await _fetch("https://shop.example.com/sitemap.xml")
    assert out.location == "https://shop.example.com/maps/main.xml"


async def test_gzipped_sitemap_is_read(site):
    routes, _ = site
    routes["https://shop.example.com/s.xml.gz"] = httpx.Response(
        200, content=gzip.compress(_urlset("https://shop.example.com/z"))
    )
    out = await _fetch("https://shop.example.com/s.xml.gz")
    assert (out.kind, out.locs) == ("urlset", ["https://shop.example.com/z"])


async def test_gzip_bomb_is_unavailable(site):
    routes, _ = site
    routes["https://shop.example.com/s.xml.gz"] = httpx.Response(
        200, content=gzip.compress(b" " * (crawl.SITEMAP_MAX_BYTES + 10))
    )
    assert (await _fetch("https://shop.example.com/s.xml.gz")).kind == "unavailable"


async def test_doctype_is_refused(site):
    routes, _ = site
    routes["https://shop.example.com/sitemap.xml"] = httpx.Response(
        200,
        content=(
            b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa">]>'
            b"<urlset><url><loc>&a;</loc></url></urlset>"
        ),
    )
    assert (await _fetch("https://shop.example.com/sitemap.xml")).kind == "unavailable"


async def test_plain_text_sitemap_is_a_urlset(site):
    routes, _ = site
    routes["https://shop.example.com/sitemap.txt"] = httpx.Response(
        200, text="https://shop.example.com/a\n\nhttps://shop.example.com/b\n"
    )
    out = await _fetch("https://shop.example.com/sitemap.txt")
    assert out.locs == ["https://shop.example.com/a", "https://shop.example.com/b"]


async def test_entries_are_capped_at_max_entries(site):
    routes, _ = site
    routes["https://shop.example.com/sitemap.xml"] = httpx.Response(
        200, content=_urlset(*(f"https://shop.example.com/{i}" for i in range(10)))
    )
    out = await _fetch("https://shop.example.com/sitemap.xml", max_entries=3)
    assert (len(out.locs), out.truncated) == (3, True)


async def test_entries_are_capped_at_the_payload_limit(site):
    routes, _ = site
    long = "https://shop.example.com/" + "p" * 1000
    routes["https://shop.example.com/sitemap.xml"] = httpx.Response(
        200, content=_urlset(*(f"{long}{i}" for i in range(400)))
    )
    out = await _fetch("https://shop.example.com/sitemap.xml", max_entries=10_000)
    assert out.truncated
    assert sum(len(loc) for loc in out.locs) <= crawl.PAYLOAD_MAX_CHARS


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, content=b"<urlset><url><loc>broken"),
        httpx.Response(200, content=b"<html><body>not a sitemap</body></html>"),
        httpx.Response(200, content=b"x" * (crawl.SITEMAP_MAX_BYTES + 1)),
    ],
    ids=["404", "malformed", "not-a-sitemap", "oversized"],
)
async def test_unreadable_sitemap_is_unavailable(site, response):
    routes, _ = site
    routes["https://shop.example.com/sitemap.xml"] = response
    assert (await _fetch("https://shop.example.com/sitemap.xml")).kind == "unavailable"


# ---------------------------------------------------------------------------
# ExtractLinks
# ---------------------------------------------------------------------------


def _minio_with(html: bytes):
    response = MagicMock()
    response.read = AsyncMock(return_value=html)
    minio = AsyncMock()
    minio.get_object.return_value = response
    return minio, response


async def test_links_are_read_from_the_stored_page():
    html = b'<a href="/a">a</a><a href="/b#x">b</a><a href="https://other.com/">x</a>'
    minio, response = _minio_with(html)
    out = await ActivityEnvironment().run(
        crawl.CrawlActivities(minio).extract_links,
        ExtractLinksInput(path="bucket/history/p1/scrape.html", base_url=SEED),
    )
    minio.get_object.assert_awaited_once_with("bucket", "history/p1/scrape.html")
    response.close.assert_called_once()
    assert out.links == ["https://shop.example.com/a", "https://shop.example.com/b"]


async def test_path_filters_are_not_applied_here():
    # Admission applies include/exclude to every discovered URL, and counts what it drops.
    minio, _ = _minio_with(b'<a href="/admin/x">x</a>')
    out = await ActivityEnvironment().run(
        crawl.CrawlActivities(minio).extract_links,
        ExtractLinksInput(path="b/k", base_url=SEED),
    )
    assert out.links == ["https://shop.example.com/admin/x"]
