"""migration_4_9_drop_nats_stream_seq

Revision ID: 47a8f9f3f4e2
Revises: 437442ce59b2
Create Date: 2026-10-29 11:49:43.230477

I.1: drops `job_runs.nats_stream_seq` and its partial index. H.2 unmapped the column; nothing
reads it. The downgrade re-adds both, empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "47a8f9f3f4e2"
down_revision: str | Sequence[str] | None = "437442ce59b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(
        "idx_job_runs_nats_stream_seq",
        table_name="job_runs",
        postgresql_where=sa.text("nats_stream_seq IS NOT NULL"),
    )
    op.drop_column("job_runs", "nats_stream_seq")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("job_runs", sa.Column("nats_stream_seq", sa.BigInteger(), nullable=True))
    op.create_index(
        "idx_job_runs_nats_stream_seq",
        "job_runs",
        ["nats_stream_seq"],
        postgresql_where=sa.text("nats_stream_seq IS NOT NULL"),
    )
