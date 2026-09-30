"""
Unit tests for worker/activity.py — the LLMExtract Temporal activity (B.6).

ActivityEnvironment runs the activity without a server; MinIO and the providers
are patched at the import site in worker.activity. The bad-input test needs a
real worker, so it runs against the SDK's time-skipping test server (downloaded
on first use).
"""

import asyncio
import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import httpx
import openai
import pytest
from cryptography.fernet import Fernet
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from worker import llm
from worker.activity import (
    LLM_FAILED,
    LLM_TRANSIENT,
    STORAGE_TRANSIENT,
    LLMActivities,
)
from worker.config import settings
from worker.contracts import LLM_EXTRACT_ACTIVITY, LLMInput, LLMOutput
from worker.errors import WarmupTimeout

_REQ = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
_ARTIFACT = "3f8b7a12-0c44-4e7a-9a1e-1b2c3d4e5f60"
_RAW = f"scrapeflow-results/history/{_ARTIFACT}/scrape.html"
_RESULT = {"name": "Alice", "price": 9.99}
_BASE = "https://model--x.modal.run/v1"


def _input(**overrides) -> LLMInput:
    fields = {
        "artifact_id": _ARTIFACT,
        "raw_minio_path": _RAW,
        "provider": "anthropic",
        "encrypted_api_key": "gAAAAAB_placeholder",
        "model": "claude-sonnet-5-5",
        "output_schema": {"type": "object"},
    }
    fields.update(overrides)
    return LLMInput(**fields)


def _env() -> tuple[ActivityEnvironment, list]:
    env = ActivityEnvironment()
    beats: list = []
    env.on_heartbeat = lambda *details: beats.append(details)
    return env, beats


async def _run(
    *,
    llm_side_effect=None,
    upload_side_effect=None,
    fetch_side_effect=None,
    mocks_out=None,
):
    """Run llm_extract with fetch/call_llm/upload patched. Returns (output, mocks, beats).

    mocks_out, if given, receives the mocks before the run — for tests that expect a raise.
    """
    env, beats = _env()
    with patch("worker.activity.fetch_content", new_callable=AsyncMock) as fetch, patch(
        "worker.activity.call_llm", new_callable=AsyncMock
    ) as call, patch("worker.activity.upload", new_callable=AsyncMock) as up:
        if mocks_out is not None:
            mocks_out.extend([fetch, call, up])
        fetch.return_value = "<html>Alice $9.99</html>"
        fetch.side_effect = fetch_side_effect
        call.return_value = _RESULT
        call.side_effect = llm_side_effect
        up.side_effect = upload_side_effect or (
            lambda _m,
            artifact_id,
            _d: f"scrapeflow-results/history/{artifact_id}/llm.json"
        )
        out = await env.run(LLMActivities(AsyncMock()).llm_extract, _input())
    return out, (fetch, call, up), beats


async def _raises(**kwargs) -> ApplicationError:
    with pytest.raises(ApplicationError) as excinfo:
        await _run(**kwargs)
    return excinfo.value


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


async def test_returns_the_stored_object_with_its_size():
    out, (fetch, call, up), _ = await _run()

    assert isinstance(out, LLMOutput)
    assert out.result.path == f"scrapeflow-results/history/{_ARTIFACT}/llm.json"
    assert out.result.size == len(json.dumps(_RESULT).encode())
    fetch.assert_awaited_once()
    assert fetch.await_args.args[1] == _RAW
    assert up.await_args.args[2] == json.dumps(_RESULT).encode()


async def test_passes_the_input_to_call_llm():
    _, (_, call, _), _ = await _run()

    kwargs = call.await_args.kwargs
    assert kwargs["provider"] == "anthropic"
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["encrypted_api_key"] == "gAAAAAB_placeholder"
    assert kwargs["output_schema"] == {"type": "object"}
    assert kwargs["content"] == "<html>Alice $9.99</html>"


# ---------------------------------------------------------------------------
# Failures → ApplicationError, via classify()
# ---------------------------------------------------------------------------


async def test_rate_limit_is_retryable():
    err = await _raises(
        llm_side_effect=openai.RateLimitError(
            "slow down", response=httpx.Response(429, request=_REQ), body=None
        )
    )

    assert err.non_retryable is False
    assert err.type == LLM_TRANSIENT
    assert "RateLimitError" in err.message


async def test_bad_key_is_not_retryable():
    err = await _raises(
        llm_side_effect=openai.AuthenticationError(
            "invalid key", response=httpx.Response(401, request=_REQ), body=None
        )
    )

    assert err.non_retryable is True
    assert err.type == LLM_FAILED
    assert "AuthenticationError" in err.message


async def test_unknown_error_is_not_retryable():
    """Fail closed: a raw exception would be retried by the SDK against the user's key."""
    err = await _raises(llm_side_effect=KeyError("choices"))

    assert err.non_retryable is True
    assert err.type == LLM_FAILED


async def test_warmup_timeout_is_retryable():
    err = await _raises(llm_side_effect=WarmupTimeout("never woke"))

    assert err.non_retryable is False
    assert err.type == LLM_TRANSIENT


async def test_minio_unreachable_on_upload_is_retryable():
    err = await _raises(upload_side_effect=aiohttp.ClientConnectionError("refused"))

    assert err.non_retryable is False
    assert err.type == STORAGE_TRANSIENT


async def test_minio_unreachable_on_fetch_is_retryable_and_skips_the_llm():
    mocks: list = []
    err = await _raises(
        fetch_side_effect=aiohttp.ClientConnectionError("refused"), mocks_out=mocks
    )

    assert err.non_retryable is False
    assert err.type == STORAGE_TRANSIENT
    mocks[1].assert_not_awaited()


async def test_cancel_is_not_converted_to_an_application_error():
    env, _ = _env()
    started = asyncio.Event()

    async def hang(**_):
        started.set()
        await asyncio.Event().wait()

    with patch("worker.activity.fetch_content", new_callable=AsyncMock), patch(
        "worker.activity.call_llm", side_effect=hang
    ):
        task = asyncio.create_task(
            env.run(LLMActivities(AsyncMock()).llm_extract, _input())
        )
        await started.wait()
        env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Heartbeat through a cold start (Rider 2)
# ---------------------------------------------------------------------------


async def test_heartbeats_through_the_warmup_probe():
    llm._warm_until.clear()
    encrypted = Fernet(settings.llm_key_encryption_key).encrypt(b"sk-test").decode()

    probe = MagicMock()
    probe.get = AsyncMock(
        side_effect=[httpx.ConnectError("booting")] * 6 + [httpx.Response(200)]
    )
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=probe)
    ctx.__aexit__ = AsyncMock(return_value=False)

    env, beats = _env()
    with patch("worker.activity.fetch_content", new_callable=AsyncMock) as fetch, patch(
        "worker.activity.upload", new_callable=AsyncMock
    ) as up, patch("worker.llm.httpx.AsyncClient", return_value=ctx), patch(
        "worker.llm._call_openai_compatible", new_callable=AsyncMock
    ) as call, patch.object(
        settings, "llm_warmup_poll_interval_seconds", 0.05
    ), patch.object(settings, "llm_heartbeat_seconds", 0.02):
        fetch.return_value = "content"
        up.return_value = "b/k"
        call.return_value = _RESULT
        await env.run(
            LLMActivities(AsyncMock()).llm_extract,
            _input(
                provider="openai_compatible",
                base_url=_BASE,
                encrypted_api_key=encrypted,
            ),
        )
    llm._warm_until.clear()

    assert probe.get.await_count == 7
    # ~0.3 s of warm-up at a 0.02 s interval; a single beat would mean the task stopped.
    assert len(beats) >= 5


# ---------------------------------------------------------------------------
# A bad input is rejected before the activity body runs
# ---------------------------------------------------------------------------


@workflow.defn(sandboxed=False)
class _BadInputWorkflow:
    @workflow.run
    async def run(self, task_queue: str) -> str:
        try:
            await workflow.execute_activity(
                LLM_EXTRACT_ACTIVITY,
                # artifact_id="" fails LLMInput's min_length on decode.
                {
                    "artifact_id": "",
                    "raw_minio_path": _RAW,
                    "provider": "anthropic",
                    "encrypted_api_key": "k",
                    "model": "m",
                    "output_schema": {},
                },
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=5),
                    backoff_coefficient=2,
                    maximum_interval=timedelta(seconds=60),
                    maximum_attempts=3,
                ),
            )
        except ActivityError as err:
            return f"{err.retry_state.name}|{err.cause.message}"
        return "completed"


async def test_bad_input_fails_on_decode_after_the_retry_policy_runs_out():
    """The SDK raises a *retryable* ApplicationError on decode: three instant attempts, no call."""
    task_queue = f"test-llm-{uuid.uuid4()}"
    with patch("worker.activity.fetch_content", new_callable=AsyncMock) as fetch, patch(
        "worker.activity.call_llm", new_callable=AsyncMock
    ) as call:
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[_BadInputWorkflow],
                activities=[LLMActivities(AsyncMock()).llm_extract],
            ):
                result = await env.client.execute_workflow(
                    _BadInputWorkflow.run,
                    task_queue,
                    id=f"bad-input-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

    assert (
        result
        == f"{RetryState.MAXIMUM_ATTEMPTS_REACHED.name}|Failed decoding arguments"
    )
    fetch.assert_not_awaited()
    call.assert_not_awaited()
