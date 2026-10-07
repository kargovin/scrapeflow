"""C.4 + C.5 (MVP) — POST/GET /pipelines, POST/GET /pipelines/{id}/runs."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from temporalio.common import WorkflowIDReusePolicy

from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.llm_keys import UserLLMKey
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.workflows.client import get_temporal
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

    async with AsyncSessionLocal() as db:
        pipeline = await db.get(Pipeline, uuid.UUID(body["id"]))
        assert pipeline.user_id == db_user.id
        versions = (
            await db.scalars(
                select(PipelineVersion).where(PipelineVersion.pipeline_id == pipeline.id)
            )
        ).all()
    assert [v.version for v in versions] == [1]
    assert versions[0].definition == body["definition"]


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


async def test_secret_fields_refused(client, headers, key):
    for field, value in (("proxy_url", "http://u:p@proxy:8080"), ("cookies", [{"name": "a"}])):
        definition = recipe(key.id)
        definition["blocks"][0]["config"][field] = value
        resp = await client.post(
            "/pipelines",
            json={"name": f"secret-{field}", "definition": definition},
            headers=headers,
        )
        assert resp.status_code == 422, field
        assert resp.json()["detail"] == [
            {"block_id": "fetch", "message": f"{field} is not supported yet"}
        ]


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


@pytest.fixture
def resolves_to():
    """Patch DNS for the SSRF check; yields a setter for the address every host resolves to."""
    with patch("app.core.security.socket.getaddrinfo") as gai:

        def set_ip(ip: str) -> None:
            gai.return_value = [(None, None, None, None, (ip, 0))]

        set_ip("93.184.216.34")
        yield set_ip


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
    assert workflow_input.definition == pipeline["definition"]
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


async def test_unsupported_block_type_refused(client, headers, key, db_user, temporal, resolves_to):
    pipeline = await _create(client, headers, recipe(key.id))
    resp = await client.post(f"/pipelines/{pipeline['id']}/runs", json={}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {"block_id": "notify", "message": "block type 'webhook' cannot run yet"}
    ]
    assert await _runs(db_user.id) == []
    temporal.start_workflow.assert_not_awaited()


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
