"""
Shared fixtures and helpers for llm-worker unit tests.

The LLM_KEY_ENCRYPTION_KEY env var is set at module level — before any worker
module import triggers Settings() — so that pydantic-settings can validate the
field without a real .env present in non-Docker environments.

os.environ.setdefault is used so that a real key already present in the
environment (e.g. inside Docker with the mounted .env) is never clobbered.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

# Must be module-level: Settings() runs at import time, before any fixture executes.
os.environ.setdefault("LLM_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture
def mock_minio():
    """AsyncMock MinIO client — put_object and get_object are awaitable."""
    return AsyncMock()


def make_nats_msg(
    job_id: str = "job-aaa",
    run_id: str = "run-bbb",
    raw_minio_path: str = "scrapeflow-results/history/job-aaa/1234567890.html",
    provider: str = "anthropic",
    encrypted_api_key: str = "gAAAAAB_placeholder_encrypted_key",
    base_url: str | None = None,
    model: str = "claude-3-5-sonnet-20241022",
    output_schema: dict | None = None,
    stream_seq: int = 42,
) -> MagicMock:
    """
    Build a mock NATS message whose .data is a valid LLM JobMessage JSON payload.
    msg.ack is an AsyncMock so tests can assert it was called.

    encrypted_api_key is a placeholder string — tests that exercise actual
    decryption (test_llm.py) generate a real Fernet key+ciphertext pair inline.
    """
    if output_schema is None:
        output_schema = {"type": "object", "properties": {"name": {"type": "string"}}}

    payload: dict = {
        "job_id": job_id,
        "run_id": run_id,
        "raw_minio_path": raw_minio_path,
        "provider": provider,
        "encrypted_api_key": encrypted_api_key,
        "model": model,
        "output_schema": output_schema,
    }
    if base_url is not None:
        payload["base_url"] = base_url

    msg = MagicMock()
    msg.data = json.dumps(payload).encode()
    msg.metadata = MagicMock()
    msg.metadata.sequence = MagicMock()
    msg.metadata.sequence.stream = stream_seq
    msg.ack = AsyncMock()
    return msg
