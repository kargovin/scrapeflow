import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Raw on purpose: validate() reports every problem, block by block.
    definition: dict[str, Any]


class PipelineResponse(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PipelineRunCreate(BaseModel):
    # One value per input the pipeline declares.
    inputs: dict[str, str] = {}


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
