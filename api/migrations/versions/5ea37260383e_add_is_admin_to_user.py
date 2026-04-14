"""add_is_admin_to_user

Revision ID: 5ea37260383e
Revises: 8a673d38fe23
Create Date: 2026-04-03 08:13:02.348386

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5ea37260383e"
down_revision: str | Sequence[str] | None = "8a673d38fe23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false")
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "is_admin")
