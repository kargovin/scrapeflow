import asyncio

from fastapi import HTTPException, Request, status
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from app.settings import settings

_connect_lock = asyncio.Lock()


async def connect() -> Client:
    # No disconnect() counterpart, unlike app/core/nats.py: a Temporal Client has no close or drain;
    # the connection is dropped when the process exits.
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )


async def get_temporal(request: Request) -> Client:
    """Lazy: connects on first use, so a Temporal outage fails pipeline runs, not API startup."""
    state = request.app.state
    if getattr(state, "temporal", None) is None:
        async with _connect_lock:
            if getattr(state, "temporal", None) is None:
                try:
                    state.temporal = await connect()
                except Exception as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Workflow engine unavailable",
                    ) from exc
    return state.temporal


async def get_temporal_or_none(request: Request) -> Client | None:
    """For a caller with a fallback when the engine is down (the pipeline cancel)."""
    try:
        return await get_temporal(request)
    except HTTPException:
        return None
