"""migration_4_8_schedule_lane

Revision ID: 437442ce59b2
Revises: 532e93f0d629
Create Date: 2026-10-27 13:20:02.683342

F.1: `jobs.schedule_lane` — the engine that fires a job's schedule — plus the skip record
(ADR-010 §1: visible to the user, never a `job_runs` row) and the per-user scheduled-jobs cap
(ADR-013). Every job that has a schedule state today is a v1 schedule. A job with a cron and no
state was never armed (create never set one) and is left unarmed. The CHECK is hand-written.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "437442ce59b2"
down_revision: str | Sequence[str] | None = "532e93f0d629"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("jobs", sa.Column("schedule_lane", sa.VARCHAR(length=2), nullable=True))
    op.create_check_constraint("chk_jobs_schedule_lane", "jobs", "schedule_lane IN ('v1', 'v2')")
    op.execute("UPDATE jobs SET schedule_lane = 'v1' WHERE schedule_status IS NOT NULL")
    op.add_column(
        "jobs", sa.Column("schedule_skipped_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("jobs", sa.Column("schedule_skip_reason", sa.Text(), nullable=True))
    op.add_column(
        "jobs", sa.Column("schedule_waiting_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("user_quotas", sa.Column("scheduled_jobs_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("user_quotas", "scheduled_jobs_limit")
    op.drop_column("jobs", "schedule_waiting_since")
    op.drop_column("jobs", "schedule_skip_reason")
    op.drop_column("jobs", "schedule_skipped_at")
    op.drop_constraint("chk_jobs_schedule_lane", "jobs", type_="check")
    op.drop_column("jobs", "schedule_lane")
