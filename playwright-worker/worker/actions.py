"""
Playwright action executor.

Executes a list of page actions sequentially. Each action failure is caught
and appended to a warnings list — the sequence continues regardless. This
partial-failure design lets the scrape proceed with whatever DOM state is
available after the actions that did succeed.
"""

import asyncio
from typing import Any

import structlog
from miniopy_async import Minio

from .storage import upload_screenshot

log = structlog.get_logger()


async def execute_actions(
    page: Any,
    minio: Minio,
    job_id: str,
    actions: list[dict],
) -> tuple[list[str], list[str]]:
    """
    Execute page actions sequentially; return (warnings, screenshot_paths).

    warnings       — one entry per failed action, format: "action {type} failed: {error}"
    screenshot_paths — MinIO paths for any screenshot actions that succeeded
    """
    warnings: list[str] = []
    screenshot_paths: list[str] = []
    screenshot_index = 0

    for action in actions:
        action_type = action.get("type", "unknown")
        try:
            await _dispatch(
                action, page, minio, job_id, screenshot_index, screenshot_paths
            )
            if action_type == "screenshot":
                screenshot_index += 1
        except Exception as exc:
            warnings.append(f"action {action_type} failed: {exc}")
            log.warning("action_failed", action_type=action_type, error=str(exc))

    return warnings, screenshot_paths


async def _dispatch(
    action: dict,
    page: Any,
    minio: Minio,
    job_id: str,
    screenshot_index: int,
    screenshot_paths: list[str],
) -> None:
    action_type = action.get("type")

    if action_type == "wait":
        ms = int(action["milliseconds"])
        await asyncio.sleep(ms / 1000)

    elif action_type == "wait_for_selector":
        timeout = action.get("timeout", 5000)
        await page.wait_for_selector(action["selector"], timeout=timeout)

    elif action_type == "click":
        timeout = action.get("timeout", 30000)
        await page.click(action["selector"], timeout=timeout)

    elif action_type == "type":
        await page.fill(action["selector"], action["text"])

    elif action_type == "press":
        await page.keyboard.press(action["key"])

    elif action_type == "scroll":
        amount = int(action.get("amount", 1))
        direction = action.get("direction", "down")
        delta = amount if direction == "down" else -amount
        await page.evaluate(f"window.scrollBy(0, {delta} * window.innerHeight)")

    elif action_type == "execute_js":
        await page.evaluate(action["script"])

    elif action_type == "screenshot":
        png_bytes = await page.screenshot()
        path = await upload_screenshot(minio, job_id, screenshot_index, png_bytes)
        screenshot_paths.append(path)

    else:
        raise ValueError(f"unknown action type: {action_type!r}")
