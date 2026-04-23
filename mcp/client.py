"""
ScrapeFlow HTTP API client.

All tool modules import this — SCRAPEFLOW_API_KEY is injected here as a
Bearer token on every request, so individual tools have no auth concerns.
"""

import httpx

from config import settings

_HEADERS = {"Authorization": f"Bearer {settings.scrapeflow_api_key}"}


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.scrapeflow_api_url,
        headers=_HEADERS,
        timeout=30.0,
    )
