"""migration_4_15_approval

Revision ID: f2c328bcbfcc
Revises: 3af61135d943
Create Date: 2026-11-21 18:44:05.736061

N.1 (ADR-016 §9a, §10): the Approval block's columns on `pipeline_run_blocks`; `stop_reason` gains
the three approval endings, `status_reason` gains `awaiting_approval`. No quota view reads any of
them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c328bcbfcc"
down_revision: str | Sequence[str] | None = "3af61135d943"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STOP_REASONS = "'rule_not_met'"
APPROVAL_STOP_REASONS = "'approval_rejected', 'approval_expired', 'approval_superseded'"
STATUS_REASONS = "'stopped', 'upstream_failed', 'cancelled', 'not_started', 'alert_only'"


def _stop_reason_check(values: str) -> None:
    op.drop_constraint("ck_pipeline_runs_stop_reason", "pipeline_runs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_runs_stop_reason",
        "pipeline_runs",
        f"stop_reason IS NULL OR stop_reason IN ({values})",
    )


def _status_reason_check(values: str) -> None:
    op.drop_constraint("ck_pipeline_run_blocks_status_reason", "pipeline_run_blocks", type_="check")
    op.create_check_constraint(
        "ck_pipeline_run_blocks_status_reason",
        "pipeline_run_blocks",
        f"status_reason IS NULL OR status_reason IN ({values})",
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "pipeline_run_blocks",
        sa.Column("approval_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pipeline_run_blocks", sa.Column("approval_answer", sa.VARCHAR(length=20), nullable=True)
    )
    op.add_column(
        "pipeline_run_blocks", sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("pipeline_run_blocks", sa.Column("answered_by", sa.Uuid(), nullable=True))
    op.add_column(
        "pipeline_run_blocks", sa.Column("superseded_by_run_id", sa.Uuid(), nullable=True)
    )
    op.create_check_constraint(
        "ck_pipeline_run_blocks_approval_answer",
        "pipeline_run_blocks",
        "approval_answer IS NULL OR approval_answer IN "
        "('approved', 'rejected', 'expired', 'superseded')",
    )
    op.create_index(
        "idx_pipeline_run_blocks_answered_by",
        "pipeline_run_blocks",
        ["answered_by"],
        unique=False,
        postgresql_where=sa.text("answered_by IS NOT NULL"),
    )
    op.create_index(
        "idx_pipeline_run_blocks_superseded_by_run_id",
        "pipeline_run_blocks",
        ["superseded_by_run_id"],
        unique=False,
        postgresql_where=sa.text("superseded_by_run_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_pipeline_run_blocks_superseded_by_run_id",
        "pipeline_run_blocks",
        "pipeline_runs",
        ["superseded_by_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_pipeline_run_blocks_answered_by",
        "pipeline_run_blocks",
        "users",
        ["answered_by"],
        ["id"],
        ondelete="SET NULL",
    )
    _stop_reason_check(f"{STOP_REASONS}, {APPROVAL_STOP_REASONS}")
    _status_reason_check(f"{STATUS_REASONS}, 'awaiting_approval'")


def downgrade() -> None:
    """Downgrade schema."""
    # ⚠️ Lossy: the approval endings have no older value; the stop is forgotten, the run stays.
    op.execute(
        "UPDATE pipeline_runs SET stop_reason = NULL, stopped_at_block_id = NULL "
        f"WHERE stop_reason IN ({APPROVAL_STOP_REASONS})"
    )
    op.execute(
        "UPDATE pipeline_run_blocks SET status_reason = NULL "
        "WHERE status_reason = 'awaiting_approval'"
    )
    _status_reason_check(STATUS_REASONS)
    _stop_reason_check(STOP_REASONS)
    op.drop_constraint(
        "fk_pipeline_run_blocks_answered_by", "pipeline_run_blocks", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_pipeline_run_blocks_superseded_by_run_id", "pipeline_run_blocks", type_="foreignkey"
    )
    op.drop_index(
        "idx_pipeline_run_blocks_superseded_by_run_id",
        table_name="pipeline_run_blocks",
        postgresql_where=sa.text("superseded_by_run_id IS NOT NULL"),
    )
    op.drop_index(
        "idx_pipeline_run_blocks_answered_by",
        table_name="pipeline_run_blocks",
        postgresql_where=sa.text("answered_by IS NOT NULL"),
    )
    op.drop_constraint(
        "ck_pipeline_run_blocks_approval_answer", "pipeline_run_blocks", type_="check"
    )
    op.drop_column("pipeline_run_blocks", "superseded_by_run_id")
    op.drop_column("pipeline_run_blocks", "answered_by")
    op.drop_column("pipeline_run_blocks", "answered_at")
    op.drop_column("pipeline_run_blocks", "approval_answer")
    op.drop_column("pipeline_run_blocks", "approval_deadline")
