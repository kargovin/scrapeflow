"""
Unit tests for worker/worker.py — handle_message().

Verifies the full ADR-002 job lifecycle without any live infrastructure.
NATS, MinIO, and both LLM providers are all mocked.

Patching strategy — all patches are applied at the import site in worker.worker:
  - worker.worker.fetch_content  (defined in same module)
  - worker.worker.call_llm       (imported from worker.llm)
  - worker.worker.upload         (imported from worker.storage)

Lifecycle summary:
  1. Parse JobMessage — ack+skip if malformed
  2. Publish status='running' with nats_stream_seq
  3. fetch_content from MinIO using raw_minio_path
  4. call_llm → structured JSON dict
  5. upload JSON bytes to MinIO
  6. Publish status='completed' with minio_path
  7. msg.ack() — always, even on failure
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import make_nats_msg
from worker.worker import RESULT_SUBJECT, handle_message

_FAKE_MINIO_PATH = "scrapeflow-results/history/job-aaa/1234567890.json"
_FAKE_CONTENT = "<html><body>Product: Alice, price $9.99</body></html>"
_FAKE_RESULT = {"name": "Alice", "price": 9.99}


# ---------------------------------------------------------------------------
# Helper: run handle_message with all external deps patched
# ---------------------------------------------------------------------------


async def _run(msg, js=None):
    """
    Run handle_message with fetch_content, call_llm, and upload all stubbed.
    Returns (js, mock_fetch, mock_llm, mock_upload).
    """
    js = js or AsyncMock()
    with patch(
        "worker.worker.fetch_content", new_callable=AsyncMock
    ) as mock_fetch, patch(
        "worker.worker.call_llm", new_callable=AsyncMock
    ) as mock_llm, patch("worker.worker.upload", new_callable=AsyncMock) as mock_upload:
        mock_fetch.return_value = _FAKE_CONTENT
        mock_llm.return_value = _FAKE_RESULT
        mock_upload.return_value = _FAKE_MINIO_PATH
        await handle_message(msg, js, AsyncMock())
    return js, mock_fetch, mock_llm, mock_upload


# ---------------------------------------------------------------------------
# Malformed message
# ---------------------------------------------------------------------------


async def test_malformed_json_is_acked_immediately():
    """
    A message that cannot be parsed as JobMessage must be acked and dropped.
    No publishes or MinIO reads should occur — there is nothing to process.
    """
    msg = MagicMock()
    msg.data = b"not valid json {{{"
    msg.ack = AsyncMock()
    js = AsyncMock()

    await handle_message(msg, js, AsyncMock())

    msg.ack.assert_called_once()
    js.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path — publish ordering
# ---------------------------------------------------------------------------


async def test_first_publish_is_running_with_stream_seq():
    """
    The very first NATS publish must be status='running' and carry
    nats_stream_seq from msg.metadata.sequence.stream.
    """
    msg = make_nats_msg(stream_seq=77)
    js = AsyncMock()

    js, _, _, _ = await _run(msg, js=js)

    first_call = js.publish.call_args_list[0]
    subject, payload_bytes = first_call.args
    data = json.loads(payload_bytes)

    assert subject == RESULT_SUBJECT
    assert data["status"] == "running"
    assert data["nats_stream_seq"] == 77
    assert "minio_path" not in data


async def test_second_publish_is_completed_with_minio_path():
    """
    After a successful upload, the second publish must be status='completed'
    with the minio_path returned by upload().
    """
    msg = make_nats_msg()
    js = AsyncMock()

    js, _, _, _ = await _run(msg, js=js)

    assert js.publish.call_count == 2
    second_call = js.publish.call_args_list[1]
    _, payload_bytes = second_call.args
    data = json.loads(payload_bytes)

    assert data["status"] == "completed"
    assert data["minio_path"] == _FAKE_MINIO_PATH
    assert "error" not in data


async def test_ack_called_once_on_success():
    """msg.ack() must be called exactly once after a successful job."""
    msg = make_nats_msg()
    await _run(msg)
    msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# Input forwarding
# ---------------------------------------------------------------------------


async def test_fetch_content_called_with_raw_minio_path():
    """
    fetch_content must be called with the raw_minio_path from the job message
    so the worker reads the correct scrape output from MinIO.
    """
    path = "scrapeflow-results/history/job-xyz/9999999.html"
    msg = make_nats_msg(raw_minio_path=path)

    _, mock_fetch, _, _ = await _run(msg)

    # fetch_content(minio, raw_minio_path) — raw_minio_path is args[1]
    called_path = mock_fetch.call_args.args[1]
    assert called_path == path


async def test_call_llm_receives_correct_args():
    """
    call_llm must be called with the provider, model, base_url, and
    output_schema from the job message so the right LLM is invoked.
    """
    msg = make_nats_msg(
        provider="openai_compatible",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        output_schema={"type": "object", "properties": {"price": {"type": "number"}}},
    )

    _, _, mock_llm, _ = await _run(msg)

    call_kwargs = mock_llm.call_args.kwargs
    assert call_kwargs["provider"] == "openai_compatible"
    assert call_kwargs["model"] == "gpt-4o"
    assert call_kwargs["base_url"] == "https://api.openai.com/v1"
    assert call_kwargs["content"] == _FAKE_CONTENT


# ---------------------------------------------------------------------------
# Failure path — call_llm raises
# ---------------------------------------------------------------------------


async def test_llm_failure_publishes_failed():
    """
    When call_llm raises, the worker must publish status='failed' with the
    error string so the API result consumer can mark the run failed.
    """
    msg = make_nats_msg()
    js = AsyncMock()

    with patch(
        "worker.worker.fetch_content", new_callable=AsyncMock
    ) as mock_fetch, patch(
        "worker.worker.call_llm", new_callable=AsyncMock
    ) as mock_llm, patch("worker.worker.upload", new_callable=AsyncMock):
        mock_fetch.return_value = _FAKE_CONTENT
        mock_llm.side_effect = Exception("rate limit exceeded")
        await handle_message(msg, js, AsyncMock())

    last_call = js.publish.call_args_list[-1]
    _, payload_bytes = last_call.args
    data = json.loads(payload_bytes)

    assert data["status"] == "failed"
    assert data["error"] == "rate limit exceeded"
    assert "minio_path" not in data


async def test_ack_called_on_failure():
    """
    msg.ack() must be called even when call_llm raises.
    The API already knows the run failed via the result event — re-delivery
    won't recover a bad LLM key or a provider outage.
    """
    msg = make_nats_msg()

    with patch(
        "worker.worker.fetch_content", new_callable=AsyncMock
    ) as mock_fetch, patch(
        "worker.worker.call_llm", new_callable=AsyncMock
    ) as mock_llm, patch("worker.worker.upload", new_callable=AsyncMock):
        mock_fetch.return_value = _FAKE_CONTENT
        mock_llm.side_effect = Exception("timeout")
        await handle_message(msg, AsyncMock(), AsyncMock())

    msg.ack.assert_called_once()
