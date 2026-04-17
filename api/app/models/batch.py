import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import VARCHAR, Boolean, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.user import User


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="queued")
    output_format: Mapped[str] = mapped_column(
        VARCHAR(20), nullable=False, server_default="markdown"
    )
    engine: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="http")
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    respect_robots: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User")
    items: Mapped[list["BatchItem"]] = relationship("BatchItem", back_populates="batch")


class BatchItem(Base):
    __tablename__ = "batch_items"
    __table_args__ = (Index("idx_batch_items_batch_id", "batch_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="pending")
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch: Mapped["Batch"] = relationship("Batch", back_populates="items")
