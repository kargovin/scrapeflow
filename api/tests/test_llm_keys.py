import uuid

from app.core.db import AsyncSessionLocal
from app.models.llm_keys import UserLLMKey

# ---------------------------------------------------------------------------
# POST /users/llm-keys
# ---------------------------------------------------------------------------


async def test_create_llm_key(client, db_api_key):
    """POST /users/llm-keys returns 201 with a masked api_key."""
    raw_key, _ = db_api_key
    resp = await client.post(
        "/users/llm-keys",
        json={"name": "my key", "provider": "anthropic", "api_key": "sk-ant-testkey"},
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my key"
    assert data["provider"] == "anthropic"
    # api_key must be masked — first 4 chars + ***** suffix, never the full key
    assert data["api_key"] == "sk-a*****"
    assert "testkey" not in data["api_key"]


async def test_create_llm_key_with_base_url(client, db_api_key):
    """POST /users/llm-keys stores and returns base_url for openai_compatible keys."""
    raw_key, _ = db_api_key
    resp = await client.post(
        "/users/llm-keys",
        json={
            "name": "local llm",
            "provider": "openai_compatible",
            "api_key": "localkey123",
            "base_url": "https://api.openai.com/v1",
        },
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 201
    assert resp.json()["base_url"] == "https://api.openai.com/v1"


async def test_create_llm_key_short_api_key(client, db_api_key):
    """api_key shorter than 8 characters is rejected with 422."""
    raw_key, _ = db_api_key
    resp = await client.post(
        "/users/llm-keys",
        json={"name": "bad key", "provider": "anthropic", "api_key": "short"},
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 422


async def test_create_llm_key_ssrf_base_url(client, db_api_key):
    """base_url resolving to a private address is rejected."""
    raw_key, _ = db_api_key
    resp = await client.post(
        "/users/llm-keys",
        json={
            "name": "ssrf attempt",
            "provider": "openai_compatible",
            "api_key": "sk-ant-testkey",
            "base_url": "http://127.0.0.1/v1",
        },
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 400


async def test_create_llm_key_unauthenticated(client):
    """No auth header returns 401."""
    resp = await client.post(
        "/users/llm-keys",
        json={"name": "key", "provider": "anthropic", "api_key": "sk-ant-testkey"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /users/llm-keys
# ---------------------------------------------------------------------------


async def test_list_llm_keys(client, db_api_key):
    """GET /users/llm-keys returns a list with no api_key field in items."""
    raw_key, _ = db_api_key
    # Create a key first
    await client.post(
        "/users/llm-keys",
        json={"name": "listed key", "provider": "anthropic", "api_key": "sk-ant-testkey"},
        headers={"X-API-Key": raw_key},
    )
    resp = await client.get("/users/llm-keys", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    for item in data:
        assert "api_key" not in item
        assert "id" in item
        assert "name" in item
        assert "provider" in item
        assert "created_at" in item


async def test_list_llm_keys_empty(client, db_api_key):
    """GET /users/llm-keys returns [] when the user has no keys."""
    raw_key, api_key = db_api_key
    # Use a fresh user that has no LLM keys — db_api_key user may have keys from
    # other tests, so create a brand-new user via a new API key fixture approach.
    # Instead, query and delete any existing LLM keys for this user first.
    async with AsyncSessionLocal() as db:
        from sqlalchemy import delete as sa_delete

        await db.execute(sa_delete(UserLLMKey).where(UserLLMKey.user_id == api_key.user_id))
        await db.commit()

    resp = await client.get("/users/llm-keys", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_llm_keys_isolation(client, db_api_key, other_user):
    """GET /users/llm-keys never returns keys owned by another user."""
    raw_key, _ = db_api_key
    # Insert an LLM key directly for other_user
    async with AsyncSessionLocal() as db:
        db.add(
            UserLLMKey(
                user_id=other_user.id,
                name="other user key",
                provider="anthropic",
                encrypted_api_key="encrypted-value",
            )
        )
        await db.commit()

    resp = await client.get("/users/llm-keys", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert "other user key" not in names


# ---------------------------------------------------------------------------
# DELETE /users/llm-keys/{id}
# ---------------------------------------------------------------------------


async def test_delete_llm_key(client, db_api_key):
    """DELETE /users/llm-keys/{id} returns 200 and removes the key from DB."""
    raw_key, _ = db_api_key
    create_resp = await client.post(
        "/users/llm-keys",
        json={"name": "to delete", "provider": "anthropic", "api_key": "sk-ant-testkey"},
        headers={"X-API-Key": raw_key},
    )
    assert create_resp.status_code == 201
    key_id = create_resp.json()["id"]

    del_resp = await client.delete(f"/users/llm-keys/{key_id}", headers={"X-API-Key": raw_key})
    assert del_resp.status_code == 200
    assert del_resp.json()["id"] == key_id

    # Verify it is actually gone from the DB
    async with AsyncSessionLocal() as db:
        row = await db.get(UserLLMKey, uuid.UUID(key_id))
        assert row is None


async def test_delete_llm_key_other_user(client, db_api_key, other_user):
    """DELETE on another user's key returns 404."""
    raw_key, _ = db_api_key
    # Create key owned by other_user directly
    other_key = UserLLMKey(
        user_id=other_user.id,
        name="other key",
        provider="anthropic",
        encrypted_api_key="encrypted-value",
    )
    async with AsyncSessionLocal() as db:
        db.add(other_key)
        await db.commit()
        await db.refresh(other_key)

    resp = await client.delete(f"/users/llm-keys/{other_key.id}", headers={"X-API-Key": raw_key})
    assert resp.status_code == 404


async def test_delete_llm_key_not_found(client, db_api_key):
    """DELETE with a non-existent UUID returns 404."""
    raw_key, _ = db_api_key
    resp = await client.delete(f"/users/llm-keys/{uuid.uuid4()}", headers={"X-API-Key": raw_key})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /jobs — cross-tenant LLM key ownership check
# ---------------------------------------------------------------------------


async def test_create_job_llm_key_other_user(client, db_api_key, other_user, mock_jetstream):
    """POST /jobs returns 404 when llm_key_id belongs to a different user."""
    raw_key, _ = db_api_key
    # Create an LLM key owned by other_user
    other_key = UserLLMKey(
        user_id=other_user.id,
        name="other llm key",
        provider="anthropic",
        encrypted_api_key="encrypted-value",
    )
    async with AsyncSessionLocal() as db:
        db.add(other_key)
        await db.commit()
        await db.refresh(other_key)

    resp = await client.post(
        "/jobs",
        json={
            "url": "https://example.com",
            "llm_config": {
                "llm_key_id": str(other_key.id),
                "model": "claude-3-5-haiku-20241022",
                "output_schema": {"title": "str"},
            },
        },
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "LLM Key not found"
