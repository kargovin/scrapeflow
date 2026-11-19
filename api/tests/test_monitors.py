"""L.4 — the monitors API: create with ADR-016 §2d's checks, read, edit, pause, resume, Schedules,
and the scheduled cap shared with jobs."""

import socket
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from temporalio.client import ScheduleOverlapPolicy

from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.job import Job
from app.models.llm_keys import UserLLMKey
from app.models.monitor import Monitor, MonitorEvent
from app.models.pipeline import Pipeline
from app.models.user_quota import UserQuota
from app.settings import settings

SCHEMA = {"type": "object", "properties": {"price": {"type": "number"}}}
CRON = "*/10 * * * *"


@pytest.fixture(autouse=True)
def resolves_to():
    with patch("app.core.security.socket") as fake_socket:
        fake_socket.gaierror = socket.gaierror

        def set_ip(ip: str) -> None:
            fake_socket.getaddrinfo.return_value = [(None, None, None, None, (ip, 0))]

        set_ip("93.184.216.34")
        yield set_ip


@pytest.fixture
def temporal():
    client = MagicMock()
    client.start_workflow = AsyncMock()
    client.create_schedule = AsyncMock()
    handle = MagicMock()
    handle.update = AsyncMock()
    handle.delete = AsyncMock()
    client.get_schedule_handle = MagicMock(return_value=handle)
    workflow_handle = MagicMock()
    workflow_handle.cancel = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=workflow_handle)
    app.state.temporal = client
    yield client
    app.state.temporal = None


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


async def _llm_key(user_id):
    async with AsyncSessionLocal() as db:
        key = UserLLMKey(user_id=user_id, name="k", provider="anthropic", encrypted_api_key="x")
        db.add(key)
        await db.commit()
        await db.refresh(key)
    return key


@pytest_asyncio.fixture
async def key(db_user):
    return await _llm_key(db_user.id)


def gated(key_id, *, gate=True, carried_rule=False, failure_route=True):
    """scrape(page) -> clean -> gate -> extract -> [same price?] -> notify."""
    blocks = [
        {"id": "fetch", "type": "scrape", "config": {"url": {"$input": "page"}}},
        {"id": "clean", "type": "clean", "input": "fetch"},
    ]
    if gate:
        blocks.append(
            {
                "id": "gate",
                "type": "validate",
                "input": "clean",
                "config": {"rules": [{"kind": "changed"}], "when_unmet": "stop"},
            }
        )
    blocks.append(
        {
            "id": "extract",
            "type": "llm_extract",
            "input": blocks[-1]["id"],
            "config": {"llm_key_id": str(key_id), "model": "m", "output_schema": SCHEMA},
        }
    )
    inputs = {"page": {"type": "url"}}
    if carried_rule:
        inputs["last_price"] = {"type": "number"}
        blocks.append(
            {
                "id": "moved",
                "type": "validate",
                "input": "extract",
                "config": {
                    "rules": [
                        {
                            "kind": "compare",
                            "path": "/price",
                            "op": "ne",
                            "value": {"$input": "last_price"},
                        }
                    ],
                    "when_unmet": "stop",
                },
            }
        )
    events = ["pipeline.result", "pipeline.failed"] if failure_route else ["pipeline.result"]
    blocks.append(
        {
            "id": "notify",
            "type": "webhook",
            "input": blocks[-1]["id"],
            "config": {"url": "https://example.com/hook", "events": events},
        }
    )
    return {"inputs": inputs, "blocks": blocks}


async def _pipeline(client, headers, definition, name="watched"):
    resp = await client.post(
        "/pipelines", json={"name": name, "definition": definition}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _body(pipeline, name="prices", **fields):
    return {
        "name": name,
        "pipeline_id": pipeline["id"],
        "inputs": {"page": "https://shop.example.com/item"},
        "schedule_cron": CRON,
        **fields,
    }


async def _monitor(client, headers, body):
    resp = await client.post("/monitors", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _row(monitor_id) -> Monitor:
    async with AsyncSessionLocal() as db:
        return await db.get(Monitor, uuid.UUID(monitor_id))


def _detail(resp) -> list[str]:
    return [p["message"] for p in resp.json()["detail"]]


# --- create ------------------------------------------------------------------------------


async def test_create_pins_the_latest_version_and_arms_its_schedule(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    body = await _monitor(client, headers, _body(pipeline))

    assert (body["state"], body["version"], body["newer_version"]) == ("active", 1, None)
    assert body["schedule_cron"] == CRON and body["last_check"] is None
    assert body["next_check_at"] is not None
    assert body["warnings"] == []
    [call] = temporal.create_schedule.call_args_list
    schedule_id, schedule = call.args
    assert schedule_id == f"monitor-schedule-{body['id']}"
    assert schedule.spec.cron_expressions == [CRON]
    assert schedule.state.paused is False
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.policy.catchup_window == timedelta(days=365)
    assert schedule.policy.pause_on_failure is False
    assert schedule.action.workflow == "MonitorCheckWorkflow"
    assert schedule.action.id == f"monitor-check-{body['id']}"
    assert schedule.action.args[0].monitor_id == body["id"]


async def test_a_chosen_version_is_pinned_and_a_newer_one_is_shown(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    newer = gated(key.id)
    newer["blocks"][0]["config"]["respect_robots"] = True
    resp = await client.patch(
        f"/pipelines/{pipeline['id']}", json={"definition": newer}, headers=headers
    )
    assert resp.json()["version"] == 2

    body = await _monitor(client, headers, _body(pipeline, version=1))

    assert (body["version"], body["newer_version"]) == (1, 2)
    assert (await client.get(f"/monitors/{body['id']}", headers=headers)).json()["version"] == 1


async def test_a_schedule_that_cannot_be_armed_leaves_the_monitor_paused(
    client, headers, key, temporal
):
    temporal.create_schedule.side_effect = RuntimeError("temporal down")
    pipeline = await _pipeline(client, headers, gated(key.id))

    resp = await client.post("/monitors", json=_body(pipeline), headers=headers)

    assert resp.status_code == 503
    monitor = await _row(resp.json()["detail"]["monitor_id"])
    assert monitor.state == "paused"


async def test_warnings_name_what_the_save_allowed(client, headers, key, temporal):
    definition = {
        "inputs": {"page": {"type": "url"}},
        "blocks": [
            {"id": "fetch", "type": "scrape", "config": {"url": {"$input": "page"}}},
            {
                "id": "notify",
                "type": "webhook",
                "input": "fetch",
                "config": {"url": "https://example.com/h"},
            },
        ],
    }
    pipeline = await _pipeline(client, headers, definition)

    body = await _monitor(client, headers, _body(pipeline))

    assert [w.split(":")[0] for w in body["warnings"]] == [
        "no LLM block and no change gate",
        "nothing is sent when a check fails",
    ]


# --- §2d's refusals, in order ------------------------------------------------------------


async def test_an_unknown_deleted_or_foreign_pipeline_is_404(
    client, headers, key, temporal, other_user
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    for body in (
        _body({"id": str(uuid.uuid4())}),
        _body(pipeline, version=7),
    ):
        assert (await client.post("/monitors", json=body, headers=headers)).status_code == 404
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Pipeline)
            .where(Pipeline.id == uuid.UUID(pipeline["id"]))
            .values(deleted_at=datetime.now(UTC))
        )
        theirs = Pipeline(user_id=other_user.id, name="theirs")
        db.add(theirs)
        await db.commit()
    assert (
        await client.post("/monitors", json=_body(pipeline), headers=headers)
    ).status_code == 404
    resp = await client.post("/monitors", json=_body({"id": str(theirs.id)}), headers=headers)
    assert resp.status_code == 404
    temporal.create_schedule.assert_not_called()


async def test_the_pinned_versions_llm_key_must_still_exist(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserLLMKey).where(UserLLMKey.id == key.id))
        await db.commit()

    resp = await client.post("/monitors", json=_body(pipeline), headers=headers)

    assert resp.status_code == 422
    assert resp.json()["detail"] == [{"block_id": "extract", "message": "LLM key no longer exists"}]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"inputs": {}}, "run input 'page' needs a fixed value or to be carried"),
        (
            {"inputs": {"page": "https://shop.example.com/i", "extra": 1}},
            "unknown run input 'extra'",
        ),
        (
            {
                "carried": [
                    {
                        "input": "page",
                        "from_block": "extract",
                        "path": "/url",
                        "start": "https://a.example",
                    }
                ]
            },
            "run input 'page' is both fixed and carried",
        ),
        ({"inputs": {"page": 5}}, "run input 'page' must be a URL; got 5"),
    ],
)
async def test_every_input_is_fixed_or_carried_once_and_of_its_type(
    client, headers, key, temporal, fields, message
):
    pipeline = await _pipeline(client, headers, gated(key.id))

    resp = await client.post("/monitors", json=_body(pipeline, **fields), headers=headers)

    assert resp.status_code == 422, resp.text
    assert message in _detail(resp)


def _carry(**entry):
    return {"input": "last_price", "from_block": "extract", "path": "/price", "start": 10, **entry}


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (_carry(start="10"), "run input 'last_price' must be a number; got \"10\""),
        (_carry(start=True), None),
        (
            _carry(from_block="nowhere"),
            "carried 'last_price': from_block 'nowhere' is not in this version",
        ),
        (
            _carry(from_block="clean"),
            "carried 'last_price' reads this block, which produces page:markdown; a carried value "
            "is read from an extraction or a JSON page",
        ),
        (_carry(path="price"), None),
    ],
)
async def test_a_carried_input_names_a_json_output_and_a_start_of_its_type(
    client, headers, key, temporal, entry, message
):
    pipeline = await _pipeline(client, headers, gated(key.id, carried_rule=True))

    resp = await client.post("/monitors", json=_body(pipeline, carried=[entry]), headers=headers)

    assert resp.status_code == 422, resp.text
    if message is not None:
        assert message in _detail(resp)


async def test_a_carried_input_is_carried_once_and_only_number_or_text(
    client, headers, key, temporal
):
    pipeline = await _pipeline(client, headers, gated(key.id, carried_rule=True))
    twice = _body(pipeline, carried=[_carry(), _carry()])
    url = _body(
        pipeline,
        inputs={"last_price": 3},
        carried=[_carry(input="page", start="https://a.example")],
    )

    assert "run input 'last_price' is carried more than once" in _detail(
        await client.post("/monitors", json=twice, headers=headers)
    )
    assert "run input 'page' is a url; only number and text are carried" in _detail(
        await client.post("/monitors", json=url, headers=headers)
    )


async def test_a_carried_input_saves_with_its_start(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id, carried_rule=True))

    body = await _monitor(client, headers, _body(pipeline, carried=[_carry()]))

    assert body["carried"] == [_carry()]
    assert body["inputs"] == {"page": "https://shop.example.com/item"}


async def test_a_fixed_url_that_resolves_private_is_refused(
    client, headers, key, temporal, resolves_to
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    resolves_to("10.0.0.5")

    resp = await client.post("/monitors", json=_body(pipeline), headers=headers)

    assert resp.status_code == 400
    assert "10.0.0.5" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("cron", "message"),
    [("not a cron", "Invalid cron expression"), ("* * * * *", "at least 5 minutes")],
)
async def test_the_schedule_is_cron_and_no_more_often_than_five_minutes(
    client, headers, key, temporal, cron, message
):
    pipeline = await _pipeline(client, headers, gated(key.id))

    resp = await client.post("/monitors", json=_body(pipeline, schedule_cron=cron), headers=headers)

    assert resp.status_code == 422
    assert message in resp.json()["detail"]


async def test_an_llm_block_needs_a_change_gate_before_it(client, headers, key, temporal):
    ungated = await _pipeline(client, headers, gated(key.id, gate=False), name="ungated")
    late = gated(key.id, gate=False)
    late["blocks"].insert(
        3,
        {
            "id": "gate",
            "type": "validate",
            "input": "extract",
            "config": {"rules": [{"kind": "changed"}], "when_unmet": "stop"},
        },
    )
    late["blocks"][4]["input"] = "gate"
    gate_after = await _pipeline(client, headers, late, name="late")

    for pipeline in (ungated, gate_after):
        resp = await client.post("/monitors", json=_body(pipeline), headers=headers)
        assert resp.status_code == 422, resp.text
        [problem] = resp.json()["detail"]
        assert problem["block_id"] == "extract"
        assert problem["message"].startswith("a monitor needs a change gate before its first LLM")


async def test_the_blocks_need_plus_the_allowance_must_fit_the_limit(
    client, headers, key, temporal, monkeypatch
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    from app.core.monitors import allowance_seconds
    from app.pipelines.catalog import CATALOG, run_need_seconds
    from app.pipelines.definition import parse_definition

    async with AsyncSessionLocal() as db:
        from app.models.pipeline import PipelineVersion

        version = await db.scalar(
            select(PipelineVersion).where(PipelineVersion.pipeline_id == uuid.UUID(pipeline["id"]))
        )
    parsed, _ = parse_definition(version.definition)
    need = run_need_seconds(CATALOG[b.type].timing(b.config) for b in parsed.blocks)
    allowance = allowance_seconds(parsed, [])
    monkeypatch.setattr(settings, "pipeline_max_run_seconds", need + allowance - 1)

    resp = await client.post("/monitors", json=_body(pipeline), headers=headers)

    assert resp.status_code == 422
    assert _detail(resp) == [
        f"the blocks need {need} s and a monitor check needs {allowance} s more; "
        f"the limit is {need + allowance - 1} s"
    ]
    monkeypatch.setattr(settings, "pipeline_max_run_seconds", need + allowance)
    assert (
        await client.post("/monitors", json=_body(pipeline), headers=headers)
    ).status_code == 201


async def _scheduled_jobs(user_id, count):
    async with AsyncSessionLocal() as db:
        db.add_all(
            Job(
                user_id=user_id,
                url="https://example.com",
                schedule_cron="0 * * * *",
                schedule_status="active",
                schedule_lane="v2",
            )
            for _ in range(count)
        )
        await db.commit()


async def test_the_21st_schedule_is_refused_and_a_paused_monitor_is_not_counted(
    client, headers, key, db_user, temporal
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    await _scheduled_jobs(db_user.id, 19)
    twentieth = await _monitor(client, headers, _body(pipeline, name="m20"))

    resp = await client.post("/monitors", json=_body(pipeline, name="m21"), headers=headers)
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "scheduled_jobs"

    usage = (await client.get("/users/me/scheduled-jobs", headers=headers)).json()
    assert usage == {"active": 20, "limit": 20, "jobs": 19, "monitors": 1}

    await client.post(f"/monitors/{twentieth['id']}/pause", headers=headers)
    await _monitor(client, headers, _body(pipeline, name="m21"))
    # Resuming the paused one would make 21.
    resp = await client.post(f"/monitors/{twentieth['id']}/resume", headers=headers)
    assert resp.status_code == 429
    assert (await _row(twentieth["id"])).state == "paused"
    # A scheduled job is refused by the same shared cap.
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=db_user.id, scheduled_jobs_limit=20))
        await db.commit()
    resp = await client.post(
        "/jobs",
        json={"url": "https://example.com/j", "schedule_cron": "0 * * * *"},
        headers=headers,
    )
    assert resp.status_code == 429
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Job).where(Job.user_id == db_user.id))
        await db.commit()


async def test_switched_off_refuses_create_and_resume(client, headers, key, temporal, monkeypatch):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    await client.post(f"/monitors/{monitor['id']}/pause", headers=headers)
    monkeypatch.setattr(settings, "pipelines_enabled", False)

    resp = await client.post("/monitors", json=_body(pipeline, name="other"), headers=headers)
    assert resp.status_code == 503
    assert (
        await client.post(f"/monitors/{monitor['id']}/resume", headers=headers)
    ).status_code == 503
    # Reads and pause stay on.
    assert (await client.get(f"/monitors/{monitor['id']}", headers=headers)).status_code == 200


async def test_a_duplicate_name_is_409(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    await _monitor(client, headers, _body(pipeline))

    assert (
        await client.post("/monitors", json=_body(pipeline), headers=headers)
    ).status_code == 409


# --- read, edit, pause, resume -----------------------------------------------------------


async def test_list_and_pipeline_read_show_the_monitors(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    first = await _monitor(client, headers, _body(pipeline, name="a"))
    second = await _monitor(client, headers, _body(pipeline, name="b"))
    await client.post(f"/monitors/{second['id']}/pause", headers=headers)

    listed = (await client.get("/monitors", headers=headers)).json()
    assert [m["id"] for m in listed] == [second["id"], first["id"]]
    assert listed[0]["next_check_at"] is None  # paused
    read = (await client.get(f"/pipelines/{pipeline['id']}", headers=headers)).json()
    assert read["monitors"] == [
        {"id": first["id"], "name": "a", "state": "active"},
        {"id": second["id"], "name": "b", "state": "paused"},
    ]


async def test_patch_changes_name_and_schedule_only(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    url = f"/monitors/{monitor['id']}"

    resp = await client.patch(
        url, json={"name": "renamed", "schedule_cron": "0 * * * *"}, headers=headers
    )

    assert resp.status_code == 200
    assert (resp.json()["name"], resp.json()["schedule_cron"]) == ("renamed", "0 * * * *")
    update_fn = temporal.get_schedule_handle.return_value.update
    # Re-armed with the new cron (put creates, or replaces one already there).
    assert len(temporal.create_schedule.call_args_list) == 2
    schedule = temporal.create_schedule.call_args.args[1]
    assert schedule.spec.cron_expressions == ["0 * * * *"]
    update_fn.assert_not_called()
    for field, value in (("inputs", {}), ("pipeline_id", pipeline["id"]), ("version", 2)):
        resp = await client.patch(url, json={field: value}, headers=headers)
        assert resp.status_code == 422
        assert f"{field} cannot be changed" in str(resp.json()["detail"])
    assert (await client.patch(url, json={"colour": 1}, headers=headers)).status_code == 422
    assert (
        await client.patch(url, json={"schedule_cron": "* * * * *"}, headers=headers)
    ).status_code == 422


async def test_a_refused_rearm_puts_the_schedule_back(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    temporal.create_schedule.side_effect = RuntimeError("temporal down")

    resp = await client.patch(
        f"/monitors/{monitor['id']}", json={"schedule_cron": "0 * * * *"}, headers=headers
    )

    assert resp.status_code == 503
    assert (await _row(monitor["id"])).schedule_cron == CRON


async def test_a_paused_monitors_cron_changes_even_with_the_engine_down(
    client, headers, key, temporal
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    await client.post(f"/monitors/{monitor['id']}/pause", headers=headers)
    temporal.create_schedule.side_effect = RuntimeError("temporal down")

    resp = await client.patch(
        f"/monitors/{monitor['id']}", json={"schedule_cron": "0 * * * *"}, headers=headers
    )

    assert resp.status_code == 200
    assert (await _row(monitor["id"])).schedule_cron == "0 * * * *"


async def test_pause_closes_the_open_situation_and_resume_resets_the_count(
    client, headers, key, temporal
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        db.add(
            MonitorEvent(
                monitor_id=uuid.UUID(monitor["id"]),
                kind="skipped_monthly_runs",
                first_at=now,
                last_at=now,
            )
        )
        await db.execute(
            update(Monitor)
            .where(Monitor.id == uuid.UUID(monitor["id"]))
            .values(consecutive_failures=3)
        )
        await db.commit()

    paused = (await client.post(f"/monitors/{monitor['id']}/pause", headers=headers)).json()
    assert paused["state"] == "paused"
    assert temporal.create_schedule.call_args.args[1].state.paused is True
    async with AsyncSessionLocal() as db:
        [event] = (
            await db.scalars(
                select(MonitorEvent).where(MonitorEvent.monitor_id == uuid.UUID(monitor["id"]))
            )
        ).all()
    assert event.ended_at is not None

    resumed = (await client.post(f"/monitors/{monitor['id']}/resume", headers=headers)).json()
    assert (resumed["state"], resumed["consecutive_failures"]) == ("active", 0)
    assert temporal.create_schedule.call_args.args[1].state.paused is False


async def test_a_self_paused_monitor_resumes_and_a_refused_resume_puts_it_back(
    client, headers, key, temporal
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Monitor)
            .where(Monitor.id == uuid.UUID(monitor["id"]))
            .values(state="paused_itself", consecutive_failures=5)
        )
        await db.commit()
    temporal.create_schedule.side_effect = RuntimeError("temporal down")

    resp = await client.post(f"/monitors/{monitor['id']}/resume", headers=headers)

    assert resp.status_code == 503
    row = await _row(monitor["id"])
    assert (row.state, row.consecutive_failures) == ("paused_itself", 5)
    temporal.create_schedule.side_effect = None
    resp = await client.post(f"/monitors/{monitor['id']}/resume", headers=headers)
    assert (resp.json()["state"], resp.json()["consecutive_failures"]) == ("active", 0)


async def test_pausing_with_the_engine_down_still_pauses_the_row(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    temporal.create_schedule.side_effect = RuntimeError("temporal down")

    resp = await client.post(f"/monitors/{monitor['id']}/pause", headers=headers)

    assert resp.status_code == 200
    assert (await _row(monitor["id"])).state == "paused"


async def test_another_users_monitor_is_404_on_every_route(
    client, headers, key, temporal, other_user
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Monitor)
            .where(Monitor.id == uuid.UUID(monitor["id"]))
            .values(user_id=other_user.id)
        )
        await db.commit()
    url = f"/monitors/{monitor['id']}"

    for method, path, kwargs in (
        ("get", url, {}),
        ("patch", url, {"json": {"name": "x"}}),
        ("post", f"{url}/pause", {}),
        ("post", f"{url}/resume", {}),
    ):
        resp = await getattr(client, method)(path, headers=headers, **kwargs)
        assert resp.status_code == 404, (method, path)
    assert (await client.get("/monitors", headers=headers)).json() == []
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Monitor).where(Monitor.id == uuid.UUID(monitor["id"])))
        await db.commit()


# --- L.5: the deletes a monitor blocks or follows -----------------------------------------


class RemovingMinio:
    def __init__(self):
        self.fail: set[str] = set()
        self.removed: list[str] = []

    async def remove_object(self, bucket, key):
        if f"{bucket}/{key}" in self.fail:
            raise RuntimeError("minio down")
        self.removed.append(f"{bucket}/{key}")


@pytest.fixture
def store():
    from app.core.minio import get_minio

    fake = RemovingMinio()
    app.dependency_overrides[get_minio] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_minio, None)


async def _check_run(monitor: dict, status="completed", size=1000) -> tuple[uuid.UUID, str]:
    """A check of ``monitor`` holding one recorded object on its fetch block."""
    from app.models.pipeline import PipelineRun, PipelineRunBlock
    from app.workflows.activities.contracts import StoredObject
    from app.workflows.activities.db import record_storage
    from app.workflows.pipeline import RecordStorageInput

    row = await _row(monitor["id"])
    run_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(
            PipelineRun(
                id=run_id,
                pipeline_id=row.pipeline_id,
                pipeline_version_id=row.pipeline_version_id,
                user_id=row.user_id,
                status=status,
                workflow_id=f"pipeline-run-{run_id}",
                monitor_id=row.id,
            )
        )
        db.add(
            PipelineRunBlock(
                pipeline_run_id=run_id,
                block_id="fetch",
                position=0,
                type="scrape",
                status="completed",
            )
        )
        await db.commit()
    path = f"scrapeflow-results/history/{uuid.uuid4()}/scrape.html"
    await record_storage(
        RecordStorageInput(
            run_id=str(run_id), block_id="fetch", objects=[StoredObject(path=path, size=size)]
        )
    )
    return run_id, path


async def _used(user_id) -> int:
    async with AsyncSessionLocal() as db:
        quota = await db.get(UserQuota, user_id)
        return 0 if quota is None else quota.storage_bytes_used


async def _runs(monitor_id) -> list:
    from app.models.pipeline import PipelineRun

    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(PipelineRun).where(PipelineRun.monitor_id == uuid.UUID(monitor_id))
            )
        )


async def test_a_pipeline_a_monitor_uses_cannot_be_deleted_in_either_mode(
    client, headers, key, temporal, store
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    active = await _monitor(client, headers, _body(pipeline, name="a"))
    paused = await _monitor(client, headers, _body(pipeline, name="b"))
    await client.post(f"/monitors/{paused['id']}/pause", headers=headers)
    url = f"/pipelines/{pipeline['id']}"

    for query in ("", "?permanent=true"):
        resp = await client.delete(url + query, headers=headers)
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "pipeline_in_use"
        assert [(m["name"], m["state"]) for m in detail["monitors"]] == [
            ("a", "active"),
            ("b", "paused"),
        ]
    async with AsyncSessionLocal() as db:
        assert (await db.get(Pipeline, uuid.UUID(pipeline["id"]))).deleted_at is None

    for monitor in (active, paused):
        assert (
            await client.delete(f"/monitors/{monitor['id']}", headers=headers)
        ).status_code == 204
    assert (await client.delete(url, headers=headers)).status_code == 204


async def test_a_pipeline_with_runs_a_monitor_uses_is_not_soft_deleted(
    client, headers, key, temporal, store
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    await _check_run(monitor)

    resp = await client.delete(f"/pipelines/{pipeline['id']}", headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"]["monitors"][0]["id"] == monitor["id"]
    async with AsyncSessionLocal() as db:
        assert (await db.get(Pipeline, uuid.UUID(pipeline["id"]))).deleted_at is None


async def test_an_llm_key_a_monitor_uses_cannot_be_deleted(client, headers, key, db_user, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Monitor)
            .where(Monitor.id == uuid.UUID(monitor["id"]))
            .values(state="paused_itself")
        )
        await db.commit()
    spare = await _llm_key(db_user.id)
    # A key only a plain pipeline uses deletes as before.
    unmonitored = await _llm_key(db_user.id)
    await _pipeline(client, headers, gated(unmonitored.id), name="plain")

    resp = await client.delete(f"/users/llm-keys/{key.id}", headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"]["monitors"] == [
        {"id": monitor["id"], "name": "prices", "state": "paused_itself"}
    ]
    for free in (spare, unmonitored):
        assert (
            await client.delete(f"/users/llm-keys/{free.id}", headers=headers)
        ).status_code == 200
    await client.delete(f"/monitors/{monitor['id']}", headers=headers)
    assert (await client.delete(f"/users/llm-keys/{key.id}", headers=headers)).status_code == 200


async def test_the_llm_key_delete_waits_for_the_users_lock(client, headers, key, db_user):
    import asyncio

    from app.core.quota import lock_user

    async with AsyncSessionLocal() as holder:
        await lock_user(db_user.id, holder)
        task = asyncio.create_task(client.delete(f"/users/llm-keys/{key.id}", headers=headers))
        await asyncio.sleep(1)
        assert not task.done(), "the key delete did not wait for the user's lock"
        await holder.rollback()
    assert (await asyncio.wait_for(task, timeout=10)).status_code == 200


async def test_a_plain_delete_cancels_the_running_check_and_keeps_the_checks(
    client, headers, key, db_user, temporal, store
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    done, _ = await _check_run(monitor)
    running, _ = await _check_run(monitor, status="running")
    used = await _used(db_user.id)

    resp = await client.delete(f"/monitors/{monitor['id']}", headers=headers)

    assert resp.status_code == 204
    assert await _row(monitor["id"]) is None
    from app.models.pipeline import PipelineRun

    async with AsyncSessionLocal() as db:
        kept = {
            r.id: r
            for r in await db.scalars(
                select(PipelineRun).where(PipelineRun.id.in_([done, running]))
            )
        }
    assert set(kept) == {done, running}
    assert all(r.monitor_id is None for r in kept.values())
    assert kept[running].cancel_requested_at is not None
    temporal.get_workflow_handle.assert_called_with(f"pipeline-run-{running}")
    temporal.get_schedule_handle.return_value.delete.assert_awaited()
    assert temporal.get_schedule_handle.call_args.args[0] == f"monitor-schedule-{monitor['id']}"
    assert store.removed == [] and await _used(db_user.id) == used


async def test_a_delete_with_checks_frees_exactly_their_bytes(
    client, headers, key, db_user, temporal, store
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    other = await _monitor(client, headers, _body(pipeline, name="other"))
    paths = [(await _check_run(monitor, size=s))[1] for s in (1000, 2500)]
    await _check_run(other, size=700)
    used = await _used(db_user.id)

    resp = await client.delete(f"/monitors/{monitor['id']}?with_checks=true", headers=headers)

    assert resp.status_code == 204
    assert sorted(store.removed) == sorted(paths)
    assert await _used(db_user.id) == used - 3500
    assert await _runs(monitor["id"]) == []
    assert len(await _runs(other["id"])) == 1


async def test_a_delete_with_checks_that_cannot_remove_an_object_deletes_nothing(
    client, headers, key, db_user, temporal, store
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    await _check_run(monitor, size=1000)
    _, second = await _check_run(monitor, size=2000)
    store.fail.add(second)
    used = await _used(db_user.id)

    resp = await client.delete(f"/monitors/{monitor['id']}?with_checks=true", headers=headers)

    assert resp.status_code == 503
    assert await _row(monitor["id"]) is not None
    assert len(await _runs(monitor["id"])) == 2
    # What was freed stays freed.
    assert await _used(db_user.id) == used - 1000
    store.fail.clear()
    assert (
        await client.delete(f"/monitors/{monitor['id']}?with_checks=true", headers=headers)
    ).status_code == 204
    assert await _used(db_user.id) == used - 3000


async def test_another_users_monitor_cannot_be_deleted(client, headers, key, temporal, other_user):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Monitor)
            .where(Monitor.id == uuid.UUID(monitor["id"]))
            .values(user_id=other_user.id)
        )
        await db.commit()

    assert (await client.delete(f"/monitors/{monitor['id']}", headers=headers)).status_code == 404
    assert await _row(monitor["id"]) is not None
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Monitor).where(Monitor.id == uuid.UUID(monitor["id"])))
        await db.commit()


async def test_admin_user_delete_forgets_the_users_monitor_schedules(
    client, headers, key, db_user, temporal, store, monkeypatch
):
    from app.auth.dependencies import get_current_admin_user

    pipeline = await _pipeline(client, headers, gated(key.id))
    first = await _monitor(client, headers, _body(pipeline, name="a"))
    second = await _monitor(client, headers, _body(pipeline, name="b"))
    admin = MagicMock(id=uuid.uuid4())
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    try:
        resp = await client.delete(f"/admin/users/{db_user.id}")
    finally:
        app.dependency_overrides.pop(get_current_admin_user, None)

    assert resp.status_code == 204
    forgotten = {c.args[0] for c in temporal.get_schedule_handle.call_args_list}
    assert {f"monitor-schedule-{first['id']}", f"monitor-schedule-{second['id']}"} <= forgotten
    assert await _row(first["id"]) is None


async def test_resuming_ends_the_self_pause_situation(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Monitor)
            .where(Monitor.id == uuid.UUID(monitor["id"]))
            .values(state="paused_itself")
        )
        db.add(
            MonitorEvent(
                monitor_id=uuid.UUID(monitor["id"]), kind="paused_itself", first_at=now, last_at=now
            )
        )
        await db.commit()

    await client.post(f"/monitors/{monitor['id']}/resume", headers=headers)

    async with AsyncSessionLocal() as db:
        [event] = (
            await db.scalars(
                select(MonitorEvent).where(MonitorEvent.monitor_id == uuid.UUID(monitor["id"]))
            )
        ).all()
    assert event.ended_at is not None


# --- L.9: Check now ------------------------------------------------------------------------


async def test_check_now_starts_a_check_directly_with_its_monitor_context(
    client, headers, key, temporal
):
    from app.workflows.pipeline import PipelineWorkflow

    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        db.add(
            MonitorEvent(
                monitor_id=uuid.UUID(monitor["id"]),
                kind="skipped_monthly_runs",
                first_at=now,
                last_at=now,
            )
        )
        await db.commit()

    resp = await client.post(f"/monitors/{monitor['id']}/check", headers=headers)

    assert resp.status_code == 202, resp.text
    run_id = resp.json()["id"]
    [run] = await _runs(monitor["id"])
    assert (str(run.id), run.status, run.inputs) == (run_id, "running", monitor["inputs"])
    temporal.start_workflow.assert_awaited_once()
    (fn, sent), kwargs = temporal.start_workflow.await_args
    assert fn == PipelineWorkflow.run and kwargs["id"] == f"pipeline-run-{run_id}"
    assert sent.monitor.monitor_id == monitor["id"] and sent.monitor.baseline_run_id is None
    # No firing and no Schedule trigger.
    temporal.get_schedule_handle.return_value.trigger.assert_not_called()
    async with AsyncSessionLocal() as db:
        [event] = (
            await db.scalars(
                select(MonitorEvent).where(MonitorEvent.monitor_id == uuid.UUID(monitor["id"]))
            )
        ).all()
    assert event.ended_at is not None


async def test_check_now_hands_over_the_newest_eligible_check(client, headers, key, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    baseline, _ = await _check_run(monitor)
    async with AsyncSessionLocal() as db:
        from app.models.pipeline import PipelineRun

        await db.execute(
            update(PipelineRun).where(PipelineRun.id == baseline).values(baseline_eligible=True)
        )
        await db.commit()

    resp = await client.post(f"/monitors/{monitor['id']}/check", headers=headers)

    assert resp.status_code == 202
    sent = temporal.start_workflow.await_args.args[1]
    assert sent.monitor.baseline_run_id == str(baseline)


async def test_check_now_is_refused_while_a_check_runs_and_a_firing_is_skipped(
    client, headers, key, temporal
):
    from app.workflows.activities.monitor_checks import admit_monitor_check
    from app.workflows.monitor_check import AdmitMonitorCheckInput

    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    first = await client.post(f"/monitors/{monitor['id']}/check", headers=headers)
    assert first.status_code == 202

    again = await client.post(f"/monitors/{monitor['id']}/check", headers=headers)
    assert again.status_code == 409
    assert again.json()["detail"]["run_id"] == first.json()["id"]

    firing = await admit_monitor_check(
        AdmitMonitorCheckInput(monitor_id=monitor["id"], run_id=str(uuid.uuid4()))
    )
    assert firing.verdict == "skipped"
    async with AsyncSessionLocal() as db:
        [event] = (
            await db.scalars(
                select(MonitorEvent).where(MonitorEvent.monitor_id == uuid.UUID(monitor["id"]))
            )
        ).all()
    assert event.kind == "skipped_check_in_progress"
    assert len(await _runs(monitor["id"])) == 1


async def test_a_check_now_that_could_not_start_is_a_counted_failed_check(
    client, headers, key, temporal
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    temporal.start_workflow.side_effect = RuntimeError("engine down")

    resp = await client.post(f"/monitors/{monitor['id']}/check", headers=headers)

    assert resp.status_code == 503
    [run] = await _runs(monitor["id"])
    assert (run.status, run.error) == ("failed", "could not start workflow: engine down")
    assert (await _row(monitor["id"])).consecutive_failures == 1
    from app.models.webhook_delivery import WebhookDelivery

    async with AsyncSessionLocal() as db:
        [alert] = (
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        ).all()
    assert alert.event == "pipeline.failed"


async def test_check_now_is_admitted_against_the_meters(client, headers, key, db_user, temporal):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=db_user.id, concurrent_jobs_limit=0))
        await db.commit()

    resp = await client.post(f"/monitors/{monitor['id']}/check", headers=headers)

    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "concurrent_jobs"
    assert await _runs(monitor["id"]) == []


async def test_check_now_is_owner_scoped_and_off_when_switched_off(
    client, headers, key, temporal, other_user, monkeypatch
):
    pipeline = await _pipeline(client, headers, gated(key.id))
    monitor = await _monitor(client, headers, _body(pipeline))
    monkeypatch.setattr(settings, "pipelines_enabled", False)
    assert (
        await client.post(f"/monitors/{monitor['id']}/check", headers=headers)
    ).status_code == 503
    monkeypatch.setattr(settings, "pipelines_enabled", True)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Monitor)
            .where(Monitor.id == uuid.UUID(monitor["id"]))
            .values(user_id=other_user.id)
        )
        await db.commit()

    assert (
        await client.post(f"/monitors/{monitor['id']}/check", headers=headers)
    ).status_code == 404
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Monitor).where(Monitor.id == uuid.UUID(monitor["id"])))
        await db.commit()
