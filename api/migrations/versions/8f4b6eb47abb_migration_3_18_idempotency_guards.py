"""migration_3_18_idempotency_guards

Revision ID: 8f4b6eb47abb
Revises: 0a917a50170e
Create Date: 2026-04-29 08:33:02.884235

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f4b6eb47abb"
down_revision: str | Sequence[str] | None = "0a917a50170e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "job_runs", sa.Column("storage_accounted_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Add nullable first so existing rows don't violate NOT NULL, then backfill, then lock down.
    op.add_column("webhook_deliveries", sa.Column("event", sa.VARCHAR(length=50), nullable=True))
    op.execute("UPDATE webhook_deliveries SET event = payload->>'event'")
    op.alter_column("webhook_deliveries", "event", nullable=False)

    # Partial unique index — prevents duplicate webhook delivery rows for the same
    # (run_id, event) pair. Covers job and batch webhooks (both carry run_id IS NOT NULL).
    # Crawl webhooks (run_id IS NULL) are excluded; they're protected by the
    # check_completion terminal guard in the coordinator (Finding 7 / C5).
    op.execute("""
        CREATE UNIQUE INDEX idx_webhook_deliveries_dedup
        ON webhook_deliveries (run_id, event)
        WHERE run_id IS NOT NULL
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_webhook_deliveries_dedup")
    op.drop_column("webhook_deliveries", "event")
    op.drop_column("job_runs", "storage_accounted_at")
