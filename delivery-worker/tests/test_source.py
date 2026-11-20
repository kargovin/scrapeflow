"""ScrapeFlow's copy, read through the read-only MinIO user: the storage-fault split."""

import aiohttp
import pytest
from miniopy_async.error import S3Error
from temporalio.exceptions import ApplicationError

from worker.source import Source

_KEY = aiohttp.client_reqrep.ConnectionKey("minio", 9000, False, True, None, None, None)


class _Response:
    def __init__(self, data: bytes, length: int | None) -> None:
        self.content_length = length
        self.content = self
        self._data = data
        self.closed = self.released = False

    async def iter_chunked(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i : i + n]

    def close(self):
        self.closed = True

    async def release(self):
        self.released = True


class _Minio:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def get_object(self, bucket, key):
        self.calls.append((bucket, key))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def _read(source: Source, path: str) -> tuple[int, bytes]:
    async with source.open(path) as obj:
        return obj.length, b"".join([c async for c in obj.chunks()])


async def test_the_copy_is_streamed_with_its_length_and_released():
    response = _Response(b"a" * 150_000, 150_000)
    minio = _Minio(response)
    assert await _read(Source(minio, "scrapeflow-results"), "scrapeflow-results/p/scrape.html") == (
        150_000,
        b"a" * 150_000,
    )
    assert minio.calls == [("scrapeflow-results", "p/scrape.html")]
    assert response.closed and response.released


async def test_a_copy_shorter_than_its_length_is_a_source_failure():
    source = Source(_Minio(_Response(b"abc", 10)), "scrapeflow-results")
    async with source.open("scrapeflow-results/k") as obj:
        with pytest.raises(aiohttp.ClientPayloadError):
            [c async for c in obj.chunks()]
        assert obj.failure is not None


@pytest.mark.parametrize(
    ("error", "type_", "final", "message"),
    [
        (S3Error("NoSuchKey", "m", "/k", None, None, None), "SourceFailed", True,
         "ScrapeFlow's copy of the input is gone"),
        (S3Error("AccessDenied", "m", "/k", None, None, None), "SourceFailed", True,
         "ScrapeFlow's copy of the input could not be read (AccessDenied)"),
        (S3Error("SlowDown", "m", "/k", None, None, None), "SourceUnavailable", False,
         "ScrapeFlow's copy of the input could not be read (SlowDown)"),
        # MinIO down: aiohttp's error, not an S3Error.
        (aiohttp.ClientConnectorError(_KEY, ConnectionRefusedError(111, "x")), "SourceUnavailable",
         False, "ScrapeFlow's copy of the input could not be read (ClientConnectorError)"),
        (TimeoutError(), "SourceUnavailable", False,
         "ScrapeFlow's copy of the input could not be read (TimeoutError)"),
        (ValueError("x"), "SourceFailed", True,
         "ScrapeFlow's copy of the input could not be read (ValueError)"),
    ],
)
async def test_the_storage_fault_split(error, type_, final, message):
    with pytest.raises(ApplicationError) as exc:
        await _read(Source(_Minio(error), "scrapeflow-results"), "scrapeflow-results/k")
    assert (exc.value.type, exc.value.non_retryable, exc.value.message) == (type_, final, message)


@pytest.mark.parametrize("path", ["other-bucket/k", "scrapeflow-results/", "scrapeflow-results"])
async def test_a_path_outside_scrapeflow_s_bucket_is_final(path):
    minio = _Minio(_Response(b"", 0))
    with pytest.raises(ApplicationError) as exc:
        await _read(Source(minio, "scrapeflow-results"), path)
    assert (exc.value.type, exc.value.non_retryable) == ("SourceFailed", True)
    assert minio.calls == []
