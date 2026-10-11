"""Unit tests for worker/cleaner.py, worker/rules.py and the content activities (C.8)."""

import json
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from worker.cleaner import clean_html
from worker.content import (
    CONTENT_FAILED,
    STORAGE_TRANSIENT,
    VALIDATION_FAILED,
    ContentActivities,
)
from worker.contracts import CleanInput, ValidateInput
from worker.rules import failures

_ARTIFACT = "3f8b7a12-0c44-4e7a-9a1e-1b2c3d4e5f60"
_IN = f"scrapeflow-results/history/{_ARTIFACT}/scrape.html"

PAGE = """<!doctype html>
<html><head><title>Blue Kettle</title><style>.x{color:red}</style>
<script>window.track = 1</script></head>
<body>
  <header class="site"><a href="/">Shop</a> <a href="/cart">Cart</a></header>
  <nav><ul><li><a href="/kettles">Kettles</a></li><li><a href="/mugs">Mugs</a></li></ul></nav>
  <div role="banner">SALE — 20% off everything</div>
  <main>
    <article>
      <header><h1>Blue Kettle</h1></header>
      <p>A 1.7 litre kettle in blue enamel. Price: <b>£39.00</b></p>
      <img src="data:image/png;base64,iVBORw0KGgo=" alt="pixel">
      <img src="/img/kettle.jpg" alt="kettle">
      <button>Add to cart</button>
      <footer>Posted in Kitchen</footer>
    </article>
  </main>
  <aside>Customers also bought: mugs</aside>
  <form><input name="email"><button>Subscribe</button></form>
  <div hidden>hidden promo</div>
  <span aria-hidden="true">★</span>
  <div role="dialog">We use cookies</div>
  <iframe src="https://ads.example/"></iframe>
  <noscript>Enable JavaScript</noscript>
  <footer class="site">© 2026 Shop Ltd · Privacy</footer>
</body></html>
"""


# ---------------------------------------------------------------------------
# clean_html
# ---------------------------------------------------------------------------


def test_clean_keeps_the_content():
    md = clean_html(PAGE)

    assert "# Blue Kettle" in md
    assert "1.7 litre kettle in blue enamel" in md
    assert "**£39.00**" in md
    assert "![kettle](/img/kettle.jpg)" in md


@pytest.mark.parametrize(
    "boilerplate",
    [
        "window.track",  # script
        "color:red",  # style
        "Cart",  # page header
        "Mugs",  # nav
        "SALE",  # role=banner
        "Customers also bought",  # aside
        "Subscribe",  # form
        "hidden promo",  # [hidden]
        "★",  # aria-hidden
        "We use cookies",  # role=dialog
        "Enable JavaScript",  # noscript
        "Privacy",  # page footer
        "data:image",  # inline image
        "Add to cart",  # button
    ],
)
def test_clean_drops_boilerplate(boilerplate):
    assert boilerplate not in clean_html(PAGE)


def test_clean_keeps_a_header_and_footer_inside_the_content():
    md = clean_html(PAGE)

    assert "Blue Kettle" in md
    assert "Posted in Kitchen" in md


def test_clean_drops_the_head_title():
    # The title would otherwise be a bare line before the content.
    assert not clean_html(PAGE).startswith("Blue Kettle")


def test_clean_collapses_blank_runs_and_ends_with_one_newline():
    md = clean_html("<p>a</p><div></div><div></div><div></div><p>b</p>")

    assert "\n\n\n" not in md
    assert md.endswith("b\n") and not md.endswith("\n\n")


def test_clean_is_deterministic():
    assert clean_html(PAGE) == clean_html(PAGE)


def test_clean_survives_a_nested_drop():
    # A dropped tag inside another dropped tag is decomposed once, not twice.
    assert clean_html("<nav><form><button>x</button></form></nav><p>ok</p>") == "ok\n"


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

DOC = {
    "name": "Blue Kettle",
    "price": 39.0,
    "stock": 3,
    "in_stock": True,
    "discount": None,
    "tags": ["kitchen", "blue"],
    "a/b": {"m~n": 1},
}


def _check(*rules, text=None):
    from pydantic import TypeAdapter

    from worker.contracts import Rule

    parsed = TypeAdapter(list[Rule]).validate_python(list(rules))
    return failures(parsed, json.dumps(DOC) if text is None else text)


@pytest.mark.parametrize(
    "rule",
    [
        {"kind": "present", "path": ""},
        {"kind": "present", "path": "/discount"},  # null is present
        {"kind": "present", "path": "/tags/1"},
        {"kind": "present", "path": "/a~1b/m~0n"},  # RFC 6901 escapes
        {"kind": "type", "path": "/name", "type": "string"},
        {"kind": "type", "path": "/price", "type": "number"},
        {"kind": "type", "path": "/stock", "type": "number"},
        {"kind": "type", "path": "/stock", "type": "integer"},
        {
            "kind": "type",
            "path": "/price",
            "type": "integer",
        },  # 39.0 is an integer in JSON
        {"kind": "type", "path": "/in_stock", "type": "boolean"},
        {"kind": "type", "path": "/discount", "type": "null"},
        {"kind": "type", "path": "/tags", "type": "array"},
        {"kind": "type", "path": "", "type": "object"},
        {"kind": "compare", "path": "/price", "op": "eq", "value": 39},
        {"kind": "compare", "path": "/price", "op": "gt", "value": 0},
        {"kind": "compare", "path": "/price", "op": "le", "value": 39.0},
        {"kind": "compare", "path": "/stock", "op": "ge", "value": 3},
        {"kind": "compare", "path": "/stock", "op": "lt", "value": 4},
        {"kind": "compare", "path": "/in_stock", "op": "eq", "value": True},
        {"kind": "compare", "path": "/name", "op": "ne", "value": ""},
        {"kind": "compare", "path": "/discount", "op": "eq", "value": None},
        {
            "kind": "json_schema",
            "schema": {"type": "object", "required": ["name", "price"]},
        },
    ],
)
def test_rule_passes(rule):
    assert _check(rule) == []


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"kind": "present", "path": "/missing"}, "'/missing' is missing"),
        ({"kind": "present", "path": "/tags/2"}, "'/tags/2' is missing"),
        ({"kind": "present", "path": "/tags/01"}, "'/tags/01' is missing"),
        ({"kind": "present", "path": "/name/x"}, "'/name/x' is missing"),
        (
            {"kind": "type", "path": "/in_stock", "type": "number"},
            "is boolean, not number",
        ),
        (
            {"kind": "type", "path": "/stock", "type": "string"},
            "is integer, not string",
        ),
        ({"kind": "type", "path": "/missing", "type": "string"}, "is missing"),
        (
            {"kind": "compare", "path": "/price", "op": "lt", "value": 10},
            "is 39.0; expected lt 10",
        ),
        (
            {"kind": "compare", "path": "/name", "op": "gt", "value": 1},
            "is string, not a number",
        ),
        # true is not 1 in JSON, though it is in Python.
        (
            {"kind": "compare", "path": "/in_stock", "op": "eq", "value": 1},
            "expected eq 1",
        ),
        (
            {"kind": "compare", "path": "/stock", "op": "eq", "value": True},
            "expected eq true",
        ),
        (
            {"kind": "compare", "path": "/name", "op": "eq", "value": "Red"},
            'expected eq "Red"',
        ),
        (
            {"kind": "compare", "path": "/discount", "op": "ne", "value": None},
            "expected ne null",
        ),
        (
            {
                "kind": "json_schema",
                "schema": {"properties": {"price": {"type": "string"}}},
            },
            "39.0 is not of type 'string' at '/price'",
        ),
        (
            {"kind": "json_schema", "schema": {"required": ["sku"]}},
            "'sku' is a required property",
        ),
        ({"kind": "json_schema", "schema": {"type": 5}}, "invalid schema"),
    ],
)
def test_rule_fails_and_names_itself(rule, message):
    [problem] = _check(rule)

    assert problem.startswith(f"rules[0] ({rule['kind']}): ")
    assert message in problem


def test_text_rules_read_the_raw_page():
    page = "# Blue Kettle\n\nAdd to cart\n"

    assert _check({"kind": "contains", "text": "Add to cart"}, text=page) == []
    assert _check({"kind": "min_length", "chars": len(page)}, text=page) == []
    [missing] = _check({"kind": "contains", "text": "Sold out"}, text=page)
    assert missing == "rules[0] (contains): 'Sold out' not found"
    [short] = _check({"kind": "min_length", "chars": 500}, text=page)
    assert (
        short == f"rules[0] (min_length): {len(page)} characters; at least 500 required"
    )


def test_every_failing_rule_is_reported_by_index():
    problems = _check(
        {"kind": "present", "path": "/name"},
        {"kind": "present", "path": "/sku"},
        {"kind": "compare", "path": "/stock", "op": "gt", "value": 10},
    )

    assert [p.split(":")[0] for p in problems] == [
        "rules[1] (present)",
        "rules[2] (compare)",
    ]


def test_json_rules_on_input_that_is_not_json():
    [problem] = _check({"kind": "present", "path": "/x"}, text="<html>")

    assert problem.startswith("input is not JSON")


def test_text_rules_alone_do_not_parse_json():
    assert _check({"kind": "contains", "text": "<"}, text="<html>") == []


# ---------------------------------------------------------------------------
# activities
# ---------------------------------------------------------------------------


class _S3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _clean(fetch=None, put=None):
    with patch("worker.content.fetch_bytes", new_callable=AsyncMock) as f, patch(
        "worker.content.put", new_callable=AsyncMock
    ) as p:
        f.return_value = PAGE.encode()
        f.side_effect = fetch
        p.side_effect = put or (lambda _m, key, _d, _ct: f"scrapeflow-results/{key}")
        out = await ActivityEnvironment().run(
            ContentActivities(AsyncMock()).clean,
            CleanInput(artifact_id=_ARTIFACT, input_path=_IN),
        )
        return out, f, p


async def test_clean_writes_a_stage_named_object():
    out, fetch, put = await _clean()

    fetch.assert_awaited_once_with(fetch.await_args.args[0], _IN)
    [(_, key, data, content_type)] = [c.args for c in put.await_args_list]
    assert key == f"history/{_ARTIFACT}/clean.md"
    assert content_type == "text/markdown; charset=utf-8"
    assert data == clean_html(PAGE).encode()
    assert out.result.path == f"scrapeflow-results/history/{_ARTIFACT}/clean.md"
    assert out.result.size == len(data)


async def test_clean_retry_rewrites_the_same_object():
    first, _, put1 = await _clean()
    second, _, put2 = await _clean()

    assert first == second
    assert put1.await_args.args[1:] == put2.await_args.args[1:]


async def test_clean_decodes_a_non_utf8_page():
    with patch("worker.content.fetch_bytes", new_callable=AsyncMock) as f, patch(
        "worker.content.put", new_callable=AsyncMock
    ) as p:
        f.return_value = "<p>caf\xe9</p>".encode("latin-1")
        p.return_value = "b/k"
        await ActivityEnvironment().run(
            ContentActivities(AsyncMock()).clean,
            CleanInput(artifact_id=_ARTIFACT, input_path=_IN),
        )

    assert p.await_args.args[2] == "caf�\n".encode()


@pytest.mark.parametrize("stage", ["fetch", "put"])
async def test_clean_minio_unreachable_is_retryable(stage):
    exc = aiohttp.ClientConnectionError("refused")
    with pytest.raises(ApplicationError) as err:
        await _clean(**{stage: exc})

    assert err.value.type == STORAGE_TRANSIENT
    assert not err.value.non_retryable


async def test_clean_missing_input_is_not_retryable():
    with pytest.raises(ApplicationError) as err:
        await _clean(fetch=_S3Error("NoSuchKey"))

    assert err.value.type == CONTENT_FAILED
    assert err.value.non_retryable


async def test_clean_conversion_error_is_not_retryable():
    with patch("worker.content.clean_html", side_effect=RecursionError("too deep")):
        with pytest.raises(ApplicationError) as err:
            await _clean()

    assert err.value.type == CONTENT_FAILED
    assert err.value.non_retryable
    assert "RecursionError: too deep" in err.value.message


async def _validate(rules, body: bytes, fetch=None):
    with patch("worker.content.fetch_bytes", new_callable=AsyncMock) as f:
        f.return_value = body
        f.side_effect = fetch
        return await ActivityEnvironment().run(
            ContentActivities(AsyncMock()).validate,
            ValidateInput.model_validate({"input_path": _IN, "rules": rules}),
        )


async def test_validate_passes_and_returns_nothing():
    out = await _validate(
        [{"kind": "compare", "path": "/price", "op": "gt", "value": 0}],
        json.dumps(DOC).encode(),
    )

    assert out is None


async def test_validate_failure_is_not_retryable_and_names_each_rule():
    with pytest.raises(ApplicationError) as err:
        await _validate(
            [
                {"kind": "present", "path": "/sku"},
                {"kind": "compare", "path": "/price", "op": "lt", "value": 10},
            ],
            json.dumps(DOC).encode(),
        )

    assert err.value.type == VALIDATION_FAILED
    assert err.value.non_retryable
    assert err.value.message == (
        "rules[0] (present): '/sku' is missing; "
        "rules[1] (compare): '/price' is 39.0; expected lt 10"
    )


async def test_validate_minio_unreachable_is_retryable():
    with pytest.raises(ApplicationError) as err:
        await _validate(
            [{"kind": "min_length", "chars": 1}],
            b"",
            fetch=aiohttp.ClientConnectionError("x"),
        )

    assert err.value.type == STORAGE_TRANSIENT
    assert not err.value.non_retryable
