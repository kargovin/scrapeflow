import asyncio
import uuid

import pytest
import pytest_asyncio

from app.core.rate_limit import _increment_and_check
from app.main import app
from app.settings import settings

# ---------------------------------------------------------------------------
# Helper — new key format (no window suffix; sliding window has one key)
# ---------------------------------------------------------------------------


def _key(user_id: uuid.UUID) -> str:
    return f"rate:user:{user_id}"


# ---------------------------------------------------------------------------
# Fixture — real Redis client via the shared pool
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_client(init_clients):
    import redis.asyncio as aioredis

    async with aioredis.Redis(connection_pool=app.state.redis_pool) as r:
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
        await redis_client.delete(_key(user_id))


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
        await redis_client.delete(_key(user_id))


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
        assert "Retry-After" in exc_info.value.headers
    finally:
        settings.rate_limit_requests = original
        await redis_client.delete(_key(user_id))


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
        for _ in range(2):
            await _increment_and_check(user_a, redis_client)
        with pytest.raises(HTTPException):
            await _increment_and_check(user_a, redis_client)

        # user_b should still be unaffected
        await _increment_and_check(user_b, redis_client)  # should not raise
    finally:
        settings.rate_limit_requests = original
        await redis_client.delete(_key(user_a))
        await redis_client.delete(_key(user_b))


# ---------------------------------------------------------------------------
# 7d-5: HTTP integration — POST /jobs returns 429 when limit exceeded
# ---------------------------------------------------------------------------


async def test_create_job_rate_limited(client, mock_clerk_auth, redis_client, mock_jetstream):
    """POST /jobs returns 429 once the rate limit is exhausted."""
    headers = {"Authorization": "Bearer fake.jwt.token"}

    # Clear any existing rate limit key for the mock user from prior tests
    from sqlalchemy import select

    from app.core.db import AsyncSessionLocal
    from app.models.user import User

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.clerk_id == "user_test_123"))
        user = result.scalar_one_or_none()
    if user:
        await redis_client.delete(_key(user.id))

    original = settings.rate_limit_requests
    settings.rate_limit_requests = 2
    try:
        for _ in range(2):
            resp = await client.post("/jobs", json={"url": "https://example.com"}, headers=headers)
            assert resp.status_code == 201

        resp = await client.post("/jobs", json={"url": "https://example.com"}, headers=headers)
        assert resp.status_code == 429
    finally:
        settings.rate_limit_requests = original


# ---------------------------------------------------------------------------
# New: two concurrent requests with one slot left — only one gets through
# ---------------------------------------------------------------------------


async def test_rate_limit_concurrent_atomicity(redis_client):
    """Two concurrent requests fired when exactly one slot remains — only one is allowed."""
    from fastapi import HTTPException

    user_id = uuid.uuid4()
    original = settings.rate_limit_requests
    settings.rate_limit_requests = 5
    try:
        # Pre-fill to limit - 1 (4 requests)
        for _ in range(4):
            await _increment_and_check(user_id, redis_client)

        # Fire two concurrent requests — only one slot left
        results = await asyncio.gather(
            _increment_and_check(user_id, redis_client),
            _increment_and_check(user_id, redis_client),
            return_exceptions=True,
        )

        passes = [r for r in results if r is None]
        rejections = [r for r in results if isinstance(r, HTTPException) and r.status_code == 429]
        assert len(passes) == 1, "exactly one request should have been allowed"
        assert len(rejections) == 1, "exactly one request should have been rejected"
    finally:
        settings.rate_limit_requests = original
        await redis_client.delete(_key(user_id))


# ---------------------------------------------------------------------------
# New: entries age out after the window — counter resets naturally
# ---------------------------------------------------------------------------


async def test_rate_limit_window_reset(redis_client):
    """After the window elapses, old entries are evicted and new requests are allowed."""
    from fastapi import HTTPException

    user_id = uuid.uuid4()
    original_requests = settings.rate_limit_requests
    original_window = settings.rate_limit_window_seconds
    settings.rate_limit_requests = 2
    settings.rate_limit_window_seconds = 1  # 1-second window for a fast test
    try:
        # Exhaust the limit
        for _ in range(2):
            await _increment_and_check(user_id, redis_client)

        with pytest.raises(HTTPException) as exc_info:
            await _increment_and_check(user_id, redis_client)
        assert exc_info.value.status_code == 429

        # Wait for the 1-second window to pass
        await asyncio.sleep(1.1)

        # Should be allowed again — old entries have aged out
        await _increment_and_check(user_id, redis_client)
    finally:
        settings.rate_limit_requests = original_requests
        settings.rate_limit_window_seconds = original_window
        await redis_client.delete(_key(user_id))
