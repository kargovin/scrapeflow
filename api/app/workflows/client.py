from temporalio.client import Client

from app.settings import settings


async def connect() -> Client:
    # No disconnect() counterpart, unlike app/core/nats.py: a Temporal Client has no close or drain;
    # the connection is dropped when the process exits.
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
