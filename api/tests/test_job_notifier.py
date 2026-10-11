"""JobNotifier against the dev Postgres: the pipeline channel and BUG-009's reconnect."""

import asyncio
import json
import uuid

import asyncpg
import pytest

from app.core import job_notifier as notifier_module
from app.core.job_notifier import JobNotifier, WebSocketConnectionLimitExceeded
from app.core.pipeline_status import PIPELINE_STATUS_CHANNEL
from app.settings import settings

DSN = settings.database_url.replace("+asyncpg", "")


@pytest.fixture
async def notifier(monkeypatch):
    monkeypatch.setattr(notifier_module, "RECONNECT_BACKOFF", (0.05,))
    monkeypatch.setattr(notifier_module, "HEALTH_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(notifier_module, "HEALTH_CHECK_TIMEOUT", 0.2)
    n = JobNotifier()
    await n.start(DSN)
    yield n
    await n.stop()


async def _notify(channel: str, payload: str) -> None:
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("SELECT pg_notify($1, $2)", channel, payload)
    finally:
        await conn.close()


def _pipeline_update(run_id: str, status: str = "running") -> str:
    return json.dumps({"run_id": run_id, "pipeline_id": "p", "status": status})


async def _until(predicate, seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_pipeline_updates_reach_only_that_runs_subscribers(notifier):
    run_id, other = str(uuid.uuid4()), str(uuid.uuid4())
    async with (
        notifier.subscribe_pipeline_run(run_id, "u1") as mine,
        notifier.subscribe_pipeline_run(other, "u1") as theirs,
    ):
        await _notify(PIPELINE_STATUS_CHANNEL, _pipeline_update(run_id))
        update = await asyncio.wait_for(mine.get(), 5)
        assert (update["run_id"], update["status"]) == (run_id, "running")
        assert theirs.empty()


async def test_a_dropped_connection_reconnects_and_relistens_every_channel(notifier):
    killer = await asyncpg.connect(DSN)
    try:
        old_pid = notifier._conn.get_server_pid()
        await killer.execute("SELECT pg_terminate_backend($1)", old_pid)
        await _until(lambda: notifier.connected and notifier._conn.get_server_pid() != old_pid)
    finally:
        await killer.close()

    run_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with (
        notifier.subscribe_pipeline_run(run_id, "u1") as pipeline_queue,
        notifier.subscribe_job(job_id, "u1") as job_queue,
    ):
        await _notify(PIPELINE_STATUS_CHANNEL, _pipeline_update(run_id))
        await _notify("job_status", f"{job_id}:{uuid.uuid4()}:completed")
        assert (await asyncio.wait_for(pipeline_queue.get(), 5))["run_id"] == run_id
        assert (await asyncio.wait_for(job_queue.get(), 5))["status"] == "completed"


async def test_a_failing_health_check_replaces_the_connection(notifier, monkeypatch):
    old = notifier._conn
    real_execute = asyncpg.Connection.execute

    async def half_open(self, *args, **kwargs):
        if self is old:
            await asyncio.sleep(60)
        return await real_execute(self, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "execute", half_open)
    await _until(lambda: notifier.connected and notifier._conn is not old)
    assert old.is_closed()


async def test_stop_does_not_reconnect(notifier):
    conn = notifier._conn
    await notifier.stop()
    await asyncio.sleep(0.2)
    assert conn.is_closed()
    assert notifier._conn is None


async def test_the_connection_limit_spans_every_channel(notifier, monkeypatch):
    monkeypatch.setattr(settings, "ws_max_connections_per_user", 2)
    async with notifier.subscribe_job("j", "u1"), notifier.subscribe_pipeline_run("r", "u1"):
        with pytest.raises(WebSocketConnectionLimitExceeded):
            async with notifier.subscribe_batch("b", "u1"):
                pass
    async with notifier.subscribe_pipeline_run("r", "u1"):
        pass
