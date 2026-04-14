from app.main import app


async def test_nats_connected():
    assert app.state.nats_client.is_connected


async def test_jetstream_accessible():
    assert app.state.nats_js is not None
