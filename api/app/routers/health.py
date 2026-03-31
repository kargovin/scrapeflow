from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
import redis.asyncio as aioredis
import importlib.metadata
from sqlalchemy import text

from app.core.db import AsyncSessionLocal

router = APIRouter(prefix="/health", tags=["health"])

try:
    _VERSION = importlib.metadata.version("scrapeflow-api")
except importlib.metadata.PackageNotFoundError:
    _VERSION = "dev"

class HealthResponse(BaseModel):
    status: str
    version: str

class ReadinessResponse(BaseModel):
    status: str
    db: str
    redis: str
    nats: str

@router.get("", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", version=_VERSION)

@router.get("/ready", response_model=ReadinessResponse)
async def readiness(request: Request, response: Response):
    output = ReadinessResponse(status="ok", db="unknown", redis="unknown", nats="unknown")
    try:
        # Check DB connectivity
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        output.db = "ok"
    except Exception as e:
        output.db = f"error: {str(e)}"

    try:
        # Check Redis connectivity
        async with aioredis.Redis(connection_pool=request.app.state.redis_pool) as client:
            await client.ping()
        output.redis = "ok"
    except Exception as e:
        output.redis = f"error: {str(e)}"

    try:
        # Check Nats connectivity
        if request.app.state.nats_client.is_connected:
            output.nats = "ok"
        else:
            output.nats = "error: not connected"
    except Exception as e:
        output.nats = f"error: {str(e)}"

    if any(v != 'ok' for v in [output.db, output.redis, output.nats]):
        output.status = "degraded"
        response.status_code = 503

    return output
