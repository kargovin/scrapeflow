import asyncio
import importlib.metadata

import redis.asyncio as aioredis
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from app.core.db import AsyncSessionLocal
from app.settings import settings

router = APIRouter(prefix="/health", tags=["health"])

try:
    _VERSION = importlib.metadata.version("scrapeflow-api")
except importlib.metadata.PackageNotFoundError:
    _VERSION = "dev"

# MinIO is the only dependency check that makes a live network round-trip, so it
# gets an explicit ceiling — a hung object store must not hang the endpoint.
_MINIO_CHECK_TIMEOUT_SECONDS = 3.0


class HealthResponse(BaseModel):
    status: str
    version: str


class ReadinessResponse(BaseModel):
    """Serving readiness — only dependencies the API cannot answer a request without.

    This is what the k8s readinessProbe hits, so a dependency listed here going
    down takes the pod out of the Service. MinIO is deliberately absent: it is
    needed to store and fetch scrape output, not to serve /jobs, auth, or the
    admin panel. See /health/deps for the full picture.
    """

    status: str
    db: str
    redis: str
    nats: str


class DependencyResponse(ReadinessResponse):
    """Full dependency report — diagnostics, not a probe. Nothing routes on this."""

    minio: str


async def _check_db() -> str:
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return "ok"
    except Exception as e:
        return f"error: {e!s}"


async def _check_redis(request: Request) -> str:
    try:
        async with aioredis.Redis(connection_pool=request.app.state.redis_pool) as client:
            await client.ping()
        return "ok"
    except Exception as e:
        return f"error: {e!s}"


async def _check_nats(request: Request) -> str:
    try:
        if request.app.state.nats_client.is_connected:
            return "ok"
        return "error: not connected"
    except Exception as e:
        return f"error: {e!s}"


async def _check_minio(request: Request) -> str:
    # bucket_exists, not list_buckets: it exercises the bucket we actually write
    # results to, and needs no account-wide listing permission.
    try:
        exists = await asyncio.wait_for(
            request.app.state.minio.bucket_exists(settings.minio_bucket),
            timeout=_MINIO_CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return f"error: timeout after {_MINIO_CHECK_TIMEOUT_SECONDS}s"
    except Exception as e:
        return f"error: {e!s}"
    if not exists:
        return f"error: bucket {settings.minio_bucket!r} not found"
    return "ok"


@router.get("", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", version=_VERSION)


@router.get("/ready", response_model=ReadinessResponse)
async def readiness(request: Request, response: Response):
    output = ReadinessResponse(
        status="ok",
        db=await _check_db(),
        redis=await _check_redis(request),
        nats=await _check_nats(request),
    )

    if any(v != "ok" for v in [output.db, output.redis, output.nats]):
        output.status = "degraded"
        response.status_code = 503

    return output


@router.get("/deps", response_model=DependencyResponse)
async def dependencies(request: Request, response: Response):
    output = DependencyResponse(
        status="ok",
        db=await _check_db(),
        redis=await _check_redis(request),
        nats=await _check_nats(request),
        minio=await _check_minio(request),
    )

    if any(v != "ok" for v in [output.db, output.redis, output.nats, output.minio]):
        output.status = "degraded"
        response.status_code = 503

    return output
