"""Cross-service activity contract test: API inputs through each worker's real parser, and each
worker's outputs back through the API's models, all via the Temporal Pydantic converter.

The Go worker is covered through contracts/fixtures/activity/ — see
http-worker/internal/activity/contract_test.go. Same command and UPDATE_CONTRACT_FIXTURES switch as
test_message_contract.py; go_scrape_output.json is
regenerated from the Go side (command in that file).
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
    ],
)
def test_workers_accept_every_input_field_the_api_sends(producer, consumer):
    assert set(producer.model_fields) <= set(consumer.model_fields)


@pytest.mark.parametrize(
    ("producer", "consumer"),
    [
        (playwright.ScrapeOutput, api.ScrapeOutput),
        (playwright.StoredObject, api.StoredObject),
        (llm.LLMOutput, api.LLMOutput),
        (llm.StoredObject, api.StoredObject),
    ],
)
def test_api_accepts_every_output_field_the_workers_send(producer, consumer):
    assert set(producer.model_fields) <= set(consumer.model_fields)


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
