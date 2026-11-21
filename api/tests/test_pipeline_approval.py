"""N.1 — the Approval block in the catalog and at save and trigger (ADR-016 §9a, §9d; its
destination as ADR-015 §2b's references)."""

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
from app.models.destination import Destination
from app.models.pipeline import Pipeline, PipelineRun
from app.pipelines.catalog import (
    APPROVAL,
    APPROVAL_TIMING,
    BLOCK_OVERHEAD_SECONDS,
    DB_ACTIVITY_SECONDS,
)
from app.pipelines.definition import ApprovalBlock
from app.pipelines.validator import validate
from app.settings import settings
from app.workflows.client import get_temporal
from tests.test_pipeline_destinations import _references

URL = "https://example.com/"


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


def _fetch():
    return {"id": "fetch", "type": "scrape", "config": {"url": URL}}


def _approve(id="approve", input="fetch", **config):
    return {"id": id, "type": "approval", "input": input, "config": config}


def _hook(id="notify", input="approve"):
    return {"id": id, "type": "webhook", "input": input, "config": {"url": "https://example.com/h"}}


def _definition(*blocks):
    return {"blocks": list(blocks)}


def _guarded(**config):
    """scrape -> approval -> webhook."""
    return _definition(_fetch(), _approve(**config), _hook())


async def _save(client, headers, definition, name="approval"):
    return await client.post(
        "/pipelines", json={"name": name, "definition": definition}, headers=headers
    )


async def _refused(client, headers, definition) -> list[dict]:
    resp = await _save(client, headers, definition)
    assert resp.status_code == 422, resp.text
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(Pipeline).where(Pipeline.name == "approval")) is None
    return resp.json()["detail"]


# --- the catalog ----------------------------------------------------------------------------


def test_its_timing_is_its_two_db_steps_and_never_the_wait():
    assert APPROVAL.kind == "effect"
    assert APPROVAL_TIMING.need == 2 * DB_ACTIVITY_SECONDS + BLOCK_OVERHEAD_SECONDS
    assert not APPROVAL_TIMING.calls_activity


async def test_the_catalog_lists_it_with_its_two_destination_kinds(client, headers):
    resp = await client.get("/pipelines/catalog", headers=headers)
    block = next(b for b in resp.json()["blocks"] if b["type"] == "approval")
    assert (block["kind"], block["sink"]) == ("effect", None)
    assert block["destination_kinds"] == ["webhook", "smtp"]
    assert block["destination_kind"] is None
    timing = block["timings"]["default"]
    assert (timing["budget_seconds"], timing["need_seconds"]) == (0, APPROVAL_TIMING.need)


# --- save -----------------------------------------------------------------------------------


def test_the_run_budget_counts_its_db_steps_and_not_its_deadline():
    plain = validate(_definition(_fetch(), _hook(input="fetch"))).run_budget_seconds
    for hours in (1, 168):
        guarded = validate(_guarded(deadline_hours=hours)).run_budget_seconds
        assert guarded - plain == APPROVAL_TIMING.need


async def test_an_approval_saves_with_its_defaults(client, headers):
    resp = await _save(client, headers, _guarded())

    assert resp.status_code == 201, resp.text
    block = resp.json()["definition"]["blocks"][1]
    assert block["config"] == {"deadline_hours": 24, "destination_id": None}
    assert await _references(resp.json()["id"]) == {1: set()}


async def test_an_approval_cannot_be_the_first_block(client, headers):
    detail = await _refused(
        client, headers, _definition(_approve(input=None), _hook(input="approve"))
    )
    assert {
        "block_id": "approve",
        "message": "a pipeline must start with a scrape block, not approval",
    } in detail


async def test_an_approval_cannot_be_the_last_block(client, headers):
    assert await _refused(client, headers, _definition(_fetch(), _approve())) == [
        {
            "block_id": "approve",
            "message": "an approval block holds the blocks after it until it is answered; on "
            "the last block there is nothing to hold — put it before the blocks it guards",
        }
    ]


async def test_a_pipeline_holds_at_most_one_approval(client, headers):
    definition = _definition(
        _fetch(), _approve(), _approve(id="again", input="approve"), _hook(input="again")
    )
    assert await _refused(client, headers, definition) == [
        {
            "block_id": "again",
            "message": "a pipeline may have one approval block; 'approve' is already one",
        }
    ]


@pytest.mark.parametrize(
    ("hours", "message"),
    [
        (0, "config.deadline_hours: Input should be greater than or equal to 1"),
        ("24", "config.deadline_hours: Input should be a valid integer"),
        (1.5, "config.deadline_hours: Input should be a valid integer"),
        (169, "config.deadline_hours 169 is over the limit of 168 h"),
    ],
)
async def test_the_deadline_is_whole_hours_from_one_to_the_maximum(client, headers, hours, message):
    assert await _refused(client, headers, _guarded(deadline_hours=hours)) == [
        {"block_id": "approve", "message": message}
    ]


async def test_the_maximum_is_the_operators(client, headers, monkeypatch):
    monkeypatch.setattr(settings, "approval_max_deadline_hours", 12)
    assert await _refused(client, headers, _guarded()) == [
        {"block_id": "approve", "message": "config.deadline_hours 24 is over the limit of 12 h"}
    ]
    resp = await _save(client, headers, _guarded(deadline_hours=12))
    assert resp.status_code == 201, resp.text


async def test_a_deadline_longer_than_the_run_ceiling_saves(client, headers, monkeypatch):
    # The two parts of the rule are independent (§9d): 168 h against a 4 h ceiling.
    monkeypatch.setattr(settings, "pipeline_max_run_seconds", 4 * 3600)
    resp = await _save(client, headers, _guarded(deadline_hours=168))
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize("kind", ["webhook", "smtp"])
async def test_a_webhook_or_smtp_destination_is_recorded_and_held(client, headers, db_user, kind):
    destination = await _destination(db_user.id, kind)
    resp = await _save(client, headers, _guarded(destination_id=str(destination.id)))

    assert resp.status_code == 201, resp.text
    assert await _references(resp.json()["id"]) == {1: {destination.id}}
    held = await client.delete(f"/destinations/{destination.id}", headers=headers)
    assert held.status_code == 409, held.text
    assert held.json()["detail"]["pipelines"] == [{"id": resp.json()["id"], "name": "approval"}]


async def test_a_bucket_destination_is_refused_naming_the_block(client, headers, db_user):
    bucket = await _destination(db_user.id, "bucket", name="a-bucket")
    assert await _refused(client, headers, _guarded(destination_id=str(bucket.id))) == [
        {
            "block_id": "approve",
            "message": "destination 'a-bucket' is a bucket destination; this block needs a "
            "webhook or smtp destination",
        }
    ]


async def test_another_users_or_a_missing_destination_is_a_404(client, headers, other_user):
    theirs = await _destination(other_user.id, "webhook")
    for destination_id in (theirs.id, uuid.uuid4()):
        resp = await _save(client, headers, _guarded(destination_id=str(destination_id)))
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Destination not found"


# --- the trigger ----------------------------------------------------------------------------


async def _run_rows(user_id) -> list[PipelineRun]:
    async with AsyncSessionLocal() as db:
        return list(await db.scalars(select(PipelineRun).where(PipelineRun.user_id == user_id)))


async def test_a_run_carries_the_approval_block(client, headers, db_user, temporal):
    hook = await _destination(db_user.id, "smtp")
    pipeline = (
        await _save(client, headers, _guarded(deadline_hours=48, destination_id=str(hook.id)))
    ).json()

    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)

    assert resp.status_code == 202, resp.text
    (_, workflow_input), _ = temporal.start_workflow.await_args
    block = workflow_input.blocks[1]
    assert isinstance(block, ApprovalBlock)
    assert (block.config.deadline_hours, block.config.destination_id) == (48, hook.id)


async def test_the_trigger_checks_the_destination_under_the_users_lock(
    client, headers, db_user, temporal
):
    hook = await _destination(db_user.id, "webhook")
    pipeline = (await _save(client, headers, _guarded(destination_id=str(hook.id)))).json()

    async with AsyncSessionLocal() as holder:
        await lock_user(db_user.id, holder)
        task = asyncio.create_task(
            client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
        )
        await asyncio.sleep(1.5)
        assert not task.done(), "the trigger did not wait for the user's lock"
        await holder.delete(await holder.get(Destination, hook.id))
        await holder.commit()
    resp = await asyncio.wait_for(task, timeout=10)

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Destination not found"
    assert await _run_rows(db_user.id) == []
    temporal.start_workflow.assert_not_awaited()


async def test_the_trigger_refuses_a_destination_turned_bucket(client, headers, db_user, temporal):
    hook = await _destination(db_user.id, "webhook", name="was-a-hook")
    pipeline = (await _save(client, headers, _guarded(destination_id=str(hook.id)))).json()
    async with AsyncSessionLocal() as db:
        await db.execute(
            Destination.__table__.update().where(Destination.id == hook.id).values(kind="bucket")
        )
        await db.commit()

    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == [
        {
            "block_id": "approve",
            "message": "destination 'was-a-hook' is a bucket destination; this block needs a "
            "webhook or smtp destination",
        }
    ]
    assert await _run_rows(db_user.id) == []
