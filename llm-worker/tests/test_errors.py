"""
Unit tests for worker/errors.py — transient vs terminal classification (Q5 B).

The stakes are asymmetric, and the tests are written around that:

  * calling a terminal error "transient" retries something that can never
    succeed, re-billing the user's own API key each time (the Q6 failure mode);
  * calling a transient error "terminal" just fails a job that would have
    recovered.

So the default is terminal, and anything claimed transient is asserted here
explicitly rather than inferred from a base class.
"""

import httpx
import openai
import pytest
from cryptography.fernet import InvalidToken

import anthropic
from worker.errors import (
    TERMINAL,
    TRANSIENT,
    WarmupTimeout,
    classify,
    describe,
    retry_delay,
)

_REQ = httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status, request=_REQ)


# ---------------------------------------------------------------------------
# Transient
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("connection refused"),
        httpx.RemoteProtocolError("server disconnected"),
        WarmupTimeout("endpoint never woke"),
    ],
)
def test_network_and_warmup_errors_are_transient(exc):
    assert classify(exc) == TRANSIENT


@pytest.mark.parametrize(
    "exc",
    [
        anthropic.APITimeoutError(request=_REQ),
        anthropic.APIConnectionError(request=_REQ),
        openai.APITimeoutError(request=_REQ),
        openai.APIConnectionError(request=_REQ),
    ],
)
def test_sdk_timeout_and_connection_errors_are_transient(exc):
    assert classify(exc) == TRANSIENT


@pytest.mark.parametrize(
    "exc",
    [
        openai.RateLimitError("slow down", response=_resp(429), body=None),
        openai.InternalServerError("boom", response=_resp(500), body=None),
        anthropic.RateLimitError("slow down", response=_resp(429), body=None),
        anthropic.InternalServerError("boom", response=_resp(500), body=None),
    ],
)
def test_rate_limit_and_5xx_are_transient(exc):
    assert classify(exc) == TRANSIENT


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
def test_transient_status_codes_via_fallback(status):
    """An unrecognised error type still retries on a retryable status code."""

    class OddProviderError(Exception):
        def __init__(self, code):
            self.status_code = code

    assert classify(OddProviderError(status)) == TRANSIENT


@pytest.mark.parametrize(
    "code", ["InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"]
)
def test_minio_backend_faults_are_transient(code):
    class S3Error(Exception):
        def __init__(self, c):
            self.code = c

    assert classify(S3Error(code)) == TRANSIENT


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        openai.AuthenticationError("bad key", response=_resp(401), body=None),
        openai.PermissionDeniedError("nope", response=_resp(403), body=None),
        openai.BadRequestError("bad schema", response=_resp(400), body=None),
        openai.NotFoundError("no model", response=_resp(404), body=None),
        anthropic.AuthenticationError("bad key", response=_resp(401), body=None),
        anthropic.BadRequestError("bad schema", response=_resp(400), body=None),
    ],
)
def test_auth_and_request_errors_are_terminal(exc):
    """Retrying these re-bills the user for something that cannot succeed."""
    assert classify(exc) == TERMINAL


def test_undecryptable_key_is_terminal():
    assert classify(InvalidToken()) == TERMINAL


@pytest.mark.parametrize(
    "exc",
    [
        Exception("something we have never seen"),
        ValueError("bad json"),
        KeyError("missing field"),
    ],
)
def test_unknown_errors_default_to_terminal(exc):
    """Fail closed: an unrecognised error must not start a spend loop."""
    assert classify(exc) == TERMINAL


def test_terminal_types_win_over_status_fallback():
    """
    A 429 attribute on an auth error must not make it retryable — terminal types
    are checked before the status-code fallback.
    """
    exc = openai.AuthenticationError("bad key", response=_resp(401), body=None)
    exc.status_code = 429  # pathological, but the ordering must hold
    assert classify(exc) == TERMINAL


# ---------------------------------------------------------------------------
# Backoff + formatting
# ---------------------------------------------------------------------------


def test_retry_delay_doubles_and_caps():
    assert retry_delay(1, base=5.0, cap=60.0) == 5.0
    assert retry_delay(2, base=5.0, cap=60.0) == 10.0
    assert retry_delay(3, base=5.0, cap=60.0) == 20.0
    assert retry_delay(10, base=5.0, cap=60.0) == 60.0


def test_retry_delay_handles_zero_attempt():
    """num_delivered should never be < 1, but don't return a negative delay."""
    assert retry_delay(0, base=5.0, cap=60.0) == 5.0


def test_describe_includes_type_for_empty_message():
    """httpx timeouts often stringify to '' — the type name is all the user gets."""
    assert describe(httpx.ReadTimeout("")) == "ReadTimeout"
    assert describe(ValueError("bad json")) == "ValueError: bad json"
