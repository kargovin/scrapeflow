"""Cross-service activity contract test: API inputs through each worker's real parser, and each
worker's outputs back through the API's models, all via the Temporal Pydantic converter.

The Go worker is covered through contracts/fixtures/activity/ — see
http-worker/internal/activity/contract_test.go. go_scrape_output.json is regenerated from the Go
side (command in that file).

Run it with the repo root mounted into an image that has pydantic and temporalio:

    docker run --rm -v "$PWD:/repo" -w /repo docker-api \
        /app/.venv/bin/python -m pytest contracts -q

Regenerate the Python-written fixtures after an intentional contract change:

    ... -e UPDATE_CONTRACT_FIXTURES=1 ...
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "activity"

converter = pydantic_data_converter.payload_converter


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = _load("activity_contract_api", "api/app/workflows/activities/contracts.py")
playwright = _load("activity_contract_playwright", "playwright-worker/worker/contracts.py")
llm = _load("activity_contract_llm", "llm-worker/worker/contracts.py")
api_queues = _load("activity_contract_api_queues", "api/app/workflows/queues.py")

ARTIFACT_ID = "3f8b7a12-0c44-4e7a-9a1e-1b2c3d4e5f60"


def _encode(value) -> bytes:
    [payload] = converter.to_payloads([value])
    assert payload.metadata["encoding"] == b"json/plain"
    return payload.data


def _decode(data: bytes, model):
    [value] = converter.from_payloads(
        [Payload(metadata={"encoding": b"json/plain"}, data=data)], [model]
    )
    return value


# ---------------------------------------------------------------------------
# API → workers
# ---------------------------------------------------------------------------

SCRAPE_INPUTS = {
    "scrape_input_http": api.ScrapeInput(
        artifact_id=ARTIFACT_ID,
        url="https://example.com/",
        output_format="markdown",
        credentials=api.Credentials(encrypted_proxy_url="gAAAAAB_proxy"),
        options=api.ScrapeOptions(respect_robots=True),
    ),
    # Every optional field null — the converter sends nulls, which is Go's trap.
    "scrape_input_minimal": api.ScrapeInput(
        artifact_id=ARTIFACT_ID, url="https://example.com/", output_format="html"
    ),
    "scrape_input_playwright": api.ScrapeInput(
        artifact_id=ARTIFACT_ID,
        url="https://example.com/",
        output_format="json",
        credentials=api.Credentials(
            encrypted_proxy_url="gAAAAAB_proxy", encrypted_cookies="gAAAAAB_cookies"
        ),
        options=api.ScrapeOptions(
            respect_robots=False, actions=[{"type": "wait", "milliseconds": 500}]
        ),
        playwright_options={
            "wait_strategy": "networkidle",
            "timeout_seconds": 90,
            "block_images": True,
        },
    ),
}

LLM_INPUTS = {
    "anthropic": api.LLMInput(
        artifact_id=ARTIFACT_ID,
        raw_minio_path=f"scrapeflow-results/history/{ARTIFACT_ID}/scrape.html",
        provider="anthropic",
        encrypted_api_key="gAAAAAB_key",
        model="claude-sonnet-5-5",
        output_schema={"type": "object"},
    ),
    "openai_compatible": api.LLMInput(
        artifact_id=ARTIFACT_ID,
        raw_minio_path=f"scrapeflow-results/history/{ARTIFACT_ID}/scrape.md",
        provider="openai_compatible",
        encrypted_api_key="gAAAAAB_key",
        base_url="https://llm.example.com/v1",
        model="qwen2.5-7b-instruct",
        output_schema={"type": "object", "properties": {"title": {"type": "string"}}},
    ),
}


CLEAN_INPUT = api.CleanInput(
    artifact_id=ARTIFACT_ID,
    input_path=f"scrapeflow-results/history/{ARTIFACT_ID}/scrape.html",
)

# One rule of each kind, as the workflow dumps the catalog's models (by alias) — the API test
# test_validate_block_sends_the_rules_by_alias pins that shape on the producer side.
VALIDATE_RULES = [
    {"kind": "json_schema", "schema": {"type": "object", "required": ["price"]}},
    {"kind": "present", "path": "/price"},
    {"kind": "type", "path": "/price", "type": "number"},
    {"kind": "compare", "path": "/price", "op": "gt", "value": 0},
    {"kind": "compare", "path": "/in_stock", "op": "eq", "value": True},
    {"kind": "compare", "path": "/currency", "op": "ne", "value": None},
    {"kind": "contains", "text": "Add to cart"},
    {"kind": "min_length", "chars": 500},
]
VALIDATE_INPUT = api.ValidateInput(
    input_path=f"scrapeflow-results/history/{ARTIFACT_ID}/llm.json", rules=VALIDATE_RULES
)


@pytest.mark.parametrize("name", sorted(SCRAPE_INPUTS))
def test_playwright_worker_parses_every_scrape_input(name):
    sent = SCRAPE_INPUTS[name]
    received = _decode(_encode(sent), playwright.ScrapeInput)

    assert received.artifact_id == sent.artifact_id
    assert received.url == sent.url
    assert received.output_format == sent.output_format


def test_playwright_worker_reads_the_fields_only_it_consumes():
    received = _decode(_encode(SCRAPE_INPUTS["scrape_input_playwright"]), playwright.ScrapeInput)

    assert received.credentials.encrypted_cookies == "gAAAAAB_cookies"
    assert received.options.actions == [{"type": "wait", "milliseconds": 500}]
    assert received.playwright_options.wait_strategy == "networkidle"
    assert received.playwright_options.timeout_seconds == 90
    assert received.playwright_options.block_images is True


@pytest.mark.parametrize("name", sorted(LLM_INPUTS))
def test_llm_worker_parses_every_llm_input(name):
    sent = LLM_INPUTS[name]
    received = _decode(_encode(sent), llm.LLMInput)

    assert received.model_dump() == sent.model_dump()


def test_llm_stage_writes_beside_the_scrape_it_reads():
    received = _decode(_encode(LLM_INPUTS["anthropic"]), llm.LLMInput)

    assert f"history/{received.artifact_id}/" in received.raw_minio_path


def test_llm_worker_parses_the_clean_input():
    received = _decode(_encode(CLEAN_INPUT), llm.CleanInput)

    assert received.model_dump() == CLEAN_INPUT.model_dump()


def test_llm_worker_parses_every_validate_rule_kind():
    received = _decode(_encode(VALIDATE_INPUT), llm.ValidateInput)

    assert received.input_path == VALIDATE_INPUT.input_path
    assert [r.model_dump(by_alias=True) for r in received.rules] == VALIDATE_RULES
    # A bool stays a bool through the union: `eq true` must not become `eq 1`.
    assert received.rules[4].value is True


def test_llm_worker_refuses_an_unknown_rule_kind():
    with pytest.raises(ValidationError):
        _decode(
            _encode(api.ValidateInput(input_path="b/k", rules=[{"kind": "regex", "re": "x"}])),
            llm.ValidateInput,
        )


# ---------------------------------------------------------------------------
# Workers → API
# ---------------------------------------------------------------------------


def test_api_parses_the_playwright_worker_output():
    sent = playwright.ScrapeOutput(
        result=playwright.StoredObject(
            path=f"scrapeflow-results/history/{ARTIFACT_ID}/scrape.json", size=48213
        ),
        content_hash="00c0ffee12345678",
        warnings=["action 1 (click): selector not found"],
        screenshots=[
            playwright.StoredObject(
                path=f"scrapeflow-results/screenshots/{ARTIFACT_ID}/0.png", size=90211
            )
        ],
    )
    received = _decode(_encode(sent), api.ScrapeOutput)

    assert received.model_dump() == sent.model_dump()


def test_api_parses_the_playwright_worker_output_with_defaults():
    sent = playwright.ScrapeOutput(
        result=playwright.StoredObject(path="b/k", size=0), content_hash="0123456789abcdef"
    )
    received = _decode(_encode(sent), api.ScrapeOutput)

    assert received.warnings == []
    assert received.screenshots == []


def test_api_parses_the_llm_worker_output():
    sent = llm.LLMOutput(
        result=llm.StoredObject(path=f"scrapeflow-results/history/{ARTIFACT_ID}/llm.json", size=312)
    )
    received = _decode(_encode(sent), api.LLMOutput)

    assert received.model_dump() == sent.model_dump()


def test_api_parses_the_clean_output():
    sent = llm.CleanOutput(
        result=llm.StoredObject(
            path=f"scrapeflow-results/history/{ARTIFACT_ID}/clean.md", size=2048
        )
    )
    received = _decode(_encode(sent), api.CleanOutput)

    assert received.model_dump() == sent.model_dump()


def test_api_parses_the_go_worker_output():
    # Written by http-worker/internal/activity/contract_test.go from a marshalled ScrapeOutput.
    received = _decode((FIXTURES / "go_scrape_output.json").read_bytes(), api.ScrapeOutput)

    assert received.result.path == f"scrapeflow-results/history/{ARTIFACT_ID}/scrape.md"
    assert received.content_hash == "00c0ffee12345678"
    assert received.warnings == []
    assert received.screenshots == []


def test_api_rejects_a_null_list_from_go():
    # What a nil slice without omitempty would send.
    with pytest.raises(ValidationError):
        _decode(
            b'{"result":{"path":"b/k","size":1},"content_hash":"0123456789abcdef","warnings":null}',
            api.ScrapeOutput,
        )


# ---------------------------------------------------------------------------
# The twins cannot drift
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("producer", "consumer"),
    [
        (api.ScrapeInput, playwright.ScrapeInput),
        (api.LLMInput, llm.LLMInput),
        (api.CleanInput, llm.CleanInput),
        (api.ValidateInput, llm.ValidateInput),
    ],
)
def test_workers_accept_every_input_field_the_api_sends(producer, consumer):
    assert set(producer.model_fields) <= set(consumer.model_fields)


@pytest.mark.parametrize(
    ("producer", "consumer"),
    [
        (api.DiscoverSitemapsInput, playwright.DiscoverSitemapsInput),
        (api.FetchSitemapInput, playwright.FetchSitemapInput),
        (api.ExtractLinksInput, playwright.ExtractLinksInput),
    ],
)
def test_the_crawl_queue_accepts_every_input_field_the_api_sends(producer, consumer):
    assert set(producer.model_fields) == set(consumer.model_fields)


@pytest.mark.parametrize(
    ("producer", "consumer"),
    [
        (playwright.DiscoverSitemapsOutput, api.DiscoverSitemapsOutput),
        (playwright.FetchSitemapOutput, api.FetchSitemapOutput),
        (playwright.ExtractLinksOutput, api.ExtractLinksOutput),
    ],
)
def test_the_api_accepts_every_crawl_output_field(producer, consumer):
    assert set(producer.model_fields) == set(consumer.model_fields)


def test_crawl_payloads_round_trip_between_the_two_sides():
    for value, model in [
        (
            api.FetchSitemapInput(url="https://a.example/s.xml", max_entries=5),
            playwright.FetchSitemapInput,
        ),
        (
            api.ExtractLinksInput(path="b/history/p/scrape.html", base_url="https://a.example/"),
            playwright.ExtractLinksInput,
        ),
        (
            api.DiscoverSitemapsInput(seed_url="https://a.example/"),
            playwright.DiscoverSitemapsInput,
        ),
    ]:
        assert _decode(_encode(value), model).model_dump() == value.model_dump()
    out = playwright.FetchSitemapOutput(kind="redirect", location="https://a.example/2.xml")
    assert _decode(_encode(out), api.FetchSitemapOutput).location == "https://a.example/2.xml"


def test_the_crawl_queue_registers_the_names_the_api_calls():
    assert playwright.CRAWL_QUEUE == api_queues.CRAWL_QUEUE
    assert playwright.DISCOVER_SITEMAPS_ACTIVITY == api.DISCOVER_SITEMAPS_ACTIVITY
    assert playwright.FETCH_SITEMAP_ACTIVITY == api.FETCH_SITEMAP_ACTIVITY
    assert playwright.EXTRACT_LINKS_ACTIVITY == api.EXTRACT_LINKS_ACTIVITY
    # Not the scrape queue: a sitemap fetch must never wait behind a browser slot.
    assert api_queues.CRAWL_QUEUE != api_queues.SCRAPE_PLAYWRIGHT_QUEUE


@pytest.mark.parametrize(
    ("producer", "consumer"),
    [
        (playwright.ScrapeOutput, api.ScrapeOutput),
        (playwright.StoredObject, api.StoredObject),
        (llm.LLMOutput, api.LLMOutput),
        (llm.StoredObject, api.StoredObject),
        (llm.CleanOutput, api.CleanOutput),
    ],
)
def test_api_accepts_every_output_field_the_workers_send(producer, consumer):
    assert set(producer.model_fields) <= set(consumer.model_fields)


def test_playwright_worker_registers_the_name_and_queue_the_api_calls():
    assert playwright.SCRAPE_ACTIVITY == api.SCRAPE_ACTIVITY
    assert playwright.SCRAPE_PLAYWRIGHT_QUEUE == api_queues.SCRAPE_PLAYWRIGHT_QUEUE
    # Both engines register "Scrape"; a shared queue would let either steal the other's tasks.
    assert api_queues.SCRAPE_PLAYWRIGHT_QUEUE != api_queues.SCRAPE_HTTP_QUEUE


def test_llm_worker_registers_the_name_and_queue_the_api_calls():
    # A mismatch raises nothing: the task waits on an unpolled queue until it times out.
    assert llm.LLM_EXTRACT_ACTIVITY == api.LLM_EXTRACT_ACTIVITY
    assert llm.LLM_QUEUE == api_queues.LLM_QUEUE


def test_content_activities_register_the_names_and_queue_the_api_calls():
    assert llm.CLEAN_ACTIVITY == api.CLEAN_ACTIVITY
    assert llm.VALIDATE_ACTIVITY == api.VALIDATE_ACTIVITY
    assert llm.CONTENT_QUEUE == api_queues.CONTENT_QUEUE
    # A separate queue, so a Clean never waits behind a slow LLM call.
    assert api_queues.CONTENT_QUEUE != api_queues.LLM_QUEUE


# ---------------------------------------------------------------------------
# Fixtures for the Go parser
# ---------------------------------------------------------------------------

GO_INPUTS = ["scrape_input_http", "scrape_input_minimal"]


@pytest.mark.parametrize("name", GO_INPUTS)
def test_go_fixture_matches_what_the_api_emits(name):
    path = FIXTURES / f"{name}.json"
    current = json.dumps(json.loads(_encode(SCRAPE_INPUTS[name])), indent=2, sort_keys=True) + "\n"

    if os.environ.get("UPDATE_CONTRACT_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(current)

    assert path.exists(), f"missing fixture {path}; regenerate with UPDATE_CONTRACT_FIXTURES=1"
    assert path.read_text() == current, (
        f"{path.name} is stale — regenerate with UPDATE_CONTRACT_FIXTURES=1."
    )
