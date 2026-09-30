"""
Unit tests for worker/activity.py — the Scrape activity on scrape-playwright (B.8).

ActivityEnvironment runs the activity without a server. The browser is the conftest
mock stack; MinIO is an AsyncMock, so the real storage.upload builds the real key.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import xxhash
from miniopy_async.error import S3Error
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from tests.conftest import encrypt_credential, make_browser
from tests.test_blocking import MYNTRA_MAINTENANCE_WALL
from worker.activity import (
    BLOCKED,
    ROBOTS_DISALLOWED,
    SCRAPE_FAILED,
    STORAGE_TRANSIENT,
    PlayWrightScrapeActivities,
)
from worker.config import settings
from worker.contracts import ScrapeInput, ScrapeOutput

_ARTIFACT = "3f8b7a12-0c44-4e7a-9a1e-1b2c3d4e5f60"
_BUCKET = settings.minio_bucket

# Large enough that no size-gated wall rule fires on it.
PAGE = (
    "<html><head><title>Real page</title></head><body>"
    + "<p>Genuine product description paragraph.</p>" * 200
    + "</body></html>"
)


def _input(**overrides) -> ScrapeInput:
    fields = {
        "artifact_id": _ARTIFACT,
        "url": "https://example.com/item",
        "output_format": "html",
    }
    fields.update(overrides)
    return ScrapeInput(**fields)


def _browser(html: str = PAGE, status: int = 200):
    browser, context, page = make_browser(html=html)
    page.goto = AsyncMock(return_value=MagicMock(status=status))
    return browser, context, page


async def _run(job: ScrapeInput | None = None, *, browser=None, minio=None):
    """Run the activity; returns (output, heartbeats)."""
    env = ActivityEnvironment()
    beats: list = []
    env.on_heartbeat = lambda *details: beats.append(details)
    if browser is None:
        browser, _, _ = _browser()
    activities = PlayWrightScrapeActivities(minio or AsyncMock(), browser)
    out = await env.run(activities.playwright_scrape, job or _input())
    return out, beats


async def _raises(job: ScrapeInput | None = None, **kwargs) -> ApplicationError:
    with pytest.raises(ApplicationError) as excinfo:
        await _run(job, **kwargs)
    return excinfo.value


def _s3_error(code: str) -> S3Error:
    return S3Error(
        code=code,
        message="m",
        resource="/x",
        request_id="r",
        host_id="h",
        response=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


async def test_returns_the_stored_object_size_and_hash():
    minio = AsyncMock()
    browser, context, _ = _browser()

    out, _ = await _run(browser=browser, minio=minio)

    assert isinstance(out, ScrapeOutput)
    assert out.result.path == f"{_BUCKET}/history/{_ARTIFACT}/scrape.html"
    uploaded = minio.put_object.await_args.args[2].getvalue()
    assert out.result.size == len(uploaded)
    assert out.content_hash == xxhash.xxh64(uploaded).hexdigest()
    assert out.warnings == [] and out.screenshots == []
    context.close.assert_awaited_once()


async def test_content_hash_keeps_leading_zeros():
    # The API compares against xxh64(...).hexdigest(); a hash below 2^60 must still be 16 chars.
    html = next(
        h
        for h in (f"{PAGE}<!-- {i} -->" for i in range(10_000))
        if xxhash.xxh64(h.encode()).intdigest() < 2**60
    )

    out, _ = await _run(browser=_browser(html)[0])

    assert out.content_hash.startswith("0")
    assert len(out.content_hash) == 16


async def test_output_format_names_the_object():
    out, _ = await _run(_input(output_format="markdown"))

    assert out.result.path == f"{_BUCKET}/history/{_ARTIFACT}/scrape.md"


async def test_screenshots_carry_their_stored_size():
    minio = AsyncMock()
    minio.stat_object.return_value = MagicMock(size=4321)
    browser, _, page = _browser()
    page.screenshot = AsyncMock(return_value=b"\x89PNG")

    out, _ = await _run(
        _input(options={"actions": [{"type": "screenshot"}]}),
        browser=browser,
        minio=minio,
    )

    assert [(s.path, s.size) for s in out.screenshots] == [
        (f"{_BUCKET}/screenshots/{_ARTIFACT}/0.png", 4321)
    ]
    assert minio.stat_object.await_args.args == (
        _BUCKET,
        f"screenshots/{_ARTIFACT}/0.png",
    )


async def test_failed_action_is_a_warning_not_a_failure():
    browser, _, page = _browser()
    page.click = AsyncMock(side_effect=Exception("no such element"))

    out, _ = await _run(
        _input(options={"actions": [{"type": "click", "selector": "#x"}]}),
        browser=browser,
    )

    assert out.warnings == ["action click failed: no such element"]


async def test_heartbeats_through_the_render(monkeypatch):
    monkeypatch.setattr(settings, "playwright_heartbeat_seconds", 0.01)
    browser, _, page = _browser()

    async def slow_goto(*_a, **_kw):
        await asyncio.sleep(0.1)
        return MagicMock(status=200)

    page.goto = slow_goto

    _, beats = await _run(browser=browser)

    assert len(beats) >= 3


# ---------------------------------------------------------------------------
# Non-retryable
# ---------------------------------------------------------------------------


async def test_bot_wall_is_non_retryable_and_not_uploaded():
    minio = AsyncMock()

    err = await _raises(browser=_browser(MYNTRA_MAINTENANCE_WALL)[0], minio=minio)

    assert err.non_retryable
    assert err.type == BLOCKED
    assert err.message.startswith("blocked:")
    minio.put_object.assert_not_called()


async def test_robots_disallow_is_non_retryable_and_opens_no_context():
    browser, _, _ = _browser()
    with patch(
        "worker.activity.is_disallowed", new_callable=AsyncMock, return_value=True
    ):
        err = await _raises(_input(options={"respect_robots": True}), browser=browser)

    assert err.non_retryable
    assert err.type == ROBOTS_DISALLOWED
    assert err.message == "robots_txt_disallowed"
    browser.new_context.assert_not_called()


async def test_robots_fetch_failure_proceeds():
    with patch(
        "worker.activity.is_disallowed",
        new_callable=AsyncMock,
        side_effect=Exception("robots.txt timed out"),
    ):
        out, _ = await _run(_input(options={"respect_robots": True}))

    assert out.result.path.endswith("/scrape.html")


async def test_dead_site_is_non_retryable():
    browser, _, page = _browser()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_NAME_NOT_RESOLVED"))

    err = await _raises(browser=browser)

    assert err.non_retryable
    assert err.type == SCRAPE_FAILED
    assert "ERR_NAME_NOT_RESOLVED" in err.message


async def test_undecryptable_credentials_are_non_retryable():
    browser, _, _ = _browser()

    err = await _raises(
        _input(credentials={"encrypted_proxy_url": "gAAAAA-not-a-token"}),
        browser=browser,
    )

    assert err.non_retryable
    assert err.type == SCRAPE_FAILED
    assert err.message.startswith("InvalidToken")
    browser.new_context.assert_not_called()


async def test_decrypted_proxy_reaches_the_context():
    browser, _, _ = _browser()

    await _run(
        _input(
            credentials={
                "encrypted_proxy_url": encrypt_credential(
                    "http://u%40x:p%3Aw@proxy:8080"
                )
            }
        ),
        browser=browser,
    )

    assert browser.new_context.await_args.kwargs["proxy"] == {
        "server": "http://proxy:8080",
        "username": "u@x",
        "password": "p:w",
    }


# ---------------------------------------------------------------------------
# Retryable — MinIO faults after a successful render
# ---------------------------------------------------------------------------


async def test_minio_5xx_on_upload_is_retryable():
    minio = AsyncMock()
    minio.put_object.side_effect = _s3_error("InternalError")

    err = await _raises(minio=minio)

    assert not err.non_retryable
    assert err.type == STORAGE_TRANSIENT


async def test_minio_unreachable_on_upload_is_retryable():
    minio = AsyncMock()
    minio.put_object.side_effect = aiohttp.ClientConnectionError("connection refused")

    err = await _raises(minio=minio)

    assert not err.non_retryable
    assert err.type == STORAGE_TRANSIENT


async def test_minio_caller_error_is_non_retryable():
    minio = AsyncMock()
    minio.put_object.side_effect = _s3_error("NoSuchBucket")

    err = await _raises(minio=minio)

    assert err.non_retryable
    assert err.type == SCRAPE_FAILED


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancel_passes_through_and_closes_the_context():
    browser, context, page = _browser()
    page.goto = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _run(browser=browser)

    context.close.assert_awaited_once()
