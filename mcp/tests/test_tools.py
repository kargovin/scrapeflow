"""
Integration tests for MCP tools against a mocked ScrapeFlow API.

Each test patches `make_client` in the specific tool module it exercises
(not in `client`) — that's where the name is bound after `from client import make_client`.

HTTP calls are intercepted via a custom AsyncBaseTransport. No live server needed.
"""

import uuid
from unittest.mock import patch

import httpx
import pytest

from tests.conftest import mock_client
from tools.get_job_status import get_job_status
from tools.get_result import get_result
from tools.list_jobs import list_jobs
from tools.scrape_url import scrape_url

JOB_ID = str(uuid.uuid4())

_JOB_QUEUED = {
    "id": JOB_ID,
    "status": "queued",
    "url": "https://example.com",
    "output_format": "html",
    "engine": "http",
    "created_at": "2026-01-01T00:00:00Z",
    "completed_at": None,
    "error": None,
}
_JOB_DONE = {**_JOB_QUEUED, "status": "completed", "completed_at": "2026-01-01T00:01:00Z"}
_JOB_FAILED = {**_JOB_QUEUED, "status": "failed", "error": "connection refused"}
# 200 bytes of "A" — exceeds the conftest-patched limit of 100 bytes
_RESULT = {
    "content": "A" * 200,
    "output_format": "html",
    "result_path": "history/test.html",
    "warnings": None,
}


# ---------------------------------------------------------------------------
# scrape_url
# ---------------------------------------------------------------------------


async def test_scrape_url_polls_until_completed():
    """scrape_url must poll until 'completed', then return content."""
    poll_count = 0

    async def _handler(req: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if req.method == "POST" and req.url.path == "/jobs":
            return httpx.Response(200, json=_JOB_QUEUED)
        if req.method == "GET" and req.url.path == f"/jobs/{JOB_ID}":
            poll_count += 1
            return httpx.Response(200, json=_JOB_QUEUED if poll_count == 1 else _JOB_DONE)
        if req.method == "GET" and req.url.path == f"/jobs/{JOB_ID}/result":
            return httpx.Response(200, json=_RESULT)
        return httpx.Response(404)

    with patch("tools.scrape_url.make_client", return_value=mock_client(_handler)):
        result = await scrape_url("https://example.com")

    assert result["status"] == "completed"
    assert result["job_id"] == JOB_ID
    assert "content" in result
    assert poll_count >= 2


async def test_scrape_url_truncates_large_content():
    """scrape_url must truncate content over mcp_max_content_bytes and add a notice."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json=_JOB_QUEUED)
        if req.url.path == f"/jobs/{JOB_ID}":
            return httpx.Response(200, json=_JOB_DONE)
        if req.url.path == f"/jobs/{JOB_ID}/result":
            return httpx.Response(200, json=_RESULT)
        return httpx.Response(404)

    with patch("tools.scrape_url.make_client", return_value=mock_client(_handler)):
        result = await scrape_url("https://example.com")

    # conftest patches mcp_max_content_bytes=100; content is 200 bytes
    assert len(result["content"].encode()) <= 100
    assert "notice" in result
    assert "truncated" in result["notice"]


async def test_scrape_url_no_wait_returns_job_id_immediately():
    """scrape_url with wait_for_result=False must return before polling starts."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json=_JOB_QUEUED)
        return httpx.Response(404)  # should never be reached

    with patch("tools.scrape_url.make_client", return_value=mock_client(_handler)):
        result = await scrape_url("https://example.com", wait_for_result=False)

    assert result["job_id"] == JOB_ID
    assert "content" not in result


async def test_scrape_url_returns_timeout_when_never_completes(monkeypatch):
    """scrape_url must return a timeout notice if the job never reaches a terminal status."""
    import config as cfg_module

    monkeypatch.setattr(cfg_module.settings, "mcp_max_wait_seconds", 0.05)
    monkeypatch.setattr(cfg_module.settings, "mcp_poll_interval_seconds", 0.01)

    async def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json=_JOB_QUEUED)
        return httpx.Response(200, json=_JOB_QUEUED)  # always queued

    with patch("tools.scrape_url.make_client", return_value=mock_client(_handler)):
        result = await scrape_url("https://example.com")

    assert result["status"] == "timeout"
    assert "notice" in result
    assert JOB_ID in result["notice"]


async def test_scrape_url_failed_job():
    """scrape_url must return status=failed and the error field when a job fails."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json=_JOB_QUEUED)
        return httpx.Response(200, json=_JOB_FAILED)

    with patch("tools.scrape_url.make_client", return_value=mock_client(_handler)):
        result = await scrape_url("https://example.com")

    assert result["status"] == "failed"
    assert result["error"] == "connection refused"


async def test_scrape_url_job_creation_failure():
    """scrape_url must return an error dict when POST /jobs fails."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    with patch("tools.scrape_url.make_client", return_value=mock_client(_handler)):
        result = await scrape_url("https://example.com")

    assert "error" in result
    assert "429" in result["error"]


# ---------------------------------------------------------------------------
# get_result
# ---------------------------------------------------------------------------


async def test_get_result_truncates_and_adds_notice():
    """get_result must truncate content >mcp_max_content_bytes and include a notice."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_RESULT)

    with patch("tools.get_result.make_client", return_value=mock_client(_handler)):
        result = await get_result(JOB_ID)

    assert len(result["content"].encode()) <= 100
    assert "notice" in result
    assert "truncated" in result["notice"]


async def test_get_result_no_truncation_when_content_fits():
    """get_result must return content unmodified when it fits within the limit."""
    small_result = {**_RESULT, "content": "hello"}

    async def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=small_result)

    with patch("tools.get_result.make_client", return_value=mock_client(_handler)):
        result = await get_result(JOB_ID)

    assert result["content"] == "hello"
    assert "notice" not in result


async def test_get_result_not_found():
    """get_result must return an error dict on 404."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with patch("tools.get_result.make_client", return_value=mock_client(_handler)):
        result = await get_result(JOB_ID)

    assert "error" in result
    assert result["job_id"] == JOB_ID


# ---------------------------------------------------------------------------
# get_job_status
# ---------------------------------------------------------------------------


async def test_get_job_status_returns_key_fields():
    """get_job_status must return the expected status fields."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_JOB_DONE)

    with patch("tools.get_job_status.make_client", return_value=mock_client(_handler)):
        result = await get_job_status(JOB_ID)

    assert result["status"] == "completed"
    assert result["job_id"] == JOB_ID
    assert result["url"] == "https://example.com"
    assert result["completed_at"] is not None


async def test_get_job_status_not_found():
    """get_job_status must return an error dict on 404."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with patch("tools.get_job_status.make_client", return_value=mock_client(_handler)):
        result = await get_job_status(JOB_ID)

    assert "error" in result
    assert result["job_id"] == JOB_ID


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


async def test_list_jobs_returns_job_summaries():
    """list_jobs must return a list of job summaries with count and offset."""
    job2 = {**_JOB_DONE, "id": str(uuid.uuid4()), "status": "failed"}
    jobs_response = [_JOB_DONE, job2]

    async def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jobs_response)

    with patch("tools.list_jobs.make_client", return_value=mock_client(_handler)):
        result = await list_jobs()

    assert result["count"] == 2
    assert result["offset"] == 0
    assert result["jobs"][0]["status"] == "completed"
    assert result["jobs"][1]["status"] == "failed"


async def test_list_jobs_passes_limit_and_offset():
    """list_jobs must pass limit and offset query params to the API."""
    received_params = {}

    async def _handler(req: httpx.Request) -> httpx.Response:
        received_params["limit"] = req.url.params.get("limit")
        received_params["offset"] = req.url.params.get("offset")
        return httpx.Response(200, json=[])

    with patch("tools.list_jobs.make_client", return_value=mock_client(_handler)):
        await list_jobs(limit=5, offset=10)

    assert received_params["limit"] == "5"
    assert received_params["offset"] == "10"


async def test_list_jobs_api_error():
    """list_jobs must return an error dict on API failure."""
    async def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with patch("tools.list_jobs.make_client", return_value=mock_client(_handler)):
        result = await list_jobs()

    assert "error" in result
