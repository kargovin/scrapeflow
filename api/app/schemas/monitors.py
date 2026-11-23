import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from app.pipelines.catalog import JsonPointer
from app.schemas.pipelines import MonitorRef, MonitorState, PipelineRunSummary

__all__ = ["MonitorRef", "MonitorState"]


class CarriedInput(BaseModel):
    """A run input each check takes from the previous check's output (ADR-016 §6b)."""

    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1)
    # A content block of the pinned version whose output is an extraction or a JSON page.
    from_block: str = Field(min_length=1)
    path: JsonPointer
    # The value until a check has captured one; of the input's type.
    start: StrictInt | StrictFloat | StrictStr


class MonitorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    pipeline_id: uuid.UUID
    # The version to pin, fixed for life; omitted = the latest.
    version: int | None = Field(default=None, ge=1)
    # Each declared run input is in exactly one of these two.
    inputs: dict[str, Any] = {}
    carried: list[CarriedInput] = []
    schedule_cron: str = Field(min_length=1, max_length=200)
    # Where notices are also sent (ADR-016 §8): a webhook or smtp destination of the user's.
    notice_destination_id: uuid.UUID | None = None


# D15: a monitor's pipeline, version and inputs are fixed for its life.
_FIXED = ("pipeline_id", "version", "pipeline_version_id", "inputs", "carried")


class MonitorUpdate(BaseModel):
    """Only the name, the schedule and the notice destination change; anything else is a new
    monitor. ``notice_destination_id: null`` stops sending notices."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule_cron: str | None = Field(default=None, min_length=1, max_length=200)
    notice_destination_id: uuid.UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def _only_name_and_schedule(cls, data: Any) -> Any:
        if isinstance(data, dict):
            fixed = sorted(k for k in data if k in _FIXED)
            other = sorted(k for k in data if k not in _FIXED and k not in cls.model_fields)
            if fixed:
                raise ValueError(
                    f"{', '.join(fixed)} cannot be changed: a monitor's pipeline, version and "
                    "inputs are fixed for its life; create a new monitor instead"
                )
            if other:
                raise ValueError(f"unknown fields: {', '.join(other)}")
        return data

    @model_validator(mode="after")
    def _something(self) -> "MonitorUpdate":
        if not self.model_fields_set & {"name", "schedule_cron", "notice_destination_id"}:
            raise ValueError("send a name, a schedule_cron or a notice_destination_id")
        return self


class LastCheck(BaseModel):
    run_id: uuid.UUID
    status: str
    change_status: str | None
    stop_reason: str | None
    created_at: datetime
    finished_at: datetime | None


class MonitorResponse(BaseModel):
    id: uuid.UUID
    name: str
    pipeline_id: uuid.UUID
    pipeline_version_id: uuid.UUID
    version: int
    # The pipeline's newest version, when it is newer than the pinned one.
    newer_version: int | None
    inputs: dict[str, Any]
    carried: list[dict[str, Any]]
    schedule_cron: str
    notice_destination_id: uuid.UUID | None
    state: MonitorState
    consecutive_failures: int
    next_check_at: datetime | None
    last_check: LastCheck | None
    created_at: datetime
    updated_at: datetime


class MonitorCreatedResponse(MonitorResponse):
    # ADR-016 §2d: what the save allowed but the user should know.
    warnings: list[str] = []


MonitorEventKind = Literal[
    "skipped_monthly_runs",
    "skipped_check_in_progress",
    "skipped_pipelines_disabled",
    "waiting_for_slot",
    "gave_up_waiting",
    "paused_itself",
    "approval_expired",
]


class MonitorNotice(BaseModel):
    """The notice a situation sent to the monitor's destination (§8); undelivered = ``exhausted``."""

    kind: Literal["webhook", "email"]
    # Null once that destination is deleted.
    destination_id: uuid.UUID | None
    status: Literal["pending", "delivered", "exhausted"]
    attempts: int
    last_error: str | None
    next_attempt_at: datetime
    delivered_at: datetime | None


class MonitorEventResponse(BaseModel):
    """One situation (ADR-016 §7b): identical firings are one row with a count."""

    id: uuid.UUID
    kind: MonitorEventKind
    first_at: datetime
    last_at: datetime
    count: int
    # Null while the situation is still going on.
    ended_at: datetime | None
    detail: dict[str, Any] | None
    # Null: a history-only kind, or no notice destination was set when it began.
    notice: MonitorNotice | None = None


class MonitorHistoryItem(BaseModel):
    """A check or a situation, newest first; exactly one of the two is set."""

    type: Literal["check", "event"]
    at: datetime
    check: PipelineRunSummary | None = None
    event: MonitorEventResponse | None = None


class AdminMonitorResponse(MonitorResponse):
    user_id: uuid.UUID
    user_email: str | None


class AdminMonitorDetail(AdminMonitorResponse):
    history: list[MonitorHistoryItem]
