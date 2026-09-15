"""migration_4_1_storage_objects_ledger

Revision ID: 0c73753d5138
Revises: 8f4b6eb47abb
Create Date: 2026-09-15 06:52:48.108999

The shared per-object storage ledger (ADR-009 §8d, backlog P8). One row per MinIO
object held on a user's behalf; user_quotas.storage_bytes_used becomes a materialised
sum over it. No backfill: a pre-ledger object has no row, and the delete paths keep a
legacy branch for those (app/core/ledger.py) until scripts/reconcile_storage_ledger.py
has been run against the bucket.

ck_storage_objects_one_producer is a closed enumeration on purpose — a new lane must
widen it in the migration that adds its FK column, and fails loudly on its first insert
if it does not. The meter never reads the producer columns, so it stays correct either way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0c73753d5138"
down_revision: str | Sequence[str] | None = "8f4b6eb47abb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "storage_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("job_run_id", sa.Uuid(), nullable=True),
        sa.Column("crawl_page_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("bytes >= 0", name="ck_storage_objects_bytes_nonnegative"),
        sa.CheckConstraint(
            "num_nonnulls(job_run_id, crawl_page_id) = 1",
            name="ck_storage_objects_one_producer",
        ),
        sa.ForeignKeyConstraint(["crawl_page_id"], ["crawl_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_run_id"], ["job_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("idx_storage_objects_user_id", "storage_objects", ["user_id"], unique=False)
    op.create_index(
        "idx_storage_objects_job_run_id",
        "storage_objects",
        ["job_run_id"],
        unique=False,
        postgresql_where=sa.text("job_run_id IS NOT NULL"),
    )
    op.create_index(
        "idx_storage_objects_crawl_page_id",
        "storage_objects",
        ["crawl_page_id"],
        unique=False,
        postgresql_where=sa.text("crawl_page_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "idx_storage_objects_crawl_page_id",
        table_name="storage_objects",
        postgresql_where=sa.text("crawl_page_id IS NOT NULL"),
    )
    op.drop_index(
        "idx_storage_objects_job_run_id",
        table_name="storage_objects",
        postgresql_where=sa.text("job_run_id IS NOT NULL"),
    )
    op.drop_index("idx_storage_objects_user_id", table_name="storage_objects")
    op.drop_table("storage_objects")
