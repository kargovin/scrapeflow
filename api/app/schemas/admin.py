import uuid
from datetime import datetime

from pydantic import BaseModel, Field

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
    # Exactly one of the three owners; a crawl's row has no run.
    job_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    crawl_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
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
    # ADR-009 15e: jobs, batches and crawls only. A pipeline delivers through its Webhook block and
    # writes no webhook_deliveries row; its failures are on its run.
    job_lane_webhooks_pending: int
    job_lane_webhooks_exhausted: int
    # Every lane (ADR-009 16c): a schedule moved to Temporal stays `active` on its row.
    active_recurring_jobs: int
    # Which engine fires them; a `v1` count after the schedule cutover is a stranded schedule.
    active_recurring_jobs_by_lane: dict[str, int]
    # v1 from its clock column, v2 from the cron (core/schedules.next_run).
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
    # Jobs, batches and crawls only (15e), as above.
    job_lane_webhook_success_rate_7d: float


class AdminStatsResponse(BaseModel):
    operational: OperationalStats
    historical: HistoricalStats


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


class UserQuotaPatch(BaseModel):
    """All fields are optional — only provided fields are upserted."""

    monthly_runs_limit: int | None = Field(default=None, ge=1)
    concurrent_jobs_limit: int | None = Field(default=None, ge=1)
    storage_bytes_limit: int | None = Field(default=None, ge=1)
    # ADR-013: active scheduled jobs; 0 refuses every new schedule.
    scheduled_jobs_limit: int | None = Field(default=None, ge=0)


class UserQuotaResponse(BaseModel):
    monthly_runs_limit: int | None
    concurrent_jobs_limit: int | None
    storage_bytes_limit: int | None
    scheduled_jobs_limit: int | None
    storage_bytes_used: int
    updated_at: datetime

    model_config = {"from_attributes": True}
