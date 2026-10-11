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


# Must equal the API's CLEAN_ACTIVITY, VALIDATE_ACTIVITY and CONTENT_QUEUE.
CLEAN_ACTIVITY = "Clean"
VALIDATE_ACTIVITY = "Validate"
CONTENT_QUEUE = "content"


class CleanInput(BaseModel):
    artifact_id: NonEmptyStr
    input_path: NonEmptyStr


class CleanOutput(BaseModel):
    result: StoredObject


class JsonSchemaRule(BaseModel):
    kind: Literal["json_schema"]
    schema_: dict[str, Any] = Field(alias="schema")


class PresentRule(BaseModel):
    kind: Literal["present"]
    path: str


class TypeRule(BaseModel):
    kind: Literal["type"]
    path: str
    type: Literal["string", "number", "integer", "boolean", "object", "array", "null"]


class CompareRule(BaseModel):
    kind: Literal["compare"]
    path: str
    op: Literal["eq", "ne", "lt", "le", "gt", "ge"]
    value: str | int | float | bool | None


class ContainsRule(BaseModel):
    kind: Literal["contains"]
    text: str


class MinLengthRule(BaseModel):
    kind: Literal["min_length"]
    chars: int


Rule = Annotated[
    JsonSchemaRule
    | PresentRule
    | TypeRule
    | CompareRule
    | ContainsRule
    | MinLengthRule,
    Field(discriminator="kind"),
]


class ValidateInput(BaseModel):
    input_path: NonEmptyStr
    rules: list[Rule] = Field(min_length=1)
