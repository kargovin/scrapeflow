"""Per-host limiter activities — registered on the workflow-worker queue. Both fail open."""

import redis.asyncio as aioredis
import structlog
from redis.exceptions import RedisError
from temporalio import activity

from app.core import host_limiter
from app.core.domains import registrable_domain
from app.settings import settings
from app.workflows.steps import (
    ACQUIRE_HOST_SLOT_ACTIVITY,
    RELEASE_HOST_SLOT_ACTIVITY,
    HostSlot,
    HostSlotInput,
    HostSlotRelease,
)

logger = structlog.get_logger()

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _redis


@activity.defn(name=ACQUIRE_HOST_SLOT_ACTIVITY)
async def acquire_host_slot(input: HostSlotInput) -> HostSlot:
    try:
        domain = registrable_domain(input.url)
    except ValueError:
        logger.error("host_limiter_no_host", url=input.url)
        return HostSlot()
    try:
        wait_ms = await host_limiter.acquire(
            get_redis(), domain, input.lease_id, input.lease_ttl_ms
        )
    except RedisError as exc:
        logger.error("host_limiter_unavailable", domain=domain, error=str(exc))
        return HostSlot()
    return HostSlot(domain=domain, wait_ms=wait_ms)


@activity.defn(name=RELEASE_HOST_SLOT_ACTIVITY)
async def release_host_slot(input: HostSlotRelease) -> None:
    try:
        await host_limiter.release(get_redis(), input.domain, input.lease_id)
    except RedisError as exc:
        # The lease expires on its own.
        logger.error("host_limiter_release_failed", domain=input.domain, error=str(exc))
