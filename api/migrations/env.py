import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import MIGRATION_LOCK_KEY, Base
from app.settings import settings

# Alembic Config object — provides access to alembic.ini values
config = context.config

# Set up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so they register themselves on Base.metadata
# Alembic inspects this to detect schema changes for autogenerate
import app.models  # noqa: F401, E402

target_metadata = Base.metadata

logger = logging.getLogger("alembic.env")


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL only)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _take_migration_lock(connection) -> None:
    # Transaction-scoped: released at commit or rollback, or when a dead process's connection drops.
    # Must be taken before run_migrations() reads alembic_version, so a waiter sees the winner's head.
    params = {"key": MIGRATION_LOCK_KEY}
    if connection.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), params).scalar():
        return
    logger.info("Another process holds the migration lock; waiting for it")
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), params)
    logger.info("Migration lock acquired")


def do_run_migrations(connection) -> None:
    _take_migration_lock(connection)
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations with a live async DB connection."""
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
