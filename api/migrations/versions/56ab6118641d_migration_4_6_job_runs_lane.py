"""migration_4_6_job_runs_lane

Revision ID: 56ab6118641d
Revises: 9db1dcda44f7
Create Date: 2026-10-12 13:25:31.682781

`job_runs.lane` — the engine that owns a run (ADR-009 §7 mechanism 4). Existing rows are
all NATS runs, so the server default backfills them as 'v1'. Adding a column leaves the
quota views alone; the CHECK is hand-written, autogenerate does not see it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "56ab6118641d"
down_revision: str | Sequence[str] | None = "9db1dcda44f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "job_runs",
        sa.Column("lane", sa.VARCHAR(length=2), server_default="v1", nullable=False),
    )
    op.create_check_constraint("chk_job_runs_lane", "job_runs", "lane IN ('v1', 'v2')")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("chk_job_runs_lane", "job_runs", type_="check")
    op.drop_column("job_runs", "lane")
