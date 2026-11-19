"""What changed between two checks, and the values a check carries to the next (ADR-016 §6).

Pure functions; the activities in content.py run them off the event loop.
"""

import difflib
import json
import math
from typing import Any

from .contracts import CapturedValue, CarriedValue, DiffSummary, PathChange
from .rules import MISSING, is_number, json_equal, json_type, resolve

MAX_LINES = 50_000  # a side; difflib is quadratic in the worst case
MAX_NODES = 50_000  # a side, for an extraction
SUMMARY_LINES = 200
SUMMARY_PATHS = 100
VALUE_CHARS = 200
SUMMARY_BYTES = 16 * 1024
FULL_BYTES = 1024 * 1024
# Room for the summary's other fields within SUMMARY_BYTES.
_SUMMARY_FIELDS = 512


class CannotCompare(Exception):
    """A result, not an error: the workflow records the check as changed, comparison unavailable."""


def _dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _size(value: Any) -> int:
    return len(_dumps(value))


def _cut_text(text: str) -> tuple[str, bool]:
    return (
        (text, False)
        if len(text) <= VALUE_CHARS
        else (text[: VALUE_CHARS - 3] + "...", True)
    )


def _cut_value(value: Any) -> tuple[Any, bool]:
    text = json.dumps(value, ensure_ascii=False)
    return (
        (value, False)
        if len(text) <= VALUE_CHARS
        else (text[: VALUE_CHARS - 3] + "...", True)
    )


def _bounded(items: list[Any], count: int, budget: int) -> tuple[list[Any], bool]:
    """The first items that fit both ``count`` and ``budget`` bytes of JSON."""
    kept: list[Any] = []
    used = 0
    for item in items[:count]:
        cost = _size(item) + 1
        if used + cost > budget:
            return kept, True
        kept.append(item)
        used += cost
    return kept, len(items) > len(kept)


def _full(document: dict[str, Any], key: str) -> bytes:
    """``document`` as JSON, its ``key`` list cut to fit FULL_BYTES and marked."""
    items = document[key]
    base = _size({**document, key: [], "truncated": False})
    kept, cut = _bounded(items, len(items), FULL_BYTES - base)
    return _dumps({**document, key: kept, "truncated": cut})


def page_diff(baseline: str, current: str) -> tuple[DiffSummary, bytes]:
    old, new = baseline.splitlines(), current.splitlines()
    if max(len(old), len(new)) > MAX_LINES:
        raise CannotCompare(f"too large to compare: more than {MAX_LINES} lines")
    # The first two lines are the ---/+++ file headers.
    body = list(
        difflib.unified_diff(old, new, "baseline", "current", n=3, lineterm="")
    )[2:]
    added = sum(1 for line in body if line.startswith("+"))
    removed = sum(1 for line in body if line.startswith("-"))
    full = _full(
        {"kind": "page", "lines_added": added, "lines_removed": removed, "lines": body},
        "lines",
    )

    cut_lines = [_cut_text(line) for line in body]
    lines, dropped = _bounded(
        [text for text, _ in cut_lines], SUMMARY_LINES, SUMMARY_BYTES - _SUMMARY_FIELDS
    )
    summary = DiffSummary(
        status="ok",
        kind="page",
        lines=lines,
        lines_added=added,
        lines_removed=removed,
        truncated=dropped or any(cut for _, cut in cut_lines[: len(lines)]),
    )
    return summary, full


def _token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _count_nodes(doc: Any) -> int:
    count, stack = 0, [doc]
    while stack:
        node = stack.pop()
        count += 1
        if count > MAX_NODES:
            break
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return count


def _changes(old: Any, new: Any) -> list[dict[str, Any]]:
    """Leaf by leaf, in document order; lists by position."""
    out: list[dict[str, Any]] = []
    stack: list[tuple[str, Any, Any]] = [("", old, new)]
    while stack:
        path, a, b = stack.pop()
        if a is MISSING:
            out.append({"path": path, "change": "added", "new": b})
        elif b is MISSING:
            out.append({"path": path, "change": "removed", "old": a})
        elif isinstance(a, dict) and isinstance(b, dict):
            keys = list(a) + [k for k in b if k not in a]
            stack.extend(
                (f"{path}/{_token(k)}", a.get(k, MISSING), b.get(k, MISSING))
                for k in reversed(keys)
            )
        elif isinstance(a, list) and isinstance(b, list):
            stack.extend(
                (
                    f"{path}/{i}",
                    a[i] if i < len(a) else MISSING,
                    b[i] if i < len(b) else MISSING,
                )
                for i in reversed(range(max(len(a), len(b))))
            )
        elif not json_equal(a, b):
            out.append({"path": path, "change": "changed", "old": a, "new": b})
    return out


def _parse(data: str, which: str) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        raise CannotCompare(f"{which} is not JSON") from None


def extraction_diff(baseline: str, current: str) -> tuple[DiffSummary, bytes]:
    old, new = (
        _parse(baseline, "the baseline's extraction"),
        _parse(current, "this extraction"),
    )
    if max(_count_nodes(old), _count_nodes(new)) > MAX_NODES:
        raise CannotCompare(f"too large to compare: more than {MAX_NODES} values")
    changes = _changes(old, new)
    full = _full(
        {"kind": "extraction", "paths_changed": len(changes), "changes": changes},
        "changes",
    )

    truncated = False
    shown: list[dict[str, Any]] = []
    for change in changes[:SUMMARY_PATHS]:
        entry = dict(change)
        for side in ("old", "new"):
            if side in entry:
                entry[side], cut = _cut_value(entry[side])
                truncated |= cut
        shown.append(entry)
    kept, dropped = _bounded(shown, SUMMARY_PATHS, SUMMARY_BYTES - _SUMMARY_FIELDS)
    summary = DiffSummary(
        status="ok",
        kind="extraction",
        changes=[PathChange.model_validate(c) for c in kept],
        paths_changed=len(changes),
        truncated=truncated or dropped,
    )
    return summary, full


def capture(values: list[CarriedValue], text: str) -> dict[str, CapturedValue]:
    """Each carried input's value from the output; a wrong type is missing (ADR-014 §4)."""
    try:
        doc = json.loads(text)
    except ValueError:
        return {
            v.input: CapturedValue(missing="the output is not JSON") for v in values
        }
    out: dict[str, CapturedValue] = {}
    for v in values:
        found = resolve(doc, v.path)
        if found is MISSING:
            out[v.input] = CapturedValue(missing=f"{v.path!r} is missing")
        elif v.type == "number" and not is_number(found):
            out[v.input] = CapturedValue(
                missing=f"{v.path!r} is {json_type(found)}, not a number"
            )
        elif v.type == "number" and not math.isfinite(found):
            out[v.input] = CapturedValue(missing=f"{v.path!r} is not a finite number")
        elif v.type == "text" and not isinstance(found, str):
            out[v.input] = CapturedValue(
                missing=f"{v.path!r} is {json_type(found)}, not text"
            )
        else:
            out[v.input] = CapturedValue(value=found)
    return out
