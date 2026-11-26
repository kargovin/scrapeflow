"""The bucket sink against a real S3 server: compose's MinIO, as a stand-in third-party bucket.

MinIO speaks plain http on a private address, so the test fronts it with a TLS proxy on 127.0.0.1
holding a certificate for ``s3.test``, and answers the address check with that proxy. Signing,
streaming with a length, the delete and MinIO's refusals are real. Skipped unless
``S3_TEST_ENDPOINT`` is set. From the repo root, with compose up:

    docker run --rm --network docker_default -v "$PWD/delivery-worker:/app" -w /app \\
        -e S3_TEST_ENDPOINT=minio:9000 -e S3_TEST_ROOT_USER=scrapeflow \\
        -e S3_TEST_ROOT_PASSWORD=scrapeflow_secret -e MINIO_ENDPOINT=minio:9000 \\
        -e MINIO_ACCESS_KEY=scrapeflow-delivery -e MINIO_SECRET_KEY=scrapeflow_delivery_secret \\
        --entrypoint python delivery-worker-test -m pytest -q -p no:cacheprovider tests/test_bucket_minio.py
"""

import asyncio
import io
import json
import os
import tempfile
import uuid
from contextlib import asynccontextmanager

import pytest
from cryptography.fernet import Fernet
from miniopy_async import Minio
from miniopy_async.credentials import StaticProvider
from miniopy_async.minioadmin import MinioAdmin
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from tests.tls import contexts
from worker import checks, egress, main
from worker.bucket import BucketActivities
from worker.config import settings
from worker.contracts import DeliverBucketInput, DestinationTestInput, UndoBucketInput
from worker.source import Source

ENDPOINT = os.environ.get("S3_TEST_ENDPOINT")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="S3_TEST_ENDPOINT not set")

PAGE = b"<html>" + os.urandom(300_000).hex().encode() + b"</html>"


def _secret(access_key: str, secret_key: str) -> str:
    token = Fernet(settings.destination_encryption_key).encrypt(
        json.dumps({"access_key": access_key, "secret_key": secret_key}).encode()
    )
    return token.decode()


@asynccontextmanager
async def _tls_proxy(target: str):
    """https on 127.0.0.1 for ``s3.test``, piped to ``target`` as is."""
    host, port = target.rsplit(":", 1)
    server_ctx, client_ctx, _ = contexts("s3.test")

    async def pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async def handle(reader, writer):
        up_reader, up_writer = await asyncio.open_connection(host, int(port))
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer), return_exceptions=True)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    async with server:
        yield server.sockets[0].getsockname()[1], client_ctx


def _policy(bucket: str, actions: list[str]) -> str:
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{bucket}/*"]}
        ],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(doc, f)
    return f.name


@pytest.fixture
async def world(monkeypatch):
    root_user, root_password = os.environ["S3_TEST_ROOT_USER"], os.environ["S3_TEST_ROOT_PASSWORD"]
    root = Minio(ENDPOINT, access_key=root_user, secret_key=root_password, secure=False)
    admin = MinioAdmin(ENDPOINT, StaticProvider(root_user, root_password), secure=False)
    tag = uuid.uuid4().hex[:10]
    bucket = f"m4-dest-{tag}"
    source_key = f"tests/m4-{tag}/scrape.html"
    users = {"writer": ["s3:PutObject", "s3:DeleteObject"], "putonly": ["s3:PutObject"]}
    creds = {name: (f"m4-{name}-{tag}", uuid.uuid4().hex) for name in users}

    await root.make_bucket(bucket)
    await root.put_object(settings.minio_bucket, source_key, io.BytesIO(PAGE), len(PAGE))
    for name, actions in users.items():
        path = _policy(bucket, actions)
        await admin.policy_add(f"m4-{name}-{tag}", path)
        os.unlink(path)
        await admin.user_add(*creds[name])
        await admin.attach_policy([f"m4-{name}-{tag}"], user=creds[name][0])

    minio = main.minio_client()
    try:
        async with _tls_proxy(ENDPOINT) as (port, client_ctx):

            async def vetted(host, port_):
                assert host == "s3.test"
                return ["127.0.0.1"]

            monkeypatch.setattr(egress, "vetted_addresses", vetted)
            monkeypatch.setattr(egress, "tls_context", lambda: client_ctx)
            yield {
                "root": root,
                "bucket": bucket,
                "endpoint": f"https://s3.test:{port}/",
                "source": f"{settings.minio_bucket}/{source_key}",
                "secret": {name: _secret(*c) for name, c in creds.items()},
                "activities": BucketActivities(Source(minio, settings.minio_bucket)),
            }
    finally:
        await minio.close_session()
        for obj in await root.list_objects(bucket, recursive=True):
            await root.remove_object(bucket, obj.object_name)
        await root.remove_bucket(bucket)
        await root.remove_object(settings.minio_bucket, source_key)
        for name in users:
            await admin.user_remove(creds[name][0])
            await admin.policy_remove(f"m4-{name}-{tag}")
        await root.close_session()
        if admin._session is not None:
            await admin._session.close()


def _address(w, bucket: str | None = None) -> dict:
    return {"endpoint": w["endpoint"], "bucket": bucket or w["bucket"], "prefix": "exports"}


def _deliver_input(w, user: str = "writer", **kw) -> DeliverBucketInput:
    fields = {
        "address": _address(w),
        "encrypted_secret": w["secret"][user],
        "source_path": w["source"],
        "pipeline_id": "p1",
        "run_id": "r1",
        "block_id": "store",
        "ext": "html",
    } | kw
    return DeliverBucketInput(**fields)


async def _keys(w) -> dict[str, int]:
    return {o.object_name: o.size for o in await w["root"].list_objects(w["bucket"], recursive=True)}


async def _run(fn, inp):
    return await ActivityEnvironment().run(fn, inp)


async def test_a_retried_delivery_leaves_one_object_and_the_undo_removes_it(world):
    w = world
    deliver, undo = w["activities"].deliver_bucket, w["activities"].undo_bucket

    first = await _run(deliver, _deliver_input(w))
    second = await _run(deliver, _deliver_input(w))

    assert first == second
    assert first.location.key == "exports/p1/r1/store.html"
    assert await _keys(w) == {"exports/p1/r1/store.html": len(PAGE)}
    response = await w["root"].get_object(w["bucket"], first.location.key)
    assert await response.read() == PAGE
    assert response.headers["Content-Type"] == "text/html"
    response.close()

    undo_input = UndoBucketInput(location=first.location, encrypted_secret=w["secret"]["writer"])
    await _run(undo, undo_input)
    assert await _keys(w) == {}
    # Already gone: undone again.
    await _run(undo, undo_input)


async def test_an_undo_without_delete_permission_fails_and_leaves_the_object(world):
    w = world
    out = await _run(w["activities"].deliver_bucket, _deliver_input(w, user="putonly"))
    with pytest.raises(ApplicationError) as exc:
        await _run(
            w["activities"].undo_bucket,
            UndoBucketInput(location=out.location, encrypted_secret=w["secret"]["putonly"]),
        )
    assert (exc.value.message, exc.value.non_retryable) == (
        "s3.test refused the delete (AccessDenied, HTTP 403)",
        True,
    )
    assert list(await _keys(w)) == [out.location.key]


async def test_the_destination_test_passes_and_leaves_nothing(world):
    w = world
    out = await _run(
        checks.test_bucket,
        DestinationTestInput(kind="bucket", address=_address(w), encrypted_secret=w["secret"]["writer"]),
    )
    assert [(c.check, c.outcome) for c in out.checks] == [
        ("address", "passed"), ("tls", "passed"), ("write", "passed"), ("delete", "passed"),
    ]
    assert await _keys(w) == {}


async def test_a_delete_refused_bucket_is_reported_by_the_test(world):
    w = world
    out = await _run(
        checks.test_bucket,
        DestinationTestInput(kind="bucket", address=_address(w), encrypted_secret=w["secret"]["putonly"]),
    )
    [left] = await _keys(w)
    assert left.startswith("exports/.scrapeflow-test-")
    assert [(c.check, c.outcome) for c in out.checks][-1] == ("delete", "warning")
    assert out.checks[-1].message.startswith(
        f"delete refused — rollback to this bucket will fail; left `{left}`"
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("secret", "s3.test refused the write (SignatureDoesNotMatch, HTTP 403)"),
        # The user's policy covers its own bucket only.
        ("bucket", "s3.test refused the write (AccessDenied, HTTP 403)"),
    ],
)
async def test_a_refused_write_is_final(world, change, message):
    w = world
    inp = _deliver_input(w)
    if change == "secret":
        inp.encrypted_secret = _secret(_writer_access_key(w), "wrong-secret")
    else:
        inp.address = _address(w, bucket=f"{w['bucket']}-missing")
    with pytest.raises(ApplicationError) as exc:
        await _run(w["activities"].deliver_bucket, inp)
    assert (exc.value.message, exc.value.non_retryable) == (message, True)


def _writer_access_key(w) -> str:
    return json.loads(Fernet(settings.destination_encryption_key).decrypt(w["secret"]["writer"]))[
        "access_key"
    ]


async def test_a_missing_copy_is_final(world):
    w = world
    with pytest.raises(ApplicationError) as exc:
        await _run(w["activities"].deliver_bucket, _deliver_input(w, source_path=w["source"] + ".gone"))
    assert (exc.value.type, exc.value.message, exc.value.non_retryable) == (
        "SourceFailed",
        "ScrapeFlow's copy of the input is gone",
        True,
    )
