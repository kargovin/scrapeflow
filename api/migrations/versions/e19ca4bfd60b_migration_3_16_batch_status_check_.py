"""migration_3_16_batch_status_check_constraints

Revision ID: e19ca4bfd60b
Revises: 4facbc8484ab
Create Date: 2026-04-26 08:21:13.437840

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e19ca4bfd60b"
down_revision: str | Sequence[str] | None = "4facbc8484ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add CHECK constraints to guard batch and batch_item status columns."""
    op.create_check_constraint(
        "ck_batches_status",
        "batches",
        "status IN ('queued', 'running', 'completed', 'partial_failure', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_batch_items_status",
        "batch_items",
        "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    """Drop batch status CHECK constraints."""
    op.drop_constraint("ck_batches_status", "batches", type_="check")
    op.drop_constraint("ck_batch_items_status", "batch_items", type_="check")
