"""migration_4_14_monitor_notices

Revision ID: 3af61135d943
Revises: d0434b2ec373
Create Date: 2026-11-21 02:33:49.020503

M.9 (ADR-016 §8): `monitors.notice_destination_id`; `webhook_deliveries` gains a third owner, the
monitor (`monitor_id`, `monitor_event_id`), both owner CHECKs widened, one notice per situation.
No quota view reads any of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3af61135d943"
down_revision: str | Sequence[str] | None = "d0434b2ec373"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("monitors", sa.Column("notice_destination_id", sa.Uuid(), nullable=True))
    op.create_index(
        "idx_monitors_notice_destination_id",
        "monitors",
        ["notice_destination_id"],
        unique=False,
        postgresql_where=sa.text("notice_destination_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_monitors_notice_destination_id",
        "monitors",
        "destinations",
        ["notice_destination_id"],
        ["id"],
    )

    op.add_column("webhook_deliveries", sa.Column("monitor_id", sa.Uuid(), nullable=True))
    op.add_column("webhook_deliveries", sa.Column("monitor_event_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_webhook_deliveries_monitor_id",
        "webhook_deliveries",
        "monitors",
        ["monitor_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_webhook_deliveries_monitor_event_id",
        "webhook_deliveries",
        "monitor_events",
        ["monitor_event_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "ck_webhook_deliveries_job_or_batch_or_crawls", "webhook_deliveries", type_="check"
    )
    op.create_check_constraint(
        "ck_webhook_deliveries_job_or_batch_or_crawls",
        "webhook_deliveries",
        "num_nonnulls(job_id, batch_id, crawl_id, pipeline_run_id, monitor_id) = 1",
    )
    op.drop_constraint("ck_webhook_deliveries_run_or_crawl", "webhook_deliveries", type_="check")
    op.create_check_constraint(
        "ck_webhook_deliveries_run_or_crawl",
        "webhook_deliveries",
        "num_nonnulls(run_id, crawl_id, pipeline_run_id, monitor_id) = 1",
    )
    op.create_check_constraint(
        "ck_webhook_deliveries_monitor_notice",
        "webhook_deliveries",
        "(monitor_id IS NULL) = (monitor_event_id IS NULL)"
        " AND (monitor_id IS NULL OR webhook_url IS NULL)",
    )
    op.create_index(
        "idx_webhook_deliveries_monitor_id",
        "webhook_deliveries",
        ["monitor_id"],
        unique=False,
        postgresql_where=sa.text("monitor_id IS NOT NULL"),
    )
    op.create_index(
        "uq_webhook_deliveries_monitor_event_id",
        "webhook_deliveries",
        ["monitor_event_id"],
        unique=True,
        postgresql_where=sa.text("monitor_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM webhook_deliveries WHERE monitor_id IS NOT NULL")
    op.drop_index(
        "uq_webhook_deliveries_monitor_event_id",
        table_name="webhook_deliveries",
        postgresql_where=sa.text("monitor_event_id IS NOT NULL"),
    )
    op.drop_index(
        "idx_webhook_deliveries_monitor_id",
        table_name="webhook_deliveries",
        postgresql_where=sa.text("monitor_id IS NOT NULL"),
    )
    op.drop_constraint("ck_webhook_deliveries_monitor_notice", "webhook_deliveries", type_="check")
    op.drop_constraint("ck_webhook_deliveries_run_or_crawl", "webhook_deliveries", type_="check")
    op.create_check_constraint(
        "ck_webhook_deliveries_run_or_crawl",
        "webhook_deliveries",
        "num_nonnulls(run_id, crawl_id, pipeline_run_id) = 1",
    )
    op.drop_constraint(
        "ck_webhook_deliveries_job_or_batch_or_crawls", "webhook_deliveries", type_="check"
    )
    op.create_check_constraint(
        "ck_webhook_deliveries_job_or_batch_or_crawls",
        "webhook_deliveries",
        "num_nonnulls(job_id, batch_id, crawl_id, pipeline_run_id) = 1",
    )
    op.drop_constraint(
        "fk_webhook_deliveries_monitor_event_id", "webhook_deliveries", type_="foreignkey"
    )
    op.drop_constraint("fk_webhook_deliveries_monitor_id", "webhook_deliveries", type_="foreignkey")
    op.drop_column("webhook_deliveries", "monitor_event_id")
    op.drop_column("webhook_deliveries", "monitor_id")

    op.drop_constraint("fk_monitors_notice_destination_id", "monitors", type_="foreignkey")
    op.drop_index(
        "idx_monitors_notice_destination_id",
        table_name="monitors",
        postgresql_where=sa.text("notice_destination_id IS NOT NULL"),
    )
    op.drop_column("monitors", "notice_destination_id")
