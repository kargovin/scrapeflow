import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    ARRAY,
    VARCHAR,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.user import User


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    processing = "processing"
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
    output_format: Mapped[OutputFormat] = mapped_column(
        Enum(OutputFormat), nullable=False, default=OutputFormat.html
    )
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

    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    webhook_secret: Mapped[str | None] = mapped_column(nullable=True)  # Fernet-encrypted at rest

    webhook_events: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    llm_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # JSONB

    playwright_options: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # JSONB

    playwright_actions: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    respect_robots: Mapped[bool] = mapped_column(server_default="false", nullable=False)

    proxy_provider: Mapped[str | None] = mapped_column(VARCHAR(50), nullable=True)

    __table_args__ = (
        Index(
            "idx_jobs_next_run_at",
            "next_run_at",
            postgresql_where=text("schedule_cron IS NOT NULL AND schedule_status = 'active'"),
        ),
    )

    user: Mapped["User"] = relationship("User", back_populates="jobs")

    def __repr__(self) -> str:
        return f"<Job id={self.id} url={self.url}>"
