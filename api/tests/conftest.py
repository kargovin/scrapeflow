import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.auth.api_key import generate_api_key, hash_api_key
from app.core import minio, nats
from app.core.db import AsyncSessionLocal
from app.core.nats import get_jetstream
from app.core.rate_limit import check_rate_limit
from app.core.redis import close_pool, create_pool
from app.main import app
from app.models.api_key import ApiKey
from app.models.user import User
from app.routers.batch import check_batch_quota
from app.routers.jobs import check_job_quota


@pytest_asyncio.fixture(autouse=True, loop_scope="session", scope="session")
async def init_clients():
    """Initialize all clients once for the entire test session."""
    app.state.redis_pool = create_pool()
    app.state.minio = await minio.create_client()
    app.state.nats_client, app.state.nats_js = await nats.connect()
    yield
    await nats.disconnect(app.state.nats_client)
    await minio.close_client(app.state.minio)
    await close_pool(app.state.redis_pool)


@pytest_asyncio.fixture
async def client():
    """AsyncClient wired directly to the FastAPI app via ASGI transport.

    Rate limiting and quota checks are both disabled — all tests share one mock user
    and would exhaust the Redis window / concurrent-jobs limit mid-suite otherwise.
    Use quota_client for tests that need real quota enforcement.
    """
    app.dependency_overrides[check_rate_limit] = lambda: None
    app.dependency_overrides[check_job_quota] = lambda: None
    app.dependency_overrides[check_batch_quota] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(check_rate_limit, None)
    app.dependency_overrides.pop(check_job_quota, None)
    app.dependency_overrides.pop(check_batch_quota, None)


@pytest_asyncio.fixture
async def rate_limited_client():
    """Like `client` but with the real rate limiter active. Used only by rate-limit integration tests."""
    app.dependency_overrides[check_job_quota] = lambda: None
    app.dependency_overrides[check_batch_quota] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(check_job_quota, None)
    app.dependency_overrides.pop(check_batch_quota, None)


@pytest_asyncio.fixture
async def quota_client():
    """Like `client` but with real quota enforcement active. Used by quota integration tests."""
    app.dependency_overrides[check_rate_limit] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(check_rate_limit, None)


@pytest.fixture
def mock_clerk_auth():
    """Patch Clerk to return a fake valid payload."""
    mock_state = MagicMock()
    mock_state.is_signed_in = True
    mock_state.payload = {"sub": "user_test_123", "email": "test@example.com"}

    mock_clerk_user = MagicMock()
    mock_clerk_user.email_addresses = [MagicMock(email_address="test@example.com")]

    mock_clerk_instance = MagicMock()
    mock_clerk_instance.authenticate_request.return_value = mock_state
    mock_clerk_instance.users.get.return_value = mock_clerk_user

    with patch("app.auth.jwt._clerk", mock_clerk_instance):
        yield


@pytest_asyncio.fixture
async def db_user():
    """Create a real user in DB, clean up after."""
    user = User(clerk_id=f"user_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.com")
    async with AsyncSessionLocal() as db:
        db.add(user)
        await db.commit()
        await db.refresh(user)
    yield user
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest_asyncio.fixture
async def other_user():
    """A second independent DB user for cross-tenant tests."""
    user = User(clerk_id=f"user_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.com")
    async with AsyncSessionLocal() as db:
        db.add(user)
        await db.commit()
        await db.refresh(user)
    yield user
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest_asyncio.fixture
async def db_api_key(db_user):
    """Create a real ApiKey in DB for db_user. Returns (raw_key, api_key).
    Cleanup is handled by db_user's cascade delete.
    """
    raw_key = generate_api_key()
    api_key = ApiKey(user_id=db_user.id, key_hash=hash_api_key(raw_key), name="fixture key")
    async with AsyncSessionLocal() as db:
        db.add(api_key)
        await db.commit()
        await db.refresh(api_key)
    return raw_key, api_key


@pytest.fixture
def mock_jetstream():
    mock_js = AsyncMock()
    app.dependency_overrides[get_jetstream] = lambda: mock_js
    yield mock_js
    app.dependency_overrides.pop(get_jetstream, None)


# ---------------------------------------------------------------------------
# WebSocket test helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _noop_lifespan(app):
    """No-op lifespan used in WebSocket tests to prevent re-initialising infra."""
    yield


@contextlib.contextmanager
def patched_ws_app():
    """Replace the FastAPI lifespan with a no-op for WebSocket tests.

    TestClient(app) triggers the lifespan (re-initialises Redis/NATS/MinIO and
    starts background tasks) which conflicts with the session-scoped init_clients
    fixture. Patching the lifespan to a no-op lets TestClient run the WebSocket
    handshake against the fully-initialised app.state without double-setup.
    """
    original = app.router.lifespan_context
    app.router.lifespan_context = _noop_lifespan
    try:
        yield
    finally:
        app.router.lifespan_context = original


class MockJobNotifier:
    """Pre-fills queues with the supplied update dicts so WS handlers terminate."""

    def __init__(self, job_updates=None, batch_updates=None):
        self._job_updates = list(job_updates or [])
        self._batch_updates = list(batch_updates or [])

    @asynccontextmanager
    async def subscribe_job(self, job_id: str):
        queue: asyncio.Queue = asyncio.Queue()
        for item in self._job_updates:
            await queue.put(item)
        yield queue

    @asynccontextmanager
    async def subscribe_batch(self, batch_id: str):
        queue: asyncio.Queue = asyncio.Queue()
        for item in self._batch_updates:
            await queue.put(item)
        yield queue
