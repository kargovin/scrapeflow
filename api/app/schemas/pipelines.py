import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.pipelines.catalog import RunInputValue

MonitorState = Literal["active", "paused", "paused_itself"]
# ADR-016 §2b; the first change gate's verdict.
ChangeStatus = Literal["first_check", "no_baseline", "changed", "unchanged"]


class MonitorRef(BaseModel):
    id: uuid.UUID
    name: str
    state: MonitorState


class PipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Raw on purpose: validate() reports every problem, block by block.
    definition: dict[str, Any]


class PipelineUpdate(BaseModel):
    """A new name, a new definition (saved as the next version), or both."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    # Block ids carry through: a sent id is the same step, a missing one is a new step.
    # A secret field left out of a kept block's config keeps its stored value; null clears it.
    definition: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _something(self) -> "PipelineUpdate":
        if self.name is None and self.definition is None:
            raise ValueError("send a name, a definition, or both")
        return self


class PipelineResponse(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    # Secret fields are left out of each block's config; `secrets` names the ones that are set.
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    # Single-pipeline responses only (not the list): the monitors using it, which block its delete (ADR-016 §2a).
    monitors: list[MonitorRef] | None = None


class CatalogTiming(BaseModel):
    """One block's time budget, in seconds (PRD-016 R4, PRD-017 R6)."""

    attempt_seconds: int
    # How long an attempt may wait for a worker to take it.
    queue_wait_seconds: int
    attempts: int
    first_wait_seconds: int
    backoff: float
    max_wait_seconds: int
    # Every attempt and every wait at its limit.
    budget_seconds: int
    # What a run reserves for the block: the budget plus the run's own writes around it.
    need_seconds: int


class CatalogSink(BaseModel):
    reversible: bool
    # Reversible only: what one undo can take; a run reserves it per such block (rollback).
    undo: CatalogTiming | None = None


class CatalogBlock(BaseModel):
    type: str
    kind: Literal["content", "effect"]
    config_schema_version: int
    # Reference types accepted as input; empty for the starting block.
    consumes: list[str]
    # The kinds of saved destination its destination_id may name; empty if it takes none.
    destination_kinds: list[str]
    # The one kind, when it takes exactly one.
    destination_kind: str | None
    sink: CatalogSink | None
    # By configuration, where the budget depends on it.
    timings: dict[str, CatalogTiming]


class PipelineCatalogResponse(BaseModel):
    blocks: list[CatalogBlock]
    # The longest a run may take: its blocks' need plus every reversible sink's undo.
    run_limit_seconds: int


class PipelineVersionResponse(BaseModel):
    version: int
    definition: dict[str, Any]
    created_at: datetime


class AdminPipelineResponse(PipelineResponse):
    user_id: uuid.UUID
    user_email: str | None
    deleted_at: datetime | None


class PipelineRunCreate(BaseModel):
    # One value per input the pipeline declares; the trigger checks each against its type.
    inputs: dict[str, Any] = {}
    # The version to run; omitted = the latest.
    version: int | None = Field(default=None, ge=1)


# ADR-009 §8c: never produced, passed through (an effect block), stored, or collected
# (retention elapsed) — a collected output must not read as missing.
OutputState = Literal["none", "passed_through", "stored", "collected"]


# ADR-014 §5a's skip reasons, plus "not_recorded" for a skipped block from before them (§5c);
# "awaiting_approval" sits on a waiting Approval block (ADR-016 §9a).
BlockStatusReason = Literal[
    "stopped",
    "upstream_failed",
    "cancelled",
    "not_started",
    "alert_only",
    "awaiting_approval",
    "not_recorded",
]
# ADR-014 §5a; the approval endings are ADR-016 §10's.
StopReason = Literal["rule_not_met", "approval_rejected", "approval_expired", "approval_superseded"]


class PipelineRunBlockResponse(BaseModel):
    block_id: str
    position: int
    type: str
    status: str
    input_ref: str | None
    output_ref: str | None
    output_state: OutputState
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    collected_at: datetime | None
    # Why a skipped block never ran, and the block that reason points at.
    status_reason: BlockStatusReason | None = None
    reason_block_id: str | None = None
    # A stop-mode Validate that stopped the run: {rule_index, rule (as saved), found, expected}.
    verdict: dict[str, Any] | None = None
    # A sink's delivery and its undo (PRD-017); null until then.
    delivery: dict[str, Any] | None = None
    undo_status: str | None = None
    undo_error: str | None = None
    # An Approval block (ADR-016 §9a); null on every other block.
    approval_deadline: datetime | None = None
    approval_answer: Literal["approved", "rejected", "expired", "superseded"] | None = None
    answered_at: datetime | None = None
    answered_by: uuid.UUID | None = None
    superseded_by_run_id: uuid.UUID | None = None


class PipelineRunSummary(BaseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    pipeline_version_id: uuid.UUID
    version: int
    status: str
    # Set once a cancel is requested; status turns cancelled when the run has stopped (R3).
    cancel_requested_at: datetime | None
    inputs: dict[str, RunInputValue]
    result_path: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    # How it ended (ADR-014 §5a): a stop is completed + these two; the list's label reads them.
    stopped_at_block_id: str | None = None
    stop_reason: StopReason | None = None
    failed_block_id: str | None = None
    # Non-null while running = undoing deliveries (PRD-017).
    rollback_deadline: datetime | None = None
    # A monitor's check (ADR-016 §2b); null monitor_id = a hand-run, or its monitor was deleted.
    monitor_id: uuid.UUID | None = None
    change_status: ChangeStatus | None = None


class PipelineRunAlert(BaseModel):
    """One message this run queued: a failure alert (ADR-014 §8e) or an Approval block's
    (ADR-016 §9c). Users see an undeliverable one here."""

    block_id: str
    event: str = "pipeline.failed"
    kind: Literal["webhook", "email"] = "webhook"
    # Set when the block sends to a saved destination; null after that destination is deleted.
    destination_id: uuid.UUID | None = None
    status: Literal["pending", "delivered", "exhausted"]
    attempts: int
    last_error: str | None
    next_attempt_at: datetime
    delivered_at: datetime | None
    created_at: datetime
    # What the alert says rollback did: {undone, left_in_place, kept}, each [{block_id, kind, location}].
    rollback: dict[str, list[dict[str, Any]]] | None = None


class PipelineRunResponse(PipelineRunSummary):
    blocks: list[PipelineRunBlockResponse]
    alerts: list[PipelineRunAlert] = []
    # The Approval block's request, reminder, expired and superseded messages.
    approval_messages: list[PipelineRunAlert] = []
    # The check this one was compared with; null with a change_status other than first_check
    # (and no_baseline) = compared with a check since deleted.
    baseline_run_id: uuid.UUID | None = None
    # The bounded diff summaries, by the block that triggered each (ADR-016 §6a).
    change_summary: dict[str, Any] | None = None
    # Each carried input: {from: baseline|start, run_id | why}.
    carried_sources: dict[str, Any] | None = None


class PipelineRunDiffResponse(BaseModel):
    """The full comparison a check stored for one block (ADR-016 §6a)."""

    block_id: str
    diff_path: str
    diff: Any


class AdminPipelineRunSummary(PipelineRunSummary):
    user_id: uuid.UUID


class AdminPipelineRunResponse(PipelineRunResponse):
    user_id: uuid.UUID


class PipelineRunResultResponse(BaseModel):
    content: str
    result_path: str
    # Which block's output this is: the run's result, or ?block= for one step's.
    block_id: str


class PipelineRunCancelResponse(BaseModel):
    """A cancel is requested, not done: the run turns ``cancelled`` at its next block boundary (R3)."""

    run: PipelineRunResponse
    # Blocks that may still be executing; none is aborted mid-execution.
    still_running: list[str]
    # Worst case until the run stops: what is left of those blocks' declared budgets.
    upper_bound_seconds: int
    # An LLM block was called, so the user's key may already have been billed.
    llm_billed: bool
    # False = the engine was unreachable; the run still stops, at its next block boundary.
    workflow_notified: bool
