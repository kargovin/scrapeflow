"""
Shared fixtures for MCP tool tests.

The env var must be set before `config` is imported, because Settings()
is instantiated at module level and pydantic-settings reads the env at that point.
"""

import os

os.environ.setdefault("SCRAPEFLOW_API_KEY", "test-key")

import httpx
import pytest


class _AsyncTransport(httpx.AsyncBaseTransport):
    """Thin async transport that delegates to an async handler function."""

    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)


def mock_client(handler) -> httpx.AsyncClient:
    """Return an AsyncClient backed by the given async handler."""
    return httpx.AsyncClient(
        transport=_AsyncTransport(handler),
        base_url="http://test",
    )


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """Speed up poll loop so tests don't sleep for real intervals."""
    import config as cfg_module

    monkeypatch.setattr(cfg_module.settings, "mcp_poll_interval_seconds", 0.01)
    monkeypatch.setattr(cfg_module.settings, "mcp_max_wait_seconds", 5.0)
    monkeypatch.setattr(cfg_module.settings, "mcp_max_content_bytes", 100)
