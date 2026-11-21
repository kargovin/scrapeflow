import uuid
from datetime import UTC, datetime

from sqlalchemy import VARCHAR, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# ADR-015 §4: an email row is sent by send_email on the delivery queue.
DELIVERY_KINDS = ("webhook", "email")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    crawl_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("crawls.id", ondelete="CASCADE"), nullable=True, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_runs.id", ondelete="CASCADE"), nullable=True
    )
    # A pipeline run's failure alert (ADR-014 §8b): the run, and the block that sends it.
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "pipeline_runs.id", ondelete="CASCADE", name="fk_webhook_deliveries_pipeline_run_id"
        ),
        nullable=True,
    )
    block_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A monitor notice (ADR-016 §8): the monitor, and the situation it reports.
    monitor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE", name="fk_webhook_deliveries_monitor_id"),
        nullable=True,
    )
    monitor_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "monitor_events.id", ondelete="CASCADE", name="fk_webhook_deliveries_monitor_event_id"
        ),
        nullable=True,
    )
    # A saved destination's row (ADR-015 §2b): its URL or mail settings are read at each attempt.
    # SET NULL leaves a row with no target — its attempt fails as "destination deleted".
    destination_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "destinations.id", ondelete="SET NULL", name="fk_webhook_deliveries_destination_id"
        ),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="webhook")
    event: Mapped[str] = mapped_column(VARCHAR(50), nullable=False)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(job_id, batch_id, crawl_id, pipeline_run_id, monitor_id) = 1",
            name="ck_webhook_deliveries_job_or_batch_or_crawls",
        ),
        CheckConstraint(
            "num_nonnulls(run_id, crawl_id, pipeline_run_id, monitor_id) = 1",
            name="ck_webhook_deliveries_run_or_crawl",
        ),
        CheckConstraint(
            "(pipeline_run_id IS NULL) = (block_id IS NULL)",
            name="ck_webhook_deliveries_pipeline_block",
        ),
        # A notice goes to the monitor's destination only: never an inline URL.
        CheckConstraint(
            "(monitor_id IS NULL) = (monitor_event_id IS NULL)"
            " AND (monitor_id IS NULL OR webhook_url IS NULL)",
            name="ck_webhook_deliveries_monitor_notice",
        ),
        CheckConstraint(_in("kind", DELIVERY_KINDS), name="ck_webhook_deliveries_kind"),
        # Never both; neither only after the destination was deleted (SET NULL).
        CheckConstraint(
            "num_nonnulls(webhook_url, destination_id) <= 1",
            name="ck_webhook_deliveries_one_target",
        ),
        CheckConstraint(
            "kind = 'webhook' OR webhook_url IS NULL", name="ck_webhook_deliveries_email_no_url"
        ),
        # A retried writer inserts nothing twice (ON CONFLICT DO NOTHING).
        Index(
            "uq_webhook_deliveries_pipeline_run_block_event",
            "pipeline_run_id",
            "block_id",
            "event",
            unique=True,
            postgresql_where=text("pipeline_run_id IS NOT NULL"),
        ),
        # One notice per situation, whatever retries its writer.
        Index(
            "uq_webhook_deliveries_monitor_event_id",
            "monitor_event_id",
            unique=True,
            postgresql_where=text("monitor_event_id IS NOT NULL"),
        ),
        Index(
            "idx_webhook_deliveries_monitor_id",
            "monitor_id",
            postgresql_where=text("monitor_id IS NOT NULL"),
        ),
        Index(
            "idx_webhook_deliveries_destination_id",
            "destination_id",
            postgresql_where=text("destination_id IS NOT NULL"),
        ),
        Index(
            "idx_webhook_deliveries_status_next",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<WebhookDelivery id={self.id} job_id={self.job_id} status={self.status}>"
