"""migration_3_9_jobs_updated_at_trigger

Revision ID: ebbcc72c1472
Revises: c26955897e1d
Create Date: 2026-04-17 15:59:56.194642

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ebbcc72c1472"
down_revision: str | Sequence[str] | None = "c26955897e1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # hand-written: autogenerate cannot produce trigger DDL
    # onupdate silently skips db.execute(update(...)) paths (scheduler, cancel route)
    # asyncpg does not allow multiple statements in one execute — split into two calls
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION set_jobs_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    )
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_jobs_updated_at
        BEFORE UPDATE ON jobs
        FOR EACH ROW EXECUTE FUNCTION set_jobs_updated_at()
    """)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        sa.text("""
        DROP TRIGGER IF EXISTS trg_jobs_updated_at ON jobs;
        DROP FUNCTION IF EXISTS set_jobs_updated_at();
    """)
    )
