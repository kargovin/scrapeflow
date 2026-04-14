import hashlib
import secrets
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.api_key import ApiKey
from app.models.user import User

# Prefix makes it easy to identify ScrapeFlow API keys
API_KEY_PREFIX = "sf_"


def generate_api_key() -> str:
    """Generate a new random API key. Shown to the user once — never stored in plain text."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """Hash an API key for storage. SHA-256 is sufficient here since keys are random."""
    return hashlib.sha256(key.encode()).hexdigest()


async def verify_api_key(db: AsyncSession, raw_key: str) -> User | None:
    """Look up an API key by its hash and return the associated user.

    Returns None if the key doesn't exist or is revoked.
    """
    key_hash = hash_api_key(raw_key)

    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.key_hash == key_hash)
        .where(ApiKey.revoked == False)  # noqa: E712
        .options(selectinload(ApiKey.user))
    )
    api_key = result.scalar_one_or_none()

    if api_key is None:
        return None

    await db.execute(
        update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=datetime.now(UTC))
    )
    await db.commit()

    return api_key.user
