"""
Unit tests for worker/errors.py — transient vs terminal classification (UF-003 3a).

The classifier decides whether a failure raised while handling a job is worth a
bounded redelivery (transient infra fault) or should fail the job immediately
(terminal). Getting this wrong in the transient direction re-runs an expensive
headed-Chrome scrape; getting it wrong in the terminal direction permanently fails
a job that a retry would have saved. Default is terminal.
"""

from unittest.mock import MagicMock

import aiohttp
import pytest
from cryptography.fernet import InvalidToken
from miniopy_async.error import S3Error

from worker.errors import (
    TERMINAL,
    TRANSIENT,
    classify,
    describe,
    retry_delay,
)


def _s3_error(code: str) -> S3Error:
    return S3Error(
        code=code,
        message="m",
        resource="/x",
        request_id="r",
        host_id="h",
        response=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Transient — MinIO infrastructure faults
# ---------------------------------------------------------------------------


def test_minio_unreachable_is_transient():
    """MinIO down (connection refused) raises aiohttp.ClientConnectionError, NOT an
    S3Error — this is the literal 'MinIO down' case UF-003 names, and the S3-code
    path alone would miss it."""
    assert classify(aiohttp.ClientConnectionError("connection refused")) == TRANSIENT


def test_connection_dropped_midrequest_is_transient():
    # ServerDisconnectedError is a ClientConnectionError subclass.
    assert classify(aiohttp.ServerDisconnectedError("closed")) == TRANSIENT


def test_minio_slow_is_transient():
    assert classify(aiohttp.ServerTimeoutError("slow")) == TRANSIENT


@pytest.mark.parametrize(
    "code", ["InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"]
)
def test_transient_s3_codes(code):
    """MinIO reachable but returning a load/backend fault code."""
    assert classify(_s3_error(code)) == TRANSIENT


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


def test_bad_credentials_key_is_terminal():
    """Fernet decrypt failure on the stored proxy/cookie ciphertext — retrying
    cannot fix a corrupt or wrong-key credential."""
    assert classify(InvalidToken()) == TERMINAL


@pytest.mark.parametrize(
    "code", ["NoSuchBucket", "NoSuchKey", "AccessDenied", "InvalidAccessKeyId"]
)
def test_caller_error_s3_codes_are_terminal(code):
    """A missing bucket or bad credentials is a caller mistake, not a load fault."""
    assert classify(_s3_error(code)) == TERMINAL


def test_playwright_navigation_failure_is_terminal():
    """A goto timeout / dead site is the site's own answer, not our infra failing —
    deliberately not retried (a re-scrape burns a headed-Chrome render)."""
    assert classify(Exception("Timeout 60000ms exceeded")) == TERMINAL


def test_unknown_exception_defaults_terminal():
    assert classify(RuntimeError("who knows")) == TERMINAL


# ---------------------------------------------------------------------------
# retry_delay — exponential backoff
# ---------------------------------------------------------------------------


def test_retry_delay_grows_exponentially():
    assert retry_delay(1, base=5.0, cap=60.0) == 5.0
    assert retry_delay(2, base=5.0, cap=60.0) == 10.0
    assert retry_delay(3, base=5.0, cap=60.0) == 20.0


def test_retry_delay_is_capped():
    assert retry_delay(10, base=5.0, cap=60.0) == 60.0


def test_retry_delay_floors_attempt_at_one():
    assert retry_delay(0, base=5.0, cap=60.0) == 5.0


# ---------------------------------------------------------------------------
# describe — error string for the ResultMessage
# ---------------------------------------------------------------------------


def test_describe_includes_type_and_detail():
    assert describe(ValueError("bad thing")) == "ValueError: bad thing"


def test_describe_blank_message_falls_back_to_type():
    """Several exceptions stringify to '' and would show blank in the UI — the type
    name is the fallback (the reason describe() exists)."""
    assert describe(RuntimeError()) == "RuntimeError"
