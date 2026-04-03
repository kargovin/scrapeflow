import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import VARCHAR, CheckConstraint, DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.user import User


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class OutputFormat(str, enum.Enum):
    html = "html"
    markdown = "markdown"
    json = "json"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), nullable=False, default=JobStatus.pending
    )
    output_format: Mapped[OutputFormat] = mapped_column(
        Enum(OutputFormat), nullable=False, default=OutputFormat.html
    )
    result_path: Mapped[str | None] = mapped_column(
        nullable=True
    )  # MinIO object path, set when job completes
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # error message if status=failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    engine: Mapped[str] = mapped_column(
        VARCHAR(20), CheckConstraint("engine IN ('http', 'playwright')"), server_default="http"
    )
    schedule_cron: Mapped[str | None] = mapped_column(nullable=True)

    schedule_status: Mapped[str | None] = mapped_column(
        CheckConstraint("schedule_status IN ('active', 'paused')")
    )

    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    webhook_url: Mapped[str | None] = mapped_column(nullable=True)

    webhook_secret: Mapped[str | None] = mapped_column(nullable=True)  # Fernet-encrypted at rest

    llm_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # JSONB

    playwright_options: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # JSONB

    user: Mapped["User"] = relationship("User", back_populates="jobs")

    def __repr__(self) -> str:
        return f"<Job id={self.id} status={self.status} url={self.url}>"
