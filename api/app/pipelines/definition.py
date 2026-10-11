"""Reading a stored pipeline definition back as typed blocks (ADR-009 §4, §6).

Pure: no settings, no I/O. The run trigger parses a pinned version with ``parse_definition``
before it writes any row; ``PipelineWorkflow`` receives the parsed blocks as its input.

Parse, never re-validate: a pinned version keeps the rules of the day it was saved, so a rule
tightened since must not strand it. ``validate()`` runs at save time only.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.pipelines.catalog import (
    CATALOG,
    CleanConfigV1,
    LLMExtractConfigV1,
    ScrapeConfigV1,
    ValidateConfigV1,
    WebhookConfigV1,
    run_need_seconds,
)


class StoredBlock(BaseModel):
    """One block as ``pipeline_versions.definition`` holds it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    input: str | None = None
    config_schema_version: int
    config: dict[str, Any]
    # Secret config fields as Fernet ciphertext (credentials key); their config value is null.
    secrets: dict[str, str] = {}


class StoredDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, dict[str, Any]] = {}
    time_budget_seconds: int | None = None
    blocks: list[StoredBlock] = Field(min_length=1)


class _RunBlock(BaseModel):
    id: str
    input: str | None = None


class ScrapeBlock(_RunBlock):
    type: Literal["scrape"] = "scrape"
    config: ScrapeConfigV1


class CleanBlock(_RunBlock):
    type: Literal["clean"] = "clean"
    config: CleanConfigV1


class LLMExtractBlock(_RunBlock):
    type: Literal["llm_extract"] = "llm_extract"
    config: LLMExtractConfigV1


class ValidateBlock(_RunBlock):
    type: Literal["validate"] = "validate"
    config: ValidateConfigV1


class WebhookBlock(_RunBlock):
    type: Literal["webhook"] = "webhook"
    config: WebhookConfigV1


RunBlock = Annotated[
    ScrapeBlock | CleanBlock | LLMExtractBlock | ValidateBlock | WebhookBlock,
    Field(discriminator="type"),
]

# One config shape per type today. A V2 config fails the parse below until the interpreter
# handles it or an upgrade runs here (§4's historical-shape obligation — decide with the first V2).
_RUN_BLOCKS: dict[str, type[_RunBlock]] = {
    "scrape": ScrapeBlock,
    "clean": CleanBlock,
    "llm_extract": LLMExtractBlock,
    "validate": ValidateBlock,
    "webhook": WebhookBlock,
}


class ParsedDefinition(BaseModel):
    # Declared run-input names.
    inputs: list[str] = []
    time_budget_seconds: int | None = None
    blocks: list[RunBlock]

    @property
    def run_budget_seconds(self) -> int:
        if self.time_budget_seconds is not None:
            return self.time_budget_seconds
        return run_need_seconds(CATALOG[b.type].timing(b.config) for b in self.blocks)


class DefinitionUnreadable(ValueError):
    """A stored definition the current code cannot read. ``problems`` are (block_id, message)."""

    def __init__(self, problems: list[tuple[str | None, str]]):
        self.problems = problems
        super().__init__("; ".join(f"{b or 'definition'}: {m}" for b, m in problems))


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'] if not str(p).startswith('<'))}: {e['msg']}"
        for e in exc.errors()
    )


def parse_definition(raw: Any) -> tuple[ParsedDefinition, dict[str, dict[str, str]]]:
    """Typed blocks, plus stored secrets by block id. Raises DefinitionUnreadable."""
    try:
        stored = StoredDefinition.model_validate(raw)
    except ValidationError as exc:
        raise DefinitionUnreadable([(None, _errors(exc))]) from None

    problems: list[tuple[str | None, str]] = []
    blocks: list[_RunBlock] = []
    for block in stored.blocks:
        block_type, run_model = CATALOG.get(block.type), _RUN_BLOCKS.get(block.type)
        if block_type is None or run_model is None:
            problems.append((block.id, f"unknown block type {block.type!r}"))
            continue
        model = block_type.config_versions.get(block.config_schema_version)
        if model is None or model is not run_model.model_fields["config"].annotation:
            problems.append(
                (
                    block.id,
                    f"{block.type} config_schema_version {block.config_schema_version} "
                    "cannot be run by this version of the service",
                )
            )
            continue
        try:
            config = model.model_validate(block.config)
        except ValidationError as exc:
            problems.append((block.id, f"config: {_errors(exc)}"))
            continue
        blocks.append(run_model(id=block.id, input=block.input, config=config))
    if problems:
        raise DefinitionUnreadable(problems)

    parsed = ParsedDefinition(
        inputs=list(stored.inputs), time_budget_seconds=stored.time_budget_seconds, blocks=blocks
    )
    return parsed, {b.id: b.secrets for b in stored.blocks if b.secrets}
