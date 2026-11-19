"""What a monitor is allowed to be (ADR-016 §2d) and what its checks are given (§6c).

Pure apart from settings: the routes and the firing's admission read the rows and call these.
"""

from collections.abc import Mapping
from typing import Any

from app.pipelines.catalog import (
    CATALOG,
    EXTRACTION,
    LLM_EXTRACT,
    PAGE_JSON,
    block_outputs,
    first_change_gate,
    monitor_allowance_seconds,
    run_need_seconds,
)
from app.pipelines.definition import ParsedDefinition, WebhookBlock, input_value_problems
from app.pipelines.validator import Problem
from app.settings import settings

CARRIABLE_TYPES = ("number", "text")
CARRIABLE_OUTPUTS = (EXTRACTION, PAGE_JSON)
FAILED_EVENT = "pipeline.failed"


def input_problems(
    parsed: ParsedDefinition, inputs: Mapping[str, Any], carried: list[Mapping[str, Any]]
) -> list[Problem]:
    """§2d check 2: every declared run input is fixed or carried, once, and of its type."""
    problems: list[Problem] = []
    declared = parsed.inputs
    carried_names = [c["input"] for c in carried]
    for name in sorted({n for n in carried_names if carried_names.count(n) > 1}):
        problems.append(Problem(None, f"run input {name!r} is carried more than once"))
    for name in sorted(set(inputs) & set(carried_names)):
        problems.append(Problem(None, f"run input {name!r} is both fixed and carried"))
    for name in sorted((set(inputs) | set(carried_names)) - set(declared)):
        problems.append(Problem(None, f"unknown run input {name!r}"))
    for name in sorted(set(declared) - set(inputs) - set(carried_names)):
        problems.append(Problem(None, f"run input {name!r} needs a fixed value or to be carried"))

    outputs = block_outputs(parsed.blocks)
    for entry in carried:
        name, block_id = entry["input"], entry["from_block"]
        kind = declared.get(name)
        if kind is not None and kind not in CARRIABLE_TYPES:
            problems.append(
                Problem(None, f"run input {name!r} is a {kind}; only number and text are carried")
            )
        if block_id not in outputs:
            problems.append(
                Problem(None, f"carried {name!r}: from_block {block_id!r} is not in this version")
            )
        elif outputs[block_id] not in CARRIABLE_OUTPUTS:
            problems.append(
                Problem(
                    block_id,
                    f"carried {name!r} reads this block, which produces {outputs[block_id]}; "
                    "a carried value is read from an extraction or a JSON page",
                )
            )

    # A start value is a run input's value until a check captures one: the same strict rule.
    values = dict(inputs) | {
        c["input"]: c["start"] for c in carried if declared.get(c["input"]) in CARRIABLE_TYPES
    }
    known = {n: v for n, v in values.items() if n in declared}
    problems += [Problem(b, m) for b, m in input_value_problems(parsed, known)]
    return problems


def gate_problems(parsed: ParsedDefinition) -> list[Problem]:
    """§2d check 5 (D4): a change gate comes before the first LLM block."""
    llm = next((b for b in parsed.blocks if b.type == LLM_EXTRACT.name), None)
    if llm is None:
        return []
    gate = first_change_gate(parsed.blocks)
    position = {b.id: i for i, b in enumerate(parsed.blocks)}
    if gate is not None and position[gate] < position[llm.id]:
        return []
    return [
        Problem(
            llm.id,
            "a monitor needs a change gate before its first LLM block — a Validate holding only "
            '{"kind": "changed"}, when_unmet "stop" — so an unchanged page is not sent to the LLM',
        )
    ]


def capture_blocks(carried: list[Mapping[str, Any]]) -> list[str]:
    return [c["from_block"] for c in carried]


def allowance_seconds(parsed: ParsedDefinition, carried: list[Mapping[str, Any]]) -> int:
    return monitor_allowance_seconds(parsed.blocks, capture_blocks(carried))


def ceiling_problems(parsed: ParsedDefinition, carried: list[Mapping[str, Any]]) -> list[Problem]:
    """§2d check 6: the blocks' need plus the monitor allowance fits the maximum."""
    need = run_need_seconds(CATALOG[b.type].timing(b.config) for b in parsed.blocks)
    allowance = allowance_seconds(parsed, carried)
    limit = settings.pipeline_max_run_seconds
    if need + allowance <= limit:
        return []
    return [
        Problem(
            None,
            f"the blocks need {need} s and a monitor check needs {allowance} s more; "
            f"the limit is {limit} s",
        )
    ]


def check_ceiling_seconds(parsed: ParsedDefinition, carried: list[Mapping[str, Any]]) -> int:
    """A check's run ceiling: the version's plus the allowance, capped at the maximum (§2d 6)."""
    return min(
        parsed.run_budget_seconds + allowance_seconds(parsed, carried),
        settings.pipeline_max_run_seconds,
    )


def warnings(parsed: ParsedDefinition) -> list[str]:
    """§2d's warnings: allowed, but the user should know."""
    found = []
    types = {b.type for b in parsed.blocks}
    if LLM_EXTRACT.name not in types and first_change_gate(parsed.blocks) is None:
        found.append(
            "no LLM block and no change gate: every check stores a copy and runs every block"
        )
    if not any(
        isinstance(b, WebhookBlock) and FAILED_EVENT in b.config.events for b in parsed.blocks
    ):
        found.append(
            "nothing is sent when a check fails: add pipeline.failed to a Webhook block's "
            "events, or a failing monitor is visible only on its page"
        )
    return found
