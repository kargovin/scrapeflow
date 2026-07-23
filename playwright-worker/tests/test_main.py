"""
Unit tests for worker/worker.py — handle_message().

These tests verify the full ADR-002 job lifecycle without any live
infrastructure. NATS, MinIO, and the Playwright browser are all AsyncMocks.

The `upload` function is patched at 'worker.worker.upload' (the name as it
appears in the module under test, not where it's defined in storage.py).
This is the standard unittest.mock rule for patching imported names.

Lifecycle summary being tested:
  1. Parse JobMessage from msg.data — ack+skip if malformed
  2. robots.txt check (respect_robots=True) — publish failed+ack if disallowed;
     fires BEFORE step 3
  3. Publish status="running" with nats_stream_seq BEFORE page interaction
  4. new_context(proxy=...) if credentials.encrypted_proxy_url is set (decrypted first)
  5. add_cookies() before page.goto() if credentials.encrypted_cookies is set (decrypted first)
  6. page.route("**", csp_handler) before page.goto() if actions are present
  7. page.goto → page.wait_for_load_state → page.content()
  8. execute_actions() after page.goto
  9. format_output → upload to MinIO
  10. Publish status="completed" with minio_path
  11. msg.ack() — always, even on failure
  12. context.close() — always (finally block)
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from tests.conftest import encrypt_credential, make_browser, make_nats_msg
from worker.worker import RESULT_SUBJECT, handle_message

_FAKE_MINIO_PATH = "scrapeflow-results/history/job-aaa/1234567890.html"
_DEFAULT_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Helper: run handle_message with upload patched to return a fixed path
# ---------------------------------------------------------------------------


async def _run(msg, js=None, browser=None, extra_patches=None):
    """Run handle_message with upload stubbed out; returns (js, mock_upload)."""
    js = js or AsyncMock()
    if browser is None:
        browser, _, _ = make_browser()
    patches = [patch("worker.worker.upload", new_callable=AsyncMock)]
    if extra_patches:
        patches.extend(extra_patches)
    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = _FAKE_MINIO_PATH
        await handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)
    return js, mock_upload


# ---------------------------------------------------------------------------
# Malformed message
# ---------------------------------------------------------------------------


async def test_malformed_json_is_acked_immediately():
    """
    A message that cannot be parsed as JobMessage must be acked and dropped.
    Publishing and MinIO writes must NOT happen — there is nothing to process.
    """
    msg = MagicMock()
    msg.data = b"not valid json {{{"
    msg.ack = AsyncMock()
    js = AsyncMock()

    await handle_message(msg, js, AsyncMock(), AsyncMock(), _DEFAULT_TIMEOUT)

    msg.ack.assert_called_once()
    js.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path — publish ordering
# ---------------------------------------------------------------------------


async def test_first_publish_is_running_with_stream_seq():
    """
    The very first NATS publish must be status='running' and carry
    nats_stream_seq from msg.metadata.sequence.stream (used by Step 22
    MaxDeliver advisory handler to identify stalled runs).
    """
    msg = make_nats_msg(stream_seq=77)
    js = AsyncMock()
    browser, _, _ = make_browser()

    js, _ = await _run(msg, js=js, browser=browser)

    first_call = js.publish.call_args_list[0]
    subject, payload_bytes = first_call.args
    data = json.loads(payload_bytes)

    assert subject == RESULT_SUBJECT
    assert data["status"] == "running"
    assert data["nats_stream_seq"] == 77
    assert "minio_path" not in data


async def test_second_publish_is_completed_with_minio_path():
    """
    After a successful MinIO upload, the second publish must be
    status='completed' and include the minio_path returned by upload().
    """
    msg = make_nats_msg()
    js = AsyncMock()
    browser, _, _ = make_browser()

    js, _ = await _run(msg, js=js, browser=browser)

    assert js.publish.call_count == 2
    second_call = js.publish.call_args_list[1]
    _, payload_bytes = second_call.args
    data = json.loads(payload_bytes)

    assert data["status"] == "completed"
    assert data["minio_path"] == _FAKE_MINIO_PATH
    assert "error" not in data


async def test_ack_called_once_on_success():
    """msg.ack() must be called exactly once after a successful job run."""
    msg = make_nats_msg()
    js, _ = await _run(msg)
    msg.ack.assert_called_once()


async def test_context_closed_on_success():
    """
    context.close() must be called after a successful run (the finally block).
    This prevents browser session state from leaking between jobs.
    """
    msg = make_nats_msg()
    browser, context, _ = make_browser()
    await _run(msg, browser=browser)
    context.close.assert_called_once()


# ---------------------------------------------------------------------------
# Failure path — page.goto raises
# ---------------------------------------------------------------------------


async def test_page_goto_failure_publishes_failed():
    """
    When page.goto raises, the worker must publish status='failed' with the
    error string — the API result consumer uses this to mark the run failed.
    """
    msg = make_nats_msg()
    js = AsyncMock()
    browser, _, page = make_browser()
    page.goto = AsyncMock(side_effect=Exception("connection timeout"))

    await handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)

    last_call = js.publish.call_args_list[-1]
    _, payload_bytes = last_call.args
    data = json.loads(payload_bytes)
    assert data["status"] == "failed"
    # describe() prefixes the exception type (UF-003 3a) — mirrors the LLM worker,
    # so errors that stringify to blank are not invisible in the UI.
    assert data["error"] == "Exception: connection timeout"
    assert "minio_path" not in data


async def test_ack_called_on_failure():
    """
    msg.ack() must be called even when page.goto raises.
    Mirrors the Go worker: the API already knows the run failed via the result
    event — not acking would redeliver, but re-delivery won't fix a down site.
    """
    msg = make_nats_msg()
    browser, _, page = make_browser()
    page.goto = AsyncMock(side_effect=Exception("timeout"))

    await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    msg.ack.assert_called_once()


async def test_context_closed_on_failure():
    """context.close() must be called even when page.goto raises (finally block)."""
    msg = make_nats_msg()
    browser, context, page = make_browser()
    page.goto = AsyncMock(side_effect=Exception("timeout"))

    await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    context.close.assert_called_once()


# ---------------------------------------------------------------------------
# block_images option
# ---------------------------------------------------------------------------


async def test_block_images_true_calls_page_route():
    """
    When playwright_options.block_images=True, page.route() must be called
    once with a glob pattern that covers image and font extensions.
    """
    msg = make_nats_msg(
        playwright_options={
            "wait_strategy": "load",
            "timeout_seconds": 30,
            "block_images": True,
        }
    )
    browser, _, page = make_browser()

    await _run(msg, browser=browser)

    page.route.assert_called_once()
    route_pattern: str = page.route.call_args.args[0]
    # Pattern must cover at least common image types
    assert "png" in route_pattern
    assert "jpg" in route_pattern


async def test_block_images_false_does_not_call_page_route():
    """When block_images=False (default), page.route() must NOT be called."""
    msg = make_nats_msg(
        playwright_options={
            "wait_strategy": "load",
            "timeout_seconds": 30,
            "block_images": False,
        }
    )
    browser, _, page = make_browser()

    await _run(msg, browser=browser)

    page.route.assert_not_called()


# ---------------------------------------------------------------------------
# robots.txt enforcement (Step 15)
# ---------------------------------------------------------------------------


async def test_robots_disallowed_publishes_failed_before_running():
    """
    When respect_robots=True and is_disallowed returns True, the worker must:
    - Publish exactly one result: status='failed', error='robots_txt_disallowed'
    - NOT publish 'running' at all
    - Ack the message
    """
    msg = make_nats_msg(options={"respect_robots": True})
    js = AsyncMock()
    browser, _, _ = make_browser()

    with patch("worker.worker.is_disallowed", new_callable=AsyncMock) as mock_check:
        mock_check.return_value = True
        await handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)

    assert js.publish.call_count == 1
    _, payload_bytes = js.publish.call_args_list[0].args
    data = json.loads(payload_bytes)
    assert data["status"] == "failed"
    assert data["error"] == "robots_txt_disallowed"
    msg.ack.assert_called_once()


async def test_robots_allowed_proceeds_to_running():
    """
    When respect_robots=True and is_disallowed returns False, the first
    publish must be 'running' — robots check passed.
    """
    msg = make_nats_msg(options={"respect_robots": True})
    js = AsyncMock()
    browser, _, _ = make_browser()

    with patch("worker.worker.is_disallowed", new_callable=AsyncMock) as mock_check:
        mock_check.return_value = False
        with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = _FAKE_MINIO_PATH
            await handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)

    first_call = js.publish.call_args_list[0]
    _, payload_bytes = first_call.args
    data = json.loads(payload_bytes)
    assert data["status"] == "running"


async def test_respect_robots_false_skips_check():
    """
    When respect_robots=False (or options absent), is_disallowed must never
    be called — even if robots.txt would block the URL.
    """
    msg = make_nats_msg(options={"respect_robots": False})
    js = AsyncMock()
    browser, _, _ = make_browser()

    with patch("worker.worker.is_disallowed", new_callable=AsyncMock) as mock_check:
        mock_check.return_value = True  # would block, but should never be called
        with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = _FAKE_MINIO_PATH
            await handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)

    mock_check.assert_not_called()


# ---------------------------------------------------------------------------
# Proxy routing (Step 15)
# ---------------------------------------------------------------------------


async def test_proxy_passed_to_new_context():
    """
    When credentials.encrypted_proxy_url is set, browser.new_context must be called
    with the proxy split into server/username/password (Chromium drops userinfo
    embedded in the URL), alongside no_viewport=True.
    """
    proxy_url = "http://user:pass@proxy.example.com:8080"
    msg = make_nats_msg(
        credentials={"encrypted_proxy_url": encrypt_credential(proxy_url)}
    )
    browser, _, _ = make_browser()

    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = _FAKE_MINIO_PATH
        await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    browser.new_context.assert_called_once_with(
        no_viewport=True,
        proxy={
            "server": "http://proxy.example.com:8080",
            "username": "user",
            "password": "pass",
        },
    )


async def test_no_proxy_calls_new_context_without_proxy():
    """When no credentials are set, new_context is called with only no_viewport=True."""
    msg = make_nats_msg()
    browser, _, _ = make_browser()

    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = _FAKE_MINIO_PATH
        await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    browser.new_context.assert_called_once_with(no_viewport=True)


# ---------------------------------------------------------------------------
# Cookie injection (Step 15)
# ---------------------------------------------------------------------------


async def test_cookies_injected_before_goto():
    """
    When credentials.encrypted_cookies is set, context.add_cookies must be called
    before page.goto after decryption.
    """
    cookies = [{"name": "session", "value": "abc123", "domain": "example.com"}]
    msg = make_nats_msg(
        credentials={"encrypted_cookies": encrypt_credential(json.dumps(cookies))}
    )
    browser, context, page = make_browser()

    call_order = []
    context.add_cookies = AsyncMock(
        side_effect=lambda _: call_order.append("add_cookies")
    )
    page.goto = AsyncMock(side_effect=lambda *a, **kw: call_order.append("goto"))

    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = _FAKE_MINIO_PATH
        await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    assert call_order.index("add_cookies") < call_order.index("goto")


async def test_cookie_domain_inferred_from_url():
    """
    A cookie without a 'domain' key must have domain inferred from the job URL
    hostname before being passed to add_cookies.
    """
    # Cookie has no 'domain' field
    cookies = [{"name": "token", "value": "xyz"}]
    msg = make_nats_msg(
        url="https://example.com/page",
        credentials={"encrypted_cookies": encrypt_credential(json.dumps(cookies))},
    )
    browser, context, page = make_browser()
    captured = []
    context.add_cookies = AsyncMock(side_effect=lambda c: captured.extend(c))

    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = _FAKE_MINIO_PATH
        await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    assert len(captured) == 1
    assert captured[0]["domain"] == "example.com"


# ---------------------------------------------------------------------------
# CSP injection (Step 15)
# ---------------------------------------------------------------------------


async def test_csp_route_registered_before_goto():
    """
    When options.actions is non-empty, page.route("**", handler) must be called
    before page.goto so the CSP handler is active on the navigation response.
    """
    actions = [{"type": "wait", "milliseconds": 100}]
    msg = make_nats_msg(options={"actions": actions})
    browser, _, page = make_browser()

    call_order = []
    captured_handlers: dict = {}

    async def track_route(pattern, handler):
        call_order.append(f"route:{pattern}")
        captured_handlers[pattern] = handler

    page.route = AsyncMock(side_effect=track_route)
    page.goto = AsyncMock(side_effect=lambda *a, **kw: call_order.append("goto"))

    with patch("worker.worker.execute_actions", new_callable=AsyncMock) as mock_actions:
        mock_actions.return_value = ([], [])
        with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = _FAKE_MINIO_PATH
            await handle_message(
                msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT
            )

    assert "**" in captured_handlers, "page.route('**', handler) was not registered"
    assert call_order.index("route:**") < call_order.index("goto")


async def test_csp_handler_injects_all_directives():
    """
    The registered CSP route handler must inject a Content-Security-Policy
    response header covering connect-src, img-src, form-action, and frame-src
    for document requests, and call route.fallback() for non-document requests.
    """
    actions = [{"type": "wait", "milliseconds": 100}]
    msg = make_nats_msg(options={"actions": actions}, url="https://example.com/page")
    browser, _, page = make_browser()

    captured_handlers: dict = {}

    async def track_route(pattern, handler):
        captured_handlers[pattern] = handler

    page.route = AsyncMock(side_effect=track_route)

    with patch("worker.worker.execute_actions", new_callable=AsyncMock) as mock_actions:
        mock_actions.return_value = ([], [])
        with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = _FAKE_MINIO_PATH
            await handle_message(
                msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT
            )

    handler = captured_handlers["**"]

    # Document request — handler must fulfill with CSP response header
    doc_route = AsyncMock()
    doc_route.request.resource_type = "document"
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/html"}
    doc_route.fetch = AsyncMock(return_value=mock_response)
    await handler(doc_route)

    doc_route.fulfill.assert_called_once()
    injected_headers: dict = doc_route.fulfill.call_args.kwargs["headers"]
    csp = injected_headers.get("content-security-policy", "")
    assert "connect-src" in csp
    assert "img-src" in csp
    assert "form-action" in csp
    assert "frame-src" in csp
    assert "example.com" in csp

    # Non-document request — handler must fall through, not fulfill
    img_route = AsyncMock()
    img_route.request.resource_type = "image"
    await handler(img_route)
    img_route.fallback.assert_called_once()
    img_route.fulfill.assert_not_called()


# ---------------------------------------------------------------------------
# Action execution (Step 15)
# ---------------------------------------------------------------------------


async def test_execute_actions_called_after_goto():
    """
    execute_actions must be called after page.goto resolves, so that actions
    operate on the fully-loaded page DOM.
    """
    actions = [{"type": "wait", "milliseconds": 50}]
    msg = make_nats_msg(options={"actions": actions})
    browser, _, page = make_browser()

    call_order = []
    page.goto = AsyncMock(side_effect=lambda *a, **kw: call_order.append("goto"))

    with patch("worker.worker.execute_actions", new_callable=AsyncMock) as mock_actions:
        mock_actions.return_value = ([], [])
        mock_actions.side_effect = lambda *a, **kw: (
            call_order.append("actions"),
            ([], []),
        )[1]
        with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = _FAKE_MINIO_PATH
            await handle_message(
                msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT
            )

    assert call_order.index("goto") < call_order.index("actions")


# ---------------------------------------------------------------------------
# Transient MinIO write failure — nak + redelivery (UF-003 3a)
# ---------------------------------------------------------------------------
#
# The bug: the worker acked on *every* exception, so a momentary MinIO outage
# (upload raises) permanently failed a job whose expensive headed-Chrome render
# had already succeeded. Now a transient infra failure is naked back to JetStream
# for a bounded number of redeliveries, and only the final attempt reports.


async def test_minio_down_naks_and_does_not_ack():
    """A MinIO connection failure on upload → nak (retry), not ack (permanent fail)."""
    msg = make_nats_msg(num_delivered=1)
    js = AsyncMock()
    browser, _, _ = make_browser()

    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.side_effect = aiohttp.ClientConnectionError("connection refused")
        await handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)

    msg.nak.assert_called_once()
    msg.ack.assert_not_called()
    # No "failed" is published on a retry — the API's terminal-status guard would
    # lock the run failed and discard the eventual "completed".
    statuses = [json.loads(c.args[1])["status"] for c in js.publish.call_args_list]
    assert "failed" not in statuses
    assert statuses == ["running"]


async def test_minio_down_nak_uses_exponential_backoff():
    """Second delivery backs off to base * 2^(2-1) = 10s."""
    msg = make_nats_msg(num_delivered=2)
    browser, _, _ = make_browser()

    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.side_effect = aiohttp.ClientConnectionError("refused")
        await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    _, kwargs = msg.nak.call_args
    assert kwargs["delay"] == 10.0


async def test_minio_down_final_attempt_publishes_failed_and_acks():
    """On the last allowed delivery the worker gives up: publish failed + ack, no nak."""
    # default playwright_max_delivery_attempts is 3, so attempt 3 is terminal.
    msg = make_nats_msg(num_delivered=3)
    js = AsyncMock()
    browser, _, _ = make_browser()

    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.side_effect = aiohttp.ClientConnectionError("refused")
        await handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)

    msg.nak.assert_not_called()
    msg.ack.assert_called_once()
    last = json.loads(js.publish.call_args_list[-1].args[1])
    assert last["status"] == "failed"
    assert "gave up after 3 attempts" in last["error"]


async def test_context_closed_on_transient_nak():
    """The finally block still closes the context when we nak-and-return."""
    msg = make_nats_msg(num_delivered=1)
    browser, context, _ = make_browser()

    with patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.side_effect = aiohttp.ClientConnectionError("refused")
        await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    context.close.assert_called_once()


async def test_terminal_failure_does_not_nak():
    """A genuine site failure (goto timeout) is terminal — ack, never nak, even on
    the first delivery."""
    msg = make_nats_msg(num_delivered=1)
    browser, _, page = make_browser()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_NAME_NOT_RESOLVED"))

    await handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    msg.nak.assert_not_called()
    msg.ack.assert_called_once()
