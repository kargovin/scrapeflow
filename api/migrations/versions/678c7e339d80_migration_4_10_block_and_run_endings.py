"""migration_4_10_block_and_run_endings

Revision ID: 678c7e339d80
Revises: 47a8f9f3f4e2
Create Date: 2026-11-18 17:34:28.365437

J.3 (ADR-014 §5a, §8b): how pipeline runs and blocks ended — reason columns beside the
statuses, a stop-mode Validate's verdict, a sink's delivery and its undo — and
`webhook_deliveries` widened to a pipeline run's failure alert. Columns only, none read by
the quota views, so neither view is dropped. The downgrade deletes pipeline alert rows: the
narrower owner CHECKs cannot hold them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "678c7e339d80"
down_revision: str | Sequence[str] | None = "47a8f9f3f4e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline_run_blocks", sa.Column("status_reason", sa.VARCHAR(30), nullable=True))
    op.add_column("pipeline_run_blocks", sa.Column("reason_block_id", sa.Text(), nullable=True))
    op.add_column(
        "pipeline_run_blocks",
        sa.Column("verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "pipeline_run_blocks",
        sa.Column("delivery", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("pipeline_run_blocks", sa.Column("undo_status", sa.VARCHAR(20), nullable=True))
    op.add_column("pipeline_run_blocks", sa.Column("undo_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_pipeline_run_blocks_status_reason",
        "pipeline_run_blocks",
        "status_reason IS NULL OR status_reason IN "
        "('stopped', 'upstream_failed', 'cancelled', 'not_started', 'alert_only')",
    )
    op.create_check_constraint(
        "ck_pipeline_run_blocks_status_reason_status",
        "pipeline_run_blocks",
        "status_reason IS NULL OR status IN ('skipped', 'waiting')",
    )
    op.create_check_constraint(
        "ck_pipeline_run_blocks_undo_status",
        "pipeline_run_blocks",
        "undo_status IS NULL OR undo_status IN "
        "('undoing', 'undone', 'undo_failed', 'kept', 'not_undoable')",
    )
    op.create_check_constraint(
        "ck_pipeline_run_blocks_undo_status_completed",
        "pipeline_run_blocks",
        "undo_status IS NULL OR status = 'completed'",
    )

    op.add_column("pipeline_runs", sa.Column("stopped_at_block_id", sa.Text(), nullable=True))
    op.add_column("pipeline_runs", sa.Column("stop_reason", sa.VARCHAR(30), nullable=True))
    op.add_column("pipeline_runs", sa.Column("failed_block_id", sa.Text(), nullable=True))
    op.add_column(
        "pipeline_runs", sa.Column("rollback_deadline", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_pipeline_runs_stop_reason",
        "pipeline_runs",
        "stop_reason IS NULL OR stop_reason IN ('rule_not_met')",
    )
    op.create_check_constraint(
        "ck_pipeline_runs_stop_reason_with_block",
        "pipeline_runs",
        "(stopped_at_block_id IS NULL) = (stop_reason IS NULL)",
    )
    op.create_check_constraint(
        "ck_pipeline_runs_stopped_is_completed",
        "pipeline_runs",
        "stopped_at_block_id IS NULL OR status = 'completed'",
    )

    op.add_column("webhook_deliveries", sa.Column("pipeline_run_id", sa.Uuid(), nullable=True))
    op.add_column("webhook_deliveries", sa.Column("block_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_webhook_deliveries_pipeline_run_id",
        "webhook_deliveries",
        "pipeline_runs",
        ["pipeline_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "ck_webhook_deliveries_job_or_batch_or_crawls", "webhook_deliveries", type_="check"
    )
    op.create_check_constraint(
        "ck_webhook_deliveries_job_or_batch_or_crawls",
        "webhook_deliveries",
        "num_nonnulls(job_id, batch_id, crawl_id, pipeline_run_id) = 1",
    )
    op.drop_constraint("ck_webhook_deliveries_run_or_crawl", "webhook_deliveries", type_="check")
    op.create_check_constraint(
        "ck_webhook_deliveries_run_or_crawl",
        "webhook_deliveries",
        "num_nonnulls(run_id, crawl_id, pipeline_run_id) = 1",
    )
    op.create_check_constraint(
        "ck_webhook_deliveries_pipeline_block",
        "webhook_deliveries",
        "(pipeline_run_id IS NULL) = (block_id IS NULL)",
    )
    op.create_index(
        "uq_webhook_deliveries_pipeline_run_block_event",
        "webhook_deliveries",
        ["pipeline_run_id", "block_id", "event"],
        unique=True,
        postgresql_where=sa.text("pipeline_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM webhook_deliveries WHERE pipeline_run_id IS NOT NULL")
    op.drop_index(
        "uq_webhook_deliveries_pipeline_run_block_event",
        table_name="webhook_deliveries",
        postgresql_where=sa.text("pipeline_run_id IS NOT NULL"),
    )
    op.drop_constraint("ck_webhook_deliveries_pipeline_block", "webhook_deliveries", type_="check")
    op.drop_constraint("ck_webhook_deliveries_run_or_crawl", "webhook_deliveries", type_="check")
    op.create_check_constraint(
        "ck_webhook_deliveries_run_or_crawl",
        "webhook_deliveries",
        "num_nonnulls(run_id, crawl_id) = 1",
    )
    op.drop_constraint(
        "ck_webhook_deliveries_job_or_batch_or_crawls", "webhook_deliveries", type_="check"
    )
    op.create_check_constraint(
        "ck_webhook_deliveries_job_or_batch_or_crawls",
        "webhook_deliveries",
        "num_nonnulls(job_id, batch_id, crawl_id) = 1",
    )
    op.drop_constraint(
        "fk_webhook_deliveries_pipeline_run_id", "webhook_deliveries", type_="foreignkey"
    )
    op.drop_column("webhook_deliveries", "block_id")
    op.drop_column("webhook_deliveries", "pipeline_run_id")

    op.drop_constraint("ck_pipeline_runs_stopped_is_completed", "pipeline_runs", type_="check")
    op.drop_constraint("ck_pipeline_runs_stop_reason_with_block", "pipeline_runs", type_="check")
    op.drop_constraint("ck_pipeline_runs_stop_reason", "pipeline_runs", type_="check")
    op.drop_column("pipeline_runs", "rollback_deadline")
    op.drop_column("pipeline_runs", "failed_block_id")
    op.drop_column("pipeline_runs", "stop_reason")
    op.drop_column("pipeline_runs", "stopped_at_block_id")

    op.drop_constraint(
        "ck_pipeline_run_blocks_undo_status_completed", "pipeline_run_blocks", type_="check"
    )
    op.drop_constraint("ck_pipeline_run_blocks_undo_status", "pipeline_run_blocks", type_="check")
    op.drop_constraint(
        "ck_pipeline_run_blocks_status_reason_status", "pipeline_run_blocks", type_="check"
    )
    op.drop_constraint("ck_pipeline_run_blocks_status_reason", "pipeline_run_blocks", type_="check")
    op.drop_column("pipeline_run_blocks", "undo_error")
    op.drop_column("pipeline_run_blocks", "undo_status")
    op.drop_column("pipeline_run_blocks", "delivery")
    op.drop_column("pipeline_run_blocks", "verdict")
    op.drop_column("pipeline_run_blocks", "reason_block_id")
    op.drop_column("pipeline_run_blocks", "status_reason")
