import redis.asyncio as aioredis

from app.main import app


async def test_redis_ping():
    async with aioredis.Redis(connection_pool=app.state.redis_pool) as client:
        result = await client.ping()
        assert result is True
