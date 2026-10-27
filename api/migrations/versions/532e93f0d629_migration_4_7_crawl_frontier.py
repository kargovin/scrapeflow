"""migration_4_7_crawl_frontier

Revision ID: 532e93f0d629
Revises: 56ab6118641d
Create Date: 2026-10-27 10:16:07.308917

E.3 — the v2 crawl frontier. `crawl_queue` stays one table for the queue and the seen-set
(ADR-009 13b); a URL admission refuses is kept in it as a `dropped` row with its
`drop_reason`, so a rediscovered one is neither re-checked nor counted twice. `crawls.dropped`
holds the counts by reason (ADR-010 §2); `crawls.error` says why a crawl failed or stopped
early. The quota views read none of these columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "532e93f0d629"
down_revision: str | Sequence[str] | None = "56ab6118641d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "crawls",
        sa.Column(
            "dropped",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column("crawls", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("crawl_queue", sa.Column("drop_reason", sa.VARCHAR(length=20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("crawl_queue", "drop_reason")
    op.drop_column("crawls", "error")
    op.drop_column("crawls", "dropped")
