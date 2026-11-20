"""M.6 — a version that names destinations: checked at save and at the trigger (owner and kind,
ADR-015 §2b), written to ``pipeline_version_destinations`` (§2c's delete check reads it)."""

import asyncio
import socket
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.quota import lock_user
from app.main import app
from app.models.destination import Destination, PipelineVersionDestination
from app.models.pipeline import Pipeline, PipelineRun, PipelineVersion
from app.workflows.client import get_temporal

HOOK = "https://example.com/hook"


@pytest.fixture(autouse=True)
def resolves_to():
    with patch("app.core.security.socket") as fake_socket:
        fake_socket.gaierror = socket.gaierror
        fake_socket.getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 0))]
        yield


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


@pytest.fixture
def temporal():
    client = AsyncMock()
    app.dependency_overrides[get_temporal] = lambda: client
    yield client
    app.dependency_overrides.pop(get_temporal, None)


async def _destination(user_id, kind, name=None) -> Destination:
    async with AsyncSessionLocal() as db:
        destination = Destination(
            user_id=user_id,
            name=name or f"{kind}-{uuid.uuid4().hex[:6]}",
            kind=kind,
            encrypted_secret="x",
            display_host="h.example.com",
        )
        db.add(destination)
        await db.commit()
        await db.refresh(destination)
        return destination


def _bucket(destination_id, id="store", input="fetch"):
    return {
        "id": id,
        "type": "bucket",
        "input": input,
        "config": {"destination_id": str(destination_id)},
    }


def _hook(id="notify", input="store", **config):
    return {"id": id, "type": "webhook", "input": input, "config": config or {"url": HOOK}}


def _definition(*sinks):
    fetch = {"id": "fetch", "type": "scrape", "config": {"url": "https://example.com"}}
    return {"blocks": [fetch, *sinks]}


async def _save(client, headers, definition, name="sinks"):
    return await client.post(
        "/pipelines", json={"name": name, "definition": definition}, headers=headers
    )


async def _references(pipeline_id) -> dict[int, set[uuid.UUID]]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(PipelineVersion.version, PipelineVersionDestination.destination_id)
            .join(
                PipelineVersionDestination,
                PipelineVersionDestination.pipeline_version_id == PipelineVersion.id,
                isouter=True,
            )
            .where(PipelineVersion.pipeline_id == uuid.UUID(str(pipeline_id)))
        )
        found: dict[int, set[uuid.UUID]] = {}
        for version, destination_id in rows:
            found.setdefault(version, set())
            if destination_id is not None:
                found[version].add(destination_id)
        return found


# --- save -----------------------------------------------------------------------------------


async def test_each_version_records_the_destinations_it_names(client, headers, db_user):
    bucket = await _destination(db_user.id, "bucket")
    hook = await _destination(db_user.id, "webhook")
    other_bucket = await _destination(db_user.id, "bucket")

    resp = await _save(
        client, headers, _definition(_bucket(bucket.id), _hook(destination_id=str(hook.id)))
    )
    assert resp.status_code == 201, resp.text
    pipeline = resp.json()
    assert pipeline["definition"]["blocks"][1]["config"] == {"destination_id": str(bucket.id)}
    assert await _references(pipeline["id"]) == {1: {bucket.id, hook.id}}

    # A new version records its own; the old one keeps what it named.
    moved = _definition(_bucket(other_bucket.id), _hook())
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": moved}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert await _references(pipeline["id"]) == {1: {bucket.id, hook.id}, 2: {other_bucket.id}}

    # An unchanged definition is no new version and writes nothing.
    same = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": moved}, headers=headers
    )
    assert same.json()["version"] == 2
    assert await _references(pipeline["id"]) == {1: {bucket.id, hook.id}, 2: {other_bucket.id}}


async def test_a_definition_naming_no_destination_records_none(client, headers):
    resp = await _save(client, headers, _definition(_hook(input="fetch")))
    assert resp.status_code == 201, resp.text
    assert await _references(resp.json()["id"]) == {1: set()}


async def test_another_users_or_a_missing_destination_is_a_404(client, headers, other_user):
    theirs = await _destination(other_user.id, "bucket")
    for destination_id in (theirs.id, uuid.uuid4()):
        resp = await _save(client, headers, _definition(_bucket(destination_id)))
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Destination not found"
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(Pipeline).where(Pipeline.name == "sinks")) is None


@pytest.mark.parametrize(
    ("block", "kind", "needs"),
    [
        ("bucket", "webhook", "bucket"),
        ("webhook", "bucket", "webhook"),
        ("bucket", "smtp", "bucket"),
    ],
)
async def test_a_destination_of_the_wrong_kind_is_refused_naming_the_block(
    client, headers, db_user, block, kind, needs
):
    destination = await _destination(db_user.id, kind, name=f"wrong-{kind}")
    sink = (
        _bucket(destination.id)
        if block == "bucket"
        else _hook(input="fetch", destination_id=str(destination.id))
    )
    resp = await _save(client, headers, _definition(sink))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == [
        {
            "block_id": sink["id"],
            "message": f"destination 'wrong-{kind}' is a {kind} destination; this block needs a "
            f"{needs} destination",
        }
    ]


async def test_a_pipeline_saved_through_the_api_holds_its_destination(client, headers, db_user):
    bucket = await _destination(db_user.id, "bucket")
    pipeline = (await _save(client, headers, _definition(_bucket(bucket.id)))).json()

    resp = await client.delete(f"/destinations/{bucket.id}", headers=headers)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["pipelines"] == [{"id": pipeline["id"], "name": "sinks"}]


async def test_a_save_naming_a_destination_waits_for_the_users_lock(client, headers, db_user):
    bucket = await _destination(db_user.id, "bucket")
    async with AsyncSessionLocal() as holder:
        await lock_user(db_user.id, holder)
        task = asyncio.create_task(_save(client, headers, _definition(_bucket(bucket.id))))
        await asyncio.sleep(1.5)
        assert not task.done(), "the save did not wait for the user's lock"
        # A destination delete holding the lock lands first: the save must see it gone.
        await holder.delete(await holder.get(Destination, bucket.id))
        await holder.commit()
    resp = await asyncio.wait_for(task, timeout=10)
    assert resp.status_code == 404, resp.text


async def test_moving_an_inline_webhook_to_a_destination_drops_its_secret(client, headers, db_user):
    hook = await _destination(db_user.id, "webhook")
    inline = _definition(_hook(input="fetch", url=HOOK, secret="old-secret"))
    pipeline = (await _save(client, headers, inline)).json()
    assert pipeline["definition"]["blocks"][1]["secrets"] == ["secret"]

    moved = _definition(_hook(input="fetch", destination_id=str(hook.id)))
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": moved}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    block = resp.json()["definition"]["blocks"][1]
    assert block["secrets"] == []
    assert block["config"]["destination_id"] == str(hook.id)
    assert block["config"]["url"] is None


async def test_a_kept_inline_webhook_keeps_its_secret(client, headers):
    inline = _definition(_hook(input="fetch", url=HOOK, secret="old-secret"))
    pipeline = (await _save(client, headers, inline)).json()
    renamed = _definition(_hook(input="fetch", url=HOOK, events=["pipeline.failed"]))
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": renamed}, headers=headers
    )
    assert resp.json()["definition"]["blocks"][1]["secrets"] == ["secret"]


# --- the trigger ----------------------------------------------------------------------------


async def _run_rows(user_id) -> list[PipelineRun]:
    async with AsyncSessionLocal() as db:
        return list(await db.scalars(select(PipelineRun).where(PipelineRun.user_id == user_id)))


async def test_a_run_naming_destinations_starts_with_no_url_to_check(
    client, headers, db_user, temporal
):
    bucket = await _destination(db_user.id, "bucket")
    hook = await _destination(db_user.id, "webhook")
    definition = _definition(_bucket(bucket.id), _hook(destination_id=str(hook.id)))
    pipeline = (await _save(client, headers, definition)).json()

    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)

    assert resp.status_code == 202, resp.text
    (_, workflow_input), _ = temporal.start_workflow.await_args
    assert [b.type for b in workflow_input.blocks] == ["scrape", "bucket", "webhook"]
    assert workflow_input.webhook_secrets == {}


async def test_the_trigger_checks_destinations_under_the_users_lock(
    client, headers, db_user, temporal
):
    bucket = await _destination(db_user.id, "bucket")
    pipeline = (await _save(client, headers, _definition(_bucket(bucket.id)))).json()

    async with AsyncSessionLocal() as holder:
        await lock_user(db_user.id, holder)
        task = asyncio.create_task(
            client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
        )
        await asyncio.sleep(1.5)
        assert not task.done(), "the trigger did not wait for the user's lock"
        # Outside the API's interlock (the version rows cascade): the trigger must see it gone.
        await holder.delete(await holder.get(Destination, bucket.id))
        await holder.commit()
    resp = await asyncio.wait_for(task, timeout=10)

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Destination not found"
    assert await _run_rows(db_user.id) == []
    temporal.start_workflow.assert_not_awaited()


async def test_the_trigger_refuses_a_destination_of_the_wrong_kind(
    client, headers, db_user, temporal
):
    bucket = await _destination(db_user.id, "bucket")
    pipeline = (await _save(client, headers, _definition(_bucket(bucket.id)))).json()
    async with AsyncSessionLocal() as db:
        await db.execute(
            Destination.__table__.update().where(Destination.id == bucket.id).values(kind="smtp")
        )
        await db.commit()

    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["block_id"] == "store"
    assert await _run_rows(db_user.id) == []


def test_a_webhook_destination_adds_no_url_to_the_triggers_ssrf_list():
    from app.core.pipeline_trigger import run_urls
    from app.pipelines.definition import parse_definition
    from app.pipelines.validator import validate

    stored = validate(
        _definition(_hook(input="fetch", destination_id=str(uuid.uuid4())))
    ).definition
    parsed, _ = parse_definition(stored)
    assert run_urls(parsed, {}, {}) == ["https://example.com/"]
