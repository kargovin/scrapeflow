from app.core.redis import get_redis


async def test_redis_ping():
    async for client in get_redis():
        result = await client.ping()
        assert result is True
