"""Consumer-side twin of the API's LLM activity contracts (api/app/workflows/activities/contracts.py)."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]

# Must equal the API's LLM_EXTRACT_ACTIVITY and LLM_QUEUE — a mismatch raises no error; the task
# waits on a queue nobody polls until its timeout fires.
LLM_EXTRACT_ACTIVITY = "LLMExtract"
LLM_QUEUE = "llm"


class LLMInput(BaseModel):
    artifact_id: NonEmptyStr
    raw_minio_path: str
    provider: Literal["anthropic", "openai_compatible"]
    encrypted_api_key: str
    base_url: str | None = None
    model: str
    output_schema: dict[str, Any]


class StoredObject(BaseModel):
    path: NonEmptyStr
    size: Annotated[int, Field(ge=0)]


class LLMOutput(BaseModel):
    result: StoredObject
