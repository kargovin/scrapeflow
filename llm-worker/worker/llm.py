import asyncio
import json
import time
from typing import Any

from urllib.parse import urlsplit

import anthropic
import httpx
import openai
import structlog
from cryptography.fernet import Fernet
from openai import AsyncOpenAI

from worker.config import settings
from worker.errors import (
    AddressRefused,
    LLMOutputTruncated,
    LLMRequestTimeout,
    WarmupTimeout,
)

log = structlog.get_logger()

# base_url -> monotonic timestamp of the last successful probe. Lets a hot
# endpoint skip the probe round-trip entirely (see llm_warm_cache_seconds).
# Process-local by design: a fresh worker re-probes once, which is cheap and
# strictly safer than trusting another pod's view of readiness.
_warm_until: dict[str, float] = {}

# BUG-022: without it a small model under schema decoding can keep extending a string value until
# max_tokens. In the user turn, not a system message: some self-hosted chat templates reject a
# system role.
EXTRACTION_INSTRUCTION = (
    "Extract the requested data from the page and return only a JSON object matching the schema. "
    "Use only information present in the page. Keep each value as concise as the data allows; "
    "do not add commentary, explanations or repetition."
)


def _prompt(content: str) -> str:
    return f"{EXTRACTION_INSTRUCTION}\n\nPage:\n\n{content}"


def _decrypt_key(encrypted_api_key: str) -> str:
    """Fernet-decrypt an LLM API key using the shared encryption key."""
    fernet = Fernet(settings.llm_key_encryption_key)
    return fernet.decrypt(encrypted_api_key.encode()).decode()


def _make_timeout(timeout_cls: type) -> Any:
    # Each SDK's own Timeout class: anthropic>=1.4 raises TypeError on an httpx.Timeout (its
    # client is httpx2).
    return timeout_cls(
        connect=10.0,
        read=float(settings.llm_request_timeout_seconds),
        write=10.0,
        pool=5.0,
    )


def _anthropic_client(api_key: str) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=_make_timeout(anthropic.Timeout),
        max_retries=settings.llm_max_retries,
    )


def _openai_client(api_key: str, base_url: str | None) -> AsyncOpenAI:
    # A user's base_url is a user-chosen address: through the egress proxy (BUG-010 part 2), which
    # also checks every redirect. The hosted API (no base_url) is not, and goes direct.
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url or None,
        timeout=_make_timeout(openai.Timeout),
        max_retries=settings.llm_max_retries,
        http_client=_egress_http_client() if base_url else None,
    )


def _egress_http_client() -> httpx.AsyncClient:
    return openai.DefaultAsyncHttpxClient(proxy=settings.egress_proxy_url)


# How the egress proxy names a refusal on its 407.
_REFUSAL_HEADER = "x-smokescreen-error"


def _refusal(exc: BaseException, fallback_host: str) -> AddressRefused | None:
    """The egress refusal exc carries, or None. A plain-http endpoint gets the proxy's 407 back as a
    response; an https one fails the CONNECT, reported as a ProxyError with the status only — the
    egress proxy sends a 407 for nothing else, and nothing else here has a proxy.

    By shape, not class: the probe uses httpx, the OpenAI SDK its own fork (httpx2), whose classes
    do not subclass httpx's."""
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 407:
        reason = response.headers.get(_REFUSAL_HEADER)
        if reason:
            return AddressRefused(_host(response.request, fallback_host), reason)
    for err in (exc, exc.__cause__):
        if type(err).__name__ == "ProxyError" and str(err).startswith("407"):
            try:
                request = err.request
            except RuntimeError:  # raised when the error carries no request
                request = None
            return AddressRefused(_host(request, fallback_host), str(err))
    return None


def _host(request: Any, fallback: str) -> str:
    return urlsplit(str(request.url)).netloc if request is not None else fallback


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

    Caller must keep heartbeating: this loop can outlast the activity's
    heartbeat_timeout.
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

    host = httpx.URL(base_url).netloc.decode()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.llm_warmup_probe_timeout_seconds),
        proxy=settings.egress_proxy_url,
    ) as client:
        while time.monotonic() < deadline:
            attempts += 1
            try:
                response = await client.get(probe_url, headers=headers)
            except httpx.HTTPError as exc:
                if refused := _refusal(exc, host):
                    raise refused from None
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 407 and (
                    reason := response.headers.get(_REFUSAL_HEADER)
                ):
                    raise AddressRefused(host, reason)
                _warm_until[base_url] = (
                    time.monotonic() + settings.llm_warm_cache_seconds
                )
                if attempts > 1:
                    log.info(
                        "llm_endpoint_warm",
                        base_url=base_url,
                        attempts=attempts,
                        status=response.status_code,
                        waited_s=round(
                            time.monotonic()
                            - (deadline - settings.llm_warmup_max_wait_seconds),
                            1,
                        ),
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
    client = _anthropic_client(api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=settings.llm_max_output_tokens,
        tools=[{"name": "extract", "input_schema": output_schema}],
        tool_choice={"type": "tool", "name": "extract"},
        messages=[{"role": "user", "content": _prompt(content)}],
    )
    if response.stop_reason == "max_tokens":
        raise LLMOutputTruncated(
            f"the answer reached max_tokens ({settings.llm_max_output_tokens}) before it closed"
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
    client = _openai_client(api_key, base_url)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _prompt(content)}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": output_schema},
            },
            max_tokens=settings.llm_max_output_tokens,
        )
    except (openai.APIStatusError, openai.APIConnectionError) as exc:
        if base_url and (refused := _refusal(exc, httpx.URL(base_url).netloc.decode())):
            raise refused from None
        raise
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise LLMOutputTruncated(
            f"the answer reached max_tokens ({settings.llm_max_output_tokens}) before it closed"
        )
    return json.loads(choice.message.content)


async def _within_deadline(call: Any) -> dict[str, Any]:
    # The SDK's read timeout restarts on every redirect it follows (Modal answers a long request
    # with 303s), so it is no upper bound on its own (BUG-022). Cancelling closes the connection.
    limit = settings.llm_request_timeout_seconds
    try:
        return await asyncio.wait_for(call, timeout=limit)
    except TimeoutError:
        raise LLMRequestTimeout(f"no complete answer within {limit}s") from None


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
        log.warning(
            "content_truncated",
            original_len=len(content),
            truncated_to=settings.llm_max_content_chars,
        )
        content = content[: settings.llm_max_content_chars]

    if provider == "anthropic":
        return await _within_deadline(
            _call_anthropic(api_key, model, content, output_schema)
        )

    # Self-hosted openai-compatible endpoints may be scaled to zero. Wait for the
    # endpoint to wake before spending the real request's timeout budget on it.
    if settings.llm_warmup_enabled and base_url:
        await ensure_ready(base_url, api_key)

    return await _within_deadline(
        _call_openai_compatible(api_key, base_url, model, content, output_schema)
    )
