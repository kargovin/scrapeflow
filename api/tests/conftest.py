import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core import minio, nats
from app.core.redis import create_pool, close_pool


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
