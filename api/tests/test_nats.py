from app.core.nats import get_nats, get_jetstream


async def test_nats_connected():
    nc = get_nats()
    assert nc.is_connected


async def test_jetstream_accessible():
    js = get_jetstream()
    assert js is not None
