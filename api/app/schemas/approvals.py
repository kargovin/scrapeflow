import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.pipelines import ChangeStatus

# The recorded answer, else where the block is (core/approvals.approval_state).
ApprovalState = Literal[
    "not_requested", "waiting", "approved", "rejected", "expired", "superseded", "closed"
]


class ApprovalAnswer(BaseModel):
    decision: Literal["approved", "rejected"]


class ApprovalResponse(BaseModel):
    """A run's Approval block (ADR-016 §9): what it asks, by when, and how it stands."""

    pipeline_id: uuid.UUID
    pipeline_name: str
    run_id: uuid.UUID
    block_id: str
    # A monitor's check; null for a hand-run.
    monitor_id: uuid.UUID | None
    run_status: str
    state: ApprovalState
    requested_at: datetime | None
    deadline: datetime | None
    answered_at: datetime | None
    answered_by: uuid.UUID | None
    superseded_by_run_id: uuid.UUID | None
    # What changed against the baseline, and what this run has already delivered (§9c).
    change_status: ChangeStatus | None
    change_summary: dict[str, Any] | None
    deliveries: list[dict[str, Any]]


class AdminApprovalResponse(ApprovalResponse):
    user_id: uuid.UUID
    user_email: str | None
