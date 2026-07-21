"""
Unit tests for worker/llm.py ensure_ready() — the cold-start probe (Q5 C).

Scale-to-zero endpoints (Modal, vLLM, RunPod) take 90-110s to answer the first
request after idle. ensure_ready polls until the endpoint responds so the real
call runs against a warm endpoint instead of burning its timeout on the boot.

httpx.AsyncClient is patched throughout — no sockets are opened.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from worker import llm
from worker.errors import WarmupTimeout

_BASE = "https://model--x.modal.run/v1"
_KEY = "sk-test"


@pytest.fixture(autouse=True)
def _clear_warm_cache():
    """The warm cache is module-level; isolate each test."""
    llm._warm_until.clear()
    yield
    llm._warm_until.clear()


def _client_returning(*side_effects):
    """Patch httpx.AsyncClient so .get() yields the given results in order."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=list(side_effects))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("worker.llm.httpx.AsyncClient", return_value=ctx), client


async def test_returns_immediately_when_endpoint_is_up():
    patcher, client = _client_returning(httpx.Response(200))
    with patcher:
        await llm.ensure_ready(_BASE, _KEY)

    assert client.get.await_count == 1
    assert client.get.await_args.args[0] == f"{_BASE}/models"


async def test_polls_until_endpoint_wakes():
    """Connection errors while booting are expected — keep probing."""
    patcher, client = _client_returning(
        httpx.ConnectError("refused"),
        httpx.ConnectError("refused"),
        httpx.Response(200),
    )
    with patcher, patch("worker.llm.asyncio.sleep", new_callable=AsyncMock):
        await llm.ensure_ready(_BASE, _KEY)

    assert client.get.await_count == 3


async def test_sends_bearer_token():
    patcher, client = _client_returning(httpx.Response(200))
    with patcher:
        await llm.ensure_ready(_BASE, _KEY)

    assert client.get.await_args.kwargs["headers"]["Authorization"] == f"Bearer {_KEY}"


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_any_http_response_counts_as_awake(status):
    """
    A response — even an error one — proves a server is listening, which is all
    a cold-start probe needs to know. Auth/routing failures are the real call's
    business, where they are correctly terminal; retrying them here would loop
    on something that can never succeed.
    """
    patcher, client = _client_returning(httpx.Response(status))
    with patcher:
        await llm.ensure_ready(_BASE, _KEY)

    assert client.get.await_count == 1


async def test_raises_warmup_timeout_when_never_ready():
    # side_effect as a callable (not a finite list) so the loop can spin as long
    # as it likes — asyncio.sleep is mocked, so the deadline is what stops it.
    client = MagicMock()
    client.get = AsyncMock(
        side_effect=lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused"))
    )
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("worker.llm.httpx.AsyncClient", return_value=ctx), patch(
        "worker.llm.asyncio.sleep", new_callable=AsyncMock
    ), patch.object(llm.settings, "llm_warmup_max_wait_seconds", 0.05):
        with pytest.raises(WarmupTimeout) as excinfo:
            await llm.ensure_ready(_BASE, _KEY)

    assert _BASE in str(excinfo.value)
    assert client.get.await_count >= 1


async def test_warm_endpoint_skips_the_probe():
    """A hot endpoint must not pay a round-trip per job."""
    patcher, client = _client_returning(httpx.Response(200), httpx.Response(200))
    with patcher:
        await llm.ensure_ready(_BASE, _KEY)
        await llm.ensure_ready(_BASE, _KEY)

    assert client.get.await_count == 1


async def test_warm_cache_expires():
    patcher, client = _client_returning(httpx.Response(200), httpx.Response(200))
    with patcher, patch.object(llm.settings, "llm_warm_cache_seconds", -1):
        await llm.ensure_ready(_BASE, _KEY)
        await llm.ensure_ready(_BASE, _KEY)

    assert client.get.await_count == 2


async def test_warm_cache_is_per_base_url():
    patcher, client = _client_returning(httpx.Response(200), httpx.Response(200))
    with patcher:
        await llm.ensure_ready(_BASE, _KEY)
        await llm.ensure_ready("https://other--y.modal.run/v1", _KEY)

    assert client.get.await_count == 2


# ---------------------------------------------------------------------------
# Dispatch: who gets probed
# ---------------------------------------------------------------------------


async def test_anthropic_is_never_probed():
    """Hosted Anthropic has no health endpoint and never cold-starts."""
    from cryptography.fernet import Fernet

    key = llm.settings.llm_key_encryption_key
    encrypted = Fernet(key).encrypt(b"sk-ant-test").decode()

    with patch("worker.llm.ensure_ready", new_callable=AsyncMock) as probe, patch(
        "worker.llm._call_anthropic", new_callable=AsyncMock
    ) as call:
        call.return_value = {"ok": True}
        await llm.call_llm(encrypted, "anthropic", None, "claude-x", "content", {})

    probe.assert_not_awaited()


async def test_openai_compatible_with_base_url_is_probed():
    from cryptography.fernet import Fernet

    key = llm.settings.llm_key_encryption_key
    encrypted = Fernet(key).encrypt(b"sk-test").decode()

    with patch("worker.llm.ensure_ready", new_callable=AsyncMock) as probe, patch(
        "worker.llm._call_openai_compatible", new_callable=AsyncMock
    ) as call:
        call.return_value = {"ok": True}
        await llm.call_llm(encrypted, "openai_compatible", _BASE, "m", "content", {})

    probe.assert_awaited_once()


async def test_hosted_openai_without_base_url_is_not_probed():
    """No base_url means api.openai.com — hosted, always warm."""
    from cryptography.fernet import Fernet

    key = llm.settings.llm_key_encryption_key
    encrypted = Fernet(key).encrypt(b"sk-test").decode()

    with patch("worker.llm.ensure_ready", new_callable=AsyncMock) as probe, patch(
        "worker.llm._call_openai_compatible", new_callable=AsyncMock
    ) as call:
        call.return_value = {"ok": True}
        await llm.call_llm(encrypted, "openai_compatible", None, "m", "content", {})

    probe.assert_not_awaited()


async def test_warmup_can_be_disabled():
    from cryptography.fernet import Fernet

    key = llm.settings.llm_key_encryption_key
    encrypted = Fernet(key).encrypt(b"sk-test").decode()

    with patch("worker.llm.ensure_ready", new_callable=AsyncMock) as probe, patch(
        "worker.llm._call_openai_compatible", new_callable=AsyncMock
    ) as call, patch.object(llm.settings, "llm_warmup_enabled", False):
        call.return_value = {"ok": True}
        await llm.call_llm(encrypted, "openai_compatible", _BASE, "m", "content", {})

    probe.assert_not_awaited()
