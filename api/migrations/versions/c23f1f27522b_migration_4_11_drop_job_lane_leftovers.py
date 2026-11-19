"""migration_4_11_drop_job_lane_leftovers

Revision ID: c23f1f27522b
Revises: 678c7e339d80
Create Date: 2026-11-19 17:36:19.759817

Y.2: drops `job_runs.content_hash`, `diff_detected`, `diff_summary` and `crawls.schedule_cron`.
Y.1 (released with J-a) unmapped all four; nothing reads them and no quota view names them. The
downgrade re-adds them, empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c23f1f27522b"
down_revision: str | Sequence[str] | None = "678c7e339d80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("crawls", "schedule_cron")
    op.drop_column("job_runs", "diff_detected")
    op.drop_column("job_runs", "diff_summary")
    op.drop_column("job_runs", "content_hash")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("job_runs", sa.Column("content_hash", sa.VARCHAR(length=16), nullable=True))
    op.add_column(
        "job_runs",
        sa.Column("diff_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("job_runs", sa.Column("diff_detected", sa.BOOLEAN(), nullable=True))
    op.add_column("crawls", sa.Column("schedule_cron", sa.TEXT(), nullable=True))
