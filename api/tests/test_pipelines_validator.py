"""C.3 — the block catalog and the save-time validator (PRD-016 R1, R2, R4; ADR-009 §4, §5, §8)."""

import copy

import pytest

from app.pipelines import validator
from app.pipelines.catalog import (
    BLOCK_OVERHEAD_SECONDS,
    CATALOG,
    LLM_EXTRACT,
    SCRAPE,
    WEBHOOK,
    ScrapeConfigV1,
)
from app.pipelines.validator import PipelineValidationError, validate
from app.settings import settings

KEY_ID = "7b1c1d5e-0000-4000-8000-000000000001"
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
    definition["inputs"]["url"] = {"type": "number"}
    assert one_problem(definition).startswith("inputs.url.type")


def test_scrape_url_is_the_only_bindable_field():
    assert {t.name: t.bindable_fields for t in CATALOG.values() if t.bindable_fields} == {
        "scrape": frozenset({"url"})
    }


# --- at most one Webhook (PRD-016 R2) ------------------------------------------------------


def test_second_webhook_rejected_and_names_layer_c():
    found = one_problem(pipeline(scrape(), llm(), webhook(), webhook(id="again", input="notify")))
    assert found.startswith("block 'again': a pipeline may have at most one webhook block")
    assert "layer C" in found


# --- limits --------------------------------------------------------------------------------


def test_max_blocks_per_pipeline(monkeypatch):
    monkeypatch.setattr(settings, "max_blocks_per_pipeline", 3)
    validate(r6())
    found = one_problem(pipeline(scrape(), check(), llm(input="check"), webhook()))
    assert found == "4 blocks; the limit is 3"


# --- time budgets compose (PRD-016 R4; ADR-009 §15c) ---------------------------------------


def _needed(definition) -> int:
    return sum(b.timing.budget + BLOCK_OVERHEAD_SECONDS for b in validate(definition).blocks)


def test_run_budget_defaults_to_what_the_blocks_need():
    result = validate(r6())
    assert result.run_budget_seconds == _needed(r6())


def test_run_budget_shorter_than_its_blocks_rejected():
    needed = _needed(r6())
    found = one_problem({**r6(), "time_budget_seconds": needed - 1})
    assert found.startswith(f"time_budget_seconds {needed - 1} is shorter than its blocks need")
    assert f"notify {WEBHOOK.timing(None).budget + BLOCK_OVERHEAD_SECONDS}" in found


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


def test_webhook_ladder_matches_adr_15c():
    timing = WEBHOOK.timing(None)
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
