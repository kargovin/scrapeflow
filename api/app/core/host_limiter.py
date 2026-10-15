import random

import redis.asyncio as aioredis

from app.settings import settings

# Per-host politeness toward scrape targets, keyed on the registrable domain.
#
# KEYS[1] host:<domain>:leases — sorted set, member = lease id, score = lease expiry (ms)
# KEYS[2] host:<domain>:last   — present while the last admitted start is < interval ago
#
# Returns 0 = admitted (or already held: a retried acquire), -1 = all slots in flight,
# n > 0 = ms until the rate window reopens. Time is Redis's, so every caller shares one clock.
_ACQUIRE_SCRIPT = """
local t        = redis.call('TIME')
local now      = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local lease_id = ARGV[1]
local ttl      = tonumber(ARGV[2])
local limit    = tonumber(ARGV[3])
local interval = tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], lease_id) then
    return 0
end
if redis.call('ZCARD', KEYS[1]) >= limit then
    return -1
end
local wait = redis.call('PTTL', KEYS[2])
if wait > 0 then
    return wait
end

redis.call('ZADD', KEYS[1], now + ttl, lease_id)
local last = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
redis.call('PEXPIRE', KEYS[1], tonumber(last[2]) - now)
if interval > 0 then
    redis.call('SET', KEYS[2], lease_id, 'PX', interval)
end
return 0
"""

BUSY_POLL_MS = 1000
JITTER_MS = 250


def _keys(domain: str) -> tuple[str, str]:
    return f"host:{domain}:leases", f"host:{domain}:last"


async def acquire(
    redis: aioredis.Redis,
    domain: str,
    lease_id: str,
    lease_ttl_ms: int,
    *,
    max_in_flight: int | None = None,
    interval_ms: int | None = None,
) -> int:
    """Takes a slot on ``domain``: 0 = go, else ms to wait before asking again."""
    result = int(
        await redis.eval(
            _ACQUIRE_SCRIPT,
            2,
            *_keys(domain),
            lease_id,
            lease_ttl_ms,
            settings.host_limit_in_flight if max_in_flight is None else max_in_flight,
            settings.host_limit_interval_ms if interval_ms is None else interval_ms,
        )
    )
    if result == 0:
        return 0
    wait = BUSY_POLL_MS if result < 0 else result
    return wait + random.randint(0, JITTER_MS)


async def release(redis: aioredis.Redis, domain: str, lease_id: str) -> None:
    await redis.zrem(_keys(domain)[0], lease_id)
