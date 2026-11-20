"""migration_4_13_destinations

Revision ID: d0434b2ec373
Revises: 72b61b3efb61
Create Date: 2026-11-20 12:24:04.462282

M.1 (ADR-015 §2a, §2b, §8): `destinations`, `pipeline_version_destinations`; `webhook_deliveries`
gains `destination_id` and `kind`, and `webhook_url` becomes nullable. No quota view reads any of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d0434b2ec373"
down_revision: str | Sequence[str] | None = "72b61b3efb61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "destinations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.VARCHAR(length=100), nullable=False),
        sa.Column("kind", sa.VARCHAR(length=20), nullable=False),
        sa.Column(
            "address",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("display_host", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('bucket', 'postgres', 'smtp', 'webhook')", name="ck_destinations_kind"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_destinations_user_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_destinations_user_id_name"),
    )
    op.create_table(
        "pipeline_version_destinations",
        sa.Column("pipeline_version_id", sa.Uuid(), nullable=False),
        sa.Column("destination_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["destinations.id"],
            name="fk_pipeline_version_destinations_destination_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_version_id"],
            ["pipeline_versions.id"],
            name="fk_pipeline_version_destinations_pipeline_version_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("pipeline_version_id", "destination_id"),
    )
    op.create_index(
        "idx_pipeline_version_destinations_destination_id",
        "pipeline_version_destinations",
        ["destination_id"],
        unique=False,
    )

    op.add_column("webhook_deliveries", sa.Column("destination_id", sa.Uuid(), nullable=True))
    op.add_column(
        "webhook_deliveries",
        sa.Column("kind", sa.VARCHAR(length=20), server_default="webhook", nullable=False),
    )
    op.alter_column("webhook_deliveries", "webhook_url", existing_type=sa.TEXT(), nullable=True)
    op.create_index(
        "idx_webhook_deliveries_destination_id",
        "webhook_deliveries",
        ["destination_id"],
        unique=False,
        postgresql_where=sa.text("destination_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_webhook_deliveries_destination_id",
        "webhook_deliveries",
        "destinations",
        ["destination_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Autogenerate never sees a CHECK on an existing table.
    op.create_check_constraint(
        "ck_webhook_deliveries_kind", "webhook_deliveries", "kind IN ('webhook', 'email')"
    )
    op.create_check_constraint(
        "ck_webhook_deliveries_one_target",
        "webhook_deliveries",
        "num_nonnulls(webhook_url, destination_id) <= 1",
    )
    op.create_check_constraint(
        "ck_webhook_deliveries_email_no_url",
        "webhook_deliveries",
        "kind = 'webhook' OR webhook_url IS NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_webhook_deliveries_email_no_url", "webhook_deliveries", type_="check")
    op.drop_constraint("ck_webhook_deliveries_one_target", "webhook_deliveries", type_="check")
    op.drop_constraint("ck_webhook_deliveries_kind", "webhook_deliveries", type_="check")
    op.drop_constraint(
        "fk_webhook_deliveries_destination_id", "webhook_deliveries", type_="foreignkey"
    )
    op.drop_index(
        "idx_webhook_deliveries_destination_id",
        table_name="webhook_deliveries",
        postgresql_where=sa.text("destination_id IS NOT NULL"),
    )
    # Rows with no URL belong to a destination or an email; the old schema cannot hold them.
    op.execute("DELETE FROM webhook_deliveries WHERE webhook_url IS NULL")
    op.alter_column("webhook_deliveries", "webhook_url", existing_type=sa.TEXT(), nullable=False)
    op.drop_column("webhook_deliveries", "kind")
    op.drop_column("webhook_deliveries", "destination_id")
    op.drop_index(
        "idx_pipeline_version_destinations_destination_id",
        table_name="pipeline_version_destinations",
    )
    op.drop_table("pipeline_version_destinations")
    op.drop_table("destinations")
