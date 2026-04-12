import json
from typing import Any

import anthropic
import httpx
from cryptography.fernet import Fernet
from openai import AsyncOpenAI

from worker.config import settings


def _decrypt_key(encrypted_api_key: str) -> str:
    """Fernet-decrypt an LLM API key using the shared encryption key."""
    fernet = Fernet(settings.llm_key_encryption_key.encode())
    return fernet.decrypt(encrypted_api_key.encode()).decode()


def _make_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=10.0,
        read=float(settings.llm_request_timeout_seconds),
        write=10.0,
        pool=5.0,
    )


async def _call_anthropic(
    api_key: str,
    model: str,
    content: str,
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=_make_timeout())
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
        content = content[: settings.llm_max_content_chars]

    if provider == "anthropic":
        return await _call_anthropic(api_key, model, content, output_schema)
    return await _call_openai_compatible(
        api_key, base_url, model, content, output_schema
    )
