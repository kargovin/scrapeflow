"""migration_4_12_monitors

Revision ID: 72b61b3efb61
Revises: c23f1f27522b
Create Date: 2026-11-19 18:26:00.814585

L.2 (ADR-016 §2a, §2b, §7b): `monitors`, `monitor_events`; a check's columns on `pipeline_runs`
and the change gate's `fingerprint` on `pipeline_run_blocks`. No quota view reads any of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "72b61b3efb61"
down_revision: str | Sequence[str] | None = "c23f1f27522b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "monitors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "inputs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "carried",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("schedule_cron", sa.Text(), nullable=False),
        sa.Column("state", sa.VARCHAR(length=20), server_default="active", nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'paused_itself')", name="ck_monitors_state"
        ),
        sa.CheckConstraint("consecutive_failures >= 0", name="ck_monitors_failures_nonnegative"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], name="fk_monitors_pipeline_id"),
        sa.ForeignKeyConstraint(
            ["pipeline_version_id"],
            ["pipeline_versions.id"],
            name="fk_monitors_pipeline_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_monitors_user_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_monitors_user_id_name"),
    )
    op.create_index("idx_monitors_pipeline_id", "monitors", ["pipeline_id"], unique=False)
    op.create_table(
        "monitor_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("monitor_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.VARCHAR(length=30), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "kind IN ('skipped_monthly_runs', 'skipped_check_in_progress', "
            "'skipped_pipelines_disabled', 'waiting_for_slot', 'gave_up_waiting', "
            "'paused_itself')",
            name="ck_monitor_events_kind",
        ),
        sa.CheckConstraint("count >= 1", name="ck_monitor_events_count_positive"),
        sa.ForeignKeyConstraint(
            ["monitor_id"],
            ["monitors.id"],
            name="fk_monitor_events_monitor_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_monitor_events_monitor_id_first_at",
        "monitor_events",
        ["monitor_id", "first_at"],
        unique=False,
    )
    op.create_index(
        "uq_monitor_events_open_kind",
        "monitor_events",
        ["monitor_id", "kind"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.add_column(
        "pipeline_run_blocks", sa.Column("fingerprint", sa.VARCHAR(length=64), nullable=True)
    )
    op.add_column("pipeline_runs", sa.Column("monitor_id", sa.Uuid(), nullable=True))
    op.add_column("pipeline_runs", sa.Column("baseline_run_id", sa.Uuid(), nullable=True))
    op.add_column("pipeline_runs", sa.Column("change_status", sa.VARCHAR(length=20), nullable=True))
    op.add_column(
        "pipeline_runs",
        sa.Column("change_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("carried_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("captured_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column(
            "baseline_eligible", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.create_index(
        "idx_pipeline_runs_monitor_id_baseline",
        "pipeline_runs",
        ["monitor_id", sa.literal_column("created_at DESC")],
        unique=False,
        postgresql_where=sa.text("baseline_eligible"),
    )
    op.create_index(
        "idx_pipeline_runs_monitor_id_created_at",
        "pipeline_runs",
        ["monitor_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_foreign_key(
        "fk_pipeline_runs_monitor_id",
        "pipeline_runs",
        "monitors",
        ["monitor_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_pipeline_runs_baseline_run_id",
        "pipeline_runs",
        "pipeline_runs",
        ["baseline_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Autogenerate never sees a CHECK on an existing table.
    op.create_check_constraint(
        "ck_pipeline_runs_change_status",
        "pipeline_runs",
        "change_status IS NULL OR change_status IN "
        "('first_check', 'no_baseline', 'changed', 'unchanged')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_pipeline_runs_change_status", "pipeline_runs", type_="check")
    op.drop_constraint("fk_pipeline_runs_baseline_run_id", "pipeline_runs", type_="foreignkey")
    op.drop_constraint("fk_pipeline_runs_monitor_id", "pipeline_runs", type_="foreignkey")
    op.drop_index("idx_pipeline_runs_monitor_id_created_at", table_name="pipeline_runs")
    op.drop_index(
        "idx_pipeline_runs_monitor_id_baseline",
        table_name="pipeline_runs",
        postgresql_where=sa.text("baseline_eligible"),
    )
    op.drop_column("pipeline_runs", "baseline_eligible")
    op.drop_column("pipeline_runs", "captured_values")
    op.drop_column("pipeline_runs", "carried_sources")
    op.drop_column("pipeline_runs", "change_summary")
    op.drop_column("pipeline_runs", "change_status")
    op.drop_column("pipeline_runs", "baseline_run_id")
    op.drop_column("pipeline_runs", "monitor_id")
    op.drop_column("pipeline_run_blocks", "fingerprint")
    op.drop_index(
        "uq_monitor_events_open_kind",
        table_name="monitor_events",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_index("idx_monitor_events_monitor_id_first_at", table_name="monitor_events")
    op.drop_table("monitor_events")
    op.drop_index("idx_monitors_pipeline_id", table_name="monitors")
    op.drop_table("monitors")
