import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core.rate_limit import _increment_and_check
from app.core.redis import get_redis
from app.settings import settings


# ---------------------------------------------------------------------------
# Helper fixture — real Redis client via the shared pool
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def redis_client(init_clients):
    """Yield a real Redis client using the session-scoped pool."""
    async for r in get_redis():
        yield r


# ---------------------------------------------------------------------------
# 7d-1: requests under the limit all pass
# ---------------------------------------------------------------------------

async def test_rate_limit_under_limit(redis_client):
    """All requests below the limit succeed without raising."""
    user_id = uuid.uuid4()
    original = settings.rate_limit_requests
    settings.rate_limit_requests = 5
    try:
        for _ in range(4):  # 4 out of 5 — all should pass
            await _increment_and_check(user_id, redis_client)
    finally:
        settings.rate_limit_requests = original
        await redis_client.delete(f"scrapeflow:rl:{user_id}:{_window()}")


# ---------------------------------------------------------------------------
# 7d-2: request exactly at the limit still passes
# ---------------------------------------------------------------------------

async def test_rate_limit_at_limit(redis_client):
    """The Nth request (exactly at the limit) still succeeds."""
    user_id = uuid.uuid4()
    original = settings.rate_limit_requests
    settings.rate_limit_requests = 5
    try:
        for _ in range(5):
            await _increment_and_check(user_id, redis_client)  # should not raise
    finally:
        settings.rate_limit_requests = original
        await redis_client.delete(f"scrapeflow:rl:{user_id}:{_window()}")


# ---------------------------------------------------------------------------
# 7d-3: request over the limit raises 429
# ---------------------------------------------------------------------------

async def test_rate_limit_exceeded(redis_client):
    """The (N+1)th request raises HTTP 429."""
    from fastapi import HTTPException
    user_id = uuid.uuid4()
    original = settings.rate_limit_requests
    settings.rate_limit_requests = 3
    try:
        for _ in range(3):
            await _increment_and_check(user_id, redis_client)
        with pytest.raises(HTTPException) as exc_info:
            await _increment_and_check(user_id, redis_client)
        assert exc_info.value.status_code == 429
    finally:
        settings.rate_limit_requests = original
        await redis_client.delete(f"scrapeflow:rl:{user_id}:{_window()}")


# ---------------------------------------------------------------------------
# 7d-4: different users have independent counters
# ---------------------------------------------------------------------------

async def test_rate_limit_per_user_isolation(redis_client):
    """Hitting the limit for user A does not affect user B."""
    from fastapi import HTTPException
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    original = settings.rate_limit_requests
    settings.rate_limit_requests = 2
    try:
        # Exhaust user_a's quota
        for _ in range(2):
            await _increment_and_check(user_a, redis_client)
        with pytest.raises(HTTPException):
            await _increment_and_check(user_a, redis_client)

        # user_b should still be unaffected
        await _increment_and_check(user_b, redis_client)  # should not raise
    finally:
        settings.rate_limit_requests = original
        await redis_client.delete(f"scrapeflow:rl:{user_a}:{_window()}")
        await redis_client.delete(f"scrapeflow:rl:{user_b}:{_window()}")


# ---------------------------------------------------------------------------
# 7d-5: HTTP integration — POST /jobs returns 429 when limit exceeded
# ---------------------------------------------------------------------------

async def test_create_job_rate_limited(client, mock_clerk_auth, redis_client):
    """POST /jobs returns 429 once the rate limit is exhausted."""
    headers = {"Authorization": "Bearer fake.jwt.token"}

    # Clear any existing rate limit counter for the mock user from prior tests
    from app.core.db import AsyncSessionLocal
    from app.models.user import User
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.clerk_id == "user_test_123"))
        user = result.scalar_one_or_none()
    if user:
        await redis_client.delete(f"scrapeflow:rl:{user.id}:{_window()}")

    original = settings.rate_limit_requests
    settings.rate_limit_requests = 2
    try:
        for _ in range(2):
            with patch("app.routers.jobs.get_jetstream", return_value=AsyncMock()):
                resp = await client.post("/jobs", json={"url": "https://example.com"}, headers=headers)
            assert resp.status_code == 201

        with patch("app.routers.jobs.get_jetstream", return_value=AsyncMock()):
            resp = await client.post("/jobs", json={"url": "https://example.com"}, headers=headers)
        assert resp.status_code == 429
    finally:
        settings.rate_limit_requests = original


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _window() -> int:
    import time
    return int(time.time()) // settings.rate_limit_window_seconds
