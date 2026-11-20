"""ScrapeFlow's own copy of a block's output, read through the worker's read-only MinIO user."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import aiohttp
from miniopy_async import Minio
from miniopy_async.error import S3Error
from temporalio.exceptions import ApplicationError

CHUNK_BYTES = 64 * 1024

# miniopy-async's codes for a backend fault rather than a missing or refused object.
_TRANSIENT_S3_CODES = frozenset({"InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"})


def source_error(exc: BaseException) -> ApplicationError:
    """The storage-fault split every worker has: MinIO unreachable or failing is transient."""
    if isinstance(exc, S3Error):
        transient = exc.code in _TRANSIENT_S3_CODES
        detail = "is gone" if exc.code == "NoSuchKey" else f"could not be read ({exc.code})"
    elif isinstance(exc, aiohttp.ClientConnectionError | aiohttp.ClientPayloadError | TimeoutError):
        # MinIO down is aiohttp's error, not an S3Error.
        transient, detail = True, f"could not be read ({type(exc).__name__})"
    else:
        transient, detail = False, f"could not be read ({type(exc).__name__})"
    return ApplicationError(
        f"ScrapeFlow's copy of the input {detail}",
        type="SourceUnavailable" if transient else "SourceFailed",
        non_retryable=not transient,
    )


@dataclass
class SourceObject:
    length: int
    _response: aiohttp.ClientResponse
    # Set when reading failed mid-stream: the destination client may wrap or swallow the error.
    failure: BaseException | None = field(default=None)

    async def chunks(self) -> AsyncIterator[bytes]:
        sent = 0
        try:
            async for chunk in self._response.content.iter_chunked(CHUNK_BYTES):
                chunk = chunk[: self.length - sent]
                sent += len(chunk)
                yield chunk
                if sent >= self.length:
                    return
        except Exception as exc:
            self.failure = exc
            raise
        if sent < self.length:
            self.failure = aiohttp.ClientPayloadError("source ended early")
            raise self.failure


class Source:
    def __init__(self, minio: Minio, bucket: str) -> None:
        self._minio = minio
        self._bucket = bucket

    @asynccontextmanager
    async def open(self, path: str) -> AsyncIterator[SourceObject]:
        """``path`` is ``{bucket}/{key}`` in ScrapeFlow's bucket. Raises ApplicationError."""
        bucket, _, key = path.partition("/")
        if bucket != self._bucket or not key:
            raise ApplicationError(
                "the input is not in ScrapeFlow's bucket", type="SourceFailed", non_retryable=True
            )
        try:
            response = await self._minio.get_object(bucket, key)
        except Exception as exc:
            raise source_error(exc) from None
        try:
            if response.content_length is None:
                raise ApplicationError(
                    "ScrapeFlow's copy of the input has no length",
                    type="SourceFailed",
                    non_retryable=True,
                )
            yield SourceObject(response.content_length, response)
        finally:
            response.close()
            await response.release()
