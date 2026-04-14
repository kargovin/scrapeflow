import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.auth.api_key import generate_api_key, hash_api_key
from app.core import minio, nats
from app.core.db import AsyncSessionLocal
from app.core.nats import get_jetstream
from app.core.redis import close_pool, create_pool
from app.main import app
from app.models.api_key import ApiKey
from app.models.user import User


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
    """AsyncClient wired directly to the FastAPI app via ASGI transport."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_clerk_auth(client):
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
