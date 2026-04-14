"""
Unit tests for worker/llm.py — decryption, provider routing, and LLM dispatch.

No live LLM calls are made. The Anthropic and OpenAI SDK constructors are
patched at 'worker.llm.anthropic.AsyncAnthropic' and 'worker.llm.AsyncOpenAI'
respectively — the name as it appears in the module under test.

Fernet key/ciphertext pairs are generated inline per test so each test is
fully self-contained and independent of the conftest-bootstrapped settings key.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from worker.config import settings
from worker.llm import _call_anthropic, _call_openai_compatible, _decrypt_key, call_llm


# ---------------------------------------------------------------------------
# _decrypt_key
# ---------------------------------------------------------------------------


def test_decrypt_key_returns_plaintext():
    """_decrypt_key must correctly Fernet-decrypt the stored ciphertext."""
    key = Fernet.generate_key()
    plaintext = "sk-real-api-key-12345"
    encrypted = Fernet(key).encrypt(plaintext.encode()).decode()

    with patch.object(settings, "llm_key_encryption_key", key.decode()):
        result = _decrypt_key(encrypted)

    assert result == plaintext


def test_decrypt_key_raises_on_wrong_key():
    """Decrypting with the wrong key must raise an exception."""
    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()
    encrypted = Fernet(key_a).encrypt(b"secret").decode()

    with patch.object(settings, "llm_key_encryption_key", key_b.decode()):
        with pytest.raises(Exception):
            _decrypt_key(encrypted)


# ---------------------------------------------------------------------------
# call_llm — provider routing
# ---------------------------------------------------------------------------


async def test_call_llm_routes_anthropic_provider():
    """provider='anthropic' must dispatch to _call_anthropic, not _call_openai_compatible."""
    key = Fernet.generate_key()
    encrypted = Fernet(key).encrypt(b"api-key").decode()

    with patch.object(settings, "llm_key_encryption_key", key.decode()):
        with patch("worker.llm._call_anthropic", new_callable=AsyncMock) as mock_a:
            with patch(
                "worker.llm._call_openai_compatible", new_callable=AsyncMock
            ) as mock_o:
                mock_a.return_value = {"result": "from-anthropic"}
                result = await call_llm(
                    encrypted_api_key=encrypted,
                    provider="anthropic",
                    base_url=None,
                    model="claude-3-5-sonnet-20241022",
                    content="some content",
                    output_schema={"type": "object"},
                )

    mock_a.assert_called_once()
    mock_o.assert_not_called()
    assert result == {"result": "from-anthropic"}


async def test_call_llm_routes_openai_compatible_provider():
    """provider='openai_compatible' must dispatch to _call_openai_compatible."""
    key = Fernet.generate_key()
    encrypted = Fernet(key).encrypt(b"api-key").decode()

    with patch.object(settings, "llm_key_encryption_key", key.decode()):
        with patch("worker.llm._call_anthropic", new_callable=AsyncMock) as mock_a:
            with patch(
                "worker.llm._call_openai_compatible", new_callable=AsyncMock
            ) as mock_o:
                mock_o.return_value = {"result": "from-openai"}
                result = await call_llm(
                    encrypted_api_key=encrypted,
                    provider="openai_compatible",
                    base_url="https://api.openai.com/v1",
                    model="gpt-4o",
                    content="some content",
                    output_schema={"type": "object"},
                )

    mock_o.assert_called_once()
    mock_a.assert_not_called()
    assert result == {"result": "from-openai"}


async def test_call_llm_truncates_content_over_limit():
    """Content exceeding llm_max_content_chars must be truncated before the LLM call."""
    key = Fernet.generate_key()
    encrypted = Fernet(key).encrypt(b"api-key").decode()
    long_content = "x" * (settings.llm_max_content_chars + 500)

    with patch.object(settings, "llm_key_encryption_key", key.decode()):
        with patch("worker.llm._call_anthropic", new_callable=AsyncMock) as mock_a:
            mock_a.return_value = {}
            await call_llm(
                encrypted_api_key=encrypted,
                provider="anthropic",
                base_url=None,
                model="claude-3-5-sonnet-20241022",
                content=long_content,
                output_schema={},
            )

    # _call_anthropic(api_key, model, content, output_schema) — content is args[2]
    called_content: str = mock_a.call_args.args[2]
    assert len(called_content) == settings.llm_max_content_chars


async def test_call_llm_does_not_truncate_content_under_limit():
    """Content within llm_max_content_chars must be passed through unchanged."""
    key = Fernet.generate_key()
    encrypted = Fernet(key).encrypt(b"api-key").decode()
    short_content = "hello world"

    with patch.object(settings, "llm_key_encryption_key", key.decode()):
        with patch("worker.llm._call_anthropic", new_callable=AsyncMock) as mock_a:
            mock_a.return_value = {}
            await call_llm(
                encrypted_api_key=encrypted,
                provider="anthropic",
                base_url=None,
                model="claude-3-5-sonnet-20241022",
                content=short_content,
                output_schema={},
            )

    called_content: str = mock_a.call_args.args[2]
    assert called_content == short_content


# ---------------------------------------------------------------------------
# _call_anthropic — tool-use structured output
# ---------------------------------------------------------------------------


async def test_call_anthropic_returns_tool_input():
    """
    _call_anthropic must return response.content[0].input — the ToolUseBlock
    dict produced by Anthropic's forced tool_choice. No json.loads needed.
    """
    expected = {"name": "Alice", "age": 30}

    mock_tool_block = MagicMock()
    mock_tool_block.input = expected

    mock_response = MagicMock()
    mock_response.content = [mock_tool_block]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("worker.llm.anthropic.AsyncAnthropic", return_value=mock_client):
        result = await _call_anthropic(
            api_key="sk-ant-test",
            model="claude-3-5-sonnet-20241022",
            content="Extract data from: <p>Alice, age 30</p>",
            output_schema={"type": "object"},
        )

    assert result == expected


# ---------------------------------------------------------------------------
# _call_openai_compatible — json_schema response_format
# ---------------------------------------------------------------------------


async def test_call_openai_compatible_parses_json_response():
    """
    _call_openai_compatible must json.loads the string in
    response.choices[0].message.content and return the resulting dict.
    """
    expected = {"title": "Example", "price": 9.99}

    mock_message = MagicMock()
    mock_message.content = json.dumps(expected)

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch("worker.llm.AsyncOpenAI", return_value=mock_client):
        result = await _call_openai_compatible(
            api_key="sk-openai-test",
            base_url=None,
            model="gpt-4o",
            content="Extract data from: Example product, $9.99",
            output_schema={"type": "object"},
        )

    assert result == expected


async def test_call_openai_compatible_passes_base_url():
    """base_url must be forwarded to the AsyncOpenAI constructor for custom endpoints."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content='{"ok": true}'))]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    custom_url = "https://vllm.example.com/v1"

    with patch("worker.llm.AsyncOpenAI", return_value=mock_client) as mock_cls:
        await _call_openai_compatible(
            api_key="sk-test",
            base_url=custom_url,
            model="Qwen/Qwen2.5-72b",
            content="some content",
            output_schema={},
        )

    _, kwargs = mock_cls.call_args
    assert kwargs["base_url"] == custom_url
