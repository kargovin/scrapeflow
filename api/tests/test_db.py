from sqlalchemy import text
from app.core.db import AsyncSessionLocal


async def test_db_connection():
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1
