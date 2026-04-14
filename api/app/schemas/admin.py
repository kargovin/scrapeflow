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


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TopUserByJobs(BaseModel):
    user_id: uuid.UUID
    email: str
    job_count: int


class OperationalStats(BaseModel):
    """Live counts — no time filter. Dicts are sparse (absent key ≡ zero)."""

    jobs_running: int
    jobs_pending: int
    jobs_by_engine: dict[str, int]
    webhook_deliveries_pending: int
    webhook_deliveries_exhausted: int
    active_recurring_jobs: int
    next_scheduled_run_at: datetime | None


class HistoricalStats(BaseModel):
    """Aggregate queries over job_runs.created_at. Dicts are sparse."""

    jobs_today: int
    jobs_this_week: int
    jobs_this_month: int
    jobs_by_status_7d: dict[str, int]
    jobs_by_engine_7d: dict[str, int]
    top_users_by_jobs: list[TopUserByJobs]
    minio_storage_bytes: int
    webhook_delivery_success_rate_7d: float


class AdminStatsResponse(BaseModel):
    operational: OperationalStats
    historical: HistoricalStats
