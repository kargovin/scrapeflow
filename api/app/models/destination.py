import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    VARCHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# ADR-015 §2a; all four admitted from the first migration.
DESTINATION_KINDS = ("bucket", "postgres", "smtp", "webhook")


class Destination(Base):
    """A user's saved place to deliver to (ADR-015 §2a)."""

    __tablename__ = "destinations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_destinations_user_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    # Non-secret settings, by kind; returned by the API.
    address: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Fernet (DESTINATION_ENCRYPTION_KEY) of a JSON object, by kind; never returned.
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    # Written at save; a webhook URL is shown host-only from then on.
    display_host: Mapped[str] = mapped_column(Text, nullable=False)
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
        UniqueConstraint("user_id", "name", name="uq_destinations_user_id_name"),
        CheckConstraint(
            f"kind IN ({', '.join(repr(k) for k in DESTINATION_KINDS)})",
            name="ck_destinations_kind",
        ),
    )

    def __repr__(self) -> str:
        return f"<Destination {self.id} user={self.user_id} kind={self.kind}>"


class PipelineVersionDestination(Base):
    """A destination a pinned version refers to; the delete-in-use check reads it (ADR-015 §2b)."""

    __tablename__ = "pipeline_version_destinations"

    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "pipeline_versions.id",
            ondelete="CASCADE",
            name="fk_pipeline_version_destinations_pipeline_version_id",
        ),
        primary_key=True,
    )
    # CASCADE: a soft-deleted pipeline keeps its versions and these rows.
    destination_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "destinations.id",
            ondelete="CASCADE",
            name="fk_pipeline_version_destinations_destination_id",
        ),
        primary_key=True,
    )

    __table_args__ = (Index("idx_pipeline_version_destinations_destination_id", "destination_id"),)
