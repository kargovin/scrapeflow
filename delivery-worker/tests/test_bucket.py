"""The bucket sink against a local https S3 stand-in: deliver, undo, the test, and the classifier."""

import asyncio
import json
import socket
import ssl
from contextlib import asynccontextmanager

import aiohttp
import pytest
from cryptography.fernet import Fernet
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from tests import tls
from tests.s3 import Play, error_xml, s3_server
from worker import bucket, checks, egress, pinned_http
from worker.bucket import BucketActivities, Endpoint, Refused, Reply, describe, endpoint, object_key
from worker.config import settings
from worker.contracts import (
    BucketLocation,
    DeliverBucketInput,
    DestinationTestInput,
    UndoBucketInput,
)
from worker.source import SourceObject

SECRET_KEY = "s3cr3t-never-shown"
PAGE = b"<html>" + b"x" * 200_000 + b"</html>"
PIPELINE, RUN = "4c1f0e7a-0000-4000-8000-000000000001", "9d2e1b3c-0000-4000-8000-000000000002"


def _secret(access_key: str = "AKIATEST", secret_key: str = SECRET_KEY) -> str:
    return (
        Fernet(settings.destination_encryption_key)
        .encrypt(json.dumps({"access_key": access_key, "secret_key": secret_key}).encode())
        .decode()
    )


class _Body:
    def __init__(self, data: bytes, fail: bool) -> None:
        self.data, self.fail = data, fail

    async def iter_chunked(self, n: int):
        for i in range(0, len(self.data), n):
            if self.fail and i:
                raise aiohttp.ClientPayloadError("minio dropped the connection")
            yield self.data[i : i + n]


class FakeSource:
    def __init__(self, data: bytes = PAGE, error: BaseException | None = None, fail_mid=False):
        self.data, self.error, self.fail_mid = data, error, fail_mid
        self.paths: list[str] = []

    @asynccontextmanager
    async def open(self, path: str):
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        response = type("R", (), {"content": _Body(self.data, self.fail_mid)})()
        yield SourceObject(len(self.data), response)


def _deliver_input(s3, prefix: str = "runs", ext: str = "html", **address) -> DeliverBucketInput:
    return DeliverBucketInput(
        address={"endpoint": s3.endpoint, "bucket": "results", "prefix": prefix, **address},
        encrypted_secret=_secret(),
        source_path="scrapeflow-results/pipelines/x/scrape.html",
        pipeline_id=PIPELINE,
        run_id=RUN,
        block_id="store",
        ext=ext,
    )


async def _deliver(inp, source=None):
    return await ActivityEnvironment().run(BucketActivities(source or FakeSource()).deliver_bucket, inp)


async def _undo(inp):
    return await ActivityEnvironment().run(BucketActivities(FakeSource()).undo_bucket, inp)


async def _test(inp):
    out = await ActivityEnvironment().run(checks.test_bucket, inp)
    return [(c.check, c.outcome, c.message) for c in out.checks]


@pytest.fixture
async def s3(monkeypatch):
    async with s3_server() as server:
        monkeypatch.setattr(egress, "tls_context", lambda: server.client_context)
        server.lookups = []

        async def vetted(host, port):
            server.lookups.append((host, port))
            return ["127.0.0.1"]

        monkeypatch.setattr(egress, "vetted_addresses", vetted)
        yield server


# --- key and endpoint ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "key"),
    [
        ("", f"{PIPELINE}/{RUN}/store.json"),
        ("exports/daily", f"exports/daily/{PIPELINE}/{RUN}/store.json"),
        ("/slashed/", f"slashed/{PIPELINE}/{RUN}/store.json"),
    ],
)
def test_the_key_is_run_keyed_under_the_prefix(prefix, key):
    assert object_key(prefix, PIPELINE, RUN, "store", "json") == key


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://S3.Example.com/", Endpoint("s3.example.com", 443, "s3.example.com")),
        ("https://s3.example.com:9443/", Endpoint("s3.example.com", 9443, "s3.example.com:9443")),
        ("https://[2606:4700::1]:9443/", Endpoint("2606:4700::1", 9443, "[2606:4700::1]:9443")),
    ],
)
def test_the_endpoint(url, expected):
    assert endpoint(url) == expected


@pytest.mark.parametrize("url", ["http://s3.example.com/", "https:///", "s3.example.com"])
def test_an_endpoint_that_is_not_https_is_refused(url):
    with pytest.raises(ValueError, match="must be an https endpoint"):
        endpoint(url)


# --- deliver ------------------------------------------------------------------------------


async def test_deliver_streams_the_copy_to_its_key_with_its_length(s3):
    source = FakeSource()
    out = await _deliver(_deliver_input(s3), source)

    key = f"runs/{PIPELINE}/{RUN}/store.html"
    assert out.location == BucketLocation(endpoint=s3.endpoint, bucket="results", key=key)
    assert out.size == len(PAGE)
    assert source.paths == ["scrapeflow-results/pipelines/x/scrape.html"]
    [req] = s3.requests
    assert (req.method, req.path, req.body) == ("PUT", f"/results/{key}", PAGE)
    assert req.headers["Content-Length"] == str(len(PAGE))
    assert "Transfer-Encoding" not in req.headers
    assert req.headers["Content-Type"] == "text/html"
    assert req.headers["Host"] == f"s3.test:{s3.port}"
    assert req.headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert req.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIATEST/")
    assert "/us-east-1/s3/aws4_request" in req.headers["Authorization"]
    # TLS carried the name; the lookup was the checked one.
    assert s3.sni == ["s3.test"]
    assert s3.lookups == [("s3.test", s3.port)]


async def test_a_retry_overwrites_the_same_key(s3):
    s3.plays["PUT"] = [Play(503, error_xml("SlowDown"))]
    inp = _deliver_input(s3)

    with pytest.raises(ApplicationError) as exc:
        await _deliver(inp)
    assert exc.value.non_retryable is False
    out = await _deliver(inp)

    assert [r.path for r in s3.requests] == [f"/results/{out.location.key}"] * 2
    assert list(s3.objects) == [f"/results/{out.location.key}"]


async def test_the_region_signs_the_request(s3):
    await _deliver(_deliver_input(s3, region="eu-west-1"))
    assert "/eu-west-1/s3/aws4_request" in s3.requests[0].headers["Authorization"]


async def test_a_key_is_percent_encoded_once(s3):
    out = await _deliver(_deliver_input(s3, prefix="my exports/ü"))
    assert out.location.key.startswith("my exports/ü/")
    assert s3.requests[0].path.startswith("/results/my%20exports/%C3%BC/")


@pytest.mark.parametrize(
    ("ext", "content_type"),
    [("md", "text/markdown; charset=utf-8"), ("json", "application/json")],
)
async def test_the_content_type_follows_the_extension(s3, ext, content_type):
    await _deliver(_deliver_input(s3, ext=ext))
    assert s3.requests[0].headers["Content-Type"] == content_type
    assert s3.requests[0].path.endswith(f"/store.{ext}")


async def test_a_redirect_is_not_followed(s3):
    s3.plays["PUT"] = [
        Play(
            307,
            error_xml("PermanentRedirect"),
            {"x-amz-bucket-region": "eu-west-1", "Location": f"{s3.endpoint}elsewhere/x"},
        ),
    ]
    with pytest.raises(ApplicationError) as exc:
        await _deliver(_deliver_input(s3))
    assert exc.value.non_retryable is True
    assert exc.value.message == (
        "s3.test redirected the write (PermanentRedirect, HTTP 307); the bucket is in region eu-west-1"
    )
    assert len(s3.requests) == 1


async def test_an_error_never_carries_the_server_s_text_or_the_secret(s3):
    s3.plays["PUT"] = [Play(403, error_xml("SignatureDoesNotMatch", f"key {SECRET_KEY} wrong"))]
    with pytest.raises(ApplicationError) as exc:
        await _deliver(_deliver_input(s3))
    assert exc.value.message == "s3.test refused the write (SignatureDoesNotMatch, HTTP 403)"
    assert exc.value.non_retryable is True
    assert SECRET_KEY not in repr(exc.value) and exc.value.__cause__ is None


async def test_a_missing_copy_is_final_and_reaches_no_bucket(s3):
    missing = ApplicationError("ScrapeFlow's copy of the input is gone", type="SourceFailed", non_retryable=True)
    with pytest.raises(ApplicationError) as exc:
        await _deliver(_deliver_input(s3), FakeSource(error=missing))
    assert (exc.value.type, exc.value.non_retryable) == ("SourceFailed", True)
    assert s3.requests == []


async def test_a_copy_that_fails_mid_stream_is_the_source_s_transient_error(s3):
    with pytest.raises(ApplicationError) as exc:
        await _deliver(_deliver_input(s3), FakeSource(fail_mid=True))
    assert (exc.value.type, exc.value.non_retryable) == ("SourceUnavailable", False)


async def test_an_attempt_past_its_deadline_is_transient(s3, monkeypatch):
    monkeypatch.setattr(bucket, "BUCKET_DELIVER_ATTEMPT_SECONDS", bucket.ATTEMPT_MARGIN_SECONDS + 0.3)
    s3.plays["PUT"] = [Play(200, delay=2)]
    with pytest.raises(ApplicationError) as exc:
        await _deliver(_deliver_input(s3))
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationUnreachable", False)
    assert exc.value.message == f"s3.test:{s3.port} did not answer in time"


async def test_an_address_that_is_not_https_is_final_before_any_lookup(s3):
    inp = _deliver_input(s3)
    inp.address["endpoint"] = "http://s3.test/"
    with pytest.raises(ApplicationError) as exc:
        await _deliver(inp)
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationAddress", True)
    assert s3.lookups == []


# --- undo ---------------------------------------------------------------------------------


def _undo_input(s3, key="runs/p/r/store.html") -> UndoBucketInput:
    return UndoBucketInput(
        location=BucketLocation(endpoint=s3.endpoint, bucket="results", key=key),
        encrypted_secret=_secret(),
    )


async def test_undo_deletes_the_recorded_key(s3):
    out = await _deliver(_deliver_input(s3))
    undone = await _undo(UndoBucketInput(location=out.location, encrypted_secret=_secret()))

    assert undone.already_gone is False
    assert s3.requests[-1].method == "DELETE"
    assert s3.requests[-1].path == f"/results/{out.location.key}"
    assert s3.objects == {}


async def test_undo_of_a_deleted_object_is_undone(s3):
    # S3 answers 204 either way; some servers answer NoSuchKey.
    assert (await _undo(_undo_input(s3))).already_gone is False
    s3.plays["DELETE"] = [Play(404, error_xml("NoSuchKey"))]
    assert (await _undo(_undo_input(s3))).already_gone is True


@pytest.mark.parametrize(
    ("play", "message", "final"),
    [
        (Play(403, error_xml("AccessDenied")), "s3.test refused the delete (AccessDenied, HTTP 403)", True),
        (Play(404, error_xml("NoSuchBucket")), "s3.test refused the delete (NoSuchBucket, HTTP 404)", True),
        (Play(500, error_xml("InternalError")), "s3.test failed the delete (InternalError, HTTP 500)", False),
    ],
)
async def test_an_undo_that_fails(s3, play, message, final):
    s3.plays["DELETE"] = [play]
    with pytest.raises(ApplicationError) as exc:
        await _undo(_undo_input(s3))
    assert (exc.value.message, exc.value.non_retryable) == (message, final)


# --- the address is checked at every attempt ----------------------------------------------


def _lookups(monkeypatch, *answers):
    calls = []
    answers = list(answers)

    def getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers.pop(0)]

    monkeypatch.setattr(egress.socket, "getaddrinfo", getaddrinfo)
    return calls


@pytest.fixture
def reached(monkeypatch):
    """Replaces the pinned connection; records each address an attempt reached."""
    ips: list[str] = []

    async def _at(ip, host, context, attempt):
        ips.append(ip)
        return Reply(204)

    monkeypatch.setattr(pinned_http, "_at", _at)
    return ips


async def test_a_private_endpoint_is_refused_at_every_attempt(monkeypatch, reached):
    endpoint_ = "https://s3.example.com/"
    _lookups(monkeypatch, ["10.0.0.5"], ["10.0.0.5"], ["10.0.0.5"])
    deliver = _deliver_input(type("S", (), {"endpoint": endpoint_})())
    undo = UndoBucketInput(
        location=BucketLocation(endpoint=endpoint_, bucket="results", key="k"),
        encrypted_secret=_secret(),
    )
    for call in (_deliver(deliver), _undo(undo)):
        with pytest.raises(ApplicationError) as exc:
            await call
        assert (exc.value.type, exc.value.non_retryable) == ("EgressBlocked", True)
        assert exc.value.message == "URL resolves to a private address: 10.0.0.5"
    assert await _test(_test_input(endpoint_)) == [
        ("address", "failed", "URL resolves to a private address: 10.0.0.5"),
    ]
    assert reached == []


async def test_each_attempt_looks_the_name_up_again(monkeypatch, reached):
    calls = _lookups(monkeypatch, ["93.184.216.34"], ["10.0.0.5"])
    inp = _deliver_input(type("S", (), {"endpoint": "https://s3.example.com/"})())

    await _deliver(inp)
    with pytest.raises(ApplicationError) as exc:
        await _deliver(inp)

    assert exc.value.type == "EgressBlocked"
    assert calls == ["s3.example.com", "s3.example.com"]
    assert reached == ["93.184.216.34"]


# --- the pin, on real sockets -------------------------------------------------------------


async def test_a_certificate_for_another_name_is_final_and_tries_no_other_address(monkeypatch):
    async with s3_server(certificate_name="other.test") as s3:
        monkeypatch.setattr(egress, "tls_context", lambda: s3.client_context)

        async def vetted(host, port):
            return ["127.0.0.1", "127.0.0.2"]

        monkeypatch.setattr(egress, "vetted_addresses", vetted)
        tried = _record_addresses(monkeypatch)
        with pytest.raises(ApplicationError) as exc:
            await _deliver(_deliver_input(s3))
    assert (exc.value.type, exc.value.message) == ("CertificateMismatch", "certificate not valid for s3.test")
    assert exc.value.non_retryable is True
    assert tried == ["127.0.0.1"]
    assert s3.requests == []


async def test_a_refused_address_moves_on_to_the_next(monkeypatch):
    async with s3_server() as s3:
        monkeypatch.setattr(egress, "tls_context", lambda: s3.client_context)

        async def vetted(host, port):
            # Nothing listens on 127.0.0.2.
            return ["127.0.0.2", "127.0.0.1"]

        monkeypatch.setattr(egress, "vetted_addresses", vetted)
        tried = _record_addresses(monkeypatch)
        await _deliver(_deliver_input(s3))
    assert tried == ["127.0.0.2", "127.0.0.1"]
    assert len(s3.requests) == 1


async def test_the_last_refused_address_is_a_transient_error(monkeypatch):
    async with s3_server() as s3:

        async def vetted(host, port):
            return ["127.0.0.2"]

        monkeypatch.setattr(egress, "vetted_addresses", vetted)
        with pytest.raises(ApplicationError) as exc:
            await _deliver(_deliver_input(s3))
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationUnreachable", False)
    assert exc.value.message.startswith(f"could not reach s3.test:{s3.port} (")


@asynccontextmanager
async def _dropping_server():
    """Reads each request's head, records its request line, then drops the connection."""
    server_ctx, client_ctx, _ = tls.contexts("s3.test")
    received: list[str] = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            received.append(head.split(b"\r\n", 1)[0].decode())
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port, client_ctx, received


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_a_dropped_connection_is_not_resent(monkeypatch, method):
    async with _dropping_server() as (port, client_ctx, received):
        monkeypatch.setattr(egress, "tls_context", lambda: client_ctx)

        async def vetted(host, port):
            return ["127.0.0.1"]

        monkeypatch.setattr(egress, "vetted_addresses", vetted)
        s3 = type("S", (), {"endpoint": f"https://s3.test:{port}/"})()
        call = _deliver(_deliver_input(s3)) if method == "PUT" else _undo(_undo_input(s3))
        with pytest.raises(ApplicationError) as exc:
            await call
    assert [line.split(" ", 1)[0] for line in received] == [method]
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationUnreachable", False)
    assert exc.value.message == f"could not reach s3.test:{port} (ServerDisconnectedError)"


async def test_the_resolver_answers_for_the_checked_host_only():
    resolver = pinned_http.PinnedResolver("s3.test", "93.184.216.34")
    [answer] = await resolver.resolve("s3.test", 443)
    assert (answer["host"], answer["port"], answer["family"]) == ("93.184.216.34", 443, socket.AF_INET)
    with pytest.raises(OSError):
        await resolver.resolve("elsewhere.test", 443)


def _record_addresses(monkeypatch) -> list[str]:
    tried: list[str] = []
    real = pinned_http._at

    async def _at(ip, host, context, attempt):
        tried.append(ip)
        return await real(ip, host, context, attempt)

    monkeypatch.setattr(pinned_http, "_at", _at)
    return tried


# --- the classifier -----------------------------------------------------------------------

WHERE = Endpoint("s3.example.com", 443, "s3.example.com")
_KEY = aiohttp.client_reqrep.ConnectionKey("s3.example.com", 443, True, True, None, None, None)


@pytest.mark.parametrize(
    ("exc", "type_", "final"),
    [
        (Refused(Reply(403, "AccessDenied")), "DestinationRefused", True),
        (Refused(Reply(404, "NoSuchBucket")), "DestinationRefused", True),
        (Refused(Reply(400, "AuthorizationHeaderMalformed")), "DestinationRefused", True),
        (Refused(Reply(400, "RequestTimeout")), "DestinationUnavailable", False),
        (Refused(Reply(408)), "DestinationUnavailable", False),
        (Refused(Reply(429)), "DestinationUnavailable", False),
        (Refused(Reply(500)), "DestinationUnavailable", False),
        (Refused(Reply(503, "SlowDown")), "DestinationUnavailable", False),
        (Refused(Reply(307)), "DestinationRefused", True),
        (egress.EgressBlocked("URL resolves to a private address: 10.0.0.5"), "EgressBlocked", True),
        (egress.CertificateMismatch("certificate not valid for s3.example.com"), "CertificateMismatch", True),
        (aiohttp.ClientConnectorSSLError(_KEY, ssl.SSLError("bad")), "TlsFailed", True),
        (aiohttp.ClientConnectorError(_KEY, ConnectionRefusedError(111, "x")), "DestinationUnreachable", False),
        (aiohttp.ServerDisconnectedError(), "DestinationUnreachable", False),
        (aiohttp.ClientPayloadError("cut"), "DestinationUnreachable", False),
        (asyncio.TimeoutError(), "DestinationUnreachable", False),
        (ValueError("anything else"), "DeliveryFailed", True),
    ],
)
def test_the_classifier(exc, type_, final):
    got_type, message, got_final = describe(exc, WHERE, "write")
    assert (got_type, got_final) == (type_, final)
    assert "anything else" not in message


# --- the destination test -----------------------------------------------------------------


def _test_input(endpoint_: str, prefix: str = "", secret: str | None = None) -> DestinationTestInput:
    return DestinationTestInput(
        kind="bucket",
        address={"endpoint": endpoint_, "bucket": "results", "prefix": prefix},
        encrypted_secret=secret or _secret(),
    )


async def test_the_test_writes_and_deletes_one_named_object(s3):
    assert await _test(_test_input(s3.endpoint, prefix="exports")) == [
        ("address", "passed", None),
        ("tls", "passed", None),
        ("write", "passed", None),
        ("delete", "passed", None),
    ]
    put, delete = s3.requests
    assert put.method == "PUT" and put.body == b"" and put.headers["Content-Length"] == "0"
    assert put.path.startswith("/results/exports/.scrapeflow-test-")
    assert delete.method == "DELETE" and delete.path == put.path
    assert s3.objects == {}


async def test_a_refused_delete_is_a_warning_naming_what_was_left(s3):
    s3.plays["DELETE"] = [Play(403, error_xml("AccessDenied"))]
    checks_ = await _test(_test_input(s3.endpoint))

    left = s3.requests[0].path.removeprefix("/results/")
    assert checks_[:3] == [("address", "passed", None), ("tls", "passed", None), ("write", "passed", None)]
    assert checks_[3] == (
        "delete",
        "warning",
        f"delete refused — rollback to this bucket will fail; left `{left}` "
        "(s3.test refused the delete (AccessDenied, HTTP 403))",
    )
    assert left.startswith(".scrapeflow-test-")


async def test_a_refused_write_fails_the_test_and_tries_no_delete(s3):
    s3.plays["PUT"] = [Play(403, error_xml("InvalidAccessKeyId"))]
    assert (await _test(_test_input(s3.endpoint)))[-1] == (
        "write", "failed", "s3.test refused the write (InvalidAccessKeyId, HTTP 403)",
    )
    assert [r.method for r in s3.requests] == ["PUT"]


async def test_a_certificate_for_another_name_fails_the_tls_check(monkeypatch):
    async with s3_server(certificate_name="other.test") as s3:
        monkeypatch.setattr(egress, "tls_context", lambda: s3.client_context)

        async def vetted(host, port):
            return ["127.0.0.1"]

        monkeypatch.setattr(egress, "vetted_addresses", vetted)
        assert await _test(_test_input(s3.endpoint)) == [
            ("address", "passed", None),
            ("tls", "failed", "certificate not valid for s3.test"),
        ]


async def test_an_endpoint_that_is_not_https_fails_the_address_check():
    assert await _test(_test_input("http://s3.example.com/")) == [
        ("address", "failed", "must be an https endpoint"),
    ]


async def test_the_test_refuses_another_kind():
    inp = _test_input("https://s3.example.com/")
    inp.kind = "webhook"
    with pytest.raises(ApplicationError) as exc:
        await _test(inp)
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationKind", True)
