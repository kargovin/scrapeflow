"""add_processing_to_jobstatus

Revision ID: d9aeec27d6bf
Revises: fee1ff84a5bd
Create Date: 2026-04-03 17:54:11.900613

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9aeec27d6bf"
down_revision: str | Sequence[str] | None = "fee1ff84a5bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # manually added because alter type needs to run outside the
    # transaction
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'processing' AFTER 'running'"))
    op.execute(sa.text("BEGIN"))


def downgrade() -> None:
    """Downgrade schema."""
    pass
