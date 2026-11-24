"""C.3 — the block catalog and the save-time validator (PRD-016 R1, R2, R4; ADR-009 §4, §5, §8)."""

import copy

import pytest

from app.pipelines import validator
from app.pipelines.catalog import (
    BLOCK_OVERHEAD_SECONDS,
    BUCKET,
    BUCKET_UNDO_TIMING,
    CATALOG,
    DB_ACTIVITY_SECONDS,
    EMAIL,
    LLM_EXTRACT,
    POSTGRES,
    POSTGRES_UNDO_TIMING,
    SCRAPE,
    WEBHOOK,
    BucketConfigV1,
    ScrapeConfigV1,
    WebhookConfigV1,
)
from app.pipelines.validator import PipelineValidationError, validate
from app.settings import settings

KEY_ID = "7b1c1d5e-0000-4000-8000-000000000001"
DEST_ID = "7b1c1d5e-0000-4000-8000-0000000000d1"
SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}


def scrape(id="fetch", **config):
    return {"id": id, "type": "scrape", "config": {"url": {"$input": "url"}, **config}}


def llm(id="extract", input="fetch"):
    return {
        "id": id,
        "type": "llm_extract",
        "input": input,
        "config": {"llm_key_id": KEY_ID, "model": "claude-haiku-4-5", "output_schema": SCHEMA},
    }


def webhook(id="notify", input="extract", url="https://example.com/hook"):
    return {"id": id, "type": "webhook", "input": input, "config": {"url": url}}


def bucket(id="store", input="extract", destination_id=DEST_ID):
    return {
        "id": id,
        "type": "bucket",
        "input": input,
        "config": {"destination_id": destination_id},
    }


def clean(id="clean", input="fetch"):
    return {"id": id, "type": "clean", "input": input, "config": {}}


def check(id="check", input="fetch", rules=None):
    rules = [{"kind": "min_length", "chars": 100}] if rules is None else rules
    return {"id": id, "type": "validate", "input": input, "config": {"rules": rules}}


def pipeline(*blocks, **top):
    """Declares the ``url`` run input only when the scrape block binds it."""
    bound = blocks and blocks[0].get("config", {}).get("url") == {"$input": "url"}
    inputs = {"url": {"type": "url"}} if bound else {}
    return {"inputs": inputs, "blocks": list(blocks), **top}


def r6():
    """PRD-016 R6: scrape -> LLM -> webhook, URL as a run input."""
    return pipeline(scrape(), llm(), webhook())


def problems(definition) -> list[str]:
    with pytest.raises(PipelineValidationError) as exc:
        validate(definition)
    return [str(p) for p in exc.value.problems]


def one_problem(definition) -> str:
    found = problems(definition)
    assert len(found) == 1, found
    return found[0]


# --- the R6 recipe -------------------------------------------------------------------------


def test_r6_recipe_validates():
    result = validate(r6())
    assert [b.id for b in result.blocks] == ["fetch", "extract", "notify"]
    assert [b.type for b in result.blocks] == [SCRAPE, LLM_EXTRACT, WEBHOOK]


def test_normalized_definition_stamps_versions_and_fills_defaults():
    stored = validate(r6()).definition
    fetch = stored["blocks"][0]
    assert fetch["config_schema_version"] == 1
    assert fetch["config"]["url"] == {"$input": "url"}
    assert fetch["config"]["output_format"] == "html"
    assert fetch["config"]["engine"] == "http"
    assert stored["blocks"][1]["config"]["llm_key_id"] == KEY_ID
    assert stored["time_budget_seconds"] is None


def test_normalized_definition_revalidates_to_itself():
    # C.5 and C.7 re-read stored definitions; a stored one must stay valid and unchanged.
    stored = validate(r6()).definition
    assert validate(copy.deepcopy(stored)).definition == stored


# --- shape ---------------------------------------------------------------------------------


def test_unknown_top_level_field_rejected():
    assert "extra" in one_problem({**r6(), "extra": 1})


def test_block_without_id_named_by_position():
    definition = r6()
    del definition["blocks"][1]["id"]
    assert one_problem(definition).startswith("blocks[1].id")


def test_empty_pipeline_rejected():
    assert "blocks" in one_problem(pipeline())


def test_duplicate_block_id_rejected():
    found = problems(pipeline(scrape(), llm(), webhook(id="extract")))
    assert "block 'extract': block id is used more than once" in found


# --- unknown type, config version, invalid config ------------------------------------------


def test_unknown_block_type_rejected():
    definition = r6()
    definition["blocks"][1]["type"] = "summarize"
    found = problems(definition)
    assert "block 'extract': unknown block type 'summarize'" in found[0]


def test_unknown_config_schema_version_rejected():
    definition = r6()
    definition["blocks"][1]["config_schema_version"] = 2
    assert one_problem(definition) == "block 'extract': llm_extract has no config schema version 2"


def test_explicit_current_config_schema_version_accepted():
    definition = r6()
    definition["blocks"][1]["config_schema_version"] = 1
    validate(definition)


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (scrape(url="not a url"), "config.url"),
        (scrape(nope=1), "config.nope"),
        (scrape(actions=[{"type": "wait", "milliseconds": 5}]), "actions require engine"),
        (scrape(playwright_options={"timeout_seconds": 30}), "playwright_options require"),
        (scrape(engine="playwright", actions=[{"type": "fly"}]), "unknown action type"),
        (scrape(engine="playwright", playwright_options={"timeout_seconds": 301}), "300"),
    ],
)
def test_invalid_scrape_config_rejected(block, expected):
    found = one_problem(pipeline(block, llm()))
    assert found.startswith("block 'fetch': config")
    assert expected in found


def test_playwright_scrape_with_actions_accepted():
    actions = [{"type": "click", "selector": "#more"}]
    validate(pipeline(scrape(engine="playwright", actions=actions), llm()))


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"model": "m", "output_schema": SCHEMA}, "config.llm_key_id: Field required"),
        ({"llm_key_id": "x", "model": "m", "output_schema": SCHEMA}, "config.llm_key_id"),
        ({"llm_key_id": KEY_ID, "model": "", "output_schema": SCHEMA}, "config.model"),
        ({"llm_key_id": KEY_ID, "model": "m", "output_schema": {"type": "array"}}, '"object"'),
    ],
)
def test_invalid_llm_config_rejected(config, expected):
    block = {"id": "extract", "type": "llm_extract", "input": "fetch", "config": config}
    assert expected in one_problem(pipeline(scrape(), block))


def test_invalid_webhook_url_rejected():
    assert "config.url" in one_problem(pipeline(scrape(), llm(), webhook(url="ftp://x")))


def test_clean_takes_no_config():
    block = clean()
    block["config"] = {"aggressive": True}
    assert "config.aggressive" in one_problem(pipeline(scrape(), block))


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ([], "config.rules"),
        ([{"kind": "regex", "pattern": ".*"}], "config.rules[0]"),
        ([{"kind": "compare", "path": "/price", "op": "lt", "value": "100"}], "number"),
        ([{"kind": "compare", "path": "/price", "op": "lt", "value": True}], "number"),
        ([{"kind": "present", "path": "price"}], "config.rules[0].present.path"),
    ],
)
def test_invalid_validate_config_rejected(rules, expected):
    found = one_problem(pipeline(scrape(), llm(), check(input="extract", rules=rules)))
    assert expected in found


def test_declarative_validate_rules_accepted():
    rules = [
        {"kind": "json_schema", "schema": SCHEMA},
        {"kind": "present", "path": "/title"},
        {"kind": "type", "path": "/title", "type": "string"},
        {"kind": "compare", "path": "/price", "op": "lt", "value": 100},
        {"kind": "compare", "path": "/currency", "op": "eq", "value": "EUR"},
    ]
    stored = validate(pipeline(scrape(), llm(), check(input="extract", rules=rules))).definition
    assert stored["blocks"][2]["config"]["rules"][0]["schema"] == SCHEMA


def test_malformed_json_schema_rule_rejected():
    rules = [{"kind": "present", "path": "/x"}, {"kind": "json_schema", "schema": {"type": 5}}]
    found = one_problem(pipeline(scrape(), llm(), check(input="extract", rules=rules)))
    assert found.startswith("block 'check': config.rules[1].schema is not a valid JSON Schema: ")


def test_malformed_llm_output_schema_rejected():
    block = llm()
    block["config"]["output_schema"] = {"type": "object", "required": "title"}
    found = one_problem(pipeline(scrape(), block))
    assert found.startswith("block 'extract': config.output_schema is not a valid JSON Schema: ")


def test_malformed_schema_does_not_hide_later_checks():
    # The block still parses, so the budget and type walk run in the same save.
    block = llm()
    block["config"]["output_schema"] = {"type": "object", "required": "title"}
    found = problems(pipeline(scrape(), block, webhook(), bucket(input="notify")))
    assert len(found) == 2


# --- exactly one starting block, and it is a Scrape (ADR-009 §8) ---------------------------


def test_first_block_must_be_scrape():
    found = problems({"blocks": [llm(input=None), webhook()]})
    assert "block 'extract': a pipeline must start with a scrape block, not llm_extract" in found


def test_starting_scrape_takes_no_input():
    first = scrape()
    first["input"] = "fetch"
    assert "the starting scrape block takes no input" in one_problem(pipeline(first, llm()))


def test_second_scrape_rejected_even_when_the_chain_holds():
    # The chain rule alone is satisfied here; the starting-block rule must stand on its own.
    second = scrape(id="again")
    second["input"] = "fetch"
    blocks = pipeline(scrape(), second, llm(input="again"))
    assert validator._chain_problems(validator.PipelineDefinition(**blocks).blocks) == []
    found = one_problem(blocks)
    assert found.startswith("block 'again': a pipeline has exactly one starting block")


def test_later_block_without_input_is_a_second_start():
    found = one_problem(pipeline(scrape(), llm(input=None)))
    assert found == (
        "block 'extract': a pipeline has exactly one starting block; this block has no input"
    )


# --- single chain in data flow (ADR-009 §4) ------------------------------------------------


def test_input_naming_unknown_block_rejected():
    found = one_problem(pipeline(scrape(), llm(input="nowhere")))
    assert found == "block 'extract': input 'nowhere' is not a block in this pipeline"


def test_input_naming_later_block_rejected():
    found = problems(pipeline(scrape(), llm(input="notify"), webhook()))
    assert "block 'extract': input 'notify' does not come before this block" in found


def test_fan_out_rejected():
    found = one_problem(pipeline(scrape(), llm(), webhook(input="fetch")))
    assert found.startswith("block 'notify': input must be the previous block 'extract'")
    assert "fan-out" in found


# --- block n consumes what block n-1 produces ----------------------------------------------


def test_clean_needs_html():
    validate(pipeline(scrape(), clean(), llm(input="clean")))
    found = one_problem(pipeline(scrape(output_format="markdown"), clean(), llm(input="clean")))
    assert found == "block 'clean': clean consumes page:html; 'fetch' produces page:markdown"


def test_llm_cannot_consume_an_extraction():
    found = one_problem(pipeline(scrape(), llm(), llm(id="again", input="extract")))
    assert found.startswith("block 'again': llm_extract consumes page:html")
    assert "'extract' produces extraction" in found


def test_effect_block_passes_its_input_type_through():
    # validate between scrape and LLM hands the LLM a page, not nothing.
    validate(pipeline(scrape(), check(), llm(input="check"), webhook()))


def test_json_rule_on_a_text_page_rejected():
    rules = [{"kind": "present", "path": "/title"}]
    found = one_problem(pipeline(scrape(output_format="markdown"), check(rules=rules)))
    assert found == (
        "block 'check': rules[0] (present) needs JSON input; this block receives page:markdown"
    )


def test_json_rule_on_a_json_page_accepted():
    rules = [{"kind": "present", "path": "/title"}]
    validate(pipeline(scrape(output_format="json"), check(rules=rules)))


def test_text_rule_on_an_extraction_rejected():
    rules = [{"kind": "contains", "text": "price"}]
    found = one_problem(pipeline(scrape(), llm(), check(input="extract", rules=rules)))
    assert "rules[0] (contains) needs a page; this block receives extraction" in found


# --- run inputs (ADR-009 §4: bindings, declared fields only) -------------------------------


def test_literal_url_needs_no_run_input():
    validate(pipeline(scrape(url="https://example.com"), llm()))


def test_undeclared_run_input_rejected():
    found = one_problem({"blocks": [scrape(), llm()]})
    assert found == (
        "block 'fetch': config.url uses run input 'url', which the pipeline does not declare"
    )


@pytest.mark.parametrize("block", [webhook(url={"$input": "url"})])
def test_binding_a_non_bindable_field_rejected(block):
    found = one_problem(pipeline(scrape(url="https://example.com"), llm(), block))
    assert found == "block 'notify': config.url cannot take a run input; webhook binds: nothing"


def test_binding_llm_schema_rejected():
    block = llm()
    block["config"]["output_schema"] = {"$input": "url"}
    found = one_problem(pipeline(scrape(), block))
    assert "config.output_schema cannot take a run input; llm_extract binds: nothing" in found


def test_unused_run_input_rejected():
    definition = r6()
    definition["inputs"]["other"] = {"type": "url"}
    assert one_problem(definition) == "run input 'other' is declared but no block uses it"


def test_run_input_name_and_type_checked():
    definition = r6()
    definition["inputs"] = {"Bad-Name": {"type": "url"}}
    definition["blocks"][0]["config"]["url"] = {"$input": "Bad-Name"}
    assert "run input name 'Bad-Name'" in one_problem(definition)

    definition = r6()
    definition["inputs"]["url"] = {"type": "date"}
    assert one_problem(definition).startswith("inputs.url.type")

    definition = r6()
    definition["inputs"]["url"] = {"type": "number"}
    assert one_problem(definition) == (
        "block 'fetch': config.url takes a url run input; 'url' is declared number"
    )


def test_the_bindable_fields_are_scrape_url_and_two_rule_fields():
    assert {t.name: set(t.bindable_fields) for t in CATALOG.values() if t.bindable_fields} == {
        "scrape": {"url"},
        "validate": {"rules[].value", "rules[].text"},
    }


# --- several webhooks (PRD-017 R4) ----------------------------------------------------------


def test_several_webhooks_are_accepted():
    result = validate(pipeline(scrape(), llm(), webhook(), webhook(id="again", input="notify")))
    assert [b.id for b in result.blocks] == ["fetch", "extract", "notify", "again"]


# --- limits --------------------------------------------------------------------------------


def test_max_blocks_per_pipeline(monkeypatch):
    monkeypatch.setattr(settings, "max_blocks_per_pipeline", 3)
    validate(r6())
    found = one_problem(pipeline(scrape(), check(), llm(input="check"), webhook()))
    assert found == "4 blocks; the limit is 3"


# --- time budgets compose (PRD-016 R4; ADR-009 §15c) ---------------------------------------


def _needed(definition) -> int:
    return sum(
        b.timing.budget + b.timing.host_slot + BLOCK_OVERHEAD_SECONDS
        for b in validate(definition).blocks
    )


def test_run_budget_defaults_to_what_the_blocks_need():
    result = validate(r6())
    assert result.run_budget_seconds == _needed(r6())


def test_run_budget_shorter_than_its_blocks_rejected():
    needed = _needed(r6())
    found = one_problem({**r6(), "time_budget_seconds": needed - 1})
    assert found.startswith(f"time_budget_seconds {needed - 1} is shorter than its blocks need")
    assert (
        f"notify {WEBHOOK.timing(WebhookConfigV1(url='https://h.example/')).budget + BLOCK_OVERHEAD_SECONDS}"
        in found
    )


def test_run_budget_covering_its_blocks_accepted():
    needed = _needed(r6())
    assert validate({**r6(), "time_budget_seconds": needed}).run_budget_seconds == needed


def test_run_budget_over_the_operator_limit_rejected(monkeypatch):
    needed = _needed(r6())
    monkeypatch.setattr(settings, "pipeline_max_run_seconds", needed)
    found = one_problem({**r6(), "time_budget_seconds": needed + 1})
    assert found == f"time_budget_seconds {needed + 1} is over the limit of {needed} s"


def test_blocks_needing_more_than_the_operator_limit_rejected(monkeypatch):
    needed = _needed(r6())
    monkeypatch.setattr(settings, "pipeline_max_run_seconds", needed - 1)
    assert one_problem(r6()).startswith(f"blocks need {needed} s")


def test_playwright_timeout_raises_the_scrape_budget():
    slow = ScrapeConfigV1(
        url="https://example.com", engine="playwright", playwright_options={"timeout_seconds": 300}
    )
    fast = ScrapeConfigV1(url="https://example.com", engine="playwright")
    assert SCRAPE.timing(slow).start_to_close == 660
    assert SCRAPE.timing(slow).budget > SCRAPE.timing(fast).budget
    assert SCRAPE.timing(slow).heartbeat == 90


def test_scrape_need_counts_the_queue_timer_and_the_host_wait():
    http = SCRAPE.timing(ScrapeConfigV1(url="https://example.com"))
    playwright = SCRAPE.timing(ScrapeConfigV1(url="https://example.com", engine="playwright"))
    # 3 attempts x (5 min queue timer + run) + 5 s + 10 s backoff.
    assert http.budget == 3 * (300 + 90) + 15
    assert playwright.budget == 3 * (300 + 180) + 15
    # + the host-slot wait (10 min cap + its activities) + the DB overhead.
    assert http.need == http.budget + 660 + BLOCK_OVERHEAD_SECONDS
    assert playwright.need == playwright.budget + 660 + BLOCK_OVERHEAD_SECONDS


def test_only_scrape_blocks_get_the_long_queue_timer_and_a_host_wait():
    for block_type in CATALOG.values():
        if block_type is SCRAPE:
            continue
        model = block_type.config_versions[block_type.current_version]
        timing = block_type.timing(model.model_construct())
        assert (timing.schedule_to_start, timing.host_slot) == (60, 0), block_type.name


def test_webhook_ladder_matches_adr_15c():
    timing = WEBHOOK.timing(WebhookConfigV1(url="https://h.example/"))
    assert (timing.start_to_close, timing.maximum_attempts) == (20, 5)
    assert (timing.initial_interval, timing.backoff_coefficient, timing.maximum_interval) == (
        30,
        10,
        7200,
    )
    # Waits 30 + 300 + 3000 + 7200 s, then every attempt at its limit; >= the 2.6 h horizon.
    assert timing.budget == 30 + 300 + 3000 + 7200 + 5 * (60 + 20)
    assert timing.budget > 2.6 * 3600


def test_llm_attempt_covers_cold_start_and_request():
    # warm-up 180 s + request 180 s in production (ADR-009 §10).
    assert LLM_EXTRACT.timing(None).start_to_close >= 360


def test_every_block_type_declares_a_budget_and_a_finite_retry():
    for block_type in CATALOG.values():
        model = block_type.config_versions[block_type.current_version]
        config = model.model_construct(engine="http", playwright_options=None)
        timing = block_type.timing(config)
        assert timing.budget > 0
        assert 1 <= timing.maximum_attempts <= 5, block_type.name


# --- J.4: stop mode, reverse rules, events, typed run inputs (ADR-014 §4, §9) --------------


def json_scrape(id="fetch"):
    return {
        "id": id,
        "type": "scrape",
        "config": {"url": "https://example.com", "output_format": "json"},
    }


def guard(rules, id="check", input="fetch", **config):
    return {"id": id, "type": "validate", "input": input, "config": {"rules": rules, **config}}


def watch(rules, inputs, **config):
    """scrape (JSON) -> Validate(rules) -> webhook, with the given run inputs."""
    return {
        "inputs": inputs,
        "blocks": [json_scrape(), guard(rules, **config), webhook(input="check")],
    }


BELOW = {"kind": "compare", "path": "/price", "op": "lt", "value": {"$input": "max_price"}}


def test_new_settings_default_so_an_old_definition_means_what_it_meant():
    result = validate(
        pipeline(json_scrape(), guard([{"kind": "present", "path": "/p"}]), webhook(input="check"))
    )
    _, check_block, notify = result.definition["blocks"]

    assert check_block["config"] == {
        "rules": [{"kind": "present", "path": "/p", "negate": False}],
        "when_unmet": "fail",
        "on_stop_outputs": None,
    }
    assert notify["config"]["events"] == ["pipeline.result"]
    assert {b["config_schema_version"] for b in result.definition["blocks"]} == {1}


def test_a_definition_stored_before_j4_still_parses_with_the_defaults():
    from app.pipelines.definition import parse_definition

    stored = {
        "inputs": {},
        "time_budget_seconds": None,
        "blocks": [
            {
                "id": "fetch",
                "type": "scrape",
                "config_schema_version": 1,
                "config": {"url": "https://example.com", "output_format": "json"},
            },
            {
                "id": "check",
                "type": "validate",
                "input": "fetch",
                "config_schema_version": 1,
                "config": {
                    "rules": [{"kind": "compare", "path": "/price", "op": "lt", "value": 100}]
                },
            },
            {
                "id": "notify",
                "type": "webhook",
                "input": "check",
                "config_schema_version": 1,
                "config": {"url": "https://example.com/hook", "secret": None},
            },
        ],
    }
    parsed, _ = parse_definition(stored)
    _, check_block, notify = parsed.blocks

    assert check_block.config.when_unmet == "fail"
    assert check_block.config.on_stop_outputs is None
    assert check_block.config.rules[0].negate is False
    assert notify.config.events == ["pipeline.result"]


def test_a_saved_definition_round_trips_unchanged():
    first = validate(watch([BELOW], {"max_price": {"type": "number"}}, when_unmet="stop"))
    again = validate(copy.deepcopy(first.definition))

    assert again.definition == first.definition


def test_stop_without_on_stop_outputs_is_saved_as_keep():
    result = validate(watch([BELOW], {"max_price": {"type": "number"}}, when_unmet="stop"))

    assert result.definition["blocks"][1]["config"]["on_stop_outputs"] == "keep"


@pytest.mark.parametrize("outputs", ["keep", "delete"])
def test_on_stop_outputs_is_refused_with_fail(outputs):
    found = one_problem(watch([{"kind": "present", "path": "/p"}], {}, on_stop_outputs=outputs))

    assert found.startswith("block 'check': config")
    assert "on_stop_outputs applies only when when_unmet is 'stop'" in found


def test_stop_on_the_last_block_is_refused():
    definition = {
        "blocks": [json_scrape(), guard([{"kind": "present", "path": "/p"}], when_unmet="stop")]
    }

    assert one_problem(definition) == (
        "block 'check': when_unmet 'stop' on the last block skips nothing; use 'fail', "
        "or put it before the blocks it guards"
    )


def test_fail_on_the_last_block_is_still_allowed():
    validate({"blocks": [json_scrape(), guard([{"kind": "present", "path": "/p"}])]})


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"when_unmet": "skip"}, "when_unmet"),
        ({"when_unmet": "stop", "on_stop_outputs": "archive"}, "on_stop_outputs"),
    ],
)
def test_unknown_stop_settings_are_refused(config, message):
    found = one_problem(watch([{"kind": "present", "path": "/p"}], {}, **config))

    assert found.startswith("block 'check': config") and message in found


def test_negate_is_accepted_on_every_rule_kind():
    rules = [
        {"kind": "json_schema", "schema": {"type": "object"}, "negate": True},
        {"kind": "present", "path": "/p", "negate": True},
        {"kind": "type", "path": "/p", "type": "number", "negate": True},
        {"kind": "compare", "path": "/p", "op": "lt", "value": 1, "negate": True},
    ]
    result = validate(watch(rules, {}))
    page_rules = [
        {"kind": "contains", "text": "x", "negate": True},
        {"kind": "min_length", "chars": 5, "negate": True},
    ]
    page = validate({"blocks": [scrape(url="https://example.com"), guard(page_rules)]})

    assert all(r["negate"] for r in result.definition["blocks"][1]["config"]["rules"])
    assert all(r["negate"] for r in page.definition["blocks"][1]["config"]["rules"])


def test_negate_must_be_a_boolean():
    found = one_problem(watch([{"kind": "present", "path": "/p", "negate": "yes"}], {}))

    assert "negate" in found


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([], "at least 1"),
        (["pipeline.result", "pipeline.result"], "each event may be listed once"),
        (["job.failed"], "events"),
    ],
)
def test_webhook_events_are_checked(events, message):
    definition = r6()
    definition["blocks"][2]["config"]["events"] = events
    found = one_problem(definition)

    assert found.startswith("block 'notify': config") and message in found


def test_webhook_events_take_both_names():
    definition = r6()
    definition["blocks"][2]["config"]["events"] = ["pipeline.failed", "pipeline.result"]

    assert validate(definition).definition["blocks"][2]["config"]["events"] == [
        "pipeline.failed",
        "pipeline.result",
    ]


@pytest.mark.parametrize(
    ("rule", "declared"),
    [
        (BELOW, "number"),
        ({**BELOW, "op": "ge"}, "number"),
        ({**BELOW, "op": "eq"}, "number"),
        ({**BELOW, "op": "ne"}, "text"),
    ],
)
def test_a_compare_value_binds_an_input_of_a_suitable_type(rule, declared):
    validate(watch([rule], {"max_price": {"type": declared}}))


@pytest.mark.parametrize(
    ("rule", "declared", "needs"),
    [
        (BELOW, "text", "number"),
        (BELOW, "url", "number"),
        ({**BELOW, "op": "eq"}, "url", "number or text"),
    ],
)
def test_a_compare_value_refuses_an_input_of_the_wrong_type(rule, declared, needs):
    found = one_problem(watch([rule], {"max_price": {"type": declared}}))

    assert found == (
        f"block 'check': config.rules[0].value takes a {needs} run input; "
        f"'max_price' is declared {declared}"
    )


def test_a_contains_text_binds_a_text_input_only():
    bound = {"kind": "contains", "text": {"$input": "phrase"}}
    page = {"blocks": [scrape(url="https://example.com"), guard([bound])]}

    validate({**page, "inputs": {"phrase": {"type": "text"}}})
    found = one_problem({**page, "inputs": {"phrase": {"type": "number"}}})
    assert found == (
        "block 'check': config.rules[0].text takes a text run input; 'phrase' is declared number"
    )


@pytest.mark.parametrize(
    ("rule", "where"),
    [
        ({"kind": "min_length", "chars": {"$input": "n"}}, "config.rules[0].chars"),
        ({"kind": "present", "path": {"$input": "n"}}, "config.rules[0].path"),
        (
            {"kind": "type", "path": "/p", "type": "number", "negate": {"$input": "n"}},
            "config.rules[0].negate",
        ),
    ],
)
def test_only_compare_value_and_contains_text_take_a_run_input(rule, where):
    found = one_problem(watch([rule], {"n": {"type": "number"}}))

    assert found == (
        f"block 'check': {where} cannot take a run input; validate binds: "
        "rules[].text, rules[].value"
    )


def test_a_value_binding_on_a_rule_without_that_field_is_refused():
    # "value" is bindable on compare only; on contains it is not a field at all.
    rule = {"kind": "contains", "text": "x", "value": {"$input": "n"}}
    page = {
        "inputs": {"n": {"type": "text"}},
        "blocks": [scrape(url="https://example.com"), guard([rule])],
    }

    assert "config.rules[0].value cannot take a run input" in one_problem(page)


def test_a_rule_binding_an_undeclared_input_is_refused():
    found = one_problem(watch([BELOW], {}))

    assert found == (
        "block 'check': config.rules[0].value uses run input 'max_price', "
        "which the pipeline does not declare"
    )


def test_a_bound_rule_counts_as_using_its_input():
    # No "declared but no block uses it" for an input only a rule reads.
    validate(watch([BELOW], {"max_price": {"type": "number"}}))


def test_a_stop_that_deletes_needs_the_release_time_too():
    from app.pipelines.catalog import RELEASE_OUTPUTS_TIMING

    rules, inputs = [BELOW], {"max_price": {"type": "number"}}
    keep = validate(watch(rules, inputs, when_unmet="stop")).blocks[1].timing
    delete = (
        validate(watch(rules, inputs, when_unmet="stop", on_stop_outputs="delete")).blocks[1].timing
    )

    assert delete.need - keep.need == RELEASE_OUTPUTS_TIMING.budget
    assert delete.budget == keep.budget  # the Validate activity itself is unchanged


def test_an_alert_only_webhook_needs_only_the_block_overhead():
    from app.pipelines.catalog import BLOCK_OVERHEAD_SECONDS, WEBHOOK, WebhookConfigV1

    url = "https://hooks.example.com/in"
    alert_only = WEBHOOK.timing(WebhookConfigV1(url=url, events=["pipeline.failed"]))
    both = WEBHOOK.timing(WebhookConfigV1(url=url, events=["pipeline.failed", "pipeline.result"]))
    default = WEBHOOK.timing(WebhookConfigV1(url=url))

    assert alert_only.need == BLOCK_OVERHEAD_SECONDS
    assert both.need == default.need == default.budget + BLOCK_OVERHEAD_SECONDS


# --- L.3: the change gate (ADR-016 §5a, §5b) ----------------------------------------------

CHANGED = {"kind": "changed"}


def gate(id="gate", input="clean", **config):
    return {
        "id": id,
        "type": "validate",
        "input": input,
        "config": {"rules": [CHANGED], "when_unmet": "stop", **config},
    }


def monitored(*gates):
    """scrape -> clean -> gates... -> extract."""
    blocks = [scrape(url="https://example.com"), clean(), *gates]
    return {"blocks": [*blocks, llm(input=blocks[-1]["id"])]}


def _outputs(definition) -> dict[str, str | None]:
    stored = validate(definition).definition
    return {
        b["id"]: b["config"].get("on_stop_outputs")
        for b in stored["blocks"]
        if b["type"] == "validate"
    }


def test_a_change_gate_saves_and_reads_any_input():
    validate(monitored(gate()))
    validate(
        {"blocks": [scrape(url="https://example.com"), gate(input="fetch"), llm(input="gate")]}
    )


def test_the_first_gate_left_out_deletes_and_a_later_one_keeps():
    definition = monitored(gate(), gate(id="again", input="gate"))

    assert _outputs(definition) == {"gate": "delete", "again": "keep"}


def test_an_explicit_choice_at_a_gate_is_kept():
    definition = monitored(
        gate(on_stop_outputs="keep"), gate(id="again", input="gate", on_stop_outputs="delete")
    )

    assert _outputs(definition) == {"gate": "keep", "again": "delete"}


def test_a_stored_gate_round_trips():
    stored = validate(monitored(gate())).definition

    assert validate(copy.deepcopy(stored)).definition == stored


def test_the_first_gate_is_the_first_validate_holding_changed():
    definition = monitored(
        check(id="guard", input="clean"), gate(input="guard"), gate(id="again", input="gate")
    )

    outputs = _outputs(definition)
    assert outputs["gate"] == "delete" and outputs["again"] == "keep"
    assert outputs["guard"] is None  # an ordinary fail-mode Validate is untouched


def test_the_first_gates_resolved_delete_reserves_the_release_time():
    from app.pipelines.catalog import RELEASE_OUTPUTS_TIMING

    deleting = validate(monitored(gate())).blocks[2].timing
    keeping = validate(monitored(gate(on_stop_outputs="keep"))).blocks[2].timing

    assert deleting.need - keeping.need == RELEASE_OUTPUTS_TIMING.budget


def test_an_ordinary_stop_still_defaults_to_keep():
    definition = monitored(
        check(id="guard", input="clean", rules=[{"kind": "min_length", "chars": 1}])
    )
    definition["blocks"][2]["config"]["when_unmet"] = "stop"

    assert _outputs(definition) == {"guard": "keep"}


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"when_unmet": "fail"}, "the changed rule needs when_unmet 'stop'"),
        (
            {"rules": [CHANGED, {"kind": "min_length", "chars": 1}]},
            "the changed rule must be the only rule of its block",
        ),
        ({"rules": [{"kind": "changed", "negate": True}]}, "the changed rule cannot take negate"),
    ],
)
def test_a_change_gate_is_alone_stops_and_is_not_reversed(config, message):
    found = one_problem(monitored(gate(**config)))

    assert found == f"block 'gate': {message}"


# --- M.6: the bucket sink, sink order, webhook destinations, the rollback reserve -----------
# PRD-017 R1, R2, R4, R6; ADR-015 §2b, §6.


def hook_to(destination_id=DEST_ID, id="notify", input="store", **config):
    return {
        "id": id,
        "type": "webhook",
        "input": input,
        "config": {"destination_id": destination_id, **config},
    }


@pytest.mark.parametrize(
    "first", [scrape(), scrape(output_format="markdown"), scrape(output_format="json")]
)
def test_a_bucket_takes_any_input_and_passes_it_on(first):
    for chain in ([first, bucket(input="fetch")], [first, llm(), bucket()]):
        result = validate(pipeline(*chain))
        store = result.blocks[-1]
        assert store.type is BUCKET
        assert store.config.destination_id.hex == DEST_ID.replace("-", "")
        assert result.definition["blocks"][-1]["config"] == {"destination_id": DEST_ID}


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, "config.destination_id: Field required"),
        ({"destination_id": "nope"}, "config.destination_id: Input should be a valid UUID"),
        ({"destination_id": DEST_ID, "prefix": "x"}, "config.prefix: Extra inputs"),
    ],
)
def test_a_bucket_needs_a_destination_and_nothing_else(config, expected):
    block = {"id": "store", "type": "bucket", "input": "extract", "config": config}
    assert one_problem(pipeline(scrape(), llm(), block)).startswith(f"block 'store': {expected}")


def test_a_webhook_takes_a_destination_instead_of_a_url():
    result = validate(pipeline(scrape(), llm(), hook_to(input="extract")))
    stored = result.definition["blocks"][-1]["config"]
    assert stored["destination_id"] == DEST_ID
    assert (stored["url"], stored["secret"]) == (None, None)
    assert validate(copy.deepcopy(result.definition)).definition == result.definition


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, "give either url (with an optional secret) or destination_id"),
        (
            {"url": "https://example.com/hook", "destination_id": DEST_ID},
            "give either url (with an optional secret) or destination_id",
        ),
        (
            {"destination_id": DEST_ID, "secret": "s"},
            "secret goes with url; a webhook destination holds its own",
        ),
    ],
)
def test_a_webhook_takes_exactly_one_target(config, expected):
    block = {"id": "notify", "type": "webhook", "input": "extract", "config": config}
    assert one_problem(pipeline(scrape(), llm(), block)) == f"block 'notify': config: {expected}"


def test_an_inline_webhook_stored_before_m6_still_parses():
    from app.pipelines.definition import parse_definition

    stored = validate(r6()).definition
    del stored["blocks"][-1]["config"]["destination_id"]
    parsed, _ = parse_definition(stored)
    assert str(parsed.blocks[-1].config.url) == "https://example.com/hook"
    assert parsed.blocks[-1].config.destination_id is None


def test_reversible_sinks_first_then_irreversible_ones():
    validate(
        pipeline(
            scrape(), llm(), bucket(), bucket(id="again", input="store"), hook_to(input="again")
        )
    )
    validate(pipeline(scrape(), llm(), bucket(), hook_to(), webhook(id="also", input="notify")))


def test_a_bucket_after_a_webhook_is_refused_naming_both():
    found = one_problem(
        pipeline(
            scrape(), llm(), bucket(), webhook(input="store"), bucket(id="late", input="notify")
        )
    )
    assert found == (
        "block 'late': bucket 'late' cannot follow webhook 'notify': if 'late' failed, "
        "'notify' could not be undone; put every bucket and postgres block before the first webhook "
        "or email"
    )


def test_every_bucket_after_the_first_webhook_is_refused():
    found = problems(
        pipeline(
            scrape(),
            llm(),
            webhook(),
            bucket(id="b1", input="notify"),
            check(id="v", input="b1", rules=[{"kind": "present", "path": "/title"}]),
            bucket(id="b2", input="v"),
        )
    )
    assert [f.split(":")[0] for f in found] == ["block 'b1'", "block 'b2'"]
    assert all("cannot follow webhook 'notify'" in f for f in found)


def test_a_bucket_after_an_alert_only_webhook_is_accepted():
    # It sends nothing on the way through, so nothing is announced before the bucket.
    alert = webhook(input="extract")
    alert["config"]["events"] = ["pipeline.failed"]
    validate(pipeline(scrape(), llm(), alert, bucket(input="notify")))


def test_the_bucket_timing_is_the_delivery_ladder_plus_its_loop_writes():
    timing = BUCKET.timing(BucketConfigV1(destination_id=DEST_ID))
    assert (timing.start_to_close, timing.schedule_to_start, timing.maximum_attempts) == (60, 60, 5)
    assert timing.budget == 5 * (60 + 60) + 30 + 300 + 3000 + 7200
    # A load before each of 5 tries; waiting + running between them (ADR-015 §6: 13 writes).
    assert timing.loop_db == 13 * DB_ACTIVITY_SECONDS
    assert timing.need == timing.budget + 13 * DB_ACTIVITY_SECONDS + BLOCK_OVERHEAD_SECONDS
    assert timing.budget < 3.1 * 3600

    undo = BUCKET_UNDO_TIMING
    assert (undo.start_to_close, undo.maximum_attempts) == (30, 5)
    assert undo.need == 5 * (60 + 30) + 10530 + 5 * DB_ACTIVITY_SECONDS + BLOCK_OVERHEAD_SECONDS


def test_the_catalog_says_which_blocks_are_sinks_and_which_can_be_undone():
    sinks = {name: t.sink for name, t in CATALOG.items() if t.sink is not None}
    assert {n: s.reversible for n, s in sinks.items()} == {
        "bucket": True,
        "postgres": True,
        "webhook": False,
        "email": False,
    }
    assert sinks["bucket"].undo is BUCKET_UNDO_TIMING
    assert sinks["postgres"].undo is POSTGRES_UNDO_TIMING
    assert sinks["webhook"].undo is None
    assert {n: t.destination_kinds for n, t in CATALOG.items() if t.destination_kinds} == {
        "bucket": ("bucket",),
        "postgres": ("postgres",),
        "webhook": ("webhook",),
        "email": ("smtp",),
        "approval": ("webhook", "smtp"),
    }


def _forward(definition) -> int:
    return sum(b.timing.need for b in validate(definition).blocks)


def test_prd_017s_worked_example_fits_with_its_reserve():
    # scrape -> extract -> bucket -> bucket -> webhook (the second sink stands in for slice 2's db).
    worked = pipeline(
        scrape(), llm(), bucket(), bucket(id="again", input="store"), hook_to(input="again")
    )
    result = validate(worked)
    forward = _forward(worked)
    assert result.run_budget_seconds == forward
    assert 9.5 * 3600 < forward < 11 * 3600
    reserve = 2 * BUCKET_UNDO_TIMING.need
    assert 6 * 3600 < reserve < 6.5 * 3600
    assert forward + reserve <= settings.pipeline_max_run_seconds


def test_the_reserve_is_refused_with_the_hours_message(monkeypatch):
    definition = pipeline(
        scrape(),
        bucket(input="fetch"),
        bucket(id="b2", input="store"),
        bucket(id="b3", input="b2"),
        hook_to(input="b3"),
        hook_to(id="again", input="notify"),
    )
    with monkeypatch.context() as m:
        m.setattr(settings, "pipeline_max_run_seconds", 10**6)
        forward = _forward(definition)
    reserve = 3 * BUCKET_UNDO_TIMING.need

    found = one_problem(definition)

    hours = lambda s: f"{s / 3600:.1f}".removesuffix(".0") + " h"  # noqa: E731
    assert forward <= settings.pipeline_max_run_seconds < forward + reserve
    assert found.startswith(
        f"blocks need {hours(forward + reserve)} ({hours(forward)} of blocks + "
        f"{hours(reserve)} to undo 3 deliveries); the limit is 24 h — "
    )
    assert found.endswith(f" + {reserve} s against 86400 s")


def test_a_declared_budget_leaves_room_for_the_reserve(monkeypatch):
    definition = pipeline(scrape(), llm(), bucket(), hook_to())
    forward = _forward(definition)
    reserve = BUCKET_UNDO_TIMING.need
    monkeypatch.setattr(settings, "pipeline_max_run_seconds", forward + reserve)

    assert validate({**definition, "time_budget_seconds": forward}).run_budget_seconds == forward
    found = one_problem({**definition, "time_budget_seconds": forward + 1})
    assert found == (
        f"time_budget_seconds {forward + 1} plus {reserve} s to undo 1 delivery is over the "
        f"limit of {forward + reserve} s"
    )


# --- O.1: the Postgres sink (PRD-017 slice 2; ADR-015 §5-§7) --------------------------------


def db(id="db", input="extract", destination_id=DEST_ID):
    return {
        "id": id,
        "type": "postgres",
        "input": input,
        "config": {"destination_id": destination_id},
    }


@pytest.mark.parametrize(
    "chain", [[scrape(output_format="json"), db(input="fetch")], [scrape(), llm(), db()]]
)
def test_a_postgres_block_takes_json_and_passes_it_on(chain):
    result = validate(pipeline(*chain))
    block = result.blocks[-1]
    assert block.type is POSTGRES
    assert result.definition["blocks"][-1]["config"] == {"destination_id": DEST_ID}


@pytest.mark.parametrize("output_format", ["html", "markdown"])
def test_a_postgres_block_after_a_page_is_refused(output_format):
    found = one_problem(pipeline(scrape(output_format=output_format), db(input="fetch")))
    assert found.startswith(
        "block 'db': postgres consumes extraction, page:json; 'fetch' produces page:"
    )


def test_a_postgres_block_needs_a_destination_and_nothing_else():
    block = db()
    block["config"]["table"] = "rows"
    assert one_problem(pipeline(scrape(), llm(), block)).startswith(
        "block 'db': config.table: Extra inputs"
    )


def test_a_postgres_block_after_a_webhook_is_refused_naming_both():
    found = one_problem(pipeline(scrape(), llm(), webhook(), db(input="notify")))
    assert found.startswith("block 'db': postgres 'db' cannot follow webhook 'notify'")


def test_the_postgres_timing_is_30_s_tries_on_the_delivery_ladder_plus_its_loop_writes():
    timing = POSTGRES.timing(POSTGRES.config_versions[1](destination_id=DEST_ID))
    assert (timing.start_to_close, timing.maximum_attempts) == (30, 5)
    assert timing.budget == 5 * (60 + 30) + 30 + 300 + 3000 + 7200
    assert timing.loop_db == 13 * DB_ACTIVITY_SECONDS
    assert timing.budget < 3.1 * 3600
    undo = POSTGRES_UNDO_TIMING
    assert undo.need == 5 * (60 + 30) + 10530 + 5 * DB_ACTIVITY_SECONDS + BLOCK_OVERHEAD_SECONDS


def test_prd_017s_worked_example_with_a_postgres_sink():
    # scrape -> extract -> bucket -> db -> webhook: ≈ 10 h forward + ≈ 6 h reserve (R6).
    worked = pipeline(scrape(), llm(), bucket(), db(input="store"), hook_to(input="db"))
    forward = _forward(worked)
    assert 9.5 * 3600 < forward < 11 * 3600
    reserve = BUCKET_UNDO_TIMING.need + POSTGRES_UNDO_TIMING.need
    assert 6 * 3600 < reserve < 6.5 * 3600
    assert validator.rollback_reserve(validate(worked).blocks) == (reserve, 2)


# --- P.1: the Email block (PRD-017 slice 3; ADR-015 §5-§7) ----------------------------------


def mail(id="mail", input="extract", destination_id=DEST_ID, **config):
    return {
        "id": id,
        "type": "email",
        "input": input,
        "config": {"destination_id": destination_id, **config},
    }


@pytest.mark.parametrize("output_format", ["html", "markdown", "json"])
def test_an_email_block_takes_any_page_and_sends_the_result_by_default(output_format):
    result = validate(pipeline(scrape(output_format=output_format), mail(input="fetch")))
    block = result.blocks[-1]
    assert block.type is EMAIL
    assert result.definition["blocks"][-1]["config"] == {
        "destination_id": DEST_ID,
        "events": ["pipeline.result"],
    }


def test_an_email_block_after_an_extraction_and_another_sink_is_accepted():
    validate(pipeline(scrape(), llm(), bucket(), hook_to(), mail(input="notify")))
    validate(pipeline(scrape(), llm(), bucket(), mail(input="store"), webhook(input="mail")))


def test_an_email_block_needs_a_destination():
    block = mail()
    del block["config"]["destination_id"]
    assert one_problem(pipeline(scrape(), llm(), block)) == (
        "block 'mail': config.destination_id: Field required"
    )


@pytest.mark.parametrize(
    "events, expected",
    [
        ([], "config.events: List should have at least 1 item after validation, not 0"),
        (
            ["pipeline.result", "pipeline.result"],
            "config.events: each event may be listed once",
        ),
    ],
)
def test_an_email_blocks_events_are_checked_as_a_webhooks(events, expected):
    found = one_problem(pipeline(scrape(), llm(), mail(events=events)))
    assert found == f"block 'mail': {expected}"


def test_a_bucket_after_an_email_is_refused_naming_both():
    found = one_problem(pipeline(scrape(), llm(), mail(), bucket(input="mail")))
    assert found == (
        "block 'store': bucket 'store' cannot follow email 'mail': if 'store' failed, 'mail' "
        "could not be undone; put every bucket and postgres block before the first webhook or "
        "email"
    )


def test_a_postgres_block_after_an_email_is_refused_naming_both():
    found = one_problem(pipeline(scrape(), llm(), mail(), db(input="mail")))
    assert found.startswith("block 'db': postgres 'db' cannot follow email 'mail'")


def test_a_bucket_after_an_alert_only_email_is_accepted():
    # It sends nothing on the way through, so nothing is announced before the bucket.
    validate(pipeline(scrape(), llm(), mail(events=["pipeline.failed"]), bucket(input="mail")))


def test_the_email_timing_is_60_s_tries_on_the_delivery_ladder_plus_its_loop_writes():
    timing = EMAIL.timing(EMAIL.config_versions[1](destination_id=DEST_ID))
    assert (timing.start_to_close, timing.schedule_to_start, timing.maximum_attempts) == (60, 60, 5)
    assert timing.budget == 5 * (60 + 60) + 30 + 300 + 3000 + 7200
    assert timing.loop_db == 13 * DB_ACTIVITY_SECONDS
    assert timing.budget < 3.1 * 3600
    assert EMAIL.sink is not None and not EMAIL.sink.reversible and EMAIL.sink.undo is None
    alert = EMAIL.timing(
        EMAIL.config_versions[1](destination_id=DEST_ID, events=["pipeline.failed"])
    )
    assert not alert.calls_activity
    assert alert.need == BLOCK_OVERHEAD_SECONDS


def test_an_email_reserves_no_undo():
    assert validator.rollback_reserve(validate(pipeline(scrape(), llm(), mail())).blocks) == (0, 0)
