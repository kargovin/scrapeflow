import uuid
from datetime import UTC, datetime

from sqlalchemy import VARCHAR, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


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
    event: Mapped[str] = mapped_column(VARCHAR(50), nullable=False)
    webhook_url: Mapped[str] = mapped_column(Text, nullable=False)
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
            "num_nonnulls(job_id, batch_id, crawl_id) = 1",
            name="ck_webhook_deliveries_job_or_batch_or_crawls",
        ),
        CheckConstraint(
            "num_nonnulls(run_id, crawl_id) = 1",
            name="ck_webhook_deliveries_run_or_crawl",
        ),
        Index(
            "idx_webhook_deliveries_status_next",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<WebhookDelivery id={self.id} job_id={self.job_id} status={self.status}>"
