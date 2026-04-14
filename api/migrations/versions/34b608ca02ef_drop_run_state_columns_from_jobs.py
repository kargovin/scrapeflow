"""drop_run_state_columns_from_jobs

Revision ID: 34b608ca02ef
Revises: 655b7e9b461b
Create Date: 2026-04-09 18:07:19.779954

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "34b608ca02ef"
down_revision: str | Sequence[str] | None = "655b7e9b461b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("jobs", "status")
    op.drop_column("jobs", "result_path")
    op.drop_column("jobs", "error")


def downgrade() -> None:
    """Downgrade schema."""
    pass  # no downgrade — backup restore required
