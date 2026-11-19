"""K.3 — Diff and Capture (ADR-016 §6a, §6b)."""

import json
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from worker.changes import (
    FULL_BYTES,
    MAX_LINES,
    SUMMARY_BYTES,
    CannotCompare,
    capture,
    extraction_diff,
    page_diff,
)
from worker.content import CONTENT_FAILED, STORAGE_TRANSIENT, ContentActivities
from worker.contracts import CaptureInput, CarriedValue, DiffInput


def _summary_bytes(summary) -> int:
    return len(summary.model_dump_json().encode())


# --- page --------------------------------------------------------------------------------


def test_a_page_diff_is_unified_with_three_lines_of_context():
    old = "\n".join(f"line {i}" for i in range(20))
    new = old.replace("line 10", "line ten")

    summary, full = page_diff(old, new)

    assert summary.status == "ok"
    assert (summary.lines_added, summary.lines_removed) == (1, 1)
    assert summary.lines == [
        "@@ -8,7 +8,7 @@",
        " line 7",
        " line 8",
        " line 9",
        "-line 10",
        "+line ten",
        " line 11",
        " line 12",
        " line 13",
    ]
    assert not summary.truncated
    document = json.loads(full)
    assert document["lines"] == summary.lines
    assert document["truncated"] is False


def test_a_page_changed_throughout_keeps_the_summary_within_16_kib():
    old = "\n".join(f"{i} " + "a" * 300 for i in range(3000))
    new = "\n".join(f"{i} " + "é" * 300 for i in range(3000))

    summary, full = page_diff(old, new)

    assert _summary_bytes(summary) <= SUMMARY_BYTES
    assert summary.truncated
    assert (summary.lines_added, summary.lines_removed) == (3000, 3000)
    assert all(len(line) <= 200 for line in summary.lines)
    assert 0 < len(summary.lines) <= 200
    assert len(full) <= FULL_BYTES
    assert json.loads(full)["truncated"] is True


def test_short_lines_stop_at_200():
    old = "\n".join(str(i) for i in range(1000))
    new = "\n".join(f"{i}!" for i in range(1000))

    summary, _ = page_diff(old, new)

    assert len(summary.lines) == 200
    assert summary.truncated


def test_a_page_past_the_line_limit_cannot_be_compared():
    with pytest.raises(CannotCompare, match="too large"):
        page_diff("x\n" * (MAX_LINES + 1), "y")


# --- extraction --------------------------------------------------------------------------


def test_an_extraction_diff_names_each_changed_path_at_any_depth_lists_by_position():
    old = {
        "products": [{"name": "A", "price": 10}, {"name": "B", "price": 5}],
        "n": 2,
        "gone": 1,
    }
    new = {
        "products": [
            {"name": "A", "price": 8},
            {"name": "B", "price": 5},
            {"name": "C"},
        ],
        "n": 2,
    }

    summary, full = extraction_diff(json.dumps(old), json.dumps(new))

    assert [c.model_dump(exclude_none=True) for c in summary.changes] == [
        {"path": "/products/0/price", "change": "changed", "old": 10, "new": 8},
        {"path": "/products/2", "change": "added", "new": {"name": "C"}},
        {"path": "/gone", "change": "removed", "old": 1},
    ]
    assert summary.paths_changed == 3
    assert json.loads(full)["changes"][0] == {
        "path": "/products/0/price",
        "change": "changed",
        "old": 10,
        "new": 8,
    }


def test_json_equality_one_is_one_point_zero_but_true_is_not_one():
    summary, _ = extraction_diff('{"a": 1, "b": 1}', '{"a": 1.0, "b": true}')

    assert [c.path for c in summary.changes] == ["/b"]


def test_keys_with_slash_and_tilde_are_escaped():
    summary, _ = extraction_diff('{"a/b": 1, "c~d": 1}', '{"a/b": 2, "c~d": 2}')

    assert [c.path for c in summary.changes] == ["/a~1b", "/c~0d"]


def test_values_are_cut_to_200_characters_and_the_summary_says_so():
    summary, full = extraction_diff(
        json.dumps({"t": "a" * 500}), json.dumps({"t": "b" * 500})
    )

    [change] = summary.changes
    assert len(change.old) == 200 and change.old.endswith("...")
    assert summary.truncated
    # The full comparison keeps the values.
    assert json.loads(full)["changes"][0]["new"] == "b" * 500


def test_many_changed_paths_stop_at_100_within_16_kib():
    old = {f"k{i}": "x" * 150 for i in range(500)}
    new = {f"k{i}": "y" * 150 for i in range(500)}

    summary, _ = extraction_diff(json.dumps(old), json.dumps(new))

    assert summary.paths_changed == 500
    assert len(summary.changes) <= 100
    assert summary.truncated
    assert _summary_bytes(summary) <= SUMMARY_BYTES


def test_an_extraction_that_is_not_json_cannot_be_compared():
    with pytest.raises(CannotCompare, match="not JSON"):
        extraction_diff("{}", "<html>")


def test_a_deeply_nested_extraction_does_not_recurse():
    deep_old = json.loads("[" * 900 + "1" + "]" * 900)
    deep_new = json.loads("[" * 900 + "2" + "]" * 900)

    summary, _ = extraction_diff(json.dumps(deep_old), json.dumps(deep_new))

    assert summary.paths_changed == 1


# --- capture -----------------------------------------------------------------------------

DOC = {"price": 39.5, "count": 3, "in_stock": True, "title": "Kettle", "none": None}


@pytest.mark.parametrize(
    ("path", "type", "expected"),
    [
        ("/price", "number", {"value": 39.5}),
        ("/count", "number", {"value": 3}),
        ("/title", "text", {"value": "Kettle"}),
        ("/in_stock", "number", {"missing": "'/in_stock' is boolean, not a number"}),
        ("/title", "number", {"missing": "'/title' is string, not a number"}),
        ("/price", "text", {"missing": "'/price' is number, not text"}),
        ("/none", "text", {"missing": "'/none' is null, not text"}),
        ("/sku", "text", {"missing": "'/sku' is missing"}),
    ],
)
def test_capture_takes_only_a_value_of_the_carried_type(path, type, expected):
    [(_, got)] = capture(
        [CarriedValue(input="x", path=path, type=type)], json.dumps(DOC)
    ).items()

    assert got.model_dump(exclude_none=True) == expected


def test_a_number_that_is_not_finite_is_missing():
    got = capture([CarriedValue(input="x", path="/p", type="number")], '{"p": NaN}')

    assert got["x"].missing == "'/p' is not a finite number"


def test_capture_from_output_that_is_not_json_misses_every_value():
    got = capture(
        [
            CarriedValue(input="a", path="/p", type="number"),
            CarriedValue(input="b", path="/t", type="text"),
        ],
        "<html>",
    )

    assert {k: v.missing for k, v in got.items()} == {
        "a": "the output is not JSON",
        "b": "the output is not JSON",
    }


# --- the activities ----------------------------------------------------------------------

_BASE = "scrapeflow-results/history/old-block/clean.md"
_CURR = "scrapeflow-results/history/new-block/clean.md"


class _S3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _diff(objects: dict, kind="page"):
    async def fetch(_minio, path):
        found = objects[path]
        if isinstance(found, Exception):
            raise found
        return found

    with patch("worker.content.fetch_bytes", side_effect=fetch), patch(
        "worker.content.put", new_callable=AsyncMock
    ) as put:
        put.side_effect = lambda _m, key, _d, _ct: f"scrapeflow-results/{key}"
        out = await ActivityEnvironment().run(
            ContentActivities(AsyncMock()).diff,
            DiffInput(
                artifact_id="new-block",
                kind=kind,
                baseline_path=_BASE,
                current_path=_CURR,
            ),
        )
        return out, put


async def test_diff_writes_diff_json_beside_the_block_and_returns_the_summary():
    out, put = await _diff({_BASE: b"a\nb\n", _CURR: b"a\nc\n"})

    assert out.summary.status == "ok"
    assert out.diff.path == "scrapeflow-results/history/new-block/diff.json"
    [(_, key, data, content_type)] = [c.args for c in put.call_args_list]
    assert (key, content_type) == ("history/new-block/diff.json", "application/json")
    assert out.diff.size == len(data)
    assert json.loads(data)["lines"] == out.summary.lines


async def test_a_baseline_copy_that_is_gone_is_a_result_not_an_error():
    out, put = await _diff({_BASE: _S3Error("NoSuchKey"), _CURR: b"x"})

    assert out.summary.status == "baseline_gone"
    assert out.diff is None
    put.assert_not_called()


async def test_too_large_is_a_result_and_writes_nothing():
    out, put = await _diff({_BASE: b"x\n" * (MAX_LINES + 1), _CURR: b"y"})

    assert (out.summary.status, out.diff) == ("unavailable", None)
    assert "too large" in out.summary.reason
    put.assert_not_called()


async def test_an_extraction_that_is_not_json_is_a_result():
    out, _ = await _diff({_BASE: b"{}", _CURR: b"<html>"}, kind="extraction")

    assert out.summary.status == "unavailable"
    assert out.summary.reason == "this extraction is not JSON"


async def test_storage_unreachable_raises_retryable():
    with pytest.raises(ApplicationError) as err:
        await _diff({_BASE: aiohttp.ClientConnectionError("x"), _CURR: b""})

    assert err.value.type == STORAGE_TRANSIENT
    assert not err.value.non_retryable


async def test_this_checks_own_object_missing_is_not_retryable():
    with pytest.raises(ApplicationError) as err:
        await _diff({_BASE: b"a", _CURR: _S3Error("NoSuchKey")})

    assert (err.value.type, err.value.non_retryable) == (CONTENT_FAILED, True)


async def test_capture_reads_the_blocks_output():
    with patch("worker.content.fetch_bytes", new_callable=AsyncMock) as fetch:
        fetch.return_value = json.dumps(DOC).encode()
        out = await ActivityEnvironment().run(
            ContentActivities(AsyncMock()).capture,
            CaptureInput(
                input_path=_CURR,
                values=[
                    CarriedValue(input="last_price", path="/price", type="number"),
                    CarriedValue(input="flag", path="/in_stock", type="number"),
                ],
            ),
        )

    assert out.model_dump(exclude_none=True) == {
        "values": {
            "last_price": {"value": 39.5},
            "flag": {"missing": "'/in_stock' is boolean, not a number"},
        }
    }
