import uuid
from datetime import UTC, datetime

from sqlalchemy import VARCHAR, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        VARCHAR(20),
        CheckConstraint(
            "status IN ('pending', 'running', 'processing', 'completed', 'failed', 'cancelled')"
        ),
        nullable=False,
        index=True,
    )
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_detected: Mapped[bool | None] = mapped_column(nullable=True)
    diff_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    nats_stream_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    __table_args__ = (
        Index(
            "idx_job_runs_nats_stream_seq",
            "nats_stream_seq",
            postgresql_where=text("nats_stream_seq IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"Job_runs id {self.id} Job id {self.job_id} Status {self.status}"
