import json

import pytest
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter

from app.models.job import OutputFormat
from app.workflows.activities.contracts import (
    Credentials,
    LLMInput,
    LLMOutput,
    ScrapeInput,
    ScrapeOptions,
    ScrapeOutput,
    StoredObject,
)

converter = pydantic_data_converter.payload_converter


def _full_input() -> ScrapeInput:
    return ScrapeInput(
        artifact_id="7f1c2a9e-0000-4000-8000-000000000001",
        url="https://example.com",
        output_format="markdown",
        credentials=Credentials(
            encrypted_proxy_url="gAAAA-proxy", encrypted_cookies="gAAAA-cookies"
        ),
        options=ScrapeOptions(respect_robots=True, actions=[{"type": "click", "selector": "#go"}]),
        playwright_options={"wait_strategy": "networkidle", "timeout_seconds": 45},
    )


def test_scrape_input_round_trips_through_the_converter():
    original = _full_input()

    [payload] = converter.to_payloads([original])
    [decoded] = converter.from_payloads([payload], [ScrapeInput])

    assert decoded == original


def test_scrape_input_payload_is_plain_json():
    # The Go worker decodes with the Go SDK's default converter, which reads json/plain.
    [payload] = converter.to_payloads([_full_input()])

    assert payload.metadata["encoding"] == b"json/plain"
    assert json.loads(payload.data)["artifact_id"] == "7f1c2a9e-0000-4000-8000-000000000001"


def test_empty_artifact_id_is_rejected_on_decode():
    # The receiving side validates too — a payload built outside the model cannot slip "" through.
    [payload] = converter.to_payloads([_full_input()])
    body = json.loads(payload.data)
    body["artifact_id"] = ""
    payload.data = json.dumps(body).encode()

    with pytest.raises(ValidationError):
        converter.from_payloads([payload], [ScrapeInput])


def test_output_format_enum_from_the_job_row_becomes_a_plain_string():
    # jobs.output_format loads as an OutputFormat member; the contract must carry its value.
    scrape_input = ScrapeInput(
        artifact_id="a", url="https://example.com", output_format=OutputFormat.json
    )

    assert not isinstance(scrape_input.output_format, OutputFormat)
    assert scrape_input.output_format == "json"


def test_unknown_output_format_is_rejected():
    with pytest.raises(ValidationError):
        ScrapeInput(artifact_id="a", url="https://example.com", output_format="pdf")


def _full_output() -> ScrapeOutput:
    return ScrapeOutput(
        result=StoredObject(path="scrapeflow-results/history/a/scrape.md", size=5120),
        content_hash="00c0ffee12345678",
        warnings=["action 2 timed out"],
        screenshots=[StoredObject(path="scrapeflow-results/screenshots/a/0.png", size=90000)],
    )


def test_scrape_output_round_trips_through_the_converter():
    original = _full_output()

    [payload] = converter.to_payloads([original])
    [decoded] = converter.from_payloads([payload], [ScrapeOutput])

    assert decoded == original


def test_scrape_output_defaults_to_no_warnings_and_no_screenshots():
    output = ScrapeOutput(result=StoredObject(path="b/k", size=0), content_hash="0123456789abcdef")

    assert output.warnings == []
    assert output.screenshots == []


@pytest.mark.parametrize("content_hash", ["c0ffee12345678", "00C0FFEE12345678", ""])
def test_malformed_content_hash_is_rejected_on_decode(content_hash):
    [payload] = converter.to_payloads([_full_output()])
    body = json.loads(payload.data)
    body["content_hash"] = content_hash
    payload.data = json.dumps(body).encode()

    with pytest.raises(ValidationError):
        converter.from_payloads([payload], [ScrapeOutput])


@pytest.mark.parametrize("stored", [{"path": "", "size": 1}, {"path": "b/k", "size": -1}])
def test_invalid_stored_object_is_rejected(stored):
    with pytest.raises(ValidationError):
        StoredObject(**stored)


def _llm_input() -> LLMInput:
    return LLMInput(
        artifact_id="7f1c2a9e-0000-4000-8000-000000000001",
        raw_minio_path="scrapeflow-results/history/a/scrape.md",
        provider="openai_compatible",
        encrypted_api_key="gAAAA-key",
        base_url="https://llm.example.com/v1",
        model="qwen",
        output_schema={"type": "object", "properties": {"title": {"type": "string"}}},
    )


def test_llm_input_round_trips_through_the_converter():
    original = _llm_input()

    [payload] = converter.to_payloads([original])
    [decoded] = converter.from_payloads([payload], [LLMInput])

    assert decoded == original


def test_unknown_llm_provider_is_rejected():
    with pytest.raises(ValidationError):
        LLMInput(**{**_llm_input().model_dump(), "provider": "gemini"})


def test_llm_output_round_trips_through_the_converter():
    original = LLMOutput(
        result=StoredObject(path="scrapeflow-results/history/a/llm.json", size=312)
    )

    [payload] = converter.to_payloads([original])
    [decoded] = converter.from_payloads([payload], [LLMOutput])

    assert decoded == original
