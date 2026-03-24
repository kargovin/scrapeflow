from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.settings import settings

# Module-level pool — created once at startup, shared across all requests
_pool: redis.ConnectionPool | None = None


def create_pool() -> redis.ConnectionPool:
    global _pool
    _pool = redis.ConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=settings.redis_max_connections,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None


async def get_redis() -> AsyncGenerator[redis.Redis, None]:
    assert _pool is not None, "Redis pool not initialized — call create_pool() at startup"
    async with redis.Redis(connection_pool=_pool) as client:
        yield client
