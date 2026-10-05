"""The pipeline block catalog (PRD-016 R2; ADR-009 §4, §5, §15c).

Pure: no settings, no I/O. Imported by the API's save-time validator and by the workflow body.

Config-schema versioning (ADR-009 §4, §6). A pipeline version is pinned for every run made from
it, so a definition saved today must still validate and run after this file changes:
- New field, optional or required: add it **with a default**. A saved definition has no value
  for it. The version number does not move.
- Removing, renaming or retyping a field: add a new model under the next version number in
  ``config_versions`` and keep the old one. Never edit a model a saved definition may name.
"""

import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_serializer,
    field_validator,
    model_validator,
)

from app.models.job import OutputFormat
from app.schemas.jobs import Engine, PlaywrightOptions, validate_page_actions

# Reference types: what a block's output object holds (ADR-009 §5 — types of reference, not
# payload). A page keeps the format Scrape wrote it in.
PAGE_HTML = "page:html"
PAGE_MARKDOWN = "page:markdown"
PAGE_JSON = "page:json"
EXTRACTION = "extraction"
PAGES = frozenset({PAGE_HTML, PAGE_MARKDOWN, PAGE_JSON})
JSON_DOCUMENTS = frozenset({PAGE_JSON, EXTRACTION})
ALL_REFS = PAGES | {EXTRACTION}

# Per-attempt queue wait (schedule_to_start), as the probe workflows use: a queue nobody polls
# fails the attempt instead of waiting out the whole budget.
SCHEDULE_TO_START_SECONDS = 60

# Allowance per block for C.6's mirror and accounting activities around it. C.6's own timeouts
# must fit inside this, or the run budget no longer composes.
BLOCK_OVERHEAD_SECONDS = 60


# --- run-input bindings (ADR-009 §4) -------------------------------------------------------


class InputBinding(BaseModel):
    """``{"$input": "<name>"}`` in place of a bindable config field's value."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input: str = Field(alias="$input")


def is_binding(value: Any) -> bool:
    return isinstance(value, dict) and "$input" in value


# Tags start with "<" so error formatting can drop them from a location.
_LITERAL_TAG = "<literal>"
_BINDING_TAG = "<binding>"


def _binding_or_literal(value: Any) -> str:
    if isinstance(value, InputBinding) or is_binding(value):
        return _BINDING_TAG
    return _LITERAL_TAG


BindableUrl = Annotated[
    Annotated[InputBinding, Tag(_BINDING_TAG)] | Annotated[AnyHttpUrl, Tag(_LITERAL_TAG)],
    Discriminator(_binding_or_literal),
]


# --- timing (PRD-016 R4; ADR-009 §15c) -----------------------------------------------------


@dataclass(frozen=True)
class Timing:
    """What C.7 passes to ``execute_activity`` for one block. Seconds throughout."""

    start_to_close: int
    maximum_attempts: int
    initial_interval: int
    backoff_coefficient: float
    maximum_interval: int
    heartbeat: int | None = None
    schedule_to_start: int = SCHEDULE_TO_START_SECONDS

    @property
    def budget(self) -> int:
        """Every attempt at its limit plus every backoff wait — the block's ``schedule_to_close``.

        A shorter ``schedule_to_close`` would cut the retry policy off early without an error (Q6).
        """
        waits = sum(
            min(self.initial_interval * self.backoff_coefficient**n, self.maximum_interval)
            for n in range(self.maximum_attempts - 1)
        )
        per_attempt = self.schedule_to_start + self.start_to_close
        return self.maximum_attempts * per_attempt + math.ceil(waits)


def _worker_retry(start_to_close: int, heartbeat: int | None = None) -> Timing:
    # The NATS workers' transient-retry numbers, on all three: 5 s, x2, 60 s cap, 3 attempts.
    return Timing(
        start_to_close=start_to_close,
        maximum_attempts=3,
        initial_interval=5,
        backoff_coefficient=2,
        maximum_interval=60,
        heartbeat=heartbeat,
    )


# --- config schemas ------------------------------------------------------------------------


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScrapeConfigV1(_Config):
    url: BindableUrl
    output_format: OutputFormat = OutputFormat.html
    engine: Engine = Engine.http
    playwright_options: PlaywrightOptions | None = None
    respect_robots: bool = False
    # Write-only on jobs; see BlockType.secret_fields.
    proxy_url: str | None = None
    cookies: list[dict] | None = None
    actions: list[dict] | None = None

    @field_validator("actions")
    @classmethod
    def _actions(cls, v: list[dict] | None) -> list[dict] | None:
        return validate_page_actions(v)

    @field_serializer("url")
    def _url(self, v: InputBinding | AnyHttpUrl) -> Any:
        return v.model_dump(by_alias=True) if isinstance(v, InputBinding) else str(v)

    @model_validator(mode="after")
    def _playwright_only(self) -> "ScrapeConfigV1":
        if self.engine != Engine.playwright:
            for name in ("actions", "playwright_options"):
                if getattr(self, name):
                    raise ValueError(f"{name} require engine: playwright")
        return self


class CleanConfigV1(_Config):
    pass


class LLMExtractConfigV1(_Config):
    llm_key_id: uuid.UUID
    model: str = Field(min_length=1, max_length=200)
    output_schema: dict[str, Any]

    @field_validator("output_schema")
    @classmethod
    def _object_root(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Both providers take the schema as an object: Anthropic as a tool's input_schema,
        # OpenAI-compatible as response_format.json_schema.
        if v.get("type") != "object":
            raise ValueError('output_schema must have "type": "object" at its root')
        return v


# RFC 6901 JSON Pointer; "" is the whole document.
JsonPointer = Annotated[str, Field(pattern=r"^(/([^/~]|~[01])*)*$", max_length=500)]
Scalar = str | int | float | bool | None


class JsonSchemaRule(_Config):
    kind: Literal["json_schema"]
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PresentRule(_Config):
    kind: Literal["present"]
    path: JsonPointer


class TypeRule(_Config):
    kind: Literal["type"]
    path: JsonPointer
    type: Literal["string", "number", "integer", "boolean", "object", "array", "null"]


class CompareRule(_Config):
    kind: Literal["compare"]
    path: JsonPointer
    op: Literal["eq", "ne", "lt", "le", "gt", "ge"]
    value: Scalar

    @model_validator(mode="after")
    def _ordered_needs_number(self) -> "CompareRule":
        if self.op in {"lt", "le", "gt", "ge"} and (
            isinstance(self.value, bool) or not isinstance(self.value, int | float)
        ):
            raise ValueError(f"op {self.op!r} compares numbers; value must be a number")
        return self


class ContainsRule(_Config):
    kind: Literal["contains"]
    text: str = Field(min_length=1, max_length=1000)


class MinLengthRule(_Config):
    kind: Literal["min_length"]
    chars: int = Field(ge=1)


Rule = Annotated[
    JsonSchemaRule | PresentRule | TypeRule | CompareRule | ContainsRule | MinLengthRule,
    Field(discriminator="kind"),
]
JSON_RULES = frozenset({"json_schema", "present", "type", "compare"})
TEXT_RULES = frozenset({"contains", "min_length"})


class ValidateConfigV1(_Config):
    rules: list[Rule] = Field(min_length=1, max_length=50)


class WebhookConfigV1(_Config):
    url: AnyHttpUrl

    @field_serializer("url")
    def _url(self, v: AnyHttpUrl) -> str:
        return str(v)


# --- the catalog ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockType:
    name: str
    # ADR-009 §5: content blocks write an object; effect blocks pass their input reference on.
    kind: Literal["content", "effect"]
    config_versions: Mapping[int, type[BaseModel]]
    # Reference types accepted as input. Empty = a source block, which takes no input.
    consumes: frozenset[str]
    # Output reference type, given the config and the input's type. Effect blocks return the input.
    produces: Callable[[Any, str | None], str]
    timing: Callable[[Any], Timing]
    # Config fields that may hold an InputBinding (ADR-009 §4 — narrow on purpose).
    bindable_fields: frozenset[str] = frozenset()
    # Config fields holding user secrets — C.4 must not store or return them in plaintext.
    secret_fields: frozenset[str] = frozenset()
    # Extra save-time checks that need the input's reference type; returns problem messages.
    check_input: Callable[[Any, str], list[str]] = field(default=lambda _config, _ref: [])

    @property
    def current_version(self) -> int:
        return max(self.config_versions)

    @property
    def is_source(self) -> bool:
        return not self.consumes


def _scrape_timing(config: ScrapeConfigV1) -> Timing:
    if config.engine == Engine.playwright:
        # goto and wait_for_load_state each get timeout_seconds (BUG-015), + actions and upload.
        render_s = (config.playwright_options or PlaywrightOptions()).timeout_seconds
        # The activity heartbeats every 30 s.
        return _worker_retry(2 * render_s + 60, heartbeat=90)
    return _worker_retry(90)


def _pass_through(_config: Any, ref: str | None) -> str:
    # Never a starting block (the validator's rule), so there is always an input.
    assert ref is not None
    return ref


def _validate_check_input(config: ValidateConfigV1, ref: str) -> list[str]:
    problems = []
    for i, rule in enumerate(config.rules):
        if rule.kind in JSON_RULES and ref not in JSON_DOCUMENTS:
            problems.append(f"rules[{i}] ({rule.kind}) needs JSON input; this block receives {ref}")
        elif rule.kind in TEXT_RULES and ref not in PAGES:
            problems.append(f"rules[{i}] ({rule.kind}) needs a page; this block receives {ref}")
    return problems


SCRAPE = BlockType(
    name="scrape",
    kind="content",
    config_versions={1: ScrapeConfigV1},
    consumes=frozenset(),
    produces=lambda config, _ref: f"page:{config.output_format.value}",
    timing=_scrape_timing,
    bindable_fields=frozenset({"url"}),
    secret_fields=frozenset({"proxy_url", "cookies"}),
)

CLEAN = BlockType(
    name="clean",
    kind="content",
    config_versions={1: CleanConfigV1},
    consumes=frozenset({PAGE_HTML}),
    produces=lambda _config, _ref: PAGE_MARKDOWN,
    timing=lambda _config: _worker_retry(60),
)

LLM_EXTRACT = BlockType(
    name="llm_extract",
    kind="content",
    config_versions={1: LLMExtractConfigV1},
    consumes=PAGES,
    produces=lambda _config, _ref: EXTRACTION,
    # >= warm-up + request: 180 + 180 s in prod (LLMExtract's docstring). The activity heartbeats.
    timing=lambda _config: _worker_retry(400, heartbeat=90),
)

VALIDATE = BlockType(
    name="validate",
    kind="effect",
    config_versions={1: ValidateConfigV1},
    consumes=ALL_REFS,
    produces=_pass_through,
    timing=lambda _config: _worker_retry(60),
    check_input=_validate_check_input,
)

WEBHOOK = BlockType(
    name="webhook",
    kind="effect",
    config_versions={1: WebhookConfigV1},
    consumes=ALL_REFS,
    produces=_pass_through,
    # ADR-009 §15b/§15c: 10 s POST inside a ~20 s attempt; 30 s x10 capped at 7200 s, 5 attempts.
    timing=lambda _config: Timing(
        start_to_close=20,
        maximum_attempts=5,
        initial_interval=30,
        backoff_coefficient=10,
        maximum_interval=7200,
    ),
)

CATALOG: Mapping[str, BlockType] = {
    t.name: t for t in (SCRAPE, CLEAN, LLM_EXTRACT, VALIDATE, WEBHOOK)
}
