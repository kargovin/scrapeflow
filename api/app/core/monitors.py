"""What a monitor is allowed to be (ADR-016 §2d) and what its checks are given (§4, §6).

The save-time checks are pure apart from settings; ``prepare_check`` reads the database only, so
admission and Check now call it under ``lock_user``.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pipeline_trigger import CheckSetup
from app.models.monitor import Monitor
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.pipelines.catalog import (
    CATALOG,
    EXTRACTION,
    LLM_EXTRACT,
    PAGE_JSON,
    block_outputs,
    first_change_gate,
    is_change_gate,
    monitor_allowance_seconds,
    run_need_seconds,
)
from app.pipelines.definition import ParsedDefinition, WebhookBlock, input_value_problems
from app.pipelines.validator import Problem
from app.settings import settings
from app.workflows.pipeline import CaptureSpec, GateFingerprints, MonitorContext

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


# --- a check's setup: found at admission, handed to the check (§4, §6b) -------------------


async def find_baseline(db: AsyncSession, monitor_id: uuid.UUID) -> PipelineRun | None:
    """The newest eligible check, by start (§4b) — a fresh lookup every time; DB only."""
    return await db.scalar(
        select(PipelineRun)
        .where(PipelineRun.monitor_id == monitor_id, PipelineRun.baseline_eligible)
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )


def _carried_value(
    parsed: ParsedDefinition, entry: Mapping[str, Any], baseline: PipelineRun | None
) -> tuple[Any, dict[str, Any]]:
    """The value a carried input takes this check, and where it came from (§6b)."""
    name = entry["input"]
    start = {"from": "start"}
    if baseline is None:
        return entry["start"], start | {"why": "no earlier check to carry from"}
    captured = (baseline.captured_values or {}).get(name)
    if not isinstance(captured, dict) or ("value" not in captured and "missing" not in captured):
        return entry["start"], start | {"why": "the baseline captured no value"}
    if captured.get("missing") is not None:
        return entry["start"], start | {"why": f"the baseline's value: {captured['missing']}"}
    value = captured["value"]
    if input_value_problems(parsed, {name: value}):
        kind = parsed.inputs.get(name)
        return entry["start"], start | {"why": f"the baseline's value is not a valid {kind}"}
    return value, {"from": "baseline", "run_id": str(baseline.id)}


async def prepare_check(
    db: AsyncSession, monitor: Monitor, parsed: ParsedDefinition, baseline: PipelineRun | None
) -> tuple[dict[str, Any], CheckSetup]:
    """The check's run inputs and its setup. Reads the database only: run under the lock."""
    gates: dict[str, GateFingerprints] = {}
    outputs: dict[str, str] = {}
    if baseline is not None:
        gate_ids = {b.id for b in parsed.blocks if is_change_gate(b.config)}
        for row in await db.scalars(
            select(PipelineRunBlock).where(PipelineRunBlock.pipeline_run_id == baseline.id)
        ):
            if row.block_id in gate_ids and row.fingerprint is not None:
                gates[row.block_id] = GateFingerprints(baseline=row.fingerprint)
            block_type = CATALOG.get(row.type)
            stored = row.output_ref is not None and row.collected_at is None
            if stored and block_type is not None and block_type.kind == "content":
                outputs[row.block_id] = row.output_ref

    inputs = dict(monitor.inputs)
    sources: dict[str, Any] = {}
    for entry in monitor.carried:
        inputs[entry["input"]], sources[entry["input"]] = _carried_value(parsed, entry, baseline)

    context = MonitorContext(
        monitor_id=str(monitor.id),
        baseline_run_id=str(baseline.id) if baseline is not None else None,
        gates=gates,
        baseline_outputs=outputs,
        captures=[
            CaptureSpec(
                input=c["input"],
                from_block=c["from_block"],
                path=c["path"],
                type=parsed.inputs[c["input"]],
            )
            for c in monitor.carried
        ],
    )
    return inputs, CheckSetup(context, sources, check_ceiling_seconds(parsed, monitor.carried))
