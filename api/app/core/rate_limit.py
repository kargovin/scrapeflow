import time
import uuid

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, status

from app.auth.dependencies import get_current_user
from app.core.redis import get_redis
from app.models.user import User
from app.settings import settings

# Sliding window rate limiter using a Redis sorted set.
#
# Each member is "<now_ms>-<random>" so two requests arriving in the same
# millisecond get distinct entries. The score is the request timestamp in ms,
# which is what ZREMRANGEBYSCORE uses to evict stale entries.
#
# Returns a 2-element array:
#   [1, 0]            — request allowed
#   [0, oldest_ms]    — rate limited; oldest_ms is used to compute Retry-After
_SLIDING_WINDOW_SCRIPT = """
local now        = tonumber(ARGV[1])
local window_ms  = tonumber(ARGV[2])
local limit      = tonumber(ARGV[3])
local ttl        = tonumber(ARGV[4])
local cutoff     = now - window_ms

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local count = redis.call('ZCARD', KEYS[1])

if count >= limit then
    local oldest     = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    local oldest_ms  = tonumber(oldest[2]) or now
    return {0, oldest_ms}
end

redis.call('ZADD', KEYS[1], now, now .. '-' .. math.random(1000000))
redis.call('EXPIRE', KEYS[1], ttl)
return {1, 0}
"""


async def check_rate_limit(
    user: User = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
) -> None:
    """FastAPI dependency — raises 429 if the current user exceeds the rate limit."""
    await _increment_and_check(user.id, redis)


async def _increment_and_check(user_id: uuid.UUID, redis: aioredis.Redis) -> None:
    """
    Sliding-window rate limiter.

    Key: rate:user:<user_id>  (Redis sorted set)
    - Remove entries outside the current window (ZREMRANGEBYSCORE).
    - Count remaining entries (ZCARD).
    - If at limit: raise HTTP 429 with accurate Retry-After.
    - Otherwise: add this request's timestamp as a new member (ZADD).
    """
    now_ms = int(time.time() * 1000)
    window_ms = settings.rate_limit_window_seconds * 1000
    ttl_seconds = settings.rate_limit_window_seconds * 2
    key = f"rate:user:{user_id}"

    result = await redis.eval(
        _SLIDING_WINDOW_SCRIPT,
        1,
        key,
        now_ms,
        window_ms,
        settings.rate_limit_requests,
        ttl_seconds,
    )

    allowed, oldest_ms = int(result[0]), int(result[1])

    if not allowed:
        retry_after = max(1, (oldest_ms + window_ms - now_ms) // 1000)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: {settings.rate_limit_requests} requests "
            f"per {settings.rate_limit_window_seconds}s",
            headers={"Retry-After": str(retry_after)},
        )
