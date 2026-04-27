"""migration_3_17_users_email_trgm_index

Revision ID: 0a917a50170e
Revises: e19ca4bfd60b
Create Date: 2026-04-27 09:15:55.036248

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0a917a50170e"
down_revision: str | Sequence[str] | None = "e19ca4bfd60b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "idx_users_email_trgm",
        "users",
        ["email"],
        postgresql_using="gin",
        postgresql_ops={"email": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("idx_users_email_trgm", table_name="users")
