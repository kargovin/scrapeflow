"""Unit tests for worker/cleaner.py, worker/rules.py and the content activities (C.8)."""

import hashlib
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
    VALIDATION_UNVERIFIABLE,
    ContentActivities,
)
from worker.contracts import CleanInput, ValidateInput, ValidateVerdict
from worker.rules import failures, first_unmet

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


PASSING = [
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
]


@pytest.mark.parametrize("rule", PASSING)
def test_rule_passes(rule):
    assert _check(rule) == []


FAILING = [
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
]

# Not met or met is flippable; these cannot be checked, which negate never flips.
UNVERIFIABLE = [
    {"kind": "compare", "path": "/missing", "op": "eq", "value": 1},
    {"kind": "compare", "path": "/missing", "op": "ne", "value": None},
    {"kind": "compare", "path": "/name", "op": "gt", "value": 1},
    {"kind": "compare", "path": "/discount", "op": "lt", "value": 1},
    {"kind": "compare", "path": "/price", "op": "lt", "value": "100"},
    {"kind": "compare", "path": "/price", "op": "ge", "value": True},
    {"kind": "json_schema", "schema": {"type": 5}},
]


@pytest.mark.parametrize(("rule", "message"), FAILING)
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
# three answers, and negate
# ---------------------------------------------------------------------------


def _answer(rule, text=None):
    from pydantic import TypeAdapter

    from worker.contracts import Rule

    [parsed] = TypeAdapter(list[Rule]).validate_python([rule])
    first = first_unmet([parsed], json.dumps(DOC) if text is None else text)
    return "met" if first is None else first[1].answer


def _negated(rule):
    return {**rule, "negate": True}


TEXT_PAGE = "# Blue Kettle\n\nAdd to cart\n"
TEXT_RULES = [
    {"kind": "contains", "text": "Add to cart"},
    {"kind": "contains", "text": "Sold out"},
    {"kind": "min_length", "chars": 5},
    {"kind": "min_length", "chars": 500},
]


@pytest.mark.parametrize(
    "rule",
    PASSING + [r for r, _ in FAILING if r not in UNVERIFIABLE],
)
def test_a_json_rule_and_its_reverse_disagree(rule):
    answers = {_answer(rule), _answer(_negated(rule))}

    assert answers == {"met", "not_met"}


@pytest.mark.parametrize("rule", TEXT_RULES)
def test_a_text_rule_and_its_reverse_disagree(rule):
    answers = {_answer(rule, TEXT_PAGE), _answer(_negated(rule), TEXT_PAGE)}

    assert answers == {"met", "not_met"}


@pytest.mark.parametrize("rule", UNVERIFIABLE)
def test_cannot_be_checked_is_never_flipped(rule):
    assert _answer(rule) == "unverifiable"
    assert _answer(_negated(rule)) == "unverifiable"


@pytest.mark.parametrize(
    "rule",
    [
        {"kind": "present", "path": "/price"},
        {"kind": "type", "path": "/price", "type": "number"},
        {"kind": "compare", "path": "/price", "op": "lt", "value": 100},
        {"kind": "json_schema", "schema": {"type": "object"}},
    ],
)
def test_a_json_rule_on_input_that_is_not_json_cannot_be_checked(rule):
    # Else `present /price, negate` on an HTML error page would read as absent, so met.
    assert _answer(rule, "<html>Error</html>") == "unverifiable"
    assert _answer(_negated(rule), "<html>Error</html>") == "unverifiable"


def test_a_missing_path_answers_present_and_type_but_not_compare():
    # ADR-014 §3's table.
    assert _answer({"kind": "present", "path": "/x", "negate": True}) == "met"
    assert (
        _answer({"kind": "type", "path": "/x", "type": "number", "negate": True})
        == "met"
    )
    assert _answer({"kind": "type", "path": "/x", "type": "number"}) == "not_met"
    assert (
        _answer(
            {"kind": "compare", "path": "/x", "op": "lt", "value": 1, "negate": True}
        )
        == "unverifiable"
    )


def test_fail_mode_names_a_reversed_rule_as_reversed():
    problems = _check(
        {"kind": "present", "path": "/price", "negate": True},
        {"kind": "compare", "path": "/price", "op": "lt", "value": 100, "negate": True},
        {"kind": "type", "path": "/price", "type": "number", "negate": True},
        {"kind": "json_schema", "schema": {"type": "object"}, "negate": True},
    )

    assert problems == [
        "rules[0] (present, negate): '/price' is present",
        "rules[1] (compare, negate): '/price' is 39.0; expected not lt 100",
        "rules[2] (type, negate): '/price' is number",
        "rules[3] (json_schema, negate): input matches the schema",
    ]
    page = "Out of stock"
    assert _check({"kind": "contains", "text": "Out", "negate": True}, text=page) == [
        "rules[0] (contains, negate): 'Out' found"
    ]
    assert _check({"kind": "min_length", "chars": 3, "negate": True}, text=page) == [
        "rules[0] (min_length, negate): 12 characters; fewer than 3 required"
    ]


def test_fail_mode_lists_a_rule_that_cannot_be_checked():
    [problem] = _check(
        {"kind": "compare", "path": "/sku", "op": "eq", "value": 1, "negate": True}
    )

    assert problem == "rules[0] (compare, negate): '/sku' is missing"


def test_fail_mode_still_reports_only_the_json_error_when_input_is_not_json():
    problems = _check(
        {"kind": "contains", "text": "absent"},
        {"kind": "present", "path": "/x"},
        text="<html>",
    )

    assert len(problems) == 1 and problems[0].startswith("input is not JSON: ")


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


async def _validate(rules, body: bytes, fetch=None, when_unmet=None, known=None):
    job = {"input_path": _IN, "rules": rules}
    if when_unmet is not None:
        job["when_unmet"] = when_unmet
    if known is not None:
        job["known_fingerprints"] = known
    with patch("worker.content.fetch_bytes", new_callable=AsyncMock) as f:
        f.return_value = body
        f.side_effect = fetch
        return await ActivityEnvironment().run(
            ContentActivities(AsyncMock()).validate,
            ValidateInput.model_validate(job),
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


async def test_validate_fail_mode_reports_every_kind_of_problem_together():
    with pytest.raises(ApplicationError) as err:
        await _validate(
            [
                {"kind": "present", "path": "/price", "negate": True},
                {"kind": "compare", "path": "/sku", "op": "gt", "value": 1},
            ],
            json.dumps(DOC).encode(),
            when_unmet="fail",
        )

    assert err.value.type == VALIDATION_FAILED
    assert err.value.message == (
        "rules[0] (present, negate): '/price' is present; "
        "rules[1] (compare): '/sku' is missing"
    )


async def test_validate_stop_mode_all_met_returns_met():
    out = await _validate(
        [
            {"kind": "present", "path": "/price"},
            {"kind": "contains", "text": "Sold out", "negate": True},
        ],
        json.dumps(DOC).encode(),
        when_unmet="stop",
    )

    assert out == ValidateVerdict(outcome="met")


async def test_validate_stop_mode_returns_the_first_rule_not_met():
    out = await _validate(
        [
            {"kind": "present", "path": "/price"},
            {"kind": "compare", "path": "/price", "op": "lt", "value": 10},
            {"kind": "present", "path": "/sku"},
        ],
        json.dumps(DOC).encode(),
        when_unmet="stop",
    )

    assert out == ValidateVerdict(
        outcome="not_met",
        rule_index=1,
        rule={
            "kind": "compare",
            "path": "/price",
            "op": "lt",
            "value": 10,
            "negate": False,
        },
        found=39.0,
        expected=10,
    )


async def test_validate_stop_mode_on_a_null_price_stops_at_the_type_rule():
    # The type rule decides before the compare that could not be checked is reached.
    out = await _validate(
        [
            {"kind": "type", "path": "/price", "type": "number"},
            {"kind": "compare", "path": "/price", "op": "lt", "value": 100},
        ],
        b'{"price": null}',
        when_unmet="stop",
    )

    assert out.outcome == "not_met"
    assert out.rule_index == 0
    assert (out.found, out.expected) == ("null", "number")


async def test_validate_stop_mode_rule_order_decides_against_input_that_is_not_json():
    out = await _validate(
        [
            {"kind": "contains", "text": "Add to cart"},
            {"kind": "present", "path": "/price"},
        ],
        b"<html>Out of stock</html>",
        when_unmet="stop",
    )

    assert (out.outcome, out.rule_index) == ("not_met", 0)
    assert out.expected == "Add to cart"


async def test_validate_stop_mode_present_negate_on_an_html_page_cannot_be_checked():
    with pytest.raises(ApplicationError) as err:
        await _validate(
            [{"kind": "present", "path": "/price", "negate": True}],
            b"<html>Service unavailable</html>",
            when_unmet="stop",
        )

    assert err.value.type == VALIDATION_UNVERIFIABLE
    assert err.value.non_retryable
    assert err.value.message.startswith(
        "rules[0] (present, negate): input is not JSON: "
    )


async def test_validate_stop_mode_cannot_be_checked_names_the_rule_and_what_was_found():
    with pytest.raises(ApplicationError) as err:
        await _validate(
            [
                {"kind": "present", "path": "/name"},
                {"kind": "compare", "path": "/name", "op": "lt", "value": 100},
            ],
            json.dumps(DOC).encode(),
            when_unmet="stop",
        )

    assert err.value.type == VALIDATION_UNVERIFIABLE
    assert err.value.non_retryable
    assert err.value.message == "rules[1] (compare): '/name' is string, not a number"


async def test_validate_stop_mode_verdict_keeps_the_rule_as_saved():
    out = await _validate(
        [{"kind": "json_schema", "schema": {"required": ["sku"]}, "negate": False}],
        json.dumps(DOC).encode(),
        when_unmet="stop",
    )

    assert out.rule == {
        "kind": "json_schema",
        "schema": {"required": ["sku"]},
        "negate": False,
    }
    assert out.found == "'sku' is a required property"


async def test_validate_stop_mode_bounds_a_large_found_value():
    big = {"items": ["x" * 50] * 20}
    out = await _validate(
        [{"kind": "present", "path": "/items", "negate": True}],
        json.dumps(big).encode(),
        when_unmet="stop",
    )

    assert isinstance(out.found, str)
    assert len(out.found) == 200 and out.found.endswith("...")


@pytest.mark.parametrize(
    ("rule", "body", "found", "expected"),
    [
        ({"kind": "min_length", "chars": 500}, b"short", 5, 500),
        (
            {"kind": "contains", "text": "short", "negate": True},
            b"short",
            None,
            "short",
        ),
        ({"kind": "present", "path": "/sku"}, json.dumps(DOC).encode(), None, None),
        (
            {"kind": "type", "path": "/in_stock", "type": "number"},
            json.dumps(DOC).encode(),
            "boolean",
            "number",
        ),
    ],
)
async def test_validate_stop_mode_found_and_expected_per_kind(
    rule, body, found, expected
):
    out = await _validate([rule], body, when_unmet="stop")

    assert (out.outcome, out.found, out.expected) == ("not_met", found, expected)


# ---------------------------------------------------------------------------
# K.2 — the `changed` rule (ADR-016 §5c–§5d)
# ---------------------------------------------------------------------------

CHANGED = [{"kind": "changed"}]
GATE_INPUT = b"<h1>Price: 39</h1>"
GATE_INPUT_SHA = hashlib.sha256(GATE_INPUT).hexdigest()


def test_two_byte_strings_that_decode_to_one_text_hash_differently():
    a, b = b"caf\xe9", b"caf\xff"
    assert a.decode("utf-8", errors="replace") == b.decode("utf-8", errors="replace")
    assert hashlib.sha256(a).hexdigest() != hashlib.sha256(b).hexdigest()


async def test_a_page_whose_bytes_differ_only_where_the_decode_hides_it_has_changed():
    old, new = b"caf\xe9", b"caf\xff"
    out = await _validate(
        CHANGED,
        new,
        when_unmet="stop",
        known={"baseline": hashlib.sha256(old).hexdigest()},
    )

    assert out.outcome == "met"
    assert out.fingerprint == hashlib.sha256(new).hexdigest()


async def test_no_known_fingerprint_is_met_and_still_returns_the_fingerprint():
    out = await _validate(CHANGED, GATE_INPUT, when_unmet="stop")

    assert out == ValidateVerdict(outcome="met", fingerprint=GATE_INPUT_SHA)


@pytest.mark.parametrize("which", ["baseline", "waiting"])
async def test_the_same_input_is_not_met_and_names_the_check_it_matches(which):
    other = "baseline" if which == "waiting" else "waiting"
    out = await _validate(
        CHANGED,
        GATE_INPUT,
        when_unmet="stop",
        known={which: GATE_INPUT_SHA, other: "0" * 64},
    )

    assert (out.outcome, out.rule_index, out.matched, out.fingerprint) == (
        "not_met",
        0,
        which,
        GATE_INPUT_SHA,
    )
    assert out.rule == {"kind": "changed", "negate": False}


async def test_a_different_input_is_met_against_both_fingerprints():
    out = await _validate(
        CHANGED,
        GATE_INPUT,
        when_unmet="stop",
        known={"baseline": "a" * 64, "waiting": "b" * 64},
    )

    assert (out.outcome, out.matched, out.fingerprint) == ("met", None, GATE_INPUT_SHA)


async def test_a_changed_rule_never_cannot_be_checked_on_any_bytes():
    body = b"\x00\xff not json, not utf-8"
    out = await _validate(CHANGED, body, when_unmet="stop")

    assert out.outcome == "met"
    assert out.fingerprint == hashlib.sha256(body).hexdigest()


async def test_a_rule_set_without_changed_computes_no_fingerprint():
    out = await _validate(
        [{"kind": "min_length", "chars": 1}], GATE_INPUT, when_unmet="stop"
    )

    assert out == ValidateVerdict(outcome="met")


async def test_fail_mode_reports_an_unchanged_input():
    with pytest.raises(ApplicationError) as err:
        await _validate(CHANGED, GATE_INPUT, known={"baseline": GATE_INPUT_SHA})

    assert (
        err.value.message == "rules[0] (changed): input is the same as the baseline's"
    )
