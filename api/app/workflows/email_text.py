"""An Email block's message (PRD-017 R1, R2; PRD-018 R4): its Message-ID, subject and the text before
the content, which the delivery worker appends from ScrapeFlow's copy.

Pure: the workflow builds it from what it holds and what ``load_destination`` read.
"""

import json
from typing import Any


def one_line(text: str) -> str:
    return " ".join(text.split())


def message_id(run_id: str, block_id: str, host: str) -> str:
    # ADR-015 §5: fixed by run and block, so a resend repeats it.
    return f"<{run_id}.{block_id}@{host}>"


def subject(pipeline_name: str, run_id: str) -> str:
    return f"[ScrapeFlow] {one_line(pipeline_name)}: run {run_id}"


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _summary_lines(block_id: str, summary: dict[str, Any]) -> list[str]:
    status = summary.get("status")
    if status == "baseline_gone":
        return [f"  {block_id}: changed; the previous copy is no longer stored, so no diff"]
    if status != "ok":
        return [f"  {block_id}: changed; the comparison could not be computed"]
    if summary.get("kind") == "extraction":
        lines = [f"  {block_id}: {summary.get('paths_changed', 0)} values changed"]
        for c in summary.get("changes", []):
            if c.get("change") == "added":
                lines.append(f"    {c['path']}: added {_value(c.get('new'))}")
            elif c.get("change") == "removed":
                lines.append(f"    {c['path']}: removed (was {_value(c.get('old'))})")
            else:
                lines.append(f"    {c['path']}: {_value(c.get('old'))} → {_value(c.get('new'))}")
    else:
        added, removed = summary.get("lines_added", 0), summary.get("lines_removed", 0)
        lines = [f"  {block_id}: +{added} -{removed} lines"]
        lines += [f"    {line}" for line in summary.get("lines", [])]
    if summary.get("truncated"):
        lines.append("    (cut; the full comparison is on the run page)")
    return lines


def change_lines(change: dict[str, Any]) -> list[str]:
    """The Webhook payload's ``change`` in words (ADR-016 §6a): status, baseline, each summary."""
    if change.get("status") == "first_check":
        lines = ["Change: first check — nothing to compare"]
    else:
        lines = [f"Change: changed since check {change.get('baseline_run_id')}"]
    for block_id, summary in (change.get("summary") or {}).items():
        lines += _summary_lines(block_id, summary)
    return lines


def body(
    *,
    pipeline_name: str,
    pipeline_id: str,
    run_id: str,
    block_id: str,
    run_url: str,
    input_block: str,
    input_type: str,
    monitor_id: str | None = None,
    change: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"pipeline.result — pipeline {one_line(pipeline_name)}, run {run_id}",
        "",
        f"run_id: {run_id}",
        f"block_id: {block_id}",
        f"pipeline_id: {pipeline_id}",
        f"Run: {run_url}",
    ]
    if monitor_id is not None:
        lines += ["", f"monitor_id: {monitor_id}"]
        if change is not None:
            lines += change_lines(change)
    lines += ["", f"The result — {input_block}'s output ({input_type}):", "", ""]
    return "\n".join(lines)
