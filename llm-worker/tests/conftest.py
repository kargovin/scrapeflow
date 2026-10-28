"""
Shared fixtures and helpers for llm-worker unit tests.

The LLM_KEY_ENCRYPTION_KEY env var is set at module level — before any worker
module import triggers Settings() — so that pydantic-settings can validate the
field without a real .env present in non-Docker environments.

os.environ.setdefault is used so that a real key already present in the
environment (e.g. inside Docker with the mounted .env) is never clobbered.
"""

import os
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

# Must be module-level: Settings() runs at import time, before any fixture executes.
os.environ.setdefault("LLM_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture
def mock_minio():
    """AsyncMock MinIO client — put_object and get_object are awaitable."""
    return AsyncMock()
