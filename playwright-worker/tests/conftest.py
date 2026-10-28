"""
Shared test fixtures for playwright-worker unit tests.

No live connections here — everything is mocked. The fixtures below
provide reusable mock objects; make_browser lives here since it needs
per-test tuning.
"""

import os
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

# Must be module-level: Settings() runs at import time, before any fixture executes.
_TEST_CREDS_KEY = Fernet.generate_key().decode()
os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", _TEST_CREDS_KEY)


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a plaintext credential using the test key — mirrors API-side encryption."""
    return Fernet(_TEST_CREDS_KEY).encrypt(plaintext.encode()).decode()


@pytest.fixture
def mock_minio():
    """AsyncMock MinIO client — put_object is awaitable and records calls."""
    return AsyncMock()


def make_browser(
    html: str = "<html><head><title>Test Page</title></head><body><p>Hello</p></body></html>",
    page_url: str = "https://example.com/",
):
    """
    Build a mock Playwright browser / context / page stack.
    Returns (browser, context, page) so tests can assert on each level.

    page.content() returns `html`; page.url is the string `page_url`.
    All async methods are AsyncMock so they can be awaited and inspected.
    """
    page = AsyncMock()
    page.url = page_url  # property access, not a call — must be a plain string
    page.content = AsyncMock(return_value=html)

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    return browser, context, page
