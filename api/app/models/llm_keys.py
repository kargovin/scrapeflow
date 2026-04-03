import uuid
from datetime import UTC, datetime

from sqlalchemy import VARCHAR, CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UserLLMKey(Base):
    __tablename__ = "user_llm_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    provider: Mapped[str] = mapped_column(
        VARCHAR(20),
        CheckConstraint("provider IN ('openai_compatible', 'anthropic')"),
        nullable=False,
    )
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
