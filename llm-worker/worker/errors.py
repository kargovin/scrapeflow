"""
Transient vs terminal failure classification (Q5 option B).

The worker used to ack on every exception path, which conflated two very
different kinds of failure:

  * terminal  — a bad API key, a malformed schema, a missing MinIO object.
                Retrying burns the user's money and will never succeed.
  * transient — a cold-start timeout, a 5xx, a rate limit, a dropped socket.
                Retrying is very likely to succeed.

Acking both meant NATS never redelivered, so a momentary blip failed a job
permanently. This module decides which is which; worker.py naks the transient
ones so JetStream redelivers them.

Default is TERMINAL. An error we do not recognise is not retried — the failure
mode of a wrong "transient" guess is an unbounded spend loop against the user's
own API key, which is exactly the class of incident Q6 was.
"""

import aiohttp
import anthropic
import httpx
import openai
from cryptography.fernet import InvalidToken

TRANSIENT = "transient"
TERMINAL = "terminal"

# Explicitly transient SDK exception types. Both provider SDKs expose the same
# names, so the tuple covers anthropic and openai symmetrically.
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    # Network / socket level (also covers httpx.ReadTimeout, ConnectTimeout,
    # ConnectError, RemoteProtocolError — all TransportError subclasses).
    httpx.TransportError,
    # MinIO's HTTP backend is aiohttp, not httpx: when MinIO is *unreachable*
    # (pod down, connection refused) or drops the connection mid-request,
    # miniopy-async raises aiohttp.ClientConnectionError — which is NOT an S3Error,
    # so the _TRANSIENT_S3_CODES path below never sees it. Without these two lines
    # the literal "MinIO down" case falls through to TERMINAL and the job fails
    # permanently (UF-003 3a). ServerTimeoutError covers MinIO reachable-but-slow.
    aiohttp.ClientConnectionError,
    aiohttp.ServerTimeoutError,
    # Provider SDK wrappers around the above.
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)

# Terminal regardless of anything else — retrying cannot fix these.
_TERMINAL_TYPES: tuple[type[BaseException], ...] = (
    # Fernet could not decrypt the stored key: the key is corrupt or was
    # encrypted with a different LLM_KEY_ENCRYPTION_KEY.
    InvalidToken,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.BadRequestError,
    anthropic.NotFoundError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.BadRequestError,
    openai.NotFoundError,
)

# HTTP statuses worth retrying when we only have a status code to go on.
_TRANSIENT_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

# MinIO/S3 error codes that indicate load or a transient backend fault rather
# than a caller mistake. miniopy-async raises S3Error with a .code attribute.
_TRANSIENT_S3_CODES = frozenset(
    {"InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"}
)


class WarmupTimeout(Exception):
    """The warm-up probe never saw the endpoint become ready (llm.ensure_ready).

    Treated as transient: the most likely cause is a scale-to-zero endpoint that
    is slower than the warm-up budget, or a provider outage. Both can recover, so
    this is worth a bounded redelivery rather than an immediate permanent fail.
    """


def classify(exc: BaseException) -> str:
    """Return TRANSIENT or TERMINAL for an exception raised while handling a job.

    Order matters: terminal types are checked first so that an auth failure is
    never misread as retryable via a status-code fallback.
    """
    if isinstance(exc, _TERMINAL_TYPES):
        return TERMINAL

    if isinstance(exc, WarmupTimeout):
        return TRANSIENT

    if isinstance(exc, _TRANSIENT_TYPES):
        return TRANSIENT

    # Status-code fallback: covers APIStatusError subclasses we did not name
    # above, and any provider that surfaces an unusual 5xx.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUS:
        return TRANSIENT

    # MinIO / S3 backend faults. S3Error carries a string .code.
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _TRANSIENT_S3_CODES:
        return TRANSIENT

    return TERMINAL


def retry_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff for the nak delay, in seconds.

    attempt is msg.metadata.num_delivered — 1 on first delivery — so the first
    retry waits `base`, the second 2*base, and so on up to `cap`.
    """
    if attempt < 1:
        attempt = 1
    return float(min(base * (2 ** (attempt - 1)), cap))


def describe(exc: BaseException) -> str:
    """Human-readable error string for the ResultMessage.error field."""
    name = type(exc).__name__
    detail = str(exc).strip()
    return f"{name}: {detail}" if detail else name
