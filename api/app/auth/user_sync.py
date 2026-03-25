from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_clerk
from app.models.user import User


async def get_or_create_user(db: AsyncSession, payload: dict) -> User:
    """Look up a user by Clerk ID, creating them if they don't exist yet.

    On first login, fetches full user profile from Clerk API to get email.
    On subsequent requests, returns the cached local user record.
    """
    clerk_id = payload["sub"]

    # Try to find existing user
    result = await db.execute(select(User).where(User.clerk_id == clerk_id))
    user = result.scalar_one_or_none()

    if user is None:
        # First login — fetch full profile from Clerk to get email
        clerk = get_clerk()
        clerk_user = clerk.users.get(user_id=clerk_id)
        email = clerk_user.email_addresses[0].email_address if clerk_user.email_addresses else ""

        user = User(clerk_id=clerk_id, email=email)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return user
