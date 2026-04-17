import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

if TYPE_CHECKING:
    pass


class JobSecretType(str, enum.Enum):
    proxy = "proxy"
    cookies = "cookies"


class JobSecrets(Base):
    __tablename__ = "job_secrets"
    __table_args__ = (UniqueConstraint("job_id", "secret_type", name="uq_job_secrets_job_type"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="cascade"), nullable=False, index=True
    )
    secret_type: Mapped[JobSecretType] = mapped_column(
        Enum(JobSecretType, name="job_secret_type"), nullable=False
    )
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
