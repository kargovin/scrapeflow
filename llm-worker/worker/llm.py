import asyncio
import json
import logging
import time
from typing import Any

import anthropic
import httpx
from cryptography.fernet import Fernet
from openai import AsyncOpenAI

from worker.config import settings
from worker.errors import WarmupTimeout

logger = logging.getLogger(__name__)

# base_url -> monotonic timestamp of the last successful probe. Lets a hot
# endpoint skip the probe round-trip entirely (see llm_warm_cache_seconds).
# Process-local by design: a fresh worker re-probes once, which is cheap and
# strictly safer than trusting another pod's view of readiness.
_warm_until: dict[str, float] = {}


def _decrypt_key(encrypted_api_key: str) -> str:
    """Fernet-decrypt an LLM API key using the shared encryption key."""
    fernet = Fernet(settings.llm_key_encryption_key)
    return fernet.decrypt(encrypted_api_key.encode()).decode()


def _make_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=10.0,
        read=float(settings.llm_request_timeout_seconds),
        write=10.0,
        pool=5.0,
    )


async def ensure_ready(base_url: str, api_key: str) -> None:
    """Block until a scale-to-zero endpoint answers, or raise WarmupTimeout.

    Q5 option C. Polls the OpenAI-compatible ``/models`` endpoint — part of the
    OpenAI API spec and implemented by vLLM/Modal/RunPod, unlike ``/health``
    which is not standardised.

    Two-tier timeout: each probe fails fast (llm_warmup_probe_timeout_seconds)
    while the loop keeps retrying until llm_warmup_max_wait_seconds is spent.
    That turns a cold start into an expected wait instead of one long
    all-or-nothing read timeout.

    Any HTTP response at all counts as ready, including 401/404: it proves a
    server is listening, which is what a cold start is about. Auth and routing
    problems are the real call's business, and are terminal there — swallowing
    them here would retry something that can never succeed.

    Caller must ensure the NATS in-progress heartbeat is running: this loop can
    outlast ack_wait, and without the heartbeat that means duplicate delivery.
    """
    now = time.monotonic()
    warm_until = _warm_until.get(base_url)
    if warm_until is not None and now < warm_until:
        return

    probe_url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = now + settings.llm_warmup_max_wait_seconds
    attempts = 0
    last_error: str | None = None

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.llm_warmup_probe_timeout_seconds)
    ) as client:
        while time.monotonic() < deadline:
            attempts += 1
            try:
                response = await client.get(probe_url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                _warm_until[base_url] = (
                    time.monotonic() + settings.llm_warm_cache_seconds
                )
                if attempts > 1:
                    logger.info(
                        "LLM endpoint warm after cold start",
                        extra={
                            "base_url": base_url,
                            "attempts": attempts,
                            "status": response.status_code,
                            "waited_s": round(
                                time.monotonic()
                                - (deadline - settings.llm_warmup_max_wait_seconds),
                                1,
                            ),
                        },
                    )
                return

            await asyncio.sleep(settings.llm_warmup_poll_interval_seconds)

    raise WarmupTimeout(
        f"{base_url} did not become ready within "
        f"{settings.llm_warmup_max_wait_seconds}s after {attempts} probes"
        + (f" (last error: {last_error})" if last_error else "")
    )


async def _call_anthropic(
    api_key: str,
    model: str,
    content: str,
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    client = anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=_make_timeout(),
        max_retries=settings.llm_max_retries,
    )
    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        tools=[{"name": "extract", "input_schema": output_schema}],
        tool_choice={"type": "tool", "name": "extract"},
        messages=[{"role": "user", "content": f"Extract data from:\n\n{content}"}],
    )
    # tool_choice forces a ToolUseBlock at content[0]; .input is already a dict
    return response.content[0].input  # type: ignore[return-value]


async def _call_openai_compatible(
    api_key: str,
    base_url: str | None,
    model: str,
    content: str,
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url or None,
        timeout=_make_timeout(),
        max_retries=settings.llm_max_retries,
    )
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": f"Extract data from:\n\n{content}"}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": output_schema},
        },
    )
    return json.loads(response.choices[0].message.content)


async def call_llm(
    encrypted_api_key: str,
    provider: str,
    base_url: str | None,
    model: str,
    content: str,
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    """
    Decrypt the LLM API key, truncate content if needed, dispatch to the
    correct provider, and return the structured JSON result.

    provider: "anthropic" | "openai_compatible"
    """
    api_key = _decrypt_key(encrypted_api_key)

    if len(content) > settings.llm_max_content_chars:
        logger.warning(
            "Content truncated before LLM call",
            extra={
                "original_len": len(content),
                "truncated_to": settings.llm_max_content_chars,
            },
        )
        content = content[: settings.llm_max_content_chars]

    if provider == "anthropic":
        return await _call_anthropic(api_key, model, content, output_schema)

    # Self-hosted openai-compatible endpoints may be scaled to zero. Wait for the
    # endpoint to wake before spending the real request's timeout budget on it.
    if settings.llm_warmup_enabled and base_url:
        await ensure_ready(base_url, api_key)

    return await _call_openai_compatible(
        api_key, base_url, model, content, output_schema
    )
