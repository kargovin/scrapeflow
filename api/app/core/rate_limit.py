import uuid

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, status

from app.auth.dependencies import get_current_user
from app.core.redis import get_redis
from app.models.user import User
from app.settings import settings


async def check_rate_limit(
    user: User = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
) -> None:
    """FastAPI dependency — raises 429 if the current user exceeds the rate limit."""
    await _increment_and_check(user.id, redis)


async def _increment_and_check(user_id: uuid.UUID, redis: aioredis.Redis) -> None:
    """
    Fixed-window rate limiter.

    Key: scrapeflow:rl:<user_id>:<window>
    - INCR the counter for this user in the current window.
    - On first increment, set TTL = window_seconds (key auto-expires).
    - Raise HTTP 429 if counter exceeds the configured limit.
    """
    window = _current_window()
    key = f"scrapeflow:rl:{user_id}:{window}"

    count = await redis.incr(key)
    if count == 1:
        # First request in this window — set expiry so the key cleans itself up
        await redis.expire(key, settings.rate_limit_window_seconds)

    if count > settings.rate_limit_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: {settings.rate_limit_requests} requests "
                   f"per {settings.rate_limit_window_seconds}s",
        )


def _current_window() -> int:
    """Return the current fixed-window bucket (Unix epoch // window_seconds)."""
    import time
    return int(time.time()) // settings.rate_limit_window_seconds
