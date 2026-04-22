"""
Unit tests for coordinator/link_extractor.py.

Pure function — no mocks or async needed.
"""

from coordinator.link_extractor import extract_links

BASE = "https://example.com"


def test_extracts_same_origin_links():
    html = '<a href="/page1">One</a><a href="/page2">Two</a>'
    links = extract_links(html, BASE, None, None)
    assert "https://example.com/page1" in links
    assert "https://example.com/page2" in links


def test_filters_external_links():
    html = '<a href="https://other.com/page">External</a><a href="/local">Local</a>'
    links = extract_links(html, BASE, None, None)
    assert "https://other.com/page" not in links
    assert "https://example.com/local" in links


def test_strips_fragment():
    html = '<a href="/page#section">Link</a><a href="/page#other">Link 2</a>'
    links = extract_links(html, BASE, None, None)
    assert "https://example.com/page" in links
    # Fragment-stripped duplicates deduplicated to one entry
    assert links.count("https://example.com/page") == 1
    assert all("#" not in link for link in links)


def test_skips_mailto():
    html = '<a href="mailto:hi@example.com">Email</a>'
    links = extract_links(html, BASE, None, None)
    assert links == []


def test_skips_tel():
    html = '<a href="tel:+15555555555">Call</a>'
    links = extract_links(html, BASE, None, None)
    assert links == []


def test_skips_javascript():
    html = '<a href="javascript:void(0)">JS</a>'
    links = extract_links(html, BASE, None, None)
    assert links == []


def test_deduplicates_same_url():
    html = '<a href="/page">One</a><a href="/page">Two</a><a href="/page">Three</a>'
    links = extract_links(html, BASE, None, None)
    assert links.count("https://example.com/page") == 1


def test_does_not_return_base_url_itself():
    html = f'<a href="{BASE}">Home</a><a href="/">Root</a><a href="/about">About</a>'
    links = extract_links(html, BASE, None, None)
    assert BASE not in links
    assert "https://example.com/about" in links


def test_include_paths_allows_matching():
    html = '<a href="/blog/post">Blog</a><a href="/shop/item">Shop</a>'
    links = extract_links(html, BASE, ["/blog"], None)
    assert "https://example.com/blog/post" in links
    assert "https://example.com/shop/item" not in links


def test_include_paths_empty_result_when_nothing_matches():
    html = '<a href="/about">About</a>'
    links = extract_links(html, BASE, ["/blog"], None)
    assert links == []


def test_exclude_paths_removes_matching():
    html = '<a href="/admin/dashboard">Admin</a><a href="/about">About</a>'
    links = extract_links(html, BASE, None, ["/admin"])
    assert "https://example.com/admin/dashboard" not in links
    assert "https://example.com/about" in links


def test_include_and_exclude_exclude_wins():
    html = '<a href="/blog/admin">Blog Admin</a><a href="/blog/post">Blog Post</a>'
    links = extract_links(html, BASE, ["/blog"], ["/blog/admin"])
    assert "https://example.com/blog/admin" not in links
    assert "https://example.com/blog/post" in links


def test_resolves_relative_urls():
    html = '<a href="page">Relative</a>'
    links = extract_links(html, "https://example.com/section/", None, None)
    assert "https://example.com/section/page" in links


def test_empty_html_returns_empty():
    links = extract_links("", BASE, None, None)
    assert links == []


def test_no_anchors_returns_empty():
    html = "<p>No links here</p>"
    links = extract_links(html, BASE, None, None)
    assert links == []
