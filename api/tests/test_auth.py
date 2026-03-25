from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_clerk_auth(client):
    """Patch Clerk's authenticate_request to return a fake valid payload."""
    mock_state = MagicMock()
    mock_state.is_signed_in = True
    mock_state.payload = {
        "sub": "user_test_123",
        "email": "test@example.com",
    }

    # Also mock clerk.users.get so user_sync doesn't call the real Clerk API
    mock_clerk_user = MagicMock()
    mock_clerk_user.email_addresses = [MagicMock(email_address="test@example.com")]

    mock_clerk_instance = MagicMock()
    mock_clerk_instance.authenticate_request.return_value = mock_state
    mock_clerk_instance.users.get.return_value = mock_clerk_user

    # Patch _clerk directly so both jwt.py and user_sync.py get the mock instance
    with patch("app.auth.jwt._clerk", mock_clerk_instance):
        yield


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
    from app.core.db import AsyncSessionLocal
    from app.models.user import User

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
