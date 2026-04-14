import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.users import UserResponse


class AdminUserResponse(UserResponse):
    """UserResponse extended with is_admin. Inherits from_attributes=True."""

    is_admin: bool


class AdminUserDetailResponse(AdminUserResponse):
    """Admin user detail with job run counts grouped by status.

    job_counts is a sparse dict — only statuses that have at least one run
    are present. Callers should use .get("pending", 0) defensively.
    Cannot be populated via from_attributes; always constructed explicitly.
    """

    job_counts: dict[str, int]


class AdminWebhookDeliveryResponse(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    run_id: uuid.UUID
    webhook_url: str
    payload: dict
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
