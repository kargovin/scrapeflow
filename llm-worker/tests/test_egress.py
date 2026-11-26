"""BUG-010 part 2: a user's base_url is reached through the egress proxy, and its refusal is final."""

from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest
from cryptography.fernet import Fernet

from worker import llm
from worker.config import Settings, settings
from worker.errors import TERMINAL, AddressRefused, classify

REASON = "Egress proxying is denied to host 'vllm.internal': 10.0.0.7 denied by rule 'Deny: Private Range'."


def _refusing(request):
    """A 407 from the egress proxy, in whichever library the request came from."""
    lib = __import__(type(request).__module__.split(".")[0])
    return lib.Response(407, headers={"X-Smokescreen-Error": REASON}, request=request)


def _client(handler):
    """The SDK's own client (its httpx fork) on a mock transport of the same library."""
    base = next(
        c
        for c in type(openai.DefaultAsyncHttpxClient()).__mro__
        if c.__name__ == "AsyncClient"
    )
    sdk_httpx = __import__(base.__module__.split(".")[0])

    def make():
        return openai.DefaultAsyncHttpxClient(
            transport=sdk_httpx.MockTransport(handler)
        )

    make.httpx = sdk_httpx
    return make


def test_a_refusal_is_terminal():
    assert classify(AddressRefused("vllm.internal", REASON)) == TERMINAL


def test_the_message_names_the_host_only():
    refused = AddressRefused("vllm.internal", REASON)
    assert str(refused) == "address refused: vllm.internal is not a public address"
    assert refused.reason == REASON


def test_the_base_url_client_goes_through_the_egress_proxy():
    with patch(
        "worker.llm._egress_http_client", wraps=llm._egress_http_client
    ) as egress:
        llm._openai_client("k", "http://vllm.example.com/v1")
        egress.assert_called_once_with()


def test_the_hosted_api_does_not():
    with patch("worker.llm._egress_http_client") as egress:
        llm._openai_client("k", None)
        egress.assert_not_called()


async def test_a_refused_plain_http_endpoint_raises_address_refused():
    with patch("worker.llm._egress_http_client", _client(_refusing)):
        with pytest.raises(AddressRefused) as excinfo:
            await llm._call_openai_compatible(
                "k", "http://vllm.internal/v1", "m", "page", {}
            )
    assert excinfo.value.host == "vllm.internal"
    assert excinfo.value.reason == REASON


async def test_a_refused_https_connect_raises_address_refused():
    client = _client(None)

    def handler(request):
        raise client.httpx.ProxyError(
            "407 Proxy Authentication Required", request=request
        )

    with patch("worker.llm._egress_http_client", _client(handler)):
        with pytest.raises(
            AddressRefused, match="vllm.internal is not a public address"
        ):
            await llm._call_openai_compatible(
                "k", "https://vllm.internal/v1", "m", "page", {}
            )


async def test_a_407_without_the_proxys_header_is_not_a_refusal():
    def handler(request):
        return _client(None).httpx.Response(407, request=request)

    with patch("worker.llm._egress_http_client", _client(handler)):
        with pytest.raises(openai.APIStatusError) as excinfo:
            await llm._call_openai_compatible(
                "k", "http://vllm.example.com/v1", "m", "page", {}
            )
    assert not isinstance(excinfo.value, AddressRefused)


async def test_another_proxy_failure_stays_transient():
    def handler(request):
        raise _client(None).httpx.ProxyError("502 Bad Gateway", request=request)

    with patch("worker.llm._egress_http_client", _client(handler)):
        with pytest.raises(openai.APIConnectionError) as excinfo:
            await llm._call_openai_compatible(
                "k", "https://vllm.example.com/v1", "m", "page", {}
            )
    assert classify(excinfo.value) != TERMINAL


def _probe_client(handler):
    real = httpx.AsyncClient

    def make(**kwargs):
        kwargs.pop("proxy", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    return patch("worker.llm.httpx.AsyncClient", side_effect=make)


@pytest.mark.parametrize("https", [False, True])
async def test_the_warm_up_probe_stops_at_a_refusal(https):
    calls = []

    def handler(request):
        calls.append(request)
        if https:
            raise httpx.ProxyError("407 Proxy Authentication Required", request=request)
        return _refusing(request)

    llm._warm_until.clear()
    base_url = f"{'https' if https else 'http'}://vllm.internal/v1"
    with (
        _probe_client(handler),
        patch("worker.llm.asyncio.sleep", new_callable=AsyncMock),
        patch.object(settings, "llm_warmup_max_wait_seconds", 0.5),
    ):
        with pytest.raises(AddressRefused, match="vllm.internal"):
            await llm.ensure_ready(base_url, "k")
    assert len(calls) == 1  # no polling a refusal for the warm-up budget


async def test_a_refusal_reaches_the_caller_through_call_llm():
    encrypted = Fernet(settings.llm_key_encryption_key).encrypt(b"k").decode()
    llm._warm_until.clear()
    with _probe_client(_refusing):
        with pytest.raises(AddressRefused):
            await llm.call_llm(
                encrypted, "openai_compatible", "http://vllm.internal/v1", "m", "x", {}
            )


@pytest.mark.parametrize(
    "value", ["", "egress:4750", "https://egress:4750", "http://egress"]
)
def test_egress_proxy_url_must_be_http_host_port(monkeypatch, value):
    from pydantic import ValidationError

    monkeypatch.setenv("EGRESS_PROXY_URL", value)
    with pytest.raises(ValidationError, match="EGRESS_PROXY_URL"):
        Settings()
