"""migration_4_3_user_delete_cascades_crawls_batches

Revision ID: 9a1ebad3fca2
Revises: 86c780f55969
Create Date: 2026-09-18 18:26:40.361456

BUG-014: crawls.user_id and batches.user_id were the only two of users' referrers with
no ON DELETE, and the ORM cascades neither, so DELETE /admin/users/{id} was refused by
Postgres for any user who owned a batch or a crawl — after the endpoint had already
removed their objects from MinIO. Both FKs now cascade like jobs.user_id does.

The constraints keep their existing names (Postgres' default `<table>_<column>_fkey`)
so nothing that names them changes. The views from 86c780f55969 read both columns;
a constraint swap does not alter the columns, so they need no drop/recreate here.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a1ebad3fca2"
down_revision: str | Sequence[str] | None = "86c780f55969"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("batches_user_id_fkey", "batches", type_="foreignkey")
    op.create_foreign_key(
        "batches_user_id_fkey", "batches", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_constraint("crawls_user_id_fkey", "crawls", type_="foreignkey")
    op.create_foreign_key(
        "crawls_user_id_fkey", "crawls", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint("crawls_user_id_fkey", "crawls", type_="foreignkey")
    op.create_foreign_key("crawls_user_id_fkey", "crawls", "users", ["user_id"], ["id"])
    op.drop_constraint("batches_user_id_fkey", "batches", type_="foreignkey")
    op.create_foreign_key("batches_user_id_fkey", "batches", "users", ["user_id"], ["id"])
