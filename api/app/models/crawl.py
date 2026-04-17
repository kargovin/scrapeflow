import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, VARCHAR, Boolean, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.user import User


class Crawl(Base):
    __tablename__ = "crawls"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    seed_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="queued")
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    include_paths: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    exclude_paths: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    ignore_sitemap: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    output_format: Mapped[str] = mapped_column(
        VARCHAR(20), nullable=False, server_default="markdown"
    )
    engine: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="http")
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    respect_robots: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    schedule_cron: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_queued: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User")
    pages: Mapped[list["CrawlPage"]] = relationship("CrawlPage", back_populates="crawl")
    queue: Mapped[list["CrawlQueueItem"]] = relationship("CrawlQueueItem", back_populates="crawl")


class CrawlPage(Base):
    __tablename__ = "crawl_pages"
    __table_args__ = (Index("idx_crawl_pages_crawl_id", "crawl_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    crawl_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crawls.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="pending")
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    crawl: Mapped["Crawl"] = relationship("Crawl", back_populates="pages")


class CrawlQueueItem(Base):
    __tablename__ = "crawl_queue"
    __table_args__ = (
        Index(
            "idx_crawl_queue_pending",
            "crawl_id",
            "created_at",
            postgresql_where="status = 'pending'",
        ),
        Index("idx_crawl_queue_url", "crawl_id", "url", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    crawl_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crawls.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, server_default="pending")
    crawl_page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("crawl_pages.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    crawl: Mapped["Crawl"] = relationship("Crawl", back_populates="queue")
    crawl_page: Mapped["CrawlPage | None"] = relationship("CrawlPage")
