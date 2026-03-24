import nats
from nats.aio.client import Client
from nats.js import JetStreamContext

from app.settings import settings

# Module-level connections — created once at startup, shared across all requests
_nc: Client | None = None
_js: JetStreamContext | None = None


async def connect() -> tuple[Client, JetStreamContext]:
    global _nc, _js
    _nc = await nats.connect(settings.nats_url)
    _js = _nc.jetstream()
    return _nc, _js


async def disconnect() -> None:
    global _nc, _js
    if _nc and not _nc.is_closed:
        await _nc.drain()
        _nc = None
        _js = None


def get_nats() -> Client:
    assert _nc is not None, "NATS client not initialized — call connect() at startup"
    return _nc


def get_jetstream() -> JetStreamContext:
    assert _js is not None, "JetStream not initialized — call connect() at startup"
    return _js
