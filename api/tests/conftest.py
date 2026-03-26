import uuid
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete

from app.main import app
from app.core import minio, nats
from app.core.db import AsyncSessionLocal
from app.core.redis import create_pool, close_pool
from app.models.user import User


@pytest_asyncio.fixture(autouse=True, loop_scope="session", scope="session")
async def init_clients():
    """Initialize all clients once for the entire test session."""
    create_pool()
    await minio.create_client()
    await nats.connect()
    yield
    await nats.disconnect()
    await minio.close_client()
    await close_pool()


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
