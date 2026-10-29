"""I.1 — `alembic upgrade` waits on the migration lock, so replicas starting together migrate one at a time.

The test holds the lock from outside and runs an upgrade at head (a no-op once it has the lock).
"""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.db import MIGRATION_LOCK_KEY, AsyncSessionLocal

HELD = 2  # well past an unblocked upgrade at head
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _upgrade() -> None:
    # No config file, so env.py skips fileConfig and leaves the test process's logging alone.
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    command.upgrade(cfg, "head")


async def test_upgrade_waits_for_the_migration_lock():
    async with AsyncSessionLocal() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
        )
        upgrade = asyncio.create_task(asyncio.to_thread(_upgrade))
        await asyncio.sleep(HELD)
        assert not upgrade.done(), "upgrade ran while another process held the migration lock"
        await holder.rollback()

    await asyncio.wait_for(upgrade, timeout=30)


async def test_upgrade_without_contention_does_not_wait():
    await asyncio.wait_for(asyncio.to_thread(_upgrade), timeout=HELD)
