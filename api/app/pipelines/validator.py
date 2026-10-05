"""Save-time validation of a pipeline definition (PRD-016 R1, R4; ADR-009 §4, §8).

Pure apart from reading limits from settings. Nothing here checks what needs the database or the
network — LLM key ownership, SSRF on literal URLs, the per-user pipeline count: those are C.4's.
"""

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.pipelines.catalog import (
    BLOCK_OVERHEAD_SECONDS,
    CATALOG,
    SCRAPE,
    WEBHOOK,
    BlockType,
    Timing,
    is_binding,
)
from app.settings import settings

_NAME = r"^[a-z][a-z0-9_]{0,62}$"
BlockId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


class RunInputDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Widen alongside BlockType.bindable_fields; Scrape's url is the only bindable field today.
    type: Literal["url"]


class BlockDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: BlockId
    type: str
    # The id of the block whose output this one consumes; None only on the starting block.
    input: str | None = None
    # None = the type's current version.
    config_schema_version: int | None = None
    config: dict[str, Any] = {}


class PipelineDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, RunInputDeclaration] = {}
    blocks: list[BlockDefinition] = Field(min_length=1)
    # The run ceiling. None = the sum of what the blocks need.
    time_budget_seconds: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class Problem:
    """One reason a definition was refused. ``block_id`` is None for pipeline-level problems."""

    block_id: str | None
    message: str

    def __str__(self) -> str:
        return f"block '{self.block_id}': {self.message}" if self.block_id else self.message


class PipelineValidationError(ValueError):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("; ".join(str(p) for p in problems))


@dataclass(frozen=True)
class ValidatedBlock:
    id: str
    type: BlockType
    config: BaseModel
    timing: Timing


@dataclass(frozen=True)
class ValidatedPipeline:
    # What C.4 stores in pipeline_versions.definition: versions stamped, config defaults filled.
    definition: dict[str, Any]
    blocks: tuple[ValidatedBlock, ...]
    run_budget_seconds: int


def _loc(loc: tuple[str | int, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif not part.startswith("<"):  # discriminator tags, see catalog.BindableUrl
            out += f".{part}" if out else part
    return out


def _pydantic_problems(
    exc: ValidationError, block_id: str | None, prefix: str = ""
) -> list[Problem]:
    problems = []
    for err in exc.errors():
        where = _loc(err["loc"])
        where = f"{prefix}.{where}" if prefix and where else prefix or where
        msg = err["msg"].removeprefix("Value error, ")
        problems.append(Problem(block_id, f"{where}: {msg}" if where else msg))
    return problems


def _shape_problems(exc: ValidationError, raw: Any) -> list[Problem]:
    """Top-level shape errors, attributed to a block where the location allows."""
    problems = []
    raw_blocks = raw.get("blocks") if isinstance(raw, dict) else None
    for err in exc.errors():
        loc = err["loc"]
        block_id = None
        if len(loc) >= 2 and loc[0] == "blocks" and isinstance(loc[1], int):
            block = raw_blocks[loc[1]] if isinstance(raw_blocks, list) else None
            block_id = block.get("id") if isinstance(block, dict) else None
            if not isinstance(block_id, str):
                block_id = None
        where = _loc(loc)
        problems.append(Problem(block_id, f"{where}: {err['msg']}" if where else err["msg"]))
    return problems


def _parse_block(
    block: BlockDefinition, declared: set[str], used: set[str]
) -> tuple[ValidatedBlock | None, list[Problem]]:
    block_type = CATALOG.get(block.type)
    if block_type is None:
        known = ", ".join(sorted(CATALOG))
        return None, [Problem(block.id, f"unknown block type {block.type!r}; known: {known}")]

    version = block.config_schema_version or block_type.current_version
    model = block_type.config_versions.get(version)
    if model is None:
        return None, [Problem(block.id, f"{block.type} has no config schema version {version}")]

    problems = []
    for name, value in block.config.items():
        if not is_binding(value):
            continue
        used.add(value.get("$input"))
        if name not in block_type.bindable_fields:
            allowed = ", ".join(sorted(block_type.bindable_fields)) or "nothing"
            problems.append(
                Problem(
                    block.id,
                    f"config.{name} cannot take a run input; {block.type} binds: {allowed}",
                )
            )
        elif value.get("$input") not in declared:
            problems.append(
                Problem(
                    block.id,
                    f"config.{name} uses run input {value.get('$input')!r}, "
                    "which the pipeline does not declare",
                )
            )
    if problems:
        return None, problems

    try:
        config = model.model_validate(block.config)
    except ValidationError as exc:
        return None, _pydantic_problems(exc, block.id, "config")
    return ValidatedBlock(block.id, block_type, config, block_type.timing(config)), []


def _starting_block_problems(blocks: list[BlockDefinition]) -> list[Problem]:
    """ADR-009 §8: exactly one starting block, and it is a Scrape.

    Its own rule on purpose. The chain rule alone would admit a second Scrape that names the
    previous block as its input, and the metering rule (one run = one fetch) rests on this.
    """
    problems = []
    first = blocks[0]
    if first.type != SCRAPE.name:
        problems.append(
            Problem(first.id, f"a pipeline must start with a scrape block, not {first.type}")
        )
    elif first.input is not None:
        problems.append(Problem(first.id, "the starting scrape block takes no input"))
    for block in blocks[1:]:
        if block.type == SCRAPE.name:
            problems.append(
                Problem(
                    block.id,
                    "a pipeline has exactly one starting block, and it is the first scrape; "
                    "a second scrape cannot be placed anywhere else",
                )
            )
        elif block.input is None:
            problems.append(
                Problem(
                    block.id,
                    "a pipeline has exactly one starting block; this block has no input",
                )
            )
    return problems


def _chain_problems(blocks: list[BlockDefinition]) -> list[Problem]:
    """ADR-009 §4: a single chain in data flow — block n consumes block n-1."""
    problems = []
    position = {b.id: i for i, b in enumerate(blocks)}
    for i, block in enumerate(blocks[1:], start=1):
        if block.input is None or block.type == SCRAPE.name:
            continue  # the starting-block rule's
        previous = blocks[i - 1].id
        if block.input == previous:
            continue
        if block.input not in position:
            msg = f"input {block.input!r} is not a block in this pipeline"
        elif position[block.input] >= i:
            msg = f"input {block.input!r} does not come before this block"
        else:
            msg = (
                f"input must be the previous block {previous!r}; consuming an earlier block "
                "(data-flow fan-out) is not supported yet"
            )
        problems.append(Problem(block.id, msg))
    return problems


def _type_problems(parsed: list[ValidatedBlock]) -> list[Problem]:
    """Each block accepts the reference type its predecessor produces."""
    problems = []
    ref: str | None = None
    previous: ValidatedBlock | None = None
    for block in parsed:
        if previous is not None and not block.type.is_source:
            if ref not in block.type.consumes:
                accepted = ", ".join(sorted(block.type.consumes))
                problems.append(
                    Problem(
                        block.id,
                        f"{block.type.name} consumes {accepted}; {previous.id!r} produces {ref}",
                    )
                )
                return problems  # every later reference type is unknown
            problems += [Problem(block.id, m) for m in block.type.check_input(block.config, ref)]
        ref = block.type.produces(block.config, ref)
        previous = block
    return problems


def _webhook_problems(blocks: list[BlockDefinition]) -> list[Problem]:
    webhooks = [b for b in blocks if b.type == WEBHOOK.name]
    return [
        Problem(
            b.id,
            "a pipeline may have at most one webhook block; delivering one result to several "
            "destinations is the job of delivery sinks (layer C), coming as their own capability",
        )
        for b in webhooks[1:]
    ]


def _budget_problems(
    declared: int | None, parsed: list[ValidatedBlock]
) -> tuple[int, list[Problem]]:
    """PRD-016 R4: budgets compose. The run ceiling must cover every block's own budget."""
    needed = sum(b.timing.budget + BLOCK_OVERHEAD_SECONDS for b in parsed)
    breakdown = " + ".join(f"{b.id} {b.timing.budget + BLOCK_OVERHEAD_SECONDS}" for b in parsed)
    ceiling = settings.pipeline_max_run_seconds
    problems = []
    if declared is not None and declared < needed:
        problems.append(
            Problem(
                None,
                f"time_budget_seconds {declared} is shorter than its blocks need: "
                f"{needed} s ({breakdown})",
            )
        )
    if needed > ceiling:
        problems.append(
            Problem(None, f"blocks need {needed} s ({breakdown}); the limit is {ceiling} s")
        )
    elif declared is not None and declared > ceiling:
        problems.append(
            Problem(None, f"time_budget_seconds {declared} is over the limit of {ceiling} s")
        )
    return (declared if declared is not None else needed), problems


def validate(raw: Any) -> ValidatedPipeline:
    """Validate a submitted definition. Raises PipelineValidationError listing every problem."""
    try:
        definition = PipelineDefinition.model_validate(raw)
    except ValidationError as exc:
        raise PipelineValidationError(_shape_problems(exc, raw)) from None

    problems: list[Problem] = []
    blocks = definition.blocks

    for name in definition.inputs:
        if not re.match(_NAME, name):
            problems.append(Problem(None, f"run input name {name!r} must match {_NAME}"))

    limit = settings.max_blocks_per_pipeline
    if len(blocks) > limit:
        problems.append(Problem(None, f"{len(blocks)} blocks; the limit is {limit}"))

    seen: set[str] = set()
    for block in blocks:
        if block.id in seen:
            problems.append(Problem(block.id, "block id is used more than once"))
        seen.add(block.id)

    declared = set(definition.inputs)
    used: set[str] = set()
    parsed: list[ValidatedBlock] = []
    for block in blocks:
        validated, block_problems = _parse_block(block, declared, used)
        problems += block_problems
        if validated is not None:
            parsed.append(validated)
    for name in sorted(declared - used):
        problems.append(Problem(None, f"run input {name!r} is declared but no block uses it"))

    starting = _starting_block_problems(blocks)
    chain = _chain_problems(blocks)
    problems += starting + chain + _webhook_problems(blocks)

    run_budget = 0
    if len(parsed) == len(blocks) and not starting and not chain:
        problems += _type_problems(parsed)
        run_budget, budget_problems = _budget_problems(definition.time_budget_seconds, parsed)
        problems += budget_problems

    if problems:
        raise PipelineValidationError(problems)

    normalized = {
        "inputs": {name: decl.model_dump() for name, decl in definition.inputs.items()},
        "time_budget_seconds": definition.time_budget_seconds,
        "blocks": [
            {
                "id": b.id,
                "type": b.type,
                "input": b.input,
                "config_schema_version": (
                    b.config_schema_version or CATALOG[b.type].current_version
                ),
                "config": v.config.model_dump(mode="json", by_alias=True),
            }
            for b, v in zip(blocks, parsed, strict=True)
        ],
    }
    return ValidatedPipeline(normalized, tuple(parsed), run_budget)
