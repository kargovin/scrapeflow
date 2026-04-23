"""Tests for WS /jobs/{id}/watch and WS /batch/{id}/watch endpoints (Step 26)."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.db import get_db
from app.main import app
from tests.conftest import MockJobNotifier, patched_ws_app

# ---------------------------------------------------------------------------
# Mock helpers — use SimpleNamespace to avoid SQLAlchemy instrumentation
# ---------------------------------------------------------------------------


def _user(uid=None):
    return SimpleNamespace(
        id=uid or uuid.uuid4(),
        clerk_id="test_clerk",
        email="test@example.com",
        is_admin=False,
    )


def _job(user_id):
    return SimpleNamespace(id=uuid.uuid4(), user_id=user_id)


def _run(job_id, status: str, error=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        job_id=job_id,
        status=status,
        error=error,
        completed_at=None,
        created_at=MagicMock(),
    )


def _batch(user_id, status: str, total=3, completed=0, failed=0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        status=status,
        total=total,
        completed=completed,
        failed=failed,
    )


# ---------------------------------------------------------------------------
# DB mock factories
# ---------------------------------------------------------------------------


def _db_with_job_run(job, run):
    """AsyncSession that returns (job, run) for a Job+JobRun select query."""
    result = MagicMock()
    result.one_or_none.return_value = (job, run)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _db_with_batch(batch_obj):
    db = AsyncMock()
    db.get = AsyncMock(return_value=batch_obj)
    return db


def _db_empty():
    result = MagicMock()
    result.one_or_none.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=None)
    return db


def _override_db(db_obj):
    async def _gen():
        yield db_obj

    return _gen


# ---------------------------------------------------------------------------
# /jobs/{id}/watch
# ---------------------------------------------------------------------------


async def test_job_watch_bad_token():
    """auth_from_token returns None → close 4001."""
    app.state.job_notifier = MockJobNotifier()
    app.dependency_overrides[get_db] = _override_db(_db_empty())

    def _run():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.jobs.auth_from_token", new=AsyncMock(return_value=None)):
                with pytest.raises(WebSocketDisconnect):
                    with client.websocket_connect(f"/jobs/{uuid.uuid4()}/watch?token=sf_bad"):
                        # receive_json triggers close-frame read → raises WebSocketDisconnect
                        pass
                    # If the handler hasn't closed yet, force a receive
                # The __exit__ of websocket_connect may raise if server already closed

    # Fallback: check that server closes with 4001 by catching on receive
    messages = []

    def _run2():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.jobs.auth_from_token", new=AsyncMock(return_value=None)):
                try:
                    with client.websocket_connect(f"/jobs/{uuid.uuid4()}/watch?token=sf_bad") as ws:
                        ws.receive_text()
                except WebSocketDisconnect as e:
                    messages.append(e.code)

    try:
        await asyncio.to_thread(_run2)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert messages == [4001]


async def test_job_watch_job_not_found():
    """Valid token but job doesn't exist for this user → close 4004."""
    user = _user()
    app.state.job_notifier = MockJobNotifier()
    app.dependency_overrides[get_db] = _override_db(_db_empty())

    messages = []

    def _run():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.jobs.auth_from_token", new=AsyncMock(return_value=user)):
                try:
                    with client.websocket_connect(f"/jobs/{uuid.uuid4()}/watch?token=sf_any") as ws:
                        ws.receive_text()
                except WebSocketDisconnect as e:
                    messages.append(e.code)

    try:
        await asyncio.to_thread(_run)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert messages == [4004]


async def test_job_watch_already_terminal():
    """Job already completed → single terminal message, then server closes."""
    user = _user()
    job = _job(user.id)
    run = _run(job.id, "completed")
    app.state.job_notifier = MockJobNotifier()
    app.dependency_overrides[get_db] = _override_db(_db_with_job_run(job, run))

    messages = []

    def _do():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.jobs.auth_from_token", new=AsyncMock(return_value=user)):
                try:
                    with client.websocket_connect(f"/jobs/{job.id}/watch?token=sf_any") as ws:
                        messages.append(ws.receive_json())  # terminal message
                        ws.receive_text()  # close frame → raises
                except WebSocketDisconnect:
                    pass

    try:
        await asyncio.to_thread(_do)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert len(messages) == 1
    assert messages[0]["type"] == "completed"
    assert messages[0]["status"] == "completed"
    assert "result_url" in messages[0]


async def test_job_watch_streams_until_terminal():
    """Running job: receives current status on connect, then streams to completed."""
    user = _user()
    job = _job(user.id)
    run = _run(job.id, "running")
    run_id = str(run.id)

    app.state.job_notifier = MockJobNotifier(
        job_updates=[
            {"run_id": run_id, "status": "running"},
            {"run_id": run_id, "status": "completed"},
        ]
    )
    app.dependency_overrides[get_db] = _override_db(_db_with_job_run(job, run))

    messages = []

    def _do():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.jobs.auth_from_token", new=AsyncMock(return_value=user)):
                try:
                    with client.websocket_connect(f"/jobs/{job.id}/watch?token=sf_any") as ws:
                        messages.append(ws.receive_json())  # current status on connect
                        messages.append(ws.receive_json())  # "running" from queue
                        messages.append(ws.receive_json())  # "completed" → terminal
                        ws.receive_text()  # close frame
                except WebSocketDisconnect:
                    pass

    try:
        await asyncio.to_thread(_do)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert messages[0] == {
        "type": "status_update",
        "job_id": str(job.id),
        "run_id": run_id,
        "status": "running",
    }
    assert messages[1]["status"] == "running"
    assert messages[2]["type"] == "completed"
    assert messages[2]["status"] == "completed"
    assert "result_url" in messages[2]


async def test_job_watch_failed_run_already_terminal():
    """Failed job already terminal → returns failed type message."""
    user = _user()
    job = _job(user.id)
    run = _run(job.id, "failed", error="timeout")
    app.state.job_notifier = MockJobNotifier()
    app.dependency_overrides[get_db] = _override_db(_db_with_job_run(job, run))

    messages = []

    def _do():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.jobs.auth_from_token", new=AsyncMock(return_value=user)):
                try:
                    with client.websocket_connect(f"/jobs/{job.id}/watch?token=sf_any") as ws:
                        messages.append(ws.receive_json())
                        ws.receive_text()
                except WebSocketDisconnect:
                    pass

    try:
        await asyncio.to_thread(_do)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert messages[0]["type"] == "failed"
    assert messages[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# /batch/{id}/watch
# ---------------------------------------------------------------------------


async def test_batch_watch_bad_token():
    """auth_from_token returns None → close 4001."""
    app.state.job_notifier = MockJobNotifier()
    app.dependency_overrides[get_db] = _override_db(_db_empty())

    messages = []

    def _run():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.batch.auth_from_token", new=AsyncMock(return_value=None)):
                try:
                    with client.websocket_connect(f"/batch/{uuid.uuid4()}/watch?token=bad") as ws:
                        ws.receive_text()
                except WebSocketDisconnect as e:
                    messages.append(e.code)

    try:
        await asyncio.to_thread(_run)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert messages == [4001]


async def test_batch_watch_batch_not_found():
    """Valid token but batch doesn't exist or belongs to another user → 4004."""
    user = _user()
    app.state.job_notifier = MockJobNotifier()
    app.dependency_overrides[get_db] = _override_db(_db_empty())

    messages = []

    def _run():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.batch.auth_from_token", new=AsyncMock(return_value=user)):
                try:
                    with client.websocket_connect(
                        f"/batch/{uuid.uuid4()}/watch?token=sf_any"
                    ) as ws:
                        ws.receive_text()
                except WebSocketDisconnect as e:
                    messages.append(e.code)

    try:
        await asyncio.to_thread(_run)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert messages == [4004]


async def test_batch_watch_already_terminal():
    """Completed batch → single terminal message, then close."""
    user = _user()
    batch = _batch(user.id, "completed", total=3, completed=3, failed=0)
    app.state.job_notifier = MockJobNotifier()
    app.dependency_overrides[get_db] = _override_db(_db_with_batch(batch))

    messages = []

    def _do():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.batch.auth_from_token", new=AsyncMock(return_value=user)):
                try:
                    with client.websocket_connect(f"/batch/{batch.id}/watch?token=sf_any") as ws:
                        messages.append(ws.receive_json())
                        ws.receive_text()
                except WebSocketDisconnect:
                    pass

    try:
        await asyncio.to_thread(_do)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert messages[0]["type"] == "completed"
    assert messages[0]["total"] == 3
    assert messages[0]["completed"] == 3


async def test_batch_watch_streams_progress():
    """Running batch: initial state on connect, then progress updates, then terminal."""
    user = _user()
    batch = _batch(user.id, "running", total=3, completed=0, failed=0)
    batch_id_str = str(batch.id)

    app.state.job_notifier = MockJobNotifier(
        batch_updates=[
            {
                "batch_id": batch_id_str,
                "completed": 1,
                "failed": 0,
                "total": 3,
                "status": "running",
                "item_url": "https://example.com/a",
                "item_status": "completed",
            },
            {
                "batch_id": batch_id_str,
                "completed": 3,
                "failed": 0,
                "total": 3,
                "status": "completed",
                "item_url": "https://example.com/c",
                "item_status": "completed",
            },
        ]
    )
    app.dependency_overrides[get_db] = _override_db(_db_with_batch(batch))

    messages = []

    def _do():
        with patched_ws_app(), TestClient(app, raise_server_exceptions=False) as client:
            with patch("app.routers.batch.auth_from_token", new=AsyncMock(return_value=user)):
                try:
                    with client.websocket_connect(f"/batch/{batch.id}/watch?token=sf_any") as ws:
                        messages.append(ws.receive_json())  # initial state
                        messages.append(ws.receive_json())  # progress: 1/3
                        messages.append(ws.receive_json())  # terminal: completed
                        ws.receive_text()  # close frame
                except WebSocketDisconnect:
                    pass

    try:
        await asyncio.to_thread(_do)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.state.job_notifier = None

    assert messages[0]["type"] == "batch_progress"
    assert messages[0]["completed"] == 0

    assert messages[1]["type"] == "batch_progress"
    assert messages[1]["completed"] == 1
    assert messages[1]["latest_item"] == {"url": "https://example.com/a", "status": "completed"}

    assert messages[2]["type"] == "completed"
    assert messages[2]["completed"] == 3
