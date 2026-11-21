import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    VARCHAR,
    Boolean,
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


class Pipeline(Base):
    """A user's named pipeline. The block list lives in `PipelineVersion`."""

    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_pipelines_user_id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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

    # Not partial on deleted_at: a soft-deleted pipeline keeps its name.
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_pipelines_user_id_name"),)

    def __repr__(self) -> str:
        return f"<Pipeline {self.id} user={self.user_id} name={self.name!r}>"


class PipelineVersion(Base):
    """One immutable saved definition of a pipeline."""

    __tablename__ = "pipeline_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE", name="fk_pipeline_versions_pipeline_id"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("pipeline_id", "version", name="uq_pipeline_versions_pipeline_id_version"),
        CheckConstraint("version >= 1", name="ck_pipeline_versions_version_positive"),
    )

    def __repr__(self) -> str:
        return f"<PipelineVersion {self.id} pipeline={self.pipeline_id} v{self.version}>"


class PipelineRun(Base):
    """One execution of a pinned pipeline version."""

    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # No ondelete on the two pipeline FKs: a pipeline or version with runs must not be deletable.
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipelines.id", name="fk_pipeline_runs_pipeline_id"), nullable=False
    )
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_versions.id", name="fk_pipeline_runs_pipeline_version_id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_pipeline_runs_user_id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False)
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How the run ended (ADR-014 §5a). A new stop_reason value widens its CHECK in a migration.
    stopped_at_block_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(VARCHAR(30), nullable=True)
    failed_block_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when rollback starts; while the run is running, non-null means "undoing deliveries".
    rollback_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A monitor's check (ADR-016 §2b). NULL monitor_id: a hand-run, or its monitor was deleted.
    monitor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("monitors.id", ondelete="SET NULL", name="fk_pipeline_runs_monitor_id"),
        nullable=True,
    )
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "pipeline_runs.id", ondelete="SET NULL", name="fk_pipeline_runs_baseline_run_id"
        ),
        nullable=True,
    )
    # The first change gate's verdict; NULL = the run has no change gate.
    change_status: Mapped[str | None] = mapped_column(VARCHAR(20), nullable=True)
    change_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    carried_sources: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    captured_values: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Written with the outcome by the workflow; a later writer never sets it (§4a).
    baseline_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_pipeline_runs_status",
        ),
        CheckConstraint(
            "stop_reason IS NULL OR stop_reason IN "
            "('rule_not_met', 'approval_rejected', 'approval_expired', 'approval_superseded')",
            name="ck_pipeline_runs_stop_reason",
        ),
        CheckConstraint(
            "(stopped_at_block_id IS NULL) = (stop_reason IS NULL)",
            name="ck_pipeline_runs_stop_reason_with_block",
        ),
        CheckConstraint(
            "stopped_at_block_id IS NULL OR status = 'completed'",
            name="ck_pipeline_runs_stopped_is_completed",
        ),
        CheckConstraint(
            "change_status IS NULL OR change_status IN "
            "('first_check', 'no_baseline', 'changed', 'unchanged')",
            name="ck_pipeline_runs_change_status",
        ),
        UniqueConstraint("workflow_id", name="uq_pipeline_runs_workflow_id"),
        Index("idx_pipeline_runs_user_id_created_at", "user_id", "created_at"),
        Index("idx_pipeline_runs_pipeline_id_created_at", "pipeline_id", "created_at"),
        # Serves the quota_active_submissions pipeline arm.
        Index(
            "idx_pipeline_runs_user_id_running",
            "user_id",
            postgresql_where=text("status = 'running'"),
        ),
        # The baseline lookup (§4b) and a monitor's history.
        Index(
            "idx_pipeline_runs_monitor_id_baseline",
            "monitor_id",
            text("created_at DESC"),
            postgresql_where=text("baseline_eligible"),
        ),
        Index("idx_pipeline_runs_monitor_id_created_at", "monitor_id", text("created_at DESC")),
    )

    def __repr__(self) -> str:
        return f"<PipelineRun {self.id} pipeline={self.pipeline_id} status={self.status}>"


class PipelineRunBlock(Base):
    """One block's execution within a pipeline run."""

    __tablename__ = "pipeline_run_blocks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "pipeline_runs.id", ondelete="CASCADE", name="fk_pipeline_run_blocks_pipeline_run_id"
        ),
        nullable=False,
    )
    block_id: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(VARCHAR(40), nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="pending")
    input_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How the block ended (ADR-014 §5a). NULL status_reason on a skipped block = not recorded.
    status_reason: Mapped[str | None] = mapped_column(VARCHAR(30), nullable=True)
    reason_block_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # A sink's delivery and its undo (PRD-017); first written in group M.
    delivery: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    undo_status: Mapped[str | None] = mapped_column(VARCHAR(20), nullable=True)
    undo_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A change gate's input, SHA-256 hex (ADR-016 §5c); outlives the stored copy.
    fingerprint: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    # An Approval block (ADR-016 §9a). The answer is written once, conditionally on IS NULL.
    approval_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_answer: Mapped[str | None] = mapped_column(VARCHAR(20), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", name="fk_pipeline_run_blocks_answered_by"),
        nullable=True,
    )
    superseded_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "pipeline_runs.id",
            ondelete="SET NULL",
            name="fk_pipeline_run_blocks_superseded_by_run_id",
        ),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'waiting')",
            name="ck_pipeline_run_blocks_status",
        ),
        CheckConstraint(
            "status_reason IS NULL OR status_reason IN "
            "('stopped', 'upstream_failed', 'cancelled', 'not_started', 'alert_only', "
            "'awaiting_approval')",
            name="ck_pipeline_run_blocks_status_reason",
        ),
        CheckConstraint(
            "status_reason IS NULL OR status IN ('skipped', 'waiting')",
            name="ck_pipeline_run_blocks_status_reason_status",
        ),
        CheckConstraint(
            "undo_status IS NULL OR undo_status IN "
            "('undoing', 'undone', 'undo_failed', 'kept', 'not_undoable')",
            name="ck_pipeline_run_blocks_undo_status",
        ),
        CheckConstraint(
            "undo_status IS NULL OR status = 'completed'",
            name="ck_pipeline_run_blocks_undo_status_completed",
        ),
        CheckConstraint(
            "approval_answer IS NULL OR approval_answer IN "
            "('approved', 'rejected', 'expired', 'superseded')",
            name="ck_pipeline_run_blocks_approval_answer",
        ),
        CheckConstraint("position >= 0", name="ck_pipeline_run_blocks_position_nonnegative"),
        UniqueConstraint(
            "pipeline_run_id", "block_id", name="uq_pipeline_run_blocks_pipeline_run_id_block_id"
        ),
        # The SET NULL actions' lookups when a run or a user is deleted.
        Index(
            "idx_pipeline_run_blocks_superseded_by_run_id",
            "superseded_by_run_id",
            postgresql_where=text("superseded_by_run_id IS NOT NULL"),
        ),
        Index(
            "idx_pipeline_run_blocks_answered_by",
            "answered_by",
            postgresql_where=text("answered_by IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<PipelineRunBlock {self.block_id} run={self.pipeline_run_id} status={self.status}>"
