"""Validate: check a block's input against declarative rules.

Each rule answers met, not met, or cannot be checked (ADR-014 §3). ``negate`` flips met and
not met; cannot be checked is never flipped. A ``changed`` rule compares fingerprints, never
the text, and is always checkable (ADR-016 §5d).
"""

import json
import operator
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import exceptions, validators

from .contracts import (
    ChangedRule,
    CompareRule,
    ContainsRule,
    JsonSchemaRule,
    FingerprintOf,
    MinLengthRule,
    PresentRule,
    Rule,
    TypeRule,
)

_JSON_RULES = (JsonSchemaRule, PresentRule, TypeRule, CompareRule)
_ORDERED = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}
MISSING = object()
_FOUND_MAX = 200

Answer = Literal["met", "not_met", "unverifiable"]


@dataclass(frozen=True)
class Fingerprints:
    """The input's, and the earlier checks' a `changed` rule compares it with."""

    computed: str
    known: dict[FingerprintOf, str]


NO_FINGERPRINTS = Fingerprints(computed="", known={})


@dataclass(frozen=True)
class Result:
    answer: Answer
    # What fail mode reports; None when met.
    problem: str | None = None
    found: Any = None
    expected: Any = None
    matched: FingerprintOf | None = None


@dataclass(frozen=True)
class _Base:
    """A rule's un-negated answer, with the message for each way it can end up not met."""

    met: bool
    problem: str  # when not met
    negated_problem: str  # when met but negated
    found: Any = None
    expected: Any = None
    matched: FingerprintOf | None = None


class _Unverifiable(Exception):
    def __init__(self, problem: str, found: Any = None, expected: Any = None) -> None:
        super().__init__(problem)
        self.problem = problem
        self.found = found
        self.expected = expected


def resolve(doc: Any, pointer: str) -> Any:
    """RFC 6901; MISSING when the path does not exist."""
    if pointer == "":
        return doc
    node = doc
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif (
            isinstance(node, list)
            and token.isdigit()
            and (token == "0" or not token.startswith("0"))
            and int(token) < len(node)
        ):
            node = node[int(token)]
        else:
            return MISSING
    return node


def is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def _is_type(v: Any, expected: str) -> bool:
    if expected == "number":
        return is_number(v)
    if expected == "integer":
        return is_number(v) and float(v).is_integer()
    return json_type(v) == expected


def json_equal(a: Any, b: Any) -> bool:
    # JSON equality: 1 == 1.0, but true is not 1.
    if is_number(a) and is_number(b):
        return a == b
    return json_type(a) == json_type(b) and a == b


def _show(v: Any) -> str:
    text = json.dumps(v)
    return text if len(text) <= 80 else text[:77] + "..."


def _bounded(v: Any) -> Any:
    """A value for the verdict; past _FOUND_MAX characters of JSON, its truncated text."""
    text = json.dumps(v)
    return v if len(text) <= _FOUND_MAX else text[: _FOUND_MAX - 3] + "..."


def _base(rule: Rule, text: str, doc: Any, prints: Fingerprints) -> _Base:
    if isinstance(rule, ChangedRule):
        # "baseline" first: the order is fixed, not the map's.
        matched = next(
            (
                k
                for k in ("baseline", "waiting")
                if prints.known.get(k) == prints.computed
            ),
            None,
        )
        return _Base(
            met=matched is None,
            problem=f"input is the same as the {matched}'s",
            negated_problem="input changed",
            matched=matched,
        )
    if isinstance(rule, ContainsRule):
        return _Base(
            met=rule.text in text,
            problem=f"{rule.text!r} not found",
            negated_problem=f"{rule.text!r} found",
            expected=_bounded(rule.text),
        )
    if isinstance(rule, MinLengthRule):
        n = len(text)
        return _Base(
            met=n >= rule.chars,
            problem=f"{n} characters; at least {rule.chars} required",
            negated_problem=f"{n} characters; fewer than {rule.chars} required",
            found=n,
            expected=rule.chars,
        )
    if isinstance(rule, JsonSchemaRule):
        cls = validators.validator_for(rule.schema_)
        try:
            cls.check_schema(rule.schema_)
        except exceptions.SchemaError as exc:
            raise _Unverifiable(f"invalid schema: {exc.message}") from None
        error = exceptions.best_match(cls(rule.schema_).iter_errors(doc))
        if error is None:
            return _Base(
                met=True, problem="", negated_problem="input matches the schema"
            )
        where = "".join(f"/{p}" for p in error.absolute_path)
        message = f"{error.message} at {where!r}" if where else error.message
        return _Base(
            met=False, problem=message, negated_problem="", found=_bounded(message)
        )

    value = resolve(doc, rule.path)
    missing = f"{rule.path!r} is missing"
    if isinstance(rule, PresentRule):
        if value is MISSING:
            return _Base(met=False, problem=missing, negated_problem="")
        return _Base(
            met=True,
            problem="",
            negated_problem=f"{rule.path!r} is present",
            found=_bounded(value),
        )
    if isinstance(rule, TypeRule):
        if value is MISSING:
            return _Base(
                met=False, problem=missing, negated_problem="", expected=rule.type
            )
        found = json_type(value)
        return _Base(
            met=_is_type(value, rule.type),
            problem=f"{rule.path!r} is {found}, not {rule.type}",
            negated_problem=f"{rule.path!r} is {rule.type}",
            found=found,
            expected=rule.type,
        )

    expected = _bounded(rule.value)
    if value is MISSING:
        raise _Unverifiable(missing, expected=expected)
    found = _bounded(value)
    if rule.op in _ORDERED:
        if not is_number(value):
            raise _Unverifiable(
                f"{rule.path!r} is {json_type(value)}, not a number",
                found=found,
                expected=expected,
            )
        if not is_number(rule.value):
            raise _Unverifiable(
                f"value {_show(rule.value)} is not a number",
                found=found,
                expected=expected,
            )
        ok = _ORDERED[rule.op](value, rule.value)
    else:
        ok = json_equal(value, rule.value) == (rule.op == "eq")
    return _Base(
        met=ok,
        problem=f"{rule.path!r} is {_show(value)}; expected {rule.op} {_show(rule.value)}",
        negated_problem=(
            f"{rule.path!r} is {_show(value)}; expected not {rule.op} {_show(rule.value)}"
        ),
        found=found,
        expected=expected,
    )


def _evaluate(
    rule: Rule, text: str, doc: Any, not_json: str | None, prints: Fingerprints
) -> Result:
    if not_json is not None and isinstance(rule, _JSON_RULES):
        return Result("unverifiable", f"input is not JSON: {not_json}")
    try:
        base = _base(rule, text, doc, prints)
    except _Unverifiable as exc:
        return Result("unverifiable", exc.problem, exc.found, exc.expected)
    if base.met != rule.negate:
        return Result("met", None, base.found, base.expected, base.matched)
    problem = base.negated_problem if rule.negate else base.problem
    return Result("not_met", problem, base.found, base.expected, base.matched)


def _parse(rules: list[Rule], text: str) -> tuple[Any, str | None]:
    """The input as JSON when a rule needs it, else None; and why it is not JSON, if it is not."""
    if not any(isinstance(r, _JSON_RULES) for r in rules):
        return None, None
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, str(exc)


def label(index: int, rule: Rule) -> str:
    return f"rules[{index}] ({rule.kind}{', negate' if rule.negate else ''})"


def failures(
    rules: list[Rule], text: str, prints: Fingerprints = NO_FINGERPRINTS
) -> list[str]:
    """Fail mode: one message per rule not met or not checkable; empty when every rule is met."""
    doc, not_json = _parse(rules, text)
    if not_json is not None:
        return [f"input is not JSON: {not_json}"]
    problems = []
    for i, rule in enumerate(rules):
        result = _evaluate(rule, text, doc, None, prints)
        if result.answer != "met":
            problems.append(f"{label(i, rule)}: {result.problem}")
    return problems


def first_unmet(
    rules: list[Rule], text: str, prints: Fingerprints = NO_FINGERPRINTS
) -> tuple[int, Result] | None:
    """Stop mode: the first rule whose answer is not met or unverifiable; None when all are met."""
    doc, not_json = _parse(rules, text)
    for i, rule in enumerate(rules):
        result = _evaluate(rule, text, doc, not_json, prints)
        if result.answer != "met":
            return i, result
    return None
