"""
Unit tests for worker/actions.py — execute_actions().

All Playwright page methods are AsyncMock. MinIO upload_screenshot is
patched at the import site in worker.actions.
"""

from unittest.mock import AsyncMock, patch


from worker.actions import execute_actions


def make_page():
    """Build a minimal mock Playwright page with all needed async methods."""
    page = AsyncMock()
    page.keyboard = AsyncMock()
    page.screenshot = AsyncMock(return_value=b"\x89PNG fake")
    return page


# ---------------------------------------------------------------------------
# Individual action types
# ---------------------------------------------------------------------------


async def test_wait_action():
    page = make_page()
    with patch("worker.actions.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        warnings, _ = await execute_actions(
            page, AsyncMock(), "job-1", [{"type": "wait", "milliseconds": 500}]
        )
    mock_sleep.assert_called_once_with(0.5)
    assert warnings == []


async def test_wait_for_selector_action():
    page = make_page()
    warnings, _ = await execute_actions(
        page,
        AsyncMock(),
        "job-1",
        [{"type": "wait_for_selector", "selector": ".content", "timeout": 3000}],
    )
    page.wait_for_selector.assert_called_once_with(".content", timeout=3000)
    assert warnings == []


async def test_wait_for_selector_default_timeout():
    page = make_page()
    await execute_actions(
        page, AsyncMock(), "job-1", [{"type": "wait_for_selector", "selector": ".btn"}]
    )
    page.wait_for_selector.assert_called_once_with(".btn", timeout=5000)


async def test_click_action():
    page = make_page()
    warnings, _ = await execute_actions(
        page, AsyncMock(), "job-1", [{"type": "click", "selector": "#submit"}]
    )
    page.click.assert_called_once_with("#submit", timeout=30000)
    assert warnings == []


async def test_type_action():
    page = make_page()
    warnings, _ = await execute_actions(
        page,
        AsyncMock(),
        "job-1",
        [{"type": "type", "selector": "input[name=q]", "text": "hello"}],
    )
    page.fill.assert_called_once_with("input[name=q]", "hello")
    assert warnings == []


async def test_press_action():
    page = make_page()
    warnings, _ = await execute_actions(
        page, AsyncMock(), "job-1", [{"type": "press", "key": "Enter"}]
    )
    page.keyboard.press.assert_called_once_with("Enter")
    assert warnings == []


async def test_scroll_down_action():
    page = make_page()
    await execute_actions(
        page,
        AsyncMock(),
        "job-1",
        [{"type": "scroll", "direction": "down", "amount": 2}],
    )
    page.evaluate.assert_called_once_with("window.scrollBy(0, 2 * window.innerHeight)")


async def test_scroll_up_action():
    page = make_page()
    await execute_actions(
        page, AsyncMock(), "job-1", [{"type": "scroll", "direction": "up", "amount": 1}]
    )
    page.evaluate.assert_called_once_with("window.scrollBy(0, -1 * window.innerHeight)")


async def test_execute_js_action():
    page = make_page()
    warnings, _ = await execute_actions(
        page,
        AsyncMock(),
        "job-1",
        [{"type": "execute_js", "script": "return document.title"}],
    )
    page.evaluate.assert_called_once_with("return document.title")
    assert warnings == []


async def test_screenshot_action_uploads_to_minio():
    """Screenshot bytes must be uploaded; path returned in screenshot_paths."""
    page = make_page()
    fake_path = "scrapeflow-results/screenshots/job-1/1234_0.png"

    with patch(
        "worker.actions.upload_screenshot", new_callable=AsyncMock
    ) as mock_upload:
        mock_upload.return_value = fake_path
        warnings, screenshot_paths = await execute_actions(
            page, AsyncMock(), "job-1", [{"type": "screenshot"}]
        )

    mock_upload.assert_called_once()
    assert screenshot_paths == [fake_path]
    assert warnings == []


# ---------------------------------------------------------------------------
# Partial failure handling
# ---------------------------------------------------------------------------


async def test_partial_failure_continues_to_next_action():
    """
    When the second action raises, the third action must still execute.
    The partial-failure design lets the scrape proceed with whatever DOM
    state the successful actions produced.
    """
    page = make_page()
    page.click = AsyncMock(side_effect=Exception("element not found"))

    actions = [
        {"type": "wait_for_selector", "selector": ".first"},
        {"type": "click", "selector": "#missing"},
        {"type": "wait_for_selector", "selector": ".third"},
    ]
    warnings, _ = await execute_actions(page, AsyncMock(), "job-1", actions)

    # First and third selectors must still have been called
    assert page.wait_for_selector.call_count == 2
    assert len(warnings) == 1


async def test_warning_message_format():
    """Warning strings must identify the action type and include the error."""
    page = make_page()
    page.click = AsyncMock(side_effect=Exception("timeout 30000ms exceeded"))

    warnings, _ = await execute_actions(
        page, AsyncMock(), "job-1", [{"type": "click", "selector": "#btn"}]
    )

    assert len(warnings) == 1
    assert "click" in warnings[0]
    assert "timeout 30000ms exceeded" in warnings[0]


async def test_unknown_action_type_produces_warning():
    """An unrecognised action type must not raise — it produces a warning."""
    page = make_page()
    warnings, _ = await execute_actions(
        page, AsyncMock(), "job-1", [{"type": "teleport", "destination": "moon"}]
    )
    assert len(warnings) == 1
    assert "teleport" in warnings[0]


async def test_multiple_screenshots_indexed():
    """Each screenshot action gets a unique index passed to upload_screenshot."""
    page = make_page()

    with patch(
        "worker.actions.upload_screenshot", new_callable=AsyncMock
    ) as mock_upload:
        mock_upload.side_effect = lambda m, job_id, idx, data: f"path/{idx}.png"
        _, screenshot_paths = await execute_actions(
            page,
            AsyncMock(),
            "job-1",
            [
                {"type": "screenshot"},
                {"type": "screenshot"},
            ],
        )

    assert len(screenshot_paths) == 2
    # Indices 0 and 1 must be distinct
    calls = mock_upload.call_args_list
    assert calls[0].args[2] == 0
    assert calls[1].args[2] == 1
