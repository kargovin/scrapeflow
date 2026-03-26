import uuid
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from app.auth.api_key import generate_api_key, hash_api_key, API_KEY_PREFIX
from app.core.db import AsyncSessionLocal
from app.models.api_key import ApiKey
from app.models.user import User


async def test_me_unauthenticated(client):
    """GET /users/me without a token returns 401."""
    response = await client.get("/users/me")
    assert response.status_code == 401


async def test_me_authenticated(client, mock_clerk_auth):
    """GET /users/me with a valid token returns the user."""
    response = await client.get(
        "/users/me",
        headers={"Authorization": "Bearer fake.jwt.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
    assert "id" in data
    assert "created_at" in data


async def test_me_creates_user_on_first_login(client, mock_clerk_auth):
    """First login creates a new user in the DB."""
    from sqlalchemy import select

    response = await client.get(
        "/users/me",
        headers={"Authorization": "Bearer fake.jwt.token"},
    )
    assert response.status_code == 200

    # Verify the user was persisted in the DB
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.clerk_id == "user_test_123"))
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.email == "test@example.com"


# --- API key unit tests ---

def test_generate_api_key_format():
    """Generated key has the sf_ prefix."""
    key = generate_api_key()
    assert key.startswith(API_KEY_PREFIX)
    assert len(key) > len(API_KEY_PREFIX)


def test_hash_api_key_deterministic():
    """Same key always produces the same hash."""
    key = generate_api_key()
    assert hash_api_key(key) == hash_api_key(key)


def test_hash_api_key_unique():
    """Different keys produce different hashes."""
    assert hash_api_key(generate_api_key()) != hash_api_key(generate_api_key())


# --- API key auth integration tests ---


async def test_api_key_auth_valid(client, db_user):
    """Valid API key authenticates the user."""
    raw_key = generate_api_key()
    api_key = ApiKey(user_id=db_user.id, key_hash=hash_api_key(raw_key), name="test key")
    async with AsyncSessionLocal() as db:
        db.add(api_key)
        await db.commit()

    response = await client.get("/users/me", headers={"X-API-Key": raw_key})
    assert response.status_code == 200
    assert response.json()["email"] == db_user.email


async def test_api_key_auth_invalid(client):
    """Unknown API key returns 401."""
    response = await client.get("/users/me", headers={"X-API-Key": "sf_doesnotexist"})
    assert response.status_code == 401


async def test_api_key_auth_revoked(client, db_user):
    """Revoked API key returns 401."""
    raw_key = generate_api_key()
    api_key = ApiKey(user_id=db_user.id, key_hash=hash_api_key(raw_key), name="revoked key", revoked=True)
    async with AsyncSessionLocal() as db:
        db.add(api_key)
        await db.commit()

    response = await client.get("/users/me", headers={"X-API-Key": raw_key})
    assert response.status_code == 401
