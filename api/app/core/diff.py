"""Diff helpers for comparing consecutive job run outputs stored in MinIO.

Text diff  — used when the job has no LLM config (raw HTML / Markdown output).
JSON diff  — used after LLM extraction (structured JSON output).

Both functions return a DiffResult with:
  detected  — True if the content changed between the two runs.
  summary   — human-readable change description, or None when no change / first run.
"""

import difflib
import json
from dataclasses import dataclass

import structlog
from miniopy_async import Minio

logger = structlog.get_logger()


@dataclass
class DiffResult:
    detected: bool
    summary: dict | None


async def _fetch_bytes(minio: Minio, path: str) -> bytes:
    """Fetch object bytes from MinIO. `path` is 'bucket/object/key'."""
    bucket, _, key = path.partition("/")
    response = await minio.get_object(bucket, key)
    data = await response.read()
    response.close()
    return data


async def compute_text_diff(path_a: str, path_b: str, minio: Minio) -> DiffResult:
    """Compare two text objects (HTML / Markdown) line by line.

    path_a  — current run's result_path  (the newer file)
    path_b  — previous run's result_path (the older file)
    """
    try:
        bytes_a = await _fetch_bytes(minio, path_a)
        bytes_b = await _fetch_bytes(minio, path_b)
    except Exception as exc:
        logger.warning("compute_text_diff: could not fetch MinIO objects", error=str(exc))
        return DiffResult(detected=False, summary=None)

    lines_a = bytes_a.decode("utf-8", errors="replace").splitlines()
    lines_b = bytes_b.decode("utf-8", errors="replace").splitlines()

    matcher = difflib.SequenceMatcher(None, lines_b, lines_a)
    ratio = matcher.ratio()

    if ratio >= 1.0:
        return DiffResult(detected=False, summary=None)

    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added += j2 - j1
        if tag in ("delete", "replace"):
            removed += i2 - i1

    return DiffResult(
        detected=True,
        summary={
            "similarity": round(ratio, 4),
            "added_lines": added,
            "removed_lines": removed,
        },
    )


async def compute_json_diff(path_a: str, path_b: str, minio: Minio) -> DiffResult:
    """Compare two JSON objects field-by-field.

    path_a  — current run's result_path  (the newer file)
    path_b  — previous run's result_path (the older file)

    Returns a summary of the form {"field": {"from": old, "to": new}} for every
    top-level key whose value changed between the two runs.
    """
    try:
        bytes_a = await _fetch_bytes(minio, path_a)
        bytes_b = await _fetch_bytes(minio, path_b)
    except Exception as exc:
        logger.warning("compute_json_diff: could not fetch MinIO objects", error=str(exc))
        return DiffResult(detected=False, summary=None)

    try:
        data_a = json.loads(bytes_a)
        data_b = json.loads(bytes_b)
    except json.JSONDecodeError as exc:
        logger.warning("compute_json_diff: could not parse JSON", error=str(exc))
        return DiffResult(detected=False, summary=None)

    # Non-dict JSON (e.g. a top-level list or scalar) — fall back to equality check.
    if not isinstance(data_a, dict) or not isinstance(data_b, dict):
        detected = data_a != data_b
        return DiffResult(detected=detected, summary={} if detected else None)

    changes = {
        key: {"from": data_b.get(key), "to": data_a.get(key)}
        for key in (data_a.keys() | data_b.keys())
        if data_a.get(key) != data_b.get(key)
    }

    if not changes:
        return DiffResult(detected=False, summary=None)

    return DiffResult(detected=True, summary=changes)
