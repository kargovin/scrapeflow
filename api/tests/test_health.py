import asyncio

from app.main import app


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


async def test_readiness(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert data["redis"] == "ok"
    assert data["nats"] == "ok"


async def test_readiness_excludes_minio(client):
    """MinIO is not a serving dependency — it must not appear on the probe endpoint."""
    response = await client.get("/health/ready")
    assert "minio" not in response.json()


async def test_dependencies(client):
    response = await client.get("/health/deps")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert data["redis"] == "ok"
    assert data["nats"] == "ok"
    assert data["minio"] == "ok"


async def test_dependencies_minio_down(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(app.state.minio, "bucket_exists", boom)

    response = await client.get("/health/deps")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["minio"].startswith("error: ")
    # the other dependencies are still reported truthfully
    assert data["db"] == "ok"


async def test_dependencies_minio_missing_bucket(client, monkeypatch):
    async def no_bucket(*args, **kwargs):
        return False

    monkeypatch.setattr(app.state.minio, "bucket_exists", no_bucket)

    response = await client.get("/health/deps")
    assert response.status_code == 503
    assert "not found" in response.json()["minio"]


async def test_dependencies_minio_timeout(client, monkeypatch):
    from app.routers import health

    monkeypatch.setattr(health, "_MINIO_CHECK_TIMEOUT_SECONDS", 0.01)

    async def hang(*args, **kwargs):
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(app.state.minio, "bucket_exists", hang)

    response = await client.get("/health/deps")
    assert response.status_code == 503
    assert "timeout" in response.json()["minio"]


async def test_readiness_unaffected_by_minio(client, monkeypatch):
    """A MinIO outage must not take the API pod out of the k8s Service."""

    async def boom(*args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(app.state.minio, "bucket_exists", boom)

    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
