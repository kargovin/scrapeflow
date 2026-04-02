import uuid


async def test_correlation_id_middleware_no_X_request_id_sent(client):
    response = await client.get("/health")
    assert "X-Request-ID" in response.headers
    uuid.UUID(response.headers["X-Request-ID"])  # should be a valid UUID


async def test_correlation_id_middleware_X_request_id_sent(client):
    custom_request_id = str(uuid.uuid4())
    response = await client.get("/health", headers={"X-Request-ID": custom_request_id})
    assert response.headers["X-Request-ID"] == custom_request_id
