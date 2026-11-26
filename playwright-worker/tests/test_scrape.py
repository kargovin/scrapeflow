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
            "server": "http://93.184.216.34:8080",
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
            "server": "http://93.184.216.34:8080",
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


@pytest.mark.parametrize(
    ("status", "smokescreen_error", "outcome"),
    [
        # The proxy could not connect (an https upgrade of an http-only site): a network error, so
        # Chrome falls back to http as it does unproxied.
        (502, "Failed to connect to remote host: dial tcp 11.222.33.2:443", "abort"),
        # A refusal is served, so the render sees it as one.
        (407, "Egress proxying is denied to host 'minio:9000'", "fulfill"),
        (200, None, "fulfill"),
    ],
)
async def test_csp_handler_under_the_egress_proxy(status, smokescreen_error, outcome):
    browser, _, page = make_browser(html=PAGE)
    captured_handlers: dict = {}

    async def track_route(pattern, handler):
        captured_handlers[pattern] = handler

    page.route = AsyncMock(side_effect=track_route)
    with _no_actions():
        await _render(browser, actions=[{"type": "wait", "milliseconds": 100}])

    route = AsyncMock()
    route.request.resource_type = "document"
    headers = {"content-type": "text/html"}
    if smokescreen_error:
        headers["x-smokescreen-error"] = smokescreen_error
    route.fetch = AsyncMock(return_value=MagicMock(status=status, headers=headers))
    await captured_handlers["**"](route)

    if outcome == "abort":
        route.abort.assert_awaited_once_with("connectionfailed")
        route.fulfill.assert_not_called()
    else:
        route.fulfill.assert_awaited_once()
        route.abort.assert_not_called()


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


# ---------------------------------------------------------------------------
# Egress refusals (BUG-010 part 2)
# ---------------------------------------------------------------------------

from worker.egress import AddressRefused  # noqa: E402

_REASON = "Egress proxying is denied to host 'minio:9000': 172.18.0.4 denied by rule 'Deny: Private Range'."


def _navigation(page, url, *, status=407, headers=None):
    request = MagicMock(frame=page.main_frame)
    request.is_navigation_request.return_value = True
    return MagicMock(
        url=url,
        status=status,
        headers={"x-smokescreen-error": _REASON} if headers is None else headers,
        frame=page.main_frame,
        request=request,
    )


def _failed_navigation(page, url, failure):
    request = MagicMock(url=url, failure=failure, frame=page.main_frame)
    request.is_navigation_request.return_value = True
    return request


async def test_a_refused_redirect_raises_and_uploads_nothing():
    browser, context, page = make_browser(html=PAGE)

    async def goto(url, **_):
        page.handlers["response"](
            _navigation(page, "http://minio:9000/minio/health/live")
        )
        return MagicMock(status=407)

    page.goto = AsyncMock(side_effect=goto)

    with (
        patch("worker.scrape.upload", new_callable=AsyncMock) as mock_upload,
        _no_actions() as mock_actions,
        pytest.raises(AddressRefused) as excinfo,
    ):
        await render(
            browser,
            AsyncMock(),
            artifact_id="a",
            url="http://shop.example/redirect",
            output_format="html",
            playwright_options=None,
            actions=[{"type": "wait", "ms": 1}],
            proxy_url=None,
            cookies=None,
            default_timeout=_DEFAULT_TIMEOUT,
        )
    assert str(excinfo.value) == "address refused: minio:9000 is not a public address"
    assert excinfo.value.reason == _REASON
    mock_actions.assert_not_called()  # no actions on a refused page
    mock_upload.assert_not_called()
    context.close.assert_awaited_once()


async def test_a_refused_https_navigation_raises_address_refused():
    browser, _, page = make_browser(html=PAGE)

    async def goto(url, **_):
        page.handlers["requestfailed"](
            _failed_navigation(
                page, "https://internal.example/", "net::ERR_PROXY_AUTH_UNSUPPORTED"
            )
        )
        raise Exception(
            "Page.goto: net::ERR_PROXY_AUTH_UNSUPPORTED at https://internal.example/"
        )

    page.goto = AsyncMock(side_effect=goto)

    with pytest.raises(
        AddressRefused, match="internal.example is not a public address"
    ):
        await _render(browser, url="https://internal.example/")


async def test_with_a_job_proxy_a_connect_407_is_the_proxys_own_failure():
    browser, _, page = make_browser(html=PAGE)

    async def goto(url, **_):
        page.handlers["requestfailed"](
            _failed_navigation(page, url, "net::ERR_PROXY_AUTH_UNSUPPORTED")
        )
        raise Exception("Page.goto: net::ERR_PROXY_AUTH_UNSUPPORTED")

    page.goto = AsyncMock(side_effect=goto)

    with pytest.raises(Exception, match="ERR_PROXY_AUTH_UNSUPPORTED") as excinfo:
        await _render(browser, proxy_url="http://proxy.example.com:8080")
    assert not isinstance(excinfo.value, AddressRefused)


async def test_a_407_without_the_proxys_header_is_not_a_refusal():
    browser, _, page = make_browser(html=PAGE)

    async def goto(url, **_):
        page.handlers["response"](_navigation(page, url, headers={}))
        return MagicMock(status=407)

    page.goto = AsyncMock(side_effect=goto)

    rendered = await _render(browser)
    assert rendered.minio_path == _PATH


async def test_a_refused_subresource_is_not_a_refusal_of_the_page():
    browser, _, page = make_browser(html=PAGE)

    def subresource(page):
        response = _navigation(page, "http://internal.example/pixel.gif")
        response.request.is_navigation_request.return_value = False
        return response

    async def goto_sub(url, **_):
        page.handlers["response"](subresource(page))
        return MagicMock(status=200)

    page.goto = AsyncMock(side_effect=goto_sub)

    rendered = await _render(browser)
    assert rendered.minio_path == _PATH


async def test_a_navigation_refused_during_the_actions_raises_before_capture():
    browser, _, page = make_browser(html=PAGE)
    page.goto = AsyncMock(return_value=MagicMock(status=200))

    async def actions(*_):
        page.handlers["response"](_navigation(page, "http://10.0.0.1/"))
        return [], []

    with (
        patch(
            "worker.scrape.execute_actions", new_callable=AsyncMock, side_effect=actions
        ),
        pytest.raises(AddressRefused, match="10.0.0.1"),
    ):
        await _render(browser, actions=[{"type": "click", "selector": "a"}])
    page.content.assert_not_called()


async def test_a_private_job_proxy_is_refused_before_a_context_opens(resolve):
    browser, _, _ = make_browser(html=PAGE)
    resolve["proxy.internal"] = ["10.43.0.5"]

    with pytest.raises(AddressRefused, match="proxy.internal is not a public address"):
        await _render(browser, proxy_url="http://proxy.internal:3128")
    browser.new_context.assert_not_called()
