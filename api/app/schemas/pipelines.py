import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


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


class PipelineVersionResponse(BaseModel):
    version: int
    definition: dict[str, Any]
    created_at: datetime


class AdminPipelineResponse(PipelineResponse):
    user_id: uuid.UUID
    user_email: str | None
    deleted_at: datetime | None


class PipelineRunCreate(BaseModel):
    # One value per input the pipeline declares.
    inputs: dict[str, str] = {}
    # The version to run; omitted = the latest.
    version: int | None = Field(default=None, ge=1)


class PipelineRunBlockResponse(BaseModel):
    block_id: str
    position: int
    type: str
    status: str
    input_ref: str | None
    output_ref: str | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class PipelineRunResponse(BaseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    pipeline_version_id: uuid.UUID
    status: str
    inputs: dict[str, str]
    result_path: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    blocks: list[PipelineRunBlockResponse]


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
