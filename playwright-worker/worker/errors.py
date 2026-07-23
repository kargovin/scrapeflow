"""
Transient vs terminal failure classification for the Playwright worker (UF-003 3a).

Ported from ``llm-worker/worker/errors.py``. Same failure mode: the worker used to
ack on *every* exception, so a momentary infra blip failed a job permanently — the
ack preempts JetStream's redelivery, so the queue never gets the chance to retry.
The Q5 fix built this classifier for the LLM worker only; this brings the Playwright
worker to the same bar.

  * terminal  — bad/undecryptable credentials, a genuine site failure, a bug.
                Retrying cannot help and just burns a headed-Chrome render.
  * transient — MinIO unreachable or overloaded. Retrying is very likely to succeed
                once the object store recovers.

The exception surface differs from the LLM worker: there are no provider SDK calls
here, so the only transient class that matters is the MinIO/S3 write (the result
upload, and screenshot uploads). One subtlety the LLM classifier gets wrong and this
one fixes: when MinIO is *unreachable* (pod down, connection refused), miniopy-async
raises ``aiohttp.ClientConnectionError`` — **not** an ``S3Error`` — so matching only on
``S3Error.code`` would miss the literal "MinIO down" case this fix targets. MinIO
*returning* a 5xx (overloaded) is the S3-code path; MinIO being *gone* is the
aiohttp path. Both are transient.

Deliberately NOT transient: Playwright navigation failures (goto timeout, net::ERR_).
A slow or dead *target site* is the site's own answer, not our infrastructure failing,
and a re-scrape costs a fresh headed-Chrome render plus proxy bandwidth. Retrying those
is a separate policy decision, out of scope here. Default is TERMINAL — an error we do
not recognise is not retried.
"""

import aiohttp  # transitive via miniopy-async; declared explicitly in pyproject.toml
from cryptography.fernet import InvalidToken
from miniopy_async.error import S3Error

TRANSIENT = "transient"
TERMINAL = "terminal"

# Infra-level failures that recover on their own — worth a bounded redelivery.
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    # MinIO unreachable (connection refused) or the connection dropped mid-request.
    # This is the literal "MinIO down" case, and it is NOT an S3Error — matching only
    # on S3Error.code (as the LLM worker does) would miss it.
    aiohttp.ClientConnectionError,
    # MinIO reachable but too slow to answer within aiohttp's timeout.
    aiohttp.ServerTimeoutError,
)

# Terminal regardless of anything else — retrying cannot fix these.
_TERMINAL_TYPES: tuple[type[BaseException], ...] = (
    # Fernet could not decrypt the stored proxy/cookie credentials: the ciphertext
    # is corrupt or was encrypted under a different CREDENTIALS_ENCRYPTION_KEY.
    InvalidToken,
)

# MinIO/S3 error codes that indicate load or a transient backend fault rather than a
# caller mistake (NoSuchBucket, AccessDenied, etc. stay terminal via the default).
_TRANSIENT_S3_CODES = frozenset(
    {"InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"}
)


def classify(exc: BaseException) -> str:
    """Return TRANSIENT or TERMINAL for an exception raised while handling a job.

    Order matters: terminal types are checked first so a bad credentials key is never
    misread as retryable via a later fallback.
    """
    if isinstance(exc, _TERMINAL_TYPES):
        return TERMINAL

    if isinstance(exc, _TRANSIENT_TYPES):
        return TRANSIENT

    # MinIO returned an error response (reachable but faulting). S3Error carries a
    # string .code; only the load/backend codes are transient.
    if isinstance(exc, S3Error):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in _TRANSIENT_S3_CODES:
            return TRANSIENT

    return TERMINAL


def retry_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff for the nak delay, in seconds.

    ``attempt`` is ``msg.metadata.num_delivered`` — 1 on first delivery — so the first
    retry waits ``base``, the second ``2*base``, and so on up to ``cap``.
    """
    if attempt < 1:
        attempt = 1
    return float(min(base * (2 ** (attempt - 1)), cap))


def describe(exc: BaseException) -> str:
    """Human-readable error string for the ResultMessage.error field."""
    name = type(exc).__name__
    detail = str(exc).strip()
    return f"{name}: {detail}" if detail else name
