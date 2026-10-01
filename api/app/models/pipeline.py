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

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_pipeline_runs_status",
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

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'waiting')",
            name="ck_pipeline_run_blocks_status",
        ),
        CheckConstraint("position >= 0", name="ck_pipeline_run_blocks_position_nonnegative"),
        UniqueConstraint(
            "pipeline_run_id", "block_id", name="uq_pipeline_run_blocks_pipeline_run_id_block_id"
        ),
    )

    def __repr__(self) -> str:
        return f"<PipelineRunBlock {self.block_id} run={self.pipeline_run_id} status={self.status}>"
