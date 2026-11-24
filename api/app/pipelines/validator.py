"""Save-time validation of a pipeline definition (PRD-016 R1, R4; ADR-009 §4, §8).

Pure apart from reading limits from settings. Nothing here checks what needs the database or the
network — LLM key ownership, SSRF on literal URLs, the per-user pipeline count: those are C.4's.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validators as jsonschema_validators
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.pipelines.catalog import (
    APPROVAL,
    CATALOG,
    SCRAPE,
    VALIDATE,
    ApprovalConfigV1,
    BlockType,
    JsonSchemaRule,
    LLMExtractConfigV1,
    RunInputType,
    Timing,
    ValidateConfigV1,
    announces,
    first_change_gate,
    is_binding,
    is_change_gate,
    rollback_reserve,
    run_need_seconds,
)
from app.settings import settings

_NAME = r"^[a-z][a-z0-9_]{0,62}$"
BlockId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


class RunInputDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A binding's field must take this type (BlockType.bindable_fields).
    type: RunInputType


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


def _bindings(config: Mapping[str, Any]) -> Iterator[tuple[str, str, Mapping[str, Any], Any]]:
    """Each binding in a config: (where, bindable-field key, the object holding it, the binding).

    Top-level fields, and fields of objects in a top-level list ("rules[].value").
    """
    for name, value in config.items():
        if is_binding(value):
            yield f"config.{name}", name, config, value
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                for key, inner in item.items():
                    if is_binding(inner):
                        yield f"config.{name}[{i}].{key}", f"{name}[].{key}", item, inner


def _types(allowed: frozenset[str]) -> str:
    return " or ".join(sorted(allowed))


def _binding_problems(
    block: BlockDefinition, block_type: BlockType, declared: Mapping[str, str], used: set[str]
) -> list[Problem]:
    problems = []
    fields = block_type.bindable_fields
    for where, key, holder, value in _bindings(block.config):
        name = value.get("$input")
        used.add(name)
        allowed = fields[key](holder) if key in fields else frozenset()
        if not allowed:
            binds = ", ".join(sorted(fields)) or "nothing"
            problems.append(
                Problem(block.id, f"{where} cannot take a run input; {block.type} binds: {binds}")
            )
        elif name not in declared:
            problems.append(
                Problem(
                    block.id,
                    f"{where} uses run input {name!r}, which the pipeline does not declare",
                )
            )
        elif declared[name] not in allowed:
            problems.append(
                Problem(
                    block.id,
                    f"{where} takes a {_types(allowed)} run input; {name!r} is declared "
                    f"{declared[name]}",
                )
            )
    return problems


def _parse_block(
    block: BlockDefinition, declared: Mapping[str, str], used: set[str]
) -> tuple[ValidatedBlock | None, list[Problem]]:
    block_type = CATALOG.get(block.type)
    if block_type is None:
        known = ", ".join(sorted(CATALOG))
        return None, [Problem(block.id, f"unknown block type {block.type!r}; known: {known}")]

    version = block.config_schema_version or block_type.current_version
    model = block_type.config_versions.get(version)
    if model is None:
        return None, [Problem(block.id, f"{block.type} has no config schema version {version}")]

    problems = _binding_problems(block, block_type, declared, used)
    if problems:
        return None, problems

    try:
        config = model.model_validate(block.config)
    except ValidationError as exc:
        return None, _pydantic_problems(exc, block.id, "config")
    validated = ValidatedBlock(block.id, block_type, config, block_type.timing(config))
    return validated, _json_schema_problems(block.id, config)


def _json_schema_problems(block_id: str, config: BaseModel) -> list[Problem]:
    """Save time only: the catalog models also parse pinned versions, which keep their day's rules."""
    schemas: list[tuple[str, dict[str, Any]]] = []
    if isinstance(config, LLMExtractConfigV1):
        schemas.append(("config.output_schema", config.output_schema))
    elif isinstance(config, ValidateConfigV1):
        schemas += [
            (f"config.rules[{i}].schema", rule.schema_)
            for i, rule in enumerate(config.rules)
            if isinstance(rule, JsonSchemaRule)
        ]
    problems = []
    for where, schema in schemas:
        try:
            jsonschema_validators.validator_for(schema).check_schema(schema)
        except jsonschema_exceptions.SchemaError as exc:
            problems.append(Problem(block_id, f"{where} is not a valid JSON Schema: {exc.message}"))
    return problems


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


def _stop_problems(parsed: list[ValidatedBlock]) -> list[Problem]:
    """A stop skips the blocks after it; on the last block there is nothing to skip (D8)."""
    last = parsed[-1]
    if last.type is VALIDATE and last.config.when_unmet == "stop":
        return [
            Problem(
                last.id,
                "when_unmet 'stop' on the last block skips nothing; use 'fail', or put it "
                "before the blocks it guards",
            )
        ]
    return []


def _change_gate_problems(parsed: list[ValidatedBlock]) -> list[Problem]:
    """ADR-016 §5a: a change gate holds only the changed rule, stops, and is never reversed."""
    problems = []
    for block in parsed:
        if not is_change_gate(block.config):
            continue
        config = block.config
        if config.when_unmet != "stop":
            problems.append(Problem(block.id, "the changed rule needs when_unmet 'stop'"))
        if len(config.rules) > 1:
            problems.append(
                Problem(block.id, "the changed rule must be the only rule of its block")
            )
        if any(r.kind == "changed" and r.negate for r in config.rules):
            problems.append(Problem(block.id, "the changed rule cannot take negate"))
    return problems


def _approval_problems(
    blocks: list[BlockDefinition], parsed: list[ValidatedBlock]
) -> list[Problem]:
    """ADR-016 §9a: at most one Approval, never the last block (never the first: the starting-block
    rule's); §9d: its deadline within the operator's maximum, apart from the run's budget."""
    problems = []
    approvals = [b.id for b in blocks if b.type == APPROVAL.name]
    for block_id in approvals[1:]:
        problems.append(
            Problem(
                block_id,
                f"a pipeline may have one approval block; {approvals[0]!r} is already one",
            )
        )
    if blocks[-1].type == APPROVAL.name:
        problems.append(
            Problem(
                blocks[-1].id,
                "an approval block holds the blocks after it until it is answered; on the last "
                "block there is nothing to hold — put it before the blocks it guards",
            )
        )
    limit = settings.approval_max_deadline_hours
    for block in parsed:
        if isinstance(block.config, ApprovalConfigV1) and block.config.deadline_hours > limit:
            problems.append(
                Problem(
                    block.id,
                    f"config.deadline_hours {block.config.deadline_hours} is over the limit of "
                    f"{limit} h",
                )
            )
    return problems


def _resolve_gate_outputs(
    blocks: list[BlockDefinition], parsed: list[ValidatedBlock]
) -> list[ValidatedBlock]:
    """ADR-016 §5b: at the first change gate an on_stop_outputs left out is stored as "delete",
    at a later gate as "keep". Read from the submitted config — the parsed one is defaulted."""
    first = first_change_gate(parsed)
    given = {b.id: b.config.get("on_stop_outputs") is not None for b in blocks}
    resolved = []
    for block in parsed:
        if is_change_gate(block.config) and not given.get(block.id, False):
            value = "delete" if block.id == first else "keep"
            config = block.config.model_copy(update={"on_stop_outputs": value})
            block = ValidatedBlock(block.id, block.type, config, block.type.timing(config))
        resolved.append(block)
    return resolved


def _sink_order_problems(parsed: list[ValidatedBlock]) -> list[Problem]:
    """PRD-017 R2 (D3): no reversible sink after one that has already sent something."""
    problems = []
    announcer: ValidatedBlock | None = None
    for block in parsed:
        sink = block.type.sink
        if sink is None:
            continue
        if sink.reversible and announcer is not None:
            problems.append(
                Problem(
                    block.id,
                    f"{block.type.name} {block.id!r} cannot follow {announcer.type.name} "
                    f"{announcer.id!r}: if {block.id!r} failed, {announcer.id!r} could not be "
                    "undone; put every bucket and postgres block before the first webhook or "
                    "email",
                )
            )
        elif announcer is None and announces(block):
            announcer = block
    return problems


def _hours(seconds: int) -> str:
    return f"{seconds / 3600:.1f}".removesuffix(".0") + " h"


def _deliveries(n: int) -> str:
    return "1 delivery" if n == 1 else f"{n} deliveries"


def _budget_problems(
    declared: int | None, parsed: list[ValidatedBlock]
) -> tuple[int, list[Problem]]:
    """PRD-016 R4: budgets compose. The run ceiling must cover every block's own budget, plus
    the time to undo every reversible sink (PRD-017 R6; ADR-015 §6) — the undo is never cut by
    the run's deadline, so its reserve sits on top of it."""
    needed = run_need_seconds(b.timing for b in parsed)
    breakdown = " + ".join(f"{b.id} {b.timing.need}" for b in parsed)
    reserve, undos = rollback_reserve(parsed)
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
    if needed + reserve > ceiling:
        if reserve:
            message = (
                f"blocks need {_hours(needed + reserve)} ({_hours(needed)} of blocks + "
                f"{_hours(reserve)} to undo {_deliveries(undos)}); the limit is {_hours(ceiling)}"
                f" — {needed} s ({breakdown}) + {reserve} s against {ceiling} s"
            )
        else:
            message = f"blocks need {needed} s ({breakdown}); the limit is {ceiling} s"
        problems.append(Problem(None, message))
    elif declared is not None and declared + reserve > ceiling:
        undo = f" plus {reserve} s to undo {_deliveries(undos)}" if reserve else ""
        problems.append(
            Problem(None, f"time_budget_seconds {declared}{undo} is over the limit of {ceiling} s")
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

    declared = {name: decl.type for name, decl in definition.inputs.items()}
    used: set[str] = set()
    parsed: list[ValidatedBlock] = []
    for block in blocks:
        validated, block_problems = _parse_block(block, declared, used)
        problems += block_problems
        if validated is not None:
            parsed.append(validated)
    problems += _change_gate_problems(parsed)
    problems += _approval_problems(blocks, parsed)
    parsed = _resolve_gate_outputs(blocks, parsed)
    for name in sorted(set(declared) - used):
        problems.append(Problem(None, f"run input {name!r} is declared but no block uses it"))

    starting = _starting_block_problems(blocks)
    chain = _chain_problems(blocks)
    problems += starting + chain

    run_budget = 0
    if len(parsed) == len(blocks) and not starting and not chain:
        problems += _type_problems(parsed) + _stop_problems(parsed) + _sink_order_problems(parsed)
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
