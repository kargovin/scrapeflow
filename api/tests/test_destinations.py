"""M.2 — the destinations API (ADR-015 §2a to §2c): CRUD, the secret never returned, a webhook shown
by its host only, 404 across users, and the delete refused while something refers to one."""

import asyncio
import socket
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.destinations import decrypt_secret
from app.core.quota import lock_user
from app.main import app
from app.models.destination import Destination, PipelineVersionDestination
from app.models.pipeline import Pipeline, PipelineRun, PipelineVersion
from app.settings import settings

SENTINEL = "s3cr3t-SENTINEL-7f3a"
HOOK = f"https://hooks.example.com/services/T0001/B0002/{SENTINEL}-token"


@pytest.fixture(autouse=True)
def resolves_to():
    with patch("app.core.security.socket") as fake_socket:
        fake_socket.gaierror = socket.gaierror

        def set_ip(ip: str) -> None:
            fake_socket.getaddrinfo.return_value = [(None, None, None, None, (ip, 0))]

        set_ip("93.184.216.34")
        yield set_ip


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


@pytest.fixture
def admin():
    from app.auth.dependencies import get_current_admin_user

    app.dependency_overrides[get_current_admin_user] = lambda: MagicMock(id=uuid.uuid4())
    yield
    app.dependency_overrides.pop(get_current_admin_user, None)


BODIES = {
    "bucket": {
        "kind": "bucket",
        "address": {
            "endpoint": "https://s3.example.com",
            "bucket": "my-results",
            "prefix": "/scrapeflow/",
            "region": "eu-west-1",
        },
        "secret": {"access_key": "AKIA-" + SENTINEL, "secret_key": SENTINEL},
    },
    "postgres": {
        "kind": "postgres",
        "address": {"host": "db.example.com", "database": "warehouse", "table": "scrapes"},
        "secret": {"user": "loader", "password": SENTINEL},
    },
    "smtp": {
        "kind": "smtp",
        "address": {
            "host": "smtp.example.com",
            "port": 465,
            "tls_mode": "implicit",
            "from": "alerts@example.com",
            "to": ["me@example.com"],
        },
        "secret": {"user": "alerts", "password": SENTINEL},
    },
    "webhook": {"kind": "webhook", "secret": {"url": HOOK, "secret": SENTINEL}},
}
HOSTS = {
    "bucket": "s3.example.com",
    "postgres": "db.example.com",
    "smtp": "smtp.example.com",
    "webhook": "hooks.example.com",
}


async def _create(client, headers, kind, name=None):
    resp = await client.post(
        "/destinations", json={"name": name or f"{kind}-dest", **BODIES[kind]}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _row(destination_id) -> Destination | None:
    async with AsyncSessionLocal() as db:
        return await db.get(Destination, uuid.UUID(str(destination_id)))


def _no_secret(resp) -> None:
    assert SENTINEL not in resp.text
    assert "T0001" not in resp.text and "services" not in resp.text


# --- create and read ------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(BODIES))
async def test_each_kind_is_saved_with_its_host_and_no_secret_is_ever_returned(
    client, headers, kind, admin
):
    resp = await client.post(
        "/destinations", json={"name": f"{kind}-dest", **BODIES[kind]}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    _no_secret(resp)
    created = resp.json()
    assert created["kind"] == kind
    assert created["display_host"] == HOSTS[kind]
    assert "secret" not in created and "encrypted_secret" not in created

    for read in (
        await client.get(f"/destinations/{created['id']}", headers=headers),
        await client.get("/destinations", headers=headers),
        await client.patch(
            f"/destinations/{created['id']}", json={"name": f"{kind}-2"}, headers=headers
        ),
        await client.get(f"/admin/destinations/{created['id']}"),
        await client.get("/admin/destinations", params={"kind": kind}),
    ):
        assert read.status_code == 200, read.text
        _no_secret(read)


async def test_the_address_is_returned_as_stored(client, headers):
    bucket = await _create(client, headers, "bucket")
    assert bucket["address"] == {
        "endpoint": "https://s3.example.com/",
        "bucket": "my-results",
        "prefix": "scrapeflow",
        "region": "eu-west-1",
    }
    pg = await _create(client, headers, "postgres")
    assert pg["address"] == {
        "host": "db.example.com",
        "port": 5432,
        "database": "warehouse",
        "schema": "public",
        "table": "scrapes",
        "ca_certificate": None,
    }
    smtp = await _create(client, headers, "smtp")
    assert smtp["address"]["from"] == "alerts@example.com"
    assert smtp["address"]["to"] == ["me@example.com"]
    webhook = await _create(client, headers, "webhook")
    assert webhook["address"] == {}


async def test_the_secret_is_stored_under_the_destinations_key_only(client, headers):
    created = await _create(client, headers, "webhook")
    row = await _row(created["id"])
    assert SENTINEL not in row.encrypted_secret
    assert decrypt_secret(row.encrypted_secret) == {"url": HOOK, "secret": SENTINEL}
    with pytest.raises(InvalidToken):
        Fernet(settings.credentials_encryption_key).decrypt(row.encrypted_secret.encode())
    with pytest.raises(InvalidToken):
        Fernet(settings.llm_key_encryption_key).decrypt(row.encrypted_secret.encode())


async def test_the_list_is_the_users_own_and_filters_by_kind(client, headers, other_user):
    mine = await _create(client, headers, "bucket")
    await _create(client, headers, "webhook")
    async with AsyncSessionLocal() as db:
        db.add(
            Destination(
                user_id=other_user.id,
                name="theirs",
                kind="bucket",
                encrypted_secret="x",
                display_host="h",
            )
        )
        await db.commit()

    listed = (await client.get("/destinations", headers=headers)).json()
    assert {d["kind"] for d in listed} == {"bucket", "webhook"}
    buckets = (await client.get("/destinations", params={"kind": "bucket"}, headers=headers)).json()
    assert [d["id"] for d in buckets] == [mine["id"]]


@pytest.mark.parametrize(
    "kind, change, field",
    [
        ("webhook", {"secret": {"url": "http://hooks.example.com/x"}}, "url"),
        ("bucket", {"address": {"endpoint": "http://s3.example.com"}}, "endpoint"),
        ("bucket", {"address": {"endpoint": "https://s3.example.com/a/b"}}, "endpoint"),
        ("bucket", {"address": {"endpoint": "https://u:p@s3.example.com"}}, "endpoint"),
        ("bucket", {"address": {"bucket": "Not_A_Bucket"}}, "bucket"),
        ("postgres", {"address": {"host": "db.example.com/x"}}, "host"),
        ("postgres", {"address": {"ca_certificate": "not a cert"}}, "ca_certificate"),
        ("smtp", {"address": {"to": []}}, "to"),
        ("smtp", {"address": {"from": "nobody"}}, "from"),
        ("smtp", {"address": {"tls_mode": "none"}}, "tls_mode"),
        ("postgres", {"secret": {"password": ""}}, "password"),
        ("bucket", {"address": {"surprise": 1}}, "surprise"),
    ],
)
async def test_a_bad_field_is_refused_at_save_naming_it(client, headers, kind, change, field):
    body = {"name": "bad", **BODIES[kind]}
    for part, values in change.items():
        body[part] = {**body.get(part, {}), **values}
    resp = await client.post("/destinations", json=body, headers=headers)
    assert resp.status_code == 422
    assert any(e["loc"][-1] == field for e in resp.json()["detail"]), resp.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "post",
            "/destinations",
            {"name": "x", "kind": "webhook", "secret": {"url": HOOK.replace("https", "http")}},
        ),
        (
            "post",
            "/destinations/test",
            {
                "kind": "bucket",
                **{k: v for k, v in BODIES["bucket"].items() if k != "kind"},
                "secret": {"access_key": "a", "secret_key": SENTINEL * 20},
            },
        ),
        ("post", "/destinations", {"name": "x", **BODIES["smtp"], "secret": SENTINEL}),
        ("patch", "/destinations/{id}", {"secret": SENTINEL}),
        ("post", "/destinations/{id}/test", {"address": SENTINEL}),
    ],
)
async def test_a_refused_body_echoes_none_of_its_values(client, headers, method, path, body):
    created = await _create(client, headers, "smtp")
    resp = await getattr(client, method)(path.format(id=created["id"]), json=body, headers=headers)
    assert resp.status_code == 422
    _no_secret(resp)
    assert resp.json()["detail"] and all("input" not in e for e in resp.json()["detail"])


@pytest.mark.parametrize("kind", list(BODIES))
async def test_a_private_address_is_refused_at_save(client, headers, resolves_to, kind):
    resolves_to("10.0.0.5")
    resp = await client.post("/destinations", json={"name": "lan", **BODIES[kind]}, headers=headers)
    assert resp.status_code == 400
    assert "private address" in resp.json()["detail"]


async def test_a_name_is_unique_per_user(client, headers):
    await _create(client, headers, "bucket", name="dup")
    resp = await client.post(
        "/destinations", json={"name": "dup", **BODIES["smtp"]}, headers=headers
    )
    assert resp.status_code == 409


# --- edit -----------------------------------------------------------------------------------


async def test_an_edit_replaces_only_the_fields_given(client, headers):
    created = await _create(client, headers, "postgres")
    resp = await client.patch(
        f"/destinations/{created['id']}",
        json={"address": {"host": "db2.example.com"}, "secret": {"password": "rotated"}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_host"] == "db2.example.com"
    assert resp.json()["address"]["table"] == "scrapes"
    row = await _row(created["id"])
    assert decrypt_secret(row.encrypted_secret) == {"user": "loader", "password": "rotated"}


async def test_a_new_webhook_url_is_shown_by_its_host_only(client, headers):
    created = await _create(client, headers, "webhook")
    resp = await client.patch(
        f"/destinations/{created['id']}",
        json={"secret": {"url": f"https://other.example.org/in/{SENTINEL}"}},
        headers=headers,
    )
    assert resp.status_code == 200
    _no_secret(resp)
    assert resp.json()["display_host"] == "other.example.org"
    row = await _row(created["id"])
    assert decrypt_secret(row.encrypted_secret)["secret"] == SENTINEL


async def test_a_refused_edit_echoes_no_stored_value_and_changes_nothing(client, headers):
    created = await _create(client, headers, "smtp")
    resp = await client.patch(
        f"/destinations/{created['id']}",
        json={"secret": {"extra": "x"}, "address": {"port": 0}},
        headers=headers,
    )
    assert resp.status_code == 422
    _no_secret(resp)
    assert {tuple(e["loc"]) for e in resp.json()["detail"]} >= {("body", "address", "port")}
    # The merged secret holds stored values: no error carries its input.
    assert all("input" not in e for e in resp.json()["detail"])
    row = await _row(created["id"])
    assert row.address["port"] == 465


async def test_the_kind_cannot_change(client, headers):
    created = await _create(client, headers, "bucket")
    resp = await client.patch(
        f"/destinations/{created['id']}", json={"kind": "webhook"}, headers=headers
    )
    assert resp.status_code == 422


async def test_an_edit_to_a_private_address_is_refused(client, headers, resolves_to):
    created = await _create(client, headers, "bucket")
    resolves_to("127.0.0.1")
    resp = await client.patch(
        f"/destinations/{created['id']}",
        json={"address": {"endpoint": "https://minio.internal"}},
        headers=headers,
    )
    assert resp.status_code == 400
    assert (await _row(created["id"])).display_host == "s3.example.com"


# --- another user's -------------------------------------------------------------------------


async def test_another_users_destination_is_a_404_everywhere(client, headers, other_user):
    async with AsyncSessionLocal() as db:
        theirs = Destination(
            user_id=other_user.id,
            name="theirs",
            kind="webhook",
            encrypted_secret="x",
            display_host="h",
        )
        db.add(theirs)
        await db.commit()
    url = f"/destinations/{theirs.id}"
    assert (await client.get(url, headers=headers)).status_code == 404
    assert (await client.patch(url, json={"name": "mine"}, headers=headers)).status_code == 404
    assert (await client.delete(url, headers=headers)).status_code == 404
    assert (await _row(theirs.id)).name == "theirs"


# --- delete ---------------------------------------------------------------------------------


async def _version_using(user_id, destination_id, *, name="uses-it", deleted=False):
    async with AsyncSessionLocal() as db:
        pipeline = Pipeline(
            user_id=user_id, name=name, deleted_at=datetime.now(UTC) if deleted else None
        )
        db.add(pipeline)
        await db.flush()
        version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition={"blocks": []})
        db.add(version)
        await db.flush()
        db.add(
            PipelineVersionDestination(
                pipeline_version_id=version.id, destination_id=uuid.UUID(str(destination_id))
            )
        )
        await db.commit()
        return pipeline, version


async def _run(db, pipeline, version, status="running") -> PipelineRun:
    run = PipelineRun(
        pipeline_id=pipeline.id,
        pipeline_version_id=version.id,
        user_id=pipeline.user_id,
        status=status,
        workflow_id=f"pipeline-run-{uuid.uuid4()}",
    )
    db.add(run)
    await db.flush()
    return run


async def test_a_destination_no_one_uses_is_deleted(client, headers):
    created = await _create(client, headers, "bucket")
    resp = await client.delete(f"/destinations/{created['id']}", headers=headers)
    assert resp.status_code == 204
    assert await _row(created["id"]) is None


async def test_the_delete_is_refused_naming_the_pipelines_that_use_it(client, headers, db_user):
    created = await _create(client, headers, "bucket")
    pipeline, _ = await _version_using(db_user.id, created["id"], name="nightly export")

    resp = await client.delete(f"/destinations/{created['id']}", headers=headers)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "destination_in_use"
    assert detail["pipelines"] == [{"id": str(pipeline.id), "name": "nightly export"}]
    assert detail["runs"] == []
    assert "nightly export" in detail["message"]
    assert await _row(created["id"]) is not None


async def test_a_deleted_pipeline_holds_it_only_while_its_run_is_running(client, headers, db_user):
    created = await _create(client, headers, "webhook")
    pipeline, version = await _version_using(db_user.id, created["id"], deleted=True)
    async with AsyncSessionLocal() as db:
        run = await _run(db, pipeline, version)
        await _run(db, pipeline, version, status="completed")
        await db.commit()

    resp = await client.delete(f"/destinations/{created['id']}", headers=headers)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["pipelines"] == []
    assert detail["runs"] == [{"id": str(run.id), "pipeline_id": str(pipeline.id)}]

    async with AsyncSessionLocal() as db:
        (await db.get(PipelineRun, run.id)).status = "failed"
        await db.commit()
    assert (
        await client.delete(f"/destinations/{created['id']}", headers=headers)
    ).status_code == 204
    async with AsyncSessionLocal() as db:
        left = await db.scalars(
            select(PipelineVersionDestination).where(
                PipelineVersionDestination.pipeline_version_id == version.id
            )
        )
        assert list(left) == []


async def test_the_delete_waits_for_the_users_lock(client, headers, db_user):
    created = await _create(client, headers, "bucket")
    pipeline, version = await _version_using(db_user.id, created["id"], deleted=True)

    async with AsyncSessionLocal() as holder:
        await lock_user(db_user.id, holder)
        task = asyncio.create_task(client.delete(f"/destinations/{created['id']}", headers=headers))
        await asyncio.sleep(1)
        assert not task.done(), "the delete did not wait for the user's lock"
        # As a trigger would: under the lock, a run starts on a version using it.
        run = await _run(holder, pipeline, version)
        await holder.commit()

    resp = await asyncio.wait_for(task, timeout=10)
    assert resp.status_code == 409
    assert resp.json()["detail"]["runs"][0]["id"] == str(run.id)


# --- admin ----------------------------------------------------------------------------------


async def test_admin_reads_show_the_owner_and_never_the_secret(client, headers, db_user, admin):
    created = await _create(client, headers, "webhook")

    one = await client.get(f"/admin/destinations/{created['id']}")
    assert one.status_code == 200
    _no_secret(one)
    assert one.json()["user_email"] == db_user.email
    assert one.json()["display_host"] == "hooks.example.com"

    listed = (await client.get("/admin/destinations", params={"user_id": str(db_user.id)})).json()
    assert [d["id"] for d in listed] == [created["id"]]
    assert (await client.get(f"/admin/destinations/{uuid.uuid4()}")).status_code == 404


async def test_admin_reads_need_an_admin(client, headers):
    assert (await client.get("/admin/destinations", headers=headers)).status_code == 403
