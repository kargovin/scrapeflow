from collections.abc import AsyncGenerator

import redis.asyncio as redis
from fastapi import Request
from app.settings import settings


def create_pool() -> redis.ConnectionPool:
    return redis.ConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=settings.redis_max_connections,
    )


async def close_pool(pool: redis.ConnectionPool) -> None: 
    await pool.aclose()


async def get_redis(request: Request) -> AsyncGenerator[redis.Redis, None]:
    pool = request.app.state.redis_pool
    async with redis.Redis(connection_pool=pool) as client:
        yield client
