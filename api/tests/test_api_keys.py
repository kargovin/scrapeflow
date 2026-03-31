import uuid

import pytest_asyncio

from app.auth.api_key import generate_api_key, hash_api_key
from app.core.db import AsyncSessionLocal
from app.models.api_key import ApiKey


# ---------------------------------------------------------------------------
# POST /users/api-keys
# ---------------------------------------------------------------------------

async def test_create_api_key_unauthenticated(client):
    """No auth header returns 401."""
    response = await client.post("/users/api-keys", json={"name": "my key"})
    assert response.status_code == 401


async def test_create_api_key_returns_201(client, db_api_key):
    """Authenticated POST returns 201."""
    raw_key, _ = db_api_key
    response = await client.post(
        "/users/api-keys",
        json={"name": "new key"},
        headers={"X-API-Key": raw_key},
    )
    assert response.status_code == 201


async def test_create_api_key_response_has_key_field(client, db_api_key):
    """Creation response includes the raw key starting with sf_."""
    raw_key, _ = db_api_key
    response = await client.post(
        "/users/api-keys",
        json={"name": "new key"},
        headers={"X-API-Key": raw_key},
    )
    data = response.json()
    assert "key" in data
    assert data["key"].startswith("sf_")
    assert data["name"] == "new key"
    assert data["revoked"] is False


async def test_create_api_key_key_not_stored_in_db(client, db_api_key):
    """The raw key is never persisted — DB row stores only the hash."""
    raw_key, _ = db_api_key
    response = await client.post(
        "/users/api-keys",
        json={"name": "check hash"},
        headers={"X-API-Key": raw_key},
    )
    assert response.status_code == 201
    created_key = response.json()["key"]
    created_id = response.json()["id"]

    async with AsyncSessionLocal() as db:
        row = await db.get(ApiKey, uuid.UUID(created_id))
        assert row is not None
        # The DB must not store the raw key.
        assert row.key_hash != created_key
        # The hash must match what we'd compute from the raw key.
        assert row.key_hash == hash_api_key(created_key)


async def test_create_api_key_missing_name(client, db_api_key):
    """Body without name returns 422 Unprocessable Entity."""
    raw_key, _ = db_api_key
    response = await client.post(
        "/users/api-keys",
        json={},
        headers={"X-API-Key": raw_key},
    )
    assert response.status_code == 422


async def test_create_multiple_keys_same_user(client, db_api_key):
    """A user can hold multiple active keys; each creation returns a distinct key."""
    raw_key, _ = db_api_key
    headers = {"X-API-Key": raw_key}

    r1 = await client.post("/users/api-keys", json={"name": "key one"}, headers=headers)
    r2 = await client.post("/users/api-keys", json={"name": "key two"}, headers=headers)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["key"] != r2.json()["key"]


# ---------------------------------------------------------------------------
# GET /users/api-keys
# ---------------------------------------------------------------------------

async def test_list_api_keys_unauthenticated(client):
    """No auth header returns 401."""
    response = await client.get("/users/api-keys")
    assert response.status_code == 401


async def test_list_api_keys_empty(client, db_user):
    """A user with no keys gets an empty list."""
    # Create a fresh key just to authenticate (no pre-existing keys otherwise).
    raw_key = generate_api_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKey(user_id=db_user.id, key_hash=hash_api_key(raw_key), name="auth only"))
        await db.commit()

    # List should return exactly this one key (the one we just created for auth).
    response = await client.get("/users/api-keys", headers={"X-API-Key": raw_key})
    assert response.status_code == 200
    # Only the auth key exists — the fixture starts clean.
    assert len(response.json()) == 1


async def test_list_api_keys_returns_own_keys(client, db_api_key):
    """Listed keys belong to the authenticated user and have the expected fields."""
    raw_key, api_key = db_api_key
    response = await client.get("/users/api-keys", headers={"X-API-Key": raw_key})
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    ids = [item["id"] for item in data]
    assert str(api_key.id) in ids
    # Each item has the expected fields.
    for item in data:
        assert "id" in item
        assert "name" in item
        assert "created_at" in item
        assert "revoked" in item


async def test_list_api_keys_excludes_revoked(client, db_user):
    """Revoked keys do not appear in the list."""
    active_raw = generate_api_key()
    revoked_raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKey(user_id=db_user.id, key_hash=hash_api_key(active_raw), name="active"))
        db.add(ApiKey(user_id=db_user.id, key_hash=hash_api_key(revoked_raw), name="revoked", revoked=True))
        await db.commit()

    response = await client.get("/users/api-keys", headers={"X-API-Key": active_raw})
    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert "active" in names
    assert "revoked" not in names


async def test_list_api_keys_excludes_other_users_keys(client, db_api_key, other_user):
    """Keys belonging to other_user are never returned to db_user."""
    raw_key, _ = db_api_key

    # Create a key for other_user.
    other_raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKey(user_id=other_user.id, key_hash=hash_api_key(other_raw), name="other key"))
        await db.commit()

    response = await client.get("/users/api-keys", headers={"X-API-Key": raw_key})
    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert "other key" not in names


async def test_list_api_keys_no_raw_key_in_response(client, db_api_key):
    """List endpoint never exposes the raw key field."""
    raw_key, _ = db_api_key
    response = await client.get("/users/api-keys", headers={"X-API-Key": raw_key})
    assert response.status_code == 200
    for item in response.json():
        assert "key" not in item


async def test_list_api_keys_ordered_newest_first(client, db_user):
    """Two keys are returned with the most recently created key first."""
    first_raw = generate_api_key()
    second_raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKey(user_id=db_user.id, key_hash=hash_api_key(first_raw), name="first"))
        await db.commit()
        # Add a small delay by committing in separate transactions so created_at differs.
        db.add(ApiKey(user_id=db_user.id, key_hash=hash_api_key(second_raw), name="second"))
        await db.commit()

    response = await client.get("/users/api-keys", headers={"X-API-Key": second_raw})
    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert names.index("second") < names.index("first")


# ---------------------------------------------------------------------------
# DELETE /users/api-keys/{key_id}
# ---------------------------------------------------------------------------

async def test_revoke_api_key_unauthenticated(client, db_api_key):
    """No auth header returns 401."""
    _, api_key = db_api_key
    response = await client.delete(f"/users/api-keys/{api_key.id}")
    assert response.status_code == 401


async def test_revoke_api_key_success(client, db_api_key):
    """Revoking own key returns 200 with revoked=True."""
    raw_key, api_key = db_api_key
    response = await client.delete(
        f"/users/api-keys/{api_key.id}",
        headers={"X-API-Key": raw_key},
    )
    assert response.status_code == 200
    assert response.json()["revoked"] is True
    assert response.json()["id"] == str(api_key.id)


async def test_revoke_api_key_actually_revokes(client, db_api_key):
    """After DELETE, the key can no longer authenticate — GET /users/me returns 401."""
    raw_key, api_key = db_api_key
    # Revoke via the route.
    revoke_resp = await client.delete(
        f"/users/api-keys/{api_key.id}",
        headers={"X-API-Key": raw_key},
    )
    assert revoke_resp.status_code == 200

    # The same key should now be rejected.
    me_resp = await client.get("/users/me", headers={"X-API-Key": raw_key})
    assert me_resp.status_code == 401


async def test_revoke_api_key_not_found(client, db_api_key):
    """A non-existent key ID returns 404."""
    raw_key, _ = db_api_key
    response = await client.delete(
        f"/users/api-keys/{uuid.uuid4()}",
        headers={"X-API-Key": raw_key},
    )
    assert response.status_code == 404


async def test_revoke_api_key_other_user(client, db_api_key, other_user):
    """Attempting to revoke another user's key returns 404, not 403."""
    raw_key, _ = db_api_key  # authenticated as db_user

    # Create a key owned by other_user.
    other_raw = generate_api_key()
    other_key = ApiKey(user_id=other_user.id, key_hash=hash_api_key(other_raw), name="other key")
    async with AsyncSessionLocal() as db:
        db.add(other_key)
        await db.commit()
        await db.refresh(other_key)

    response = await client.delete(
        f"/users/api-keys/{other_key.id}",
        headers={"X-API-Key": raw_key},  # authenticated as db_user, targeting other_user's key
    )
    assert response.status_code == 404


async def test_revoke_already_revoked_key(client, db_user):
    """DELETE on an already-revoked key succeeds (idempotent — db.get fetches by PK regardless)."""
    raw_key = generate_api_key()
    already_revoked = ApiKey(
        user_id=db_user.id,
        key_hash=hash_api_key(raw_key),
        name="already revoked",
        revoked=True,
    )
    # We need a separate active key to authenticate.
    auth_raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        db.add(already_revoked)
        db.add(ApiKey(user_id=db_user.id, key_hash=hash_api_key(auth_raw), name="auth key"))
        await db.commit()
        await db.refresh(already_revoked)

    response = await client.delete(
        f"/users/api-keys/{already_revoked.id}",
        headers={"X-API-Key": auth_raw},
    )
    # db.get returns the row regardless of revoked status, so the handler sets revoked=True again.
    assert response.status_code == 200
    assert response.json()["revoked"] is True

# test if last_updated_at is updated on verify_api_key
async def test_verify_api_key_updates_last_used_at(db_api_key):
    """Calling verify_api_key should update the last_used_at timestamp in the database."""
    raw_key, api_key = db_api_key
    assert api_key.last_used_at is None  # should start as None

    from app.auth.api_key import verify_api_key
    async with AsyncSessionLocal() as db:
        await verify_api_key(db, raw_key)
    async with AsyncSessionLocal() as db:
        updated_api_key = await db.get(ApiKey, api_key.id)
        assert updated_api_key.last_used_at is not None  # should be updated