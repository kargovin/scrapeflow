import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class StorageObject(Base):
    """One row per object held in MinIO on a user's behalf — the storage ledger (ADR-009 §8d).

    The row is the unit of accounting. `user_quotas.storage_bytes_used` is a materialised
    sum of `bytes` over this table, maintained in the same transaction as every insert and
    delete here (`app/core/ledger.py`); `scripts/reconcile_storage_ledger.py` recomputes it
    from the rows and from MinIO when the two are suspected to have drifted.

    The meter reads `user_id` and `bytes` only. The producer link — which row made this
    object — is nullable FKs used solely by the delete and collection paths, never by the
    meter, so a lane that forgets to widen the CHECK below fails loudly on its first insert
    while the meter stays correct.

    Widening for a new lane: add the nullable FK column, then drop and recreate
    `ck_storage_objects_one_producer` listing it. Both in the migration that adds the lane.
    `webhook_deliveries` shows what happens otherwise — its closed CHECK structurally
    rejects the pipeline lane.
    """

    __tablename__ = "storage_objects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 'bucket/key', the same form job_runs.result_path holds — so one delete helper serves both.
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_runs.id", ondelete="CASCADE"), nullable=True
    )
    crawl_page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("crawl_pages.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        CheckConstraint("bytes >= 0", name="ck_storage_objects_bytes_nonnegative"),
        CheckConstraint(
            "num_nonnulls(job_run_id, crawl_page_id) = 1",
            name="ck_storage_objects_one_producer",
        ),
        Index("idx_storage_objects_user_id", "user_id"),
        Index(
            "idx_storage_objects_job_run_id",
            "job_run_id",
            postgresql_where=text("job_run_id IS NOT NULL"),
        ),
        Index(
            "idx_storage_objects_crawl_page_id",
            "crawl_page_id",
            postgresql_where=text("crawl_page_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<StorageObject {self.object_key} user={self.user_id} bytes={self.bytes}>"
