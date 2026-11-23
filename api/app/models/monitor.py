import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    VARCHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

MONITOR_STATES = ("active", "paused", "paused_itself")
# ADR-016 §7b; a new kind widens the CHECK in a migration.
MONITOR_EVENT_KINDS = (
    "skipped_monthly_runs",
    "skipped_check_in_progress",
    "skipped_pipelines_disabled",
    "waiting_for_slot",
    "gave_up_waiting",
    "paused_itself",
    # ADR-017: a point event, written closed.
    "approval_expired",
)
# §7b/§8: the situations a notice is sent for when they start; the others are history only.
NOTICE_KINDS = (
    "skipped_monthly_runs",
    "skipped_pipelines_disabled",
    "gave_up_waiting",
    "paused_itself",
    "approval_expired",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Monitor(Base):
    """A pinned pipeline version run on a schedule, each run compared with the last (ADR-016 §2a)."""

    __tablename__ = "monitors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_monitors_user_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # No ondelete: a pipeline delete is refused with 409 while a monitor uses it (§2a).
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipelines.id", name="fk_monitors_pipeline_id"), nullable=False
    )
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_versions.id", name="fk_monitors_pipeline_version_id"),
        nullable=False,
    )
    # Fixed for life: each fixed run input's value, by name.
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Fixed for life: [{input, from_block, path, start}] (§6b).
    carried: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    schedule_cron: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="active")
    # Where notices are also sent (§8): a webhook or smtp destination of the owner's. No ondelete:
    # a destination delete is refused with 409 while a monitor names it (ADR-015 §2c).
    notice_destination_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("destinations.id", name="fk_monitors_notice_destination_id"), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    # onupdate skips db.execute(update(...)) — set it explicitly on those paths.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_monitors_user_id_name"),
        CheckConstraint(_in("state", MONITOR_STATES), name="ck_monitors_state"),
        CheckConstraint("consecutive_failures >= 0", name="ck_monitors_failures_nonnegative"),
        Index("idx_monitors_pipeline_id", "pipeline_id"),
        Index(
            "idx_monitors_notice_destination_id",
            "notice_destination_id",
            postgresql_where=text("notice_destination_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Monitor {self.id} user={self.user_id} state={self.state}>"


class MonitorEvent(Base):
    """One situation a monitor was in — a run of identical skipped firings is one row (§7b)."""

    __tablename__ = "monitor_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE", name="fk_monitor_events_monitor_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # NULL = the situation is still going on.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(_in("kind", MONITOR_EVENT_KINDS), name="ck_monitor_events_kind"),
        CheckConstraint("count >= 1", name="ck_monitor_events_count_positive"),
        Index("idx_monitor_events_monitor_id_first_at", "monitor_id", "first_at"),
        # One open situation per kind; writers hold lock_user, this is the backstop.
        Index(
            "uq_monitor_events_open_kind",
            "monitor_id",
            "kind",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<MonitorEvent {self.kind} monitor={self.monitor_id} count={self.count}>"
