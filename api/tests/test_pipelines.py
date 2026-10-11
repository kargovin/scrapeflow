"""C.4 + C.5 — pipelines CRUD, versions, delete, admin reads, and the run trigger."""

import asyncio
import json
import socket
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, update
from temporalio.common import WorkflowIDReusePolicy

from app.core.db import AsyncSessionLocal
from app.core.pipeline_status import PIPELINE_STATUS_CHANNEL
from app.main import app
from app.models.llm_keys import UserLLMKey
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.user import User
from app.models.user_quota import UserQuota
from app.pipelines.catalog import LLM_EXTRACT, WEBHOOK
from app.pipelines.definition import parse_definition
from app.settings import settings
from app.workflows.client import get_temporal, get_temporal_or_none
from app.workflows.pipeline import PipelineRunInput, PipelineWorkflow
from app.workflows.queues import WORKFLOW_QUEUE

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}


def runnable(key_id, url="https://example.com"):
    return {
        "blocks": [
            {"id": "fetch", "type": "scrape", "config": {"url": url}},
            {
                "id": "extract",
                "type": "llm_extract",
                "input": "fetch",
                "config": {"llm_key_id": str(key_id), "model": "m", "output_schema": SCHEMA},
            },
        ]
    }


def recipe(key_id, url="https://example.com"):
    definition = runnable(key_id, url)
    definition["blocks"].append(
        {
            "id": "notify",
            "type": "webhook",
            "input": "extract",
            "config": {"url": "https://example.com/hook"},
        }
    )
    return definition


@pytest.fixture(autouse=True)
def resolves_to():
    """Patch DNS for every SSRF check (save and trigger); yields a setter for the address every host resolves to."""
    # The module's name, not socket.getaddrinfo itself: the DB driver resolves through that.
    with patch("app.core.security.socket") as fake_socket:
        fake_socket.gaierror = socket.gaierror

        def set_ip(ip: str) -> None:
            fake_socket.getaddrinfo.return_value = [(None, None, None, None, (ip, 0))]

        set_ip("93.184.216.34")
        yield set_ip


async def _llm_key(user_id):
    async with AsyncSessionLocal() as db:
        key = UserLLMKey(user_id=user_id, name="k", provider="anthropic", encrypted_api_key="x")
        db.add(key)
        await db.commit()
        await db.refresh(key)
    return key


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


@pytest_asyncio.fixture
async def key(db_user):
    return await _llm_key(db_user.id)


async def test_unauthenticated(client):
    assert (await client.post("/pipelines", json={})).status_code == 401
    assert (await client.get("/pipelines")).status_code == 401


async def test_create_stores_normalized_version_1(client, headers, key, db_user):
    resp = await client.post(
        "/pipelines", json={"name": "r6", "definition": recipe(key.id)}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "r6"
    assert body["version"] == 1
    fetch = body["definition"]["blocks"][0]
    assert fetch["config_schema_version"] == 1
    assert fetch["config"]["output_format"] == "html"
    # Secret fields never appear in a returned config; `secrets` names the ones set.
    assert "proxy_url" not in fetch["config"] and "cookies" not in fetch["config"]
    assert fetch["secrets"] == []

    async with AsyncSessionLocal() as db:
        pipeline = await db.get(Pipeline, uuid.UUID(body["id"]))
        assert pipeline.user_id == db_user.id
        versions = (
            await db.scalars(
                select(PipelineVersion).where(PipelineVersion.pipeline_id == pipeline.id)
            )
        ).all()
    assert [v.version for v in versions] == [1]
    stored = versions[0].definition["blocks"][0]
    assert (stored["config"]["proxy_url"], stored["secrets"]) == (None, {})


async def test_missing_block_id_is_assigned(client, headers):
    definition = {"blocks": [{"type": "scrape", "config": {"url": "https://example.com"}}]}
    resp = await client.post(
        "/pipelines", json={"name": "no-id", "definition": definition}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["definition"]["blocks"][0]["id"]


async def test_invalid_definition_names_the_block(client, headers, key):
    definition = recipe(key.id)
    definition["blocks"][1]["input"] = "nowhere"
    resp = await client.post(
        "/pipelines", json={"name": "bad", "definition": definition}, headers=headers
    )
    assert resp.status_code == 422
    assert any(p["block_id"] == "extract" for p in resp.json()["detail"])


def _decrypt(ciphertext):
    return Fernet(settings.credentials_encryption_key).decrypt(ciphertext.encode()).decode()


async def _stored(pipeline_id, version=None):
    async with AsyncSessionLocal() as db:
        stmt = select(PipelineVersion).where(PipelineVersion.pipeline_id == uuid.UUID(pipeline_id))
        if version is not None:
            stmt = stmt.where(PipelineVersion.version == version)
        rows = (await db.scalars(stmt.order_by(PipelineVersion.version))).all()
    return rows


async def test_secret_fields_are_stored_encrypted_and_never_returned(client, headers, key):
    definition = recipe(key.id)
    cookies = [{"name": "session", "value": "s3cret"}]
    definition["blocks"][0]["config"] |= {"proxy_url": "http://u:p@proxy:8080", "cookies": cookies}
    resp = await client.post(
        "/pipelines", json={"name": "secret", "definition": definition}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert "s3cret" not in resp.text and "u:p@proxy" not in resp.text
    assert resp.json()["definition"]["blocks"][0]["secrets"] == ["cookies", "proxy_url"]

    [version] = await _stored(resp.json()["id"])
    fetch = version.definition["blocks"][0]
    assert (fetch["config"]["proxy_url"], fetch["config"]["cookies"]) == (None, None)
    assert _decrypt(fetch["secrets"]["proxy_url"]) == "http://u:p@proxy:8080"
    assert json.loads(_decrypt(fetch["secrets"]["cookies"])) == cookies


async def test_other_users_llm_key_is_404_and_nothing_stored(client, headers, db_user, other_user):
    foreign = await _llm_key(other_user.id)
    resp = await client.post(
        "/pipelines", json={"name": "steal", "definition": recipe(foreign.id)}, headers=headers
    )
    assert resp.status_code == 404
    async with AsyncSessionLocal() as db:
        count = await db.scalar(
            select(func.count()).select_from(Pipeline).where(Pipeline.user_id == db_user.id)
        )
    assert count == 0


async def test_duplicate_name_is_409(client, headers, key):
    payload = {"name": "dup", "definition": recipe(key.id)}
    assert (await client.post("/pipelines", json=payload, headers=headers)).status_code == 201
    resp = await client.post("/pipelines", json=payload, headers=headers)
    assert resp.status_code == 409


async def test_get_own_and_list(client, headers, key):
    created = (
        await client.post(
            "/pipelines", json={"name": "mine", "definition": recipe(key.id)}, headers=headers
        )
    ).json()

    resp = await client.get(f"/pipelines/{created['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == created

    listed = (await client.get("/pipelines", headers=headers)).json()
    assert [p["id"] for p in listed] == [created["id"]]


async def test_other_users_pipeline_is_404_and_not_listed(client, headers, other_user):
    async with AsyncSessionLocal() as db:
        theirs = Pipeline(user_id=other_user.id, name="theirs")
        db.add(theirs)
        await db.flush()
        db.add(PipelineVersion(pipeline_id=theirs.id, version=1, definition={"blocks": []}))
        await db.commit()

    assert (await client.get(f"/pipelines/{theirs.id}", headers=headers)).status_code == 404
    assert (await client.get(f"/pipelines/{uuid.uuid4()}", headers=headers)).status_code == 404
    assert (await client.get("/pipelines", headers=headers)).json() == []


# ---------------------------------------------------------------------------
# C.5 — the run trigger
# ---------------------------------------------------------------------------


@pytest.fixture
def temporal():
    client = AsyncMock()
    app.dependency_overrides[get_temporal] = lambda: client
    yield client
    app.dependency_overrides.pop(get_temporal, None)


async def _create(client, headers, definition, name="p"):
    resp = await client.post(
        "/pipelines", json={"name": name, "definition": definition}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _runs(user_id):
    async with AsyncSessionLocal() as db:
        return (await db.scalars(select(PipelineRun).where(PipelineRun.user_id == user_id))).all()


async def test_run_commits_rows_then_starts_the_workflow(
    client, headers, key, db_user, temporal, resolves_to
):
    pipeline = await _create(client, headers, runnable(key.id))

    async def start_workflow(*args, **kwargs):
        # The rows must already be committed when the workflow starts.
        async with AsyncSessionLocal() as db:
            assert await db.get(PipelineRun, uuid.UUID(args[1].run_id)) is not None

    temporal.start_workflow.side_effect = start_workflow
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 202, resp.text
    run = resp.json()
    assert run["status"] == "running"
    assert [(b["block_id"], b["position"], b["status"]) for b in run["blocks"]] == [
        ("fetch", 0, "pending"),
        ("extract", 1, "pending"),
    ]

    temporal.start_workflow.assert_awaited_once()
    (workflow_fn, workflow_input), kwargs = temporal.start_workflow.await_args
    assert workflow_fn == PipelineWorkflow.run
    assert kwargs["id"] == f"pipeline-run-{run['id']}"
    assert kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert kwargs["task_queue"] == WORKFLOW_QUEUE

    assert isinstance(workflow_input, PipelineRunInput)
    assert workflow_input.run_id == run["id"]
    assert [(b.id, b.type) for b in workflow_input.blocks] == [
        ("fetch", "scrape"),
        ("extract", "llm_extract"),
    ]
    assert (
        workflow_input.time_budget_seconds
        == parse_definition((await _stored(pipeline["id"]))[0].definition)[0].run_budget_seconds
    )
    assert workflow_input.credentials == {}
    assert workflow_input.llm_keys[str(key.id)].encrypted_api_key == key.encrypted_api_key
    async with AsyncSessionLocal() as db:
        rows = (
            await db.scalars(
                select(PipelineRunBlock).where(
                    PipelineRunBlock.pipeline_run_id == uuid.UUID(run["id"])
                )
            )
        ).all()
    assert workflow_input.artifact_ids == {r.block_id: str(r.id) for r in rows}


async def test_start_failure_marks_the_run_failed(
    client, headers, key, db_user, temporal, resolves_to
):
    pipeline = await _create(client, headers, runnable(key.id))
    temporal.start_workflow.side_effect = RuntimeError("engine down")
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 503
    [run] = await _runs(db_user.id)
    assert run.status == "failed"
    assert run.error == "could not start workflow: engine down"
    assert run.finished_at is not None
    async with AsyncSessionLocal() as db:
        statuses = (
            await db.scalars(
                select(PipelineRunBlock.status).where(PipelineRunBlock.pipeline_run_id == run.id)
            )
        ).all()
    assert statuses == ["skipped", "skipped"]


async def test_the_r6_recipe_runs_with_its_webhook_secret_and_pipeline_id(
    client, headers, key, db_user, temporal, resolves_to
):
    definition = recipe(key.id)
    definition["blocks"][2]["config"]["secret"] = "whsec"
    pipeline = await _create(client, headers, definition)
    assert pipeline["definition"]["blocks"][2]["secrets"] == ["secret"]
    [version] = await _stored(pipeline["id"])
    stored = version.definition["blocks"][2]
    assert stored["config"]["secret"] is None
    assert _decrypt(stored["secrets"]["secret"]) == "whsec"

    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 202, resp.text
    workflow_input = temporal.start_workflow.await_args.args[1]
    assert workflow_input.pipeline_id == pipeline["id"]
    assert workflow_input.webhook_secrets == {"notify": stored["secrets"]["secret"]}
    assert workflow_input.blocks[2].config.secret is None


async def test_a_webhook_url_now_private_is_refused_at_the_trigger(
    client, headers, key, db_user, temporal, resolves_to
):
    definition = recipe(key.id)
    definition["blocks"][2]["config"]["url"] = "https://hooks.example.net/in"
    pipeline = await _create(client, headers, definition)

    def resolve(host, *_):
        ip = "10.0.0.5" if host == "hooks.example.net" else "93.184.216.34"
        return [(None, None, None, None, (ip, 0))]

    with patch("app.core.security.socket.getaddrinfo", side_effect=resolve):
        resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 400
    assert "10.0.0.5" in resp.json()["detail"]
    assert await _runs(db_user.id) == []
    temporal.start_workflow.assert_not_awaited()


async def test_clean_and_validate_blocks_run(client, headers, db_user, temporal, resolves_to):
    definition = {
        "blocks": [
            {"id": "fetch", "type": "scrape", "config": {"url": "https://example.com"}},
            {"id": "clean", "type": "clean", "input": "fetch"},
            {
                "id": "guard",
                "type": "validate",
                "input": "clean",
                "config": {"rules": [{"kind": "min_length", "chars": 100}]},
            },
        ]
    }
    pipeline = await _create(client, headers, definition)
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 202, resp.text
    workflow_input = temporal.start_workflow.await_args.args[1]
    assert [b.type for b in workflow_input.blocks] == ["scrape", "clean", "validate"]


async def test_run_inputs_must_match_and_bound_url_is_ssrf_checked(
    client, headers, db_user, temporal, resolves_to
):
    definition = {
        "inputs": {"url": {"type": "url"}},
        "blocks": [{"id": "fetch", "type": "scrape", "config": {"url": {"$input": "url"}}}],
    }
    pipeline = await _create(client, headers, definition)
    runs_url = f"/pipelines/{pipeline['id']}/runs"

    resp = await client.post(runs_url, json={}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == [{"block_id": None, "message": "missing run input 'url'"}]

    body = {"inputs": {"url": "https://example.com", "extra": "x"}}
    resp = await client.post(runs_url, json=body, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == [{"block_id": None, "message": "unexpected run input 'extra'"}]

    resolves_to("10.0.0.1")
    resp = await client.post(
        runs_url, json={"inputs": {"url": "http://internal/"}}, headers=headers
    )
    assert resp.status_code == 400
    assert await _runs(db_user.id) == []

    resolves_to("93.184.216.34")
    resp = await client.post(
        runs_url, json={"inputs": {"url": "https://example.com"}}, headers=headers
    )
    assert resp.status_code == 202
    assert temporal.start_workflow.await_args.args[1].inputs == {"url": "https://example.com"}


async def test_literal_private_url_refused(client, headers, key, db_user, temporal, resolves_to):
    pipeline = await _create(client, headers, runnable(key.id, url="http://redis/"))
    resolves_to("10.0.0.1")
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 400
    assert await _runs(db_user.id) == []
    temporal.start_workflow.assert_not_awaited()


async def test_deleted_llm_key_refused(client, headers, key, db_user, temporal, resolves_to):
    pipeline = await _create(client, headers, runnable(key.id))
    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserLLMKey).where(UserLLMKey.id == key.id))
        await db.commit()
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == [{"block_id": "extract", "message": "LLM key no longer exists"}]
    assert await _runs(db_user.id) == []


async def test_run_and_get_run_are_tenant_scoped(
    client, headers, key, other_user, temporal, resolves_to
):
    pipeline = await _create(client, headers, runnable(key.id))
    run = (await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)).json()

    resp = await client.get(f"/pipelines/{pipeline['id']}/runs/{run['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == run

    async with AsyncSessionLocal() as db:
        theirs = Pipeline(user_id=other_user.id, name="theirs")
        db.add(theirs)
        await db.flush()
        db.add(PipelineVersion(pipeline_id=theirs.id, version=1, definition=runnable(key.id)))
        await db.commit()
    resp = await client.post(f"/pipelines/{theirs.id}/runs", json={}, headers=headers)
    assert resp.status_code == 404
    resp = await client.get(f"/pipelines/{theirs.id}/runs/{run['id']}", headers=headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# C.4 — update, versions, delete, limits, admin reads
# ---------------------------------------------------------------------------


async def test_patch_saves_a_new_version_and_carries_block_ids(client, headers, key):
    pipeline = await _create(client, headers, runnable(key.id))
    definition = pipeline["definition"]
    definition["blocks"][0]["config"]["output_format"] = "markdown"
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": definition}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert [b["id"] for b in resp.json()["definition"]["blocks"]] == ["fetch", "extract"]

    # A block sent without an id is a new step; one left out is deleted.
    v3 = {"blocks": [{"type": "scrape", "config": {"url": "https://example.com"}}]}
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": v3}, headers=headers
    )
    [block] = resp.json()["definition"]["blocks"]
    assert block["id"] not in ("fetch", "extract")
    assert [v.version for v in await _stored(pipeline["id"])] == [1, 2, 3]

    versions = (await client.get(f"/pipelines/{pipeline['id']}/versions", headers=headers)).json()
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert versions[1]["definition"]["blocks"][0]["config"]["output_format"] == "markdown"


async def test_patch_with_an_unchanged_definition_saves_no_version(client, headers, key):
    pipeline = await _create(client, headers, runnable(key.id))
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}",
        json={"definition": pipeline["definition"]},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1
    assert len(await _stored(pipeline["id"])) == 1


async def test_patch_keeps_secrets_a_kept_block_does_not_send(client, headers, key):
    definition = runnable(key.id)
    definition["blocks"][0]["config"] |= {"proxy_url": "http://p:8080", "cookies": [{"n": 1}]}
    pipeline = await _create(client, headers, definition)
    [v1] = await _stored(pipeline["id"])

    # GET's definition round-trips: the secrets stay, and nothing changed.
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": pipeline["definition"]}, headers=headers
    )
    assert resp.json()["version"] == 1

    # null clears one; the other is kept as stored.
    edited = pipeline["definition"]
    edited["blocks"][0]["config"]["proxy_url"] = None
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": edited}, headers=headers
    )
    assert resp.json()["version"] == 2
    assert resp.json()["definition"]["blocks"][0]["secrets"] == ["cookies"]
    [v2] = await _stored(pipeline["id"], version=2)
    secrets = v2.definition["blocks"][0]["secrets"]
    assert secrets == {"cookies": v1.definition["blocks"][0]["secrets"]["cookies"]}


async def test_patch_rename_validation_and_conflicts(client, headers, key):
    a = await _create(client, headers, runnable(key.id), name="a")
    await _create(client, headers, runnable(key.id), name="b")
    url = f"/pipelines/{a['id']}"

    assert (await client.patch(url, json={}, headers=headers)).status_code == 422
    assert (await client.patch(url, json={"name": "b"}, headers=headers)).status_code == 409

    bad = runnable(key.id)
    bad["blocks"][1]["input"] = "nowhere"
    resp = await client.patch(url, json={"definition": bad}, headers=headers)
    assert resp.status_code == 422
    assert len(await _stored(a["id"])) == 1

    resp = await client.patch(url, json={"name": "renamed"}, headers=headers)
    assert resp.status_code == 200
    assert (resp.json()["name"], resp.json()["version"]) == ("renamed", 1)
    assert resp.json()["updated_at"] > a["updated_at"]


async def test_delete_without_runs_removes_the_pipeline(client, headers, key):
    pipeline = await _create(client, headers, runnable(key.id))
    resp = await client.delete(f"/pipelines/{pipeline['id']}", headers=headers)
    assert resp.status_code == 204
    async with AsyncSessionLocal() as db:
        assert await db.get(Pipeline, uuid.UUID(pipeline["id"])) is None
    assert await _stored(pipeline["id"]) == []
    # The name is free again.
    await _create(client, headers, runnable(key.id))


async def test_delete_with_runs_soft_deletes_and_holds_the_name(client, headers, key, temporal):
    pipeline = await _create(client, headers, runnable(key.id))
    run = (await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)).json()

    resp = await client.delete(f"/pipelines/{pipeline['id']}", headers=headers)
    assert resp.status_code == 204
    async with AsyncSessionLocal() as db:
        row = await db.get(Pipeline, uuid.UUID(pipeline["id"]))
    assert row.deleted_at is not None

    assert (await client.get(f"/pipelines/{pipeline['id']}", headers=headers)).status_code == 404
    assert (await client.get("/pipelines", headers=headers)).json() == []
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 404
    # The run and its rows stay readable.
    resp = await client.get(f"/pipelines/{pipeline['id']}/runs/{run['id']}", headers=headers)
    assert resp.status_code == 200
    resp = await client.post(
        "/pipelines", json={"name": "p", "definition": runnable(key.id)}, headers=headers
    )
    assert resp.status_code == 409


async def test_delete_falls_back_to_soft_when_a_run_lands_first(client, headers, key, db_user):
    pipeline = await _create(client, headers, runnable(key.id))
    # The check sees no runs; the run is inserted before the DELETE reaches the row.
    with patch("app.routers.pipelines.exists") as no_runs:
        no_runs.return_value.where.return_value = select(False).scalar_subquery()
        async with AsyncSessionLocal() as db:
            [version] = await _stored(pipeline["id"])
            run_id = uuid.uuid4()
            db.add(
                PipelineRun(
                    id=run_id,
                    pipeline_id=version.pipeline_id,
                    pipeline_version_id=version.id,
                    user_id=db_user.id,
                    status="completed",
                    workflow_id=f"pipeline-run-{run_id}",
                )
            )
            await db.commit()
        resp = await client.delete(f"/pipelines/{pipeline['id']}", headers=headers)
    assert resp.status_code == 204
    async with AsyncSessionLocal() as db:
        assert (await db.get(Pipeline, uuid.UUID(pipeline["id"]))).deleted_at is not None


async def test_other_users_pipeline_cannot_be_changed(client, headers, key, other_user):
    async with AsyncSessionLocal() as db:
        theirs = Pipeline(user_id=other_user.id, name="theirs")
        db.add(theirs)
        await db.flush()
        db.add(PipelineVersion(pipeline_id=theirs.id, version=1, definition=runnable(key.id)))
        await db.commit()
    url = f"/pipelines/{theirs.id}"
    assert (await client.patch(url, json={"name": "x"}, headers=headers)).status_code == 404
    assert (await client.delete(url, headers=headers)).status_code == 404
    assert (await client.get(f"{url}/versions", headers=headers)).status_code == 404


async def test_pipeline_count_is_limited_per_user(client, headers, key, monkeypatch):
    monkeypatch.setattr(settings, "max_pipelines_per_user", 1)
    first = await _create(client, headers, runnable(key.id), name="one")
    resp = await client.post(
        "/pipelines", json={"name": "two", "definition": runnable(key.id)}, headers=headers
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "pipelines"
    # A deleted pipeline frees its place.
    await client.delete(f"/pipelines/{first['id']}", headers=headers)
    await _create(client, headers, runnable(key.id), name="two")


async def test_literal_private_url_refused_at_save(client, headers, key, resolves_to):
    resolves_to("10.0.0.1")
    resp = await client.post(
        "/pipelines",
        json={"name": "internal", "definition": runnable(key.id, url="http://redis/")},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_admin_reads_every_users_pipelines(client, headers, key, db_user, monkeypatch):
    pipeline = await _create(client, headers, runnable(key.id))
    assert (await client.get("/admin/pipelines", headers=headers)).status_code == 403

    async with AsyncSessionLocal() as db:
        user = await db.get(User, db_user.id)
        user.is_admin = True
        await db.commit()
    listed = (await client.get(f"/admin/pipelines?user_id={db_user.id}", headers=headers)).json()
    assert [(p["id"], p["user_email"], p["deleted_at"]) for p in listed] == [
        (pipeline["id"], db_user.email, None)
    ]
    resp = await client.get(f"/admin/pipelines/{pipeline['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["definition"] == pipeline["definition"]

    async with AsyncSessionLocal() as db:
        row = await db.get(Pipeline, uuid.UUID(pipeline["id"]))
        row.deleted_at = row.updated_at
        await db.commit()
    url = f"/admin/pipelines?user_id={db_user.id}"
    assert (await client.get(url, headers=headers)).json() == []
    assert len((await client.get(f"{url}&include_deleted=true", headers=headers)).json()) == 1


# ---------------------------------------------------------------------------
# C.5 — versions, secrets, admission, unreadable definitions
# ---------------------------------------------------------------------------


async def test_a_chosen_version_runs_pinned(client, headers, key, temporal):
    pipeline = await _create(client, headers, runnable(key.id))
    edited = pipeline["definition"]
    edited["blocks"][0]["config"]["output_format"] = "markdown"
    await client.patch(f"/pipelines/{pipeline['id']}", json={"definition": edited}, headers=headers)
    v1, v2 = await _stored(pipeline["id"])
    runs_url = f"/pipelines/{pipeline['id']}/runs"

    for body, expected, fmt in (({"version": 1}, v1, "html"), ({}, v2, "markdown")):
        resp = await client.post(runs_url, json=body, headers=headers)
        assert resp.status_code == 202, resp.text
        assert resp.json()["pipeline_version_id"] == str(expected.id)
        workflow_input = temporal.start_workflow.await_args.args[1]
        assert workflow_input.blocks[0].config.output_format.value == fmt

    resp = await client.post(runs_url, json={"version": 9}, headers=headers)
    assert resp.status_code == 404


async def test_stored_secrets_reach_the_workflow_as_credentials(
    client, headers, key, temporal, monkeypatch
):
    definition = runnable(key.id)
    definition["blocks"][0]["config"]["cookies"] = [{"n": 1}]
    pipeline = await _create(client, headers, definition)
    [version] = await _stored(pipeline["id"])
    stored = version.definition["blocks"][0]["secrets"]

    await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    creds = temporal.start_workflow.await_args.args[1].credentials["fetch"]
    assert (creds.encrypted_cookies, creds.encrypted_proxy_url) == (stored["cookies"], None)

    # As jobs do: no block proxy → the platform default, encrypted for the worker.
    monkeypatch.setattr(settings, "default_proxy_url", "http://platform:3128")
    await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    creds = temporal.start_workflow.await_args.args[1].credentials["fetch"]
    assert _decrypt(creds.encrypted_proxy_url) == "http://platform:3128"


async def test_storage_near_the_wall_refuses_to_start(client, headers, key, db_user, temporal):
    pipeline = await _create(client, headers, runnable(key.id))
    async with AsyncSessionLocal() as db:
        db.add(
            UserQuota(
                user_id=db_user.id,
                storage_bytes_used=1000 - settings.storage_headroom_bytes // 2,
                storage_bytes_limit=1000 + settings.storage_headroom_bytes // 2,
            )
        )
        await db.commit()
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "storage_bytes"
    assert await _runs(db_user.id) == []
    temporal.start_workflow.assert_not_awaited()


async def test_an_unreadable_stored_definition_creates_no_run(
    client, headers, key, db_user, temporal
):
    pipeline = await _create(client, headers, runnable(key.id))
    async with AsyncSessionLocal() as db:
        [version] = (
            await db.scalars(
                select(PipelineVersion).where(
                    PipelineVersion.pipeline_id == uuid.UUID(pipeline["id"])
                )
            )
        ).all()
        definition = json.loads(json.dumps(version.definition))
        del definition["blocks"][0]["config"]["url"]
        version.definition = definition
        await db.commit()

    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 422
    [problem] = resp.json()["detail"]
    assert problem["block_id"] == "fetch"
    assert problem["message"].startswith("this version cannot be run: config: url")
    assert await _runs(db_user.id) == []
    temporal.start_workflow.assert_not_awaited()


# ---------------------------------------------------------------------------
# C.10 — cancel
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """The client the cancel route sees; ``None`` stands for the engine being unreachable."""
    handle = MagicMock()
    handle.cancel = AsyncMock()
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    holder = {"client": client}
    app.dependency_overrides[get_temporal_or_none] = lambda: holder["client"]
    yield holder
    app.dependency_overrides.pop(get_temporal_or_none, None)


async def _started_run(client, headers, key, temporal):
    """A run of the R6 recipe whose fetch is done and whose LLM block has run for 100 s."""
    pipeline = await _create(client, headers, recipe(key.id))
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 202, resp.text
    run = resp.json()
    async with AsyncSessionLocal() as db:
        rows = PipelineRunBlock.pipeline_run_id == uuid.UUID(run["id"])
        await db.execute(
            update(PipelineRunBlock)
            .where(rows, PipelineRunBlock.block_id == "fetch")
            .values(status="completed")
        )
        await db.execute(
            update(PipelineRunBlock)
            .where(rows, PipelineRunBlock.block_id == "extract")
            .values(status="running", started_at=datetime.now(UTC) - timedelta(seconds=100))
        )
        await db.commit()
    return f"/pipelines/{pipeline['id']}/runs/{run['id']}", run["id"]


async def test_cancel_is_requested_not_reported_done(client, headers, key, temporal, engine):
    url, run_id = await _started_run(client, headers, key, temporal)

    resp = await client.post(f"{url}/cancel", headers=headers)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    # R3: never report cancelled before it stopped.
    assert body["run"]["status"] == "running"
    assert body["still_running"] == ["extract"]
    budget = LLM_EXTRACT.timing(None).budget
    assert budget - 110 <= body["upper_bound_seconds"] <= budget - 100
    assert (body["llm_billed"], body["workflow_notified"]) == (True, True)
    engine["client"].get_workflow_handle.assert_called_once_with(f"pipeline-run-{run_id}")
    engine["client"].get_workflow_handle.return_value.cancel.assert_awaited_once()
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, uuid.UUID(run_id))
    assert (run.status, run.cancel_requested_at is not None) == ("running", True)


async def test_cancel_with_the_engine_down_still_lands_on_the_row(
    client, headers, key, temporal, engine
):
    url, run_id = await _started_run(client, headers, key, temporal)
    engine["client"] = None

    resp = await client.post(f"{url}/cancel", headers=headers)

    assert resp.status_code == 202
    assert resp.json()["workflow_notified"] is False
    async with AsyncSessionLocal() as db:
        assert (await db.get(PipelineRun, uuid.UUID(run_id))).cancel_requested_at is not None


async def test_a_waiting_webhook_bound_is_one_attempt_once_the_engine_is_told(
    client, headers, key, temporal, engine
):
    url, run_id = await _started_run(client, headers, key, temporal)
    async with AsyncSessionLocal() as db:
        rows = PipelineRunBlock.pipeline_run_id == uuid.UUID(run_id)
        await db.execute(
            update(PipelineRunBlock)
            .where(rows, PipelineRunBlock.block_id == "extract")
            .values(status="completed")
        )
        await db.execute(
            update(PipelineRunBlock)
            .where(rows, PipelineRunBlock.block_id == "notify")
            .values(status="waiting", started_at=datetime.now(UTC))
        )
        await db.commit()

    told = (await client.post(f"{url}/cancel", headers=headers)).json()
    engine["client"] = None
    untold = (await client.post(f"{url}/cancel", headers=headers)).json()

    assert told["still_running"] == ["notify"]
    assert told["upper_bound_seconds"] == WEBHOOK.timing(None).start_to_close
    # Unreachable engine: the fallback waits out the backoff, up to the whole horizon.
    assert untold["upper_bound_seconds"] > 2.6 * 3600


async def test_a_failed_workflow_cancel_is_reported_not_raised(
    client, headers, key, temporal, engine
):
    url, _ = await _started_run(client, headers, key, temporal)
    engine["client"].get_workflow_handle.return_value.cancel.side_effect = RuntimeError("gone")

    resp = await client.post(f"{url}/cancel", headers=headers)

    assert resp.status_code == 202
    assert resp.json()["workflow_notified"] is False


async def test_a_second_cancel_keeps_the_first_request_time(client, headers, key, temporal, engine):
    url, run_id = await _started_run(client, headers, key, temporal)
    await client.post(f"{url}/cancel", headers=headers)
    async with AsyncSessionLocal() as db:
        first = (await db.get(PipelineRun, uuid.UUID(run_id))).cancel_requested_at

    resp = await client.post(f"{url}/cancel", headers=headers)

    assert resp.status_code == 202
    async with AsyncSessionLocal() as db:
        assert (await db.get(PipelineRun, uuid.UUID(run_id))).cancel_requested_at == first


async def test_a_finished_run_cannot_be_cancelled(client, headers, key, temporal, engine):
    url, run_id = await _started_run(client, headers, key, temporal)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == uuid.UUID(run_id))
            .values(status="completed")
        )
        await db.commit()

    resp = await client.post(f"{url}/cancel", headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "The run is already completed"
    engine["client"].get_workflow_handle.assert_not_called()


async def test_another_users_run_cannot_be_cancelled(
    client, headers, key, temporal, engine, other_user
):
    url, run_id = await _started_run(client, headers, key, temporal)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == uuid.UUID(run_id))
            .values(user_id=other_user.id)
        )
        await db.commit()

    resp = await client.post(f"{url}/cancel", headers=headers)

    assert resp.status_code == 404
    engine["client"].get_workflow_handle.assert_not_called()


async def test_cancel_notifies_with_the_request_flag(client, headers, key, temporal, engine):
    url, run_id = await _started_run(client, headers, key, temporal)
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(PIPELINE_STATUS_CHANNEL, lambda *a: received.put_nowait(a[3]))
    try:
        await client.post(f"{url}/cancel", headers=headers)
        payload = json.loads(await asyncio.wait_for(received.get(), 5))
    finally:
        await conn.close()
    assert (payload["run_id"], payload["status"], payload["cancel_requested"]) == (
        run_id,
        "running",
        True,
    )
