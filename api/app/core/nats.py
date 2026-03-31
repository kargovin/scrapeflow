import nats
from nats.aio.client import Client
from nats.js import JetStreamContext
from fastapi import Request
from app.settings import settings


async def connect() -> tuple[Client, JetStreamContext]:
    nc = await nats.connect(settings.nats_url)
    js = nc.jetstream()
    return nc, js


async def disconnect(nc: Client) -> None:
    if nc and not nc.is_closed:
        await nc.drain()


def get_nats(request: Request) -> Client:
    return request.app.state.nats_client
    


def get_jetstream(request: Request) -> JetStreamContext:
    return request.app.state.nats_js