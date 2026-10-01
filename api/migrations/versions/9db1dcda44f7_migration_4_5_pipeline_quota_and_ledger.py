"""migration_4_5_pipeline_quota_and_ledger

Revision ID: 9db1dcda44f7
Revises: cff9ec8fedbe
Create Date: 2026-10-01 17:07:53.391953

Pipeline lane on the three meters: a `pipeline` arm in both quota views, and
`storage_objects.pipeline_run_block_id` in the ledger's one-producer CHECK.
The views and the CHECK are hand-written; autogenerate sees neither.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9db1dcda44f7"
down_revision: str | Sequence[str] | None = "cff9ec8fedbe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RUN_UNITS_V1 = """
        SELECT 'job'::text AS lane,
               r.id        AS unit_id,
               r.id        AS submission_id,
               j.user_id   AS user_id,
               r.created_at
        FROM job_runs r
        JOIN jobs j ON j.id = r.job_id
    UNION ALL
        SELECT 'batch'::text,
               r.id,
               b.id,
               b.user_id,
               r.created_at
        FROM job_runs r
        JOIN batch_items bi ON bi.id = r.batch_item_id
        JOIN batches b ON b.id = bi.batch_id
    UNION ALL
        SELECT 'crawl'::text,
               p.id,
               c.id,
               c.user_id,
               p.created_at
        FROM crawl_pages p
        JOIN crawls c ON c.id = p.crawl_id
"""

_RUN_UNITS_PIPELINE = """
    UNION ALL
        SELECT 'pipeline'::text,
               r.id,
               r.id,
               r.user_id,
               r.created_at
        FROM pipeline_runs r
"""

_ACTIVE_V1 = """
        SELECT 'job'::text AS lane,
               r.id        AS submission_id,
               j.user_id   AS user_id
        FROM job_runs r
        JOIN jobs j ON j.id = r.job_id
        WHERE r.status IN ('pending', 'running', 'processing')
    UNION ALL
        SELECT DISTINCT 'batch'::text,
               b.id,
               b.user_id
        FROM job_runs r
        JOIN batch_items bi ON bi.id = r.batch_item_id
        JOIN batches b ON b.id = bi.batch_id
        WHERE r.status IN ('pending', 'running', 'processing')
    UNION ALL
        SELECT 'crawl'::text,
               c.id,
               c.user_id
        FROM crawls c
        WHERE c.status IN ('queued', 'running')
"""

# A `waiting` block holds no slot (ADR-009 15a).
_ACTIVE_PIPELINE = """
    UNION ALL
        SELECT 'pipeline'::text,
               r.id,
               r.user_id
        FROM pipeline_runs r
        WHERE r.status = 'running'
          AND EXISTS (
              SELECT 1
              FROM pipeline_run_blocks b
              WHERE b.pipeline_run_id = r.id
                AND b.status = 'running'
          )
"""


def _create_views(run_units: str, active: str) -> None:
    op.execute(sa.text(f"CREATE VIEW quota_run_units AS {run_units}"))
    op.execute(sa.text(f"CREATE VIEW quota_active_submissions AS {active}"))


def _drop_views() -> None:
    op.execute(sa.text("DROP VIEW quota_active_submissions"))
    op.execute(sa.text("DROP VIEW quota_run_units"))


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "idx_pipeline_runs_user_id_running",
        "pipeline_runs",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.add_column("storage_objects", sa.Column("pipeline_run_block_id", sa.Uuid(), nullable=True))
    op.create_index(
        "idx_storage_objects_pipeline_run_block_id",
        "storage_objects",
        ["pipeline_run_block_id"],
        unique=False,
        postgresql_where=sa.text("pipeline_run_block_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_storage_objects_pipeline_run_block_id",
        "storage_objects",
        "pipeline_run_blocks",
        ["pipeline_run_block_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("ck_storage_objects_one_producer", "storage_objects", type_="check")
    op.create_check_constraint(
        "ck_storage_objects_one_producer",
        "storage_objects",
        "num_nonnulls(job_run_id, crawl_page_id, pipeline_run_block_id) = 1",
    )

    _drop_views()
    _create_views(_RUN_UNITS_V1 + _RUN_UNITS_PIPELINE, _ACTIVE_V1 + _ACTIVE_PIPELINE)


def downgrade() -> None:
    """Downgrade schema."""
    _drop_views()
    _create_views(_RUN_UNITS_V1, _ACTIVE_V1)

    # Fails while any pipeline ledger row exists — release those objects first.
    op.drop_constraint("ck_storage_objects_one_producer", "storage_objects", type_="check")
    op.create_check_constraint(
        "ck_storage_objects_one_producer",
        "storage_objects",
        "num_nonnulls(job_run_id, crawl_page_id) = 1",
    )
    op.drop_constraint(
        "fk_storage_objects_pipeline_run_block_id", "storage_objects", type_="foreignkey"
    )
    op.drop_index(
        "idx_storage_objects_pipeline_run_block_id",
        table_name="storage_objects",
        postgresql_where=sa.text("pipeline_run_block_id IS NOT NULL"),
    )
    op.drop_column("storage_objects", "pipeline_run_block_id")
    op.drop_index(
        "idx_pipeline_runs_user_id_running",
        table_name="pipeline_runs",
        postgresql_where=sa.text("status = 'running'"),
    )
