"""Validate: check a block's input against declarative rules."""

import json
import operator
from typing import Any

from jsonschema import exceptions, validators

from .contracts import (
    CompareRule,
    ContainsRule,
    JsonSchemaRule,
    MinLengthRule,
    PresentRule,
    Rule,
    TypeRule,
)

_JSON_RULES = (JsonSchemaRule, PresentRule, TypeRule, CompareRule)
_ORDERED = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}
_MISSING = object()


def _resolve(doc: Any, pointer: str) -> Any:
    """RFC 6901; _MISSING when the path does not exist."""
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
            return _MISSING
    return node


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _json_type(v: Any) -> str:
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
        return _is_number(v)
    if expected == "integer":
        return _is_number(v) and float(v).is_integer()
    return _json_type(v) == expected


def _equal(a: Any, b: Any) -> bool:
    # JSON equality: 1 == 1.0, but true is not 1.
    if _is_number(a) and _is_number(b):
        return a == b
    return _json_type(a) == _json_type(b) and a == b


def _show(v: Any) -> str:
    text = json.dumps(v)
    return text if len(text) <= 80 else text[:77] + "..."


def _check(rule: Rule, text: str, doc: Any) -> str | None:
    if isinstance(rule, ContainsRule):
        return None if rule.text in text else f"{rule.text!r} not found"
    if isinstance(rule, MinLengthRule):
        n = len(text)
        return (
            None
            if n >= rule.chars
            else f"{n} characters; at least {rule.chars} required"
        )
    if isinstance(rule, JsonSchemaRule):
        cls = validators.validator_for(rule.schema_)
        try:
            cls.check_schema(rule.schema_)
        except exceptions.SchemaError as exc:
            return f"invalid schema: {exc.message}"
        error = exceptions.best_match(cls(rule.schema_).iter_errors(doc))
        if error is None:
            return None
        where = "".join(f"/{p}" for p in error.absolute_path)
        return f"{error.message} at {where!r}" if where else error.message

    value = _resolve(doc, rule.path)
    if value is _MISSING:
        return f"{rule.path!r} is missing"
    if isinstance(rule, PresentRule):
        return None
    if isinstance(rule, TypeRule):
        if _is_type(value, rule.type):
            return None
        return f"{rule.path!r} is {_json_type(value)}, not {rule.type}"
    if rule.op in _ORDERED:
        if not _is_number(value):
            return f"{rule.path!r} is {_json_type(value)}, not a number"
        if not _is_number(rule.value):
            return f"value {_show(rule.value)} is not a number"
        ok = _ORDERED[rule.op](value, rule.value)
    else:
        ok = _equal(value, rule.value) == (rule.op == "eq")
    return (
        None
        if ok
        else f"{rule.path!r} is {_show(value)}; expected {rule.op} {_show(rule.value)}"
    )


def failures(rules: list[Rule], text: str) -> list[str]:
    """One message per failing rule, each naming it; empty when every rule passes."""
    doc: Any = None
    if any(isinstance(r, _JSON_RULES) for r in rules):
        try:
            doc = json.loads(text)
        except ValueError as exc:
            return [f"input is not JSON: {exc}"]
    problems = []
    for i, rule in enumerate(rules):
        problem = _check(rule, text, doc)
        if problem is not None:
            problems.append(f"rules[{i}] ({rule.kind}): {problem}")
    return problems
