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
    negate: bool = False
    schema_: dict[str, Any] = Field(alias="schema")


class PresentRule(BaseModel):
    kind: Literal["present"]
    negate: bool = False
    path: str


class TypeRule(BaseModel):
    kind: Literal["type"]
    negate: bool = False
    path: str
    type: Literal["string", "number", "integer", "boolean", "object", "array", "null"]


class CompareRule(BaseModel):
    kind: Literal["compare"]
    negate: bool = False
    path: str
    op: Literal["eq", "ne", "lt", "le", "gt", "ge"]
    value: str | int | float | bool | None


class ContainsRule(BaseModel):
    kind: Literal["contains"]
    negate: bool = False
    text: str


class MinLengthRule(BaseModel):
    kind: Literal["min_length"]
    negate: bool = False
    chars: int


class ChangedRule(BaseModel):
    """Met when the input's fingerprint equals none of ``known_fingerprints`` (ADR-016 §5d)."""

    kind: Literal["changed"]
    negate: bool = False


Rule = Annotated[
    JsonSchemaRule
    | PresentRule
    | TypeRule
    | CompareRule
    | ContainsRule
    | MinLengthRule
    | ChangedRule,
    Field(discriminator="kind"),
]

FingerprintOf = Literal["baseline", "waiting"]


class ValidateInput(BaseModel):
    input_path: NonEmptyStr
    rules: list[Rule] = Field(min_length=1)
    when_unmet: Literal["fail", "stop"] = "fail"
    # SHA-256 hex of earlier checks' gate inputs; read by a `changed` rule only.
    known_fingerprints: dict[FingerprintOf, str] = {}


class ValidateVerdict(BaseModel):
    """Stop mode's answer; fail mode returns None."""

    outcome: Literal["met", "not_met"]
    # The first rule not met, as saved (by alias); None when every rule is met.
    rule_index: int | None = None
    rule: dict[str, Any] | None = None
    found: Any = None
    expected: Any = None
    # A `changed` rule's: SHA-256 hex of the input bytes, and which known one it equals.
    fingerprint: str | None = None
    matched: FingerprintOf | None = None


# Must equal the API's DIFF_ACTIVITY and CAPTURE_ACTIVITY (both on CONTENT_QUEUE).
DIFF_ACTIVITY = "Diff"
CAPTURE_ACTIVITY = "Capture"


class DiffInput(BaseModel):
    """Compare the baseline's object with this check's (ADR-016 §6a)."""

    # The block that triggered the diff: diff.json is written beside its output.
    artifact_id: NonEmptyStr
    kind: Literal["page", "extraction"]
    baseline_path: NonEmptyStr
    current_path: NonEmptyStr


class PathChange(BaseModel):
    path: str
    change: Literal["added", "removed", "changed"]
    # Each cut to 200 characters of JSON (then a string); absent on the side that has none.
    old: Any = None
    new: Any = None


class DiffSummary(BaseModel):
    """At most 16 KiB of JSON. ``unavailable`` here only means too large to compare."""

    status: Literal["ok", "baseline_gone", "unavailable"]
    kind: Literal["page", "extraction"]
    reason: str | None = None
    # page: unified diff lines (3 lines of context), each cut to 200 characters.
    lines: list[str] = []
    lines_added: int = 0
    lines_removed: int = 0
    # extraction: one entry per changed leaf, by position in lists.
    changes: list[PathChange] = []
    paths_changed: int = 0
    # Something was left out: lines, paths, or characters of a value.
    truncated: bool = False


class DiffOutput(BaseModel):
    summary: DiffSummary
    # history/{artifact_id}/diff.json, for the workflow to record; None when nothing was compared.
    diff: StoredObject | None = None


class CarriedValue(BaseModel):
    """One carried input read from the block's output (ADR-016 §6b)."""

    input: NonEmptyStr
    # RFC 6901, as Validate reads it.
    path: str
    type: Literal["number", "text"]


class CaptureInput(BaseModel):
    input_path: NonEmptyStr
    values: list[CarriedValue] = Field(min_length=1)


class CapturedValue(BaseModel):
    """Exactly one of the two is set."""

    value: int | float | str | None = None
    missing: str | None = None


class CaptureOutput(BaseModel):
    values: dict[str, CapturedValue]
