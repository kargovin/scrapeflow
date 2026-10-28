"""
Unit tests for worker/scrape.py — render(), the pipeline the Scrape activity runs.

The browser is the conftest mock stack. `upload` and `execute_actions` are patched at
'worker.scrape.*' — unittest.mock patches the name where it is looked up.

Order render() must keep:
  1. new_context(proxy=...) when a proxy URL is given
  2. add_cookies() before page.goto()
  3. page.route(...) for block_images, and page.route("**", csp) when actions are present,
     both before page.goto()
  4. page.goto → page.wait_for_load_state, sharing one timeout
  5. execute_actions() after page.goto
  6. context.close() — always
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_browser
from worker.contracts import PlaywrightOptions
from worker.scrape import render

_DEFAULT_TIMEOUT = 60
_PATH = "scrapeflow-results/history/artifact-aaa/scrape.html"

# Large enough that no size-gated wall rule fires on it, whatever goto returns.
PAGE = (
    "<html><head><title>Real page</title></head><body>"
    + "<p>Genuine product description paragraph.</p>" * 200
    + "</body></html>"
)


async def _render(browser, **overrides):
    kwargs = {
        "artifact_id": "artifact-aaa",
        "url": "https://example.com/",
        "output_format": "html",
        "playwright_options": None,
        "actions": None,
        "proxy_url": None,
        "cookies": None,
        "default_timeout": _DEFAULT_TIMEOUT,
    }
    kwargs.update(overrides)
    with patch("worker.scrape.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = _PATH
        return await render(browser, AsyncMock(), **kwargs)


def _no_actions():
    return patch(
        "worker.scrape.execute_actions", new_callable=AsyncMock, return_value=([], [])
    )


# ---------------------------------------------------------------------------
# Context lifecycle
# ---------------------------------------------------------------------------


async def test_context_closed_on_success():
    browser, context, _ = make_browser(html=PAGE)

    rendered = await _render(browser)

    assert rendered.minio_path == _PATH
    context.close.assert_called_once()


async def test_context_closed_on_failure():
    browser, context, page = make_browser(html=PAGE)
    page.goto = AsyncMock(side_effect=Exception("timeout"))

    with pytest.raises(Exception, match="timeout"):
        await _render(browser)

    context.close.assert_called_once()


# ---------------------------------------------------------------------------
# block_images
# ---------------------------------------------------------------------------


async def test_block_images_true_calls_page_route():
    browser, _, page = make_browser(html=PAGE)

    await _render(browser, playwright_options=PlaywrightOptions(block_images=True))

    page.route.assert_called_once()
    route_pattern: str = page.route.call_args.args[0]
    assert "png" in route_pattern
    assert "jpg" in route_pattern


async def test_block_images_aborts_images_and_fonts_but_never_css():
    """
    BUG-016: an aborted CSS chunk rejects a lazy-loaded SPA route's import() and the
    page renders React's error boundary in place of the content. A missing font falls
    back silently, so fonts stay blocked.

    The glob is matched with Patchright's own matcher, so this pins what the browser
    will actually abort rather than the pattern's spelling.
    """
    from patchright._impl._glob import glob_to_regex_pattern

    browser, _, page = make_browser(html=PAGE)

    await _render(browser, playwright_options=PlaywrightOptions(block_images=True))

    page.route.assert_called_once()
    matcher = re.compile(glob_to_regex_pattern(page.route.call_args.args[0]))

    for blocked in (
        "https://example.com/assets/hero.png",
        "https://example.com/assets/photo.jpg",
        "https://cdn.example.com/fonts/inter.woff2",
    ):
        assert matcher.match(blocked), f"{blocked} should be aborted"

    for passed in (
        "https://example.com/static/css/route-chunk.css",
        "https://example.com/static/js/route-chunk.js",
        "https://example.com/product/123",
    ):
        assert not matcher.match(passed), f"{passed} must not be aborted"


async def test_block_images_false_does_not_call_page_route():
    browser, _, page = make_browser(html=PAGE)

    await _render(browser, playwright_options=PlaywrightOptions(block_images=False))

    page.route.assert_not_called()


# ---------------------------------------------------------------------------
# timeout_seconds governs the wait strategy, not just goto (BUG-015)
# ---------------------------------------------------------------------------


async def test_wait_for_load_state_receives_job_timeout():
    """Without it the wait gets Playwright's 30 s default."""
    browser, _, page = make_browser(html=PAGE)

    await _render(
        browser,
        playwright_options=PlaywrightOptions(
            wait_strategy="networkidle", timeout_seconds=90
        ),
    )

    page.goto.assert_called_once()
    assert page.goto.call_args.kwargs["timeout"] == 90_000
    page.wait_for_load_state.assert_called_once()
    assert page.wait_for_load_state.call_args.args[0] == "networkidle"
    assert page.wait_for_load_state.call_args.kwargs["timeout"] == 90_000


async def test_wait_for_load_state_receives_default_timeout():
    browser, _, page = make_browser(html=PAGE)

    await _render(browser)

    expected_ms = _DEFAULT_TIMEOUT * 1000
    assert page.goto.call_args.kwargs["timeout"] == expected_ms
    assert page.wait_for_load_state.call_args.args[0] == "load"
    assert page.wait_for_load_state.call_args.kwargs["timeout"] == expected_ms


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


async def test_proxy_passed_to_new_context():
    """Chromium drops userinfo embedded in the URL, so it is split out."""
    browser, _, _ = make_browser(html=PAGE)

    await _render(browser, proxy_url="http://user:pass@proxy.example.com:8080")

    browser.new_context.assert_called_once_with(
        no_viewport=True,
        proxy={
            "server": "http://proxy.example.com:8080",
            "username": "user",
            "password": "pass",
        },
    )


async def test_proxy_credentials_are_percent_decoded():
    """BUG-017: the Go worker decodes userinfo; without unquote() one engine fails."""
    browser, _, _ = make_browser(html=PAGE)

    await _render(
        browser, proxy_url="http://us%40er:p%40ss%3Aw0rd@proxy.example.com:8080"
    )

    browser.new_context.assert_called_once_with(
        no_viewport=True,
        proxy={
            "server": "http://proxy.example.com:8080",
            "username": "us@er",
            "password": "p@ss:w0rd",
        },
    )


async def test_no_proxy_calls_new_context_without_proxy():
    browser, _, _ = make_browser(html=PAGE)

    await _render(browser)

    browser.new_context.assert_called_once_with(no_viewport=True)


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


async def test_cookies_injected_before_goto():
    browser, context, page = make_browser(html=PAGE)
    call_order = []
    context.add_cookies = AsyncMock(
        side_effect=lambda _: call_order.append("add_cookies")
    )
    page.goto = AsyncMock(side_effect=lambda *a, **kw: call_order.append("goto"))

    await _render(
        browser,
        cookies=[{"name": "session", "value": "abc123", "domain": "example.com"}],
    )

    assert call_order.index("add_cookies") < call_order.index("goto")


async def test_cookie_domain_inferred_from_url():
    browser, context, _ = make_browser(html=PAGE)
    captured = []
    context.add_cookies = AsyncMock(side_effect=lambda c: captured.extend(c))

    await _render(
        browser,
        url="https://example.com/page",
        cookies=[{"name": "token", "value": "xyz"}],
    )

    assert len(captured) == 1
    assert captured[0]["domain"] == "example.com"


# ---------------------------------------------------------------------------
# CSP injection
# ---------------------------------------------------------------------------


async def test_csp_route_registered_before_goto():
    browser, _, page = make_browser(html=PAGE)
    call_order = []
    captured_handlers: dict = {}

    async def track_route(pattern, handler):
        call_order.append(f"route:{pattern}")
        captured_handlers[pattern] = handler

    page.route = AsyncMock(side_effect=track_route)
    page.goto = AsyncMock(side_effect=lambda *a, **kw: call_order.append("goto"))

    with _no_actions():
        await _render(browser, actions=[{"type": "wait", "milliseconds": 100}])

    assert "**" in captured_handlers, "page.route('**', handler) was not registered"
    assert call_order.index("route:**") < call_order.index("goto")


async def test_csp_handler_injects_all_directives():
    """Document requests get the CSP response header; everything else falls through."""
    browser, _, page = make_browser(html=PAGE)
    captured_handlers: dict = {}

    async def track_route(pattern, handler):
        captured_handlers[pattern] = handler

    page.route = AsyncMock(side_effect=track_route)

    with _no_actions():
        await _render(
            browser,
            url="https://example.com/page",
            actions=[{"type": "wait", "milliseconds": 100}],
        )

    handler = captured_handlers["**"]

    doc_route = AsyncMock()
    doc_route.request.resource_type = "document"
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/html"}
    doc_route.fetch = AsyncMock(return_value=mock_response)
    await handler(doc_route)

    doc_route.fulfill.assert_called_once()
    csp = doc_route.fulfill.call_args.kwargs["headers"].get(
        "content-security-policy", ""
    )
    for directive in ("connect-src", "img-src", "form-action", "frame-src"):
        assert directive in csp
    assert "example.com" in csp

    img_route = AsyncMock()
    img_route.request.resource_type = "image"
    await handler(img_route)
    img_route.fallback.assert_called_once()
    img_route.fulfill.assert_not_called()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def test_execute_actions_called_after_goto():
    browser, _, page = make_browser(html=PAGE)
    call_order = []
    page.goto = AsyncMock(side_effect=lambda *a, **kw: call_order.append("goto"))

    with patch(
        "worker.scrape.execute_actions",
        new_callable=AsyncMock,
        side_effect=lambda *a, **kw: (call_order.append("actions"), ([], []))[1],
    ):
        await _render(browser, actions=[{"type": "wait", "milliseconds": 50}])

    assert call_order.index("goto") < call_order.index("actions")
