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
delivery = _load("activity_contract_delivery", "delivery-worker/worker/contracts.py")
api_destinations = _load("activity_contract_api_destinations", "api/app/schemas/destinations.py")

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
    {"kind": "changed"},
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
    dumped = [r.model_dump(by_alias=True, exclude_defaults=True) for r in received.rules]
    assert dumped == VALIDATE_RULES
    # A bool stays a bool through the union: `eq true` must not become `eq 1`.
    assert received.rules[4].value is True


def test_validate_input_defaults_to_fail_mode_and_unnegated_rules():
    received = _decode(_encode(VALIDATE_INPUT), llm.ValidateInput)

    assert received.when_unmet == "fail"
    assert not any(r.negate for r in received.rules)


def test_llm_worker_reads_stop_mode_and_negate_on_every_rule_kind():
    rules = [{**r, "negate": True} for r in VALIDATE_RULES]
    sent = api.ValidateInput(input_path="b/k", rules=rules, when_unmet="stop")
    received = _decode(_encode(sent), llm.ValidateInput)

    assert received.when_unmet == "stop"
    assert all(r.negate for r in received.rules)
    assert [r.model_dump(by_alias=True) for r in received.rules] == rules


def test_a_validate_input_recorded_before_stop_mode_still_decodes():
    # The shape every pre-J.1 history holds.
    data = json.dumps({"input_path": "b/k", "rules": [{"kind": "present", "path": "/p"}]})
    received = _decode(data.encode(), llm.ValidateInput)

    assert received.when_unmet == "fail"
    assert received.rules[0].negate is False


def test_llm_worker_reads_the_known_fingerprints():
    known = {"baseline": "a" * 64, "waiting": "b" * 64}
    sent = api.ValidateInput(
        input_path="b/k", rules=[{"kind": "changed"}], when_unmet="stop", known_fingerprints=known
    )
    received = _decode(_encode(sent), llm.ValidateInput)

    assert received.known_fingerprints == known
    assert _decode(_encode(VALIDATE_INPUT), llm.ValidateInput).known_fingerprints == {}


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


@pytest.mark.parametrize(
    "verdict",
    [
        llm.ValidateVerdict(outcome="met"),
        llm.ValidateVerdict(
            outcome="not_met",
            rule_index=1,
            rule={"kind": "compare", "path": "/price", "op": "lt", "value": 100, "negate": False},
            found=120,
            expected=100,
        ),
        llm.ValidateVerdict(
            outcome="not_met",
            rule_index=0,
            rule={"kind": "json_schema", "schema": {"type": "object"}, "negate": True},
            found={"nested": [1, None, True]},
        ),
        llm.ValidateVerdict(outcome="met", fingerprint="c" * 64),
        llm.ValidateVerdict(
            outcome="not_met",
            rule_index=0,
            rule={"kind": "changed", "negate": False},
            fingerprint="c" * 64,
            matched="baseline",
        ),
    ],
)
def test_api_parses_the_validate_verdict(verdict):
    received = _decode(_encode(verdict), api.ValidateVerdict)

    assert received.model_dump() == verdict.model_dump()


def test_a_verdict_recorded_before_the_changed_rule_still_decodes():
    data = json.dumps({"outcome": "met", "rule_index": None, "rule": None})
    received = _decode(data.encode(), api.ValidateVerdict)

    assert (received.fingerprint, received.matched) == (None, None)


def test_llm_worker_parses_the_diff_and_capture_inputs():
    diff = api.DiffInput(
        artifact_id=ARTIFACT_ID,
        kind="extraction",
        baseline_path="scrapeflow-results/history/b/llm.json",
        current_path=f"scrapeflow-results/history/{ARTIFACT_ID}/llm.json",
    )
    assert _decode(_encode(diff), llm.DiffInput).model_dump() == diff.model_dump()
    capture = api.CaptureInput(
        input_path="b/k",
        values=[
            api.CarriedValue(input="last_price", path="/price", type="number"),
            api.CarriedValue(input="title", path="/title", type="text"),
        ],
    )
    assert _decode(_encode(capture), llm.CaptureInput).model_dump() == capture.model_dump()


@pytest.mark.parametrize(
    "output",
    [
        llm.DiffOutput(
            summary=llm.DiffSummary(
                status="ok",
                kind="extraction",
                changes=[
                    llm.PathChange(path="/price", change="changed", old=10, new=8.5),
                    llm.PathChange(path="/tags/2", change="added", new={"a": [True, None]}),
                ],
                paths_changed=2,
            ),
            diff=llm.StoredObject(path=f"scrapeflow-results/history/{ARTIFACT_ID}/diff.json", size=90),
        ),
        llm.DiffOutput(
            summary=llm.DiffSummary(
                status="ok", kind="page", lines=["@@ -1 +1 @@", "-a", "+b"], lines_added=1,
                lines_removed=1, truncated=True,
            ),
            diff=llm.StoredObject(path="b/k", size=1),
        ),
        llm.DiffOutput(summary=llm.DiffSummary(status="baseline_gone", kind="page")),
        llm.DiffOutput(
            summary=llm.DiffSummary(status="unavailable", kind="page", reason="too large")
        ),
    ],
)
def test_api_parses_the_diff_output(output):
    assert _decode(_encode(output), api.DiffOutput).model_dump() == output.model_dump()


def test_api_parses_the_capture_output_keeping_number_and_text_apart():
    output = llm.CaptureOutput(
        values={
            "n": llm.CapturedValue(value=3),
            "f": llm.CapturedValue(value=39.5),
            "t": llm.CapturedValue(value="3"),
            "m": llm.CapturedValue(missing="'/p' is boolean, not a number"),
        }
    )
    received = _decode(_encode(output), api.CaptureOutput)

    assert received.model_dump() == output.model_dump()
    assert type(received.values["n"].value) is int
    assert type(received.values["t"].value) is str


def test_the_content_queue_registers_diff_and_capture_under_the_api_s_names():
    assert (llm.DIFF_ACTIVITY, llm.CAPTURE_ACTIVITY) == (api.DIFF_ACTIVITY, api.CAPTURE_ACTIVITY)


def test_api_parses_fail_mode_s_none():
    # Every pre-J.1 history recorded Validate's result as null.
    [payload] = converter.to_payloads([None])
    [value] = converter.from_payloads([payload], [api.ValidateVerdict | None])
    assert value is None


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
        (api.DiffInput, llm.DiffInput),
        (api.CaptureInput, llm.CaptureInput),
        (api.CarriedValue, llm.CarriedValue),
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
        (llm.ValidateVerdict, api.ValidateVerdict),
        (llm.DiffOutput, api.DiffOutput),
        (llm.DiffSummary, api.DiffSummary),
        (llm.PathChange, api.PathChange),
        (llm.CaptureOutput, api.CaptureOutput),
        (llm.CapturedValue, api.CapturedValue),
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


# ---------------------------------------------------------------------------
# Delivery queue (ADR-015 §4, §11)
# ---------------------------------------------------------------------------

# One of each kind, as the API stores it (schemas.destinations.stored).
STORED_DESTINATIONS = {
    "bucket": (
        api_destinations.BucketAddress(
            endpoint="https://s3.example.com", bucket="results", prefix="/runs/", region="eu-west-1"
        ),
        api_destinations.BucketSecret(access_key="AKIA", secret_key="s3cr3t"),
    ),
    "postgres": (
        api_destinations.PostgresAddress(
            host="DB.Example.com.", database="d", schema="sf", table="rows"
        ),
        api_destinations.LoginSecret(user="u", password="p"),
    ),
    "smtp": (
        api_destinations.SmtpAddress(
            host="mail.example.com",
            port=465,
            tls_mode="implicit",
            **{"from": "alerts@example.com"},
            to=["a@example.com", "b@example.com"],
            ca_certificate="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
        ),
        api_destinations.LoginSecret(user="u", password="p"),
    ),
    "webhook": (
        api_destinations.WebhookAddress(),
        api_destinations.WebhookSecret(url="https://hooks.example.com/in?t=1"),
    ),
}


@pytest.mark.parametrize("kind", sorted(STORED_DESTINATIONS))
def test_the_delivery_worker_parses_every_stored_destination_shape(kind):
    address, secret = STORED_DESTINATIONS[kind]
    stored_address = api_destinations.stored(address)
    stored_secret = api_destinations.stored(secret)
    address_model, secret_model = delivery.KIND_MODELS[kind]

    parsed_address = address_model.model_validate(stored_address)
    parsed_secret = secret_model.model_validate(stored_secret)

    # Read back by alias, the worker's model says what the API stored.
    assert parsed_address.model_dump(mode="json", by_alias=True) == stored_address
    assert parsed_secret.model_dump(mode="json", by_alias=True) == stored_secret


def test_both_sides_know_the_same_destination_kinds():
    assert set(delivery.KIND_MODELS) == set(api_destinations.KIND_MODELS)


@pytest.mark.parametrize("kind", sorted(STORED_DESTINATIONS))
def test_the_destination_test_input_reaches_the_delivery_worker_whole(kind):
    address, _ = STORED_DESTINATIONS[kind]
    sent = api.DestinationTestInput(
        kind=kind, address=api_destinations.stored(address), encrypted_secret="gAAAAAB_secret"
    )
    received = _decode(_encode(sent), delivery.DestinationTestInput)

    assert received.model_dump() == sent.model_dump()


def test_the_api_parses_the_destination_test_output():
    out = delivery.DestinationTestOutput(
        checks=[
            delivery.CheckResult(check="address", outcome="passed"),
            delivery.CheckResult(check="tls", outcome="failed", message="certificate not valid for h"),
        ]
    )
    assert _decode(_encode(out), api.DestinationTestOutput).model_dump() == out.model_dump()


@pytest.mark.parametrize(
    ("producer", "consumer"),
    [
        (api.DestinationTestInput, delivery.DestinationTestInput),
        (delivery.DestinationTestOutput, api.DestinationTestOutput),
        (delivery.CheckResult, api.CheckResult),
        (api.DeliverBucketInput, delivery.DeliverBucketInput),
        (delivery.DeliverBucketOutput, api.DeliverBucketOutput),
        (api.BucketLocation, delivery.BucketLocation),
        (api.UndoBucketInput, delivery.UndoBucketInput),
        (delivery.UndoBucketOutput, api.UndoBucketOutput),
        (api.SendEmailInput, delivery.SendEmailInput),
        (delivery.SendEmailOutput, api.SendEmailOutput),
        (api.DeliverPostgresInput, delivery.DeliverPostgresInput),
        (delivery.DeliverPostgresOutput, api.DeliverPostgresOutput),
        (api.PostgresLocation, delivery.PostgresLocation),
        (api.UndoPostgresInput, delivery.UndoPostgresInput),
        (delivery.UndoPostgresOutput, api.UndoPostgresOutput),
    ],
)
def test_the_delivery_queue_carries_every_field_both_ways(producer, consumer):
    assert set(producer.model_fields) == set(consumer.model_fields)


def test_the_delivery_worker_registers_the_names_and_queue_the_api_calls():
    assert delivery.DELIVERY_QUEUE == api_queues.DELIVERY_QUEUE
    assert delivery.TEST_WEBHOOK_ACTIVITY == api.TEST_WEBHOOK_ACTIVITY
    assert delivery.TEST_BUCKET_ACTIVITY == api.TEST_BUCKET_ACTIVITY
    assert delivery.DELIVER_BUCKET_ACTIVITY == api.DELIVER_BUCKET_ACTIVITY
    assert delivery.UNDO_BUCKET_ACTIVITY == api.UNDO_BUCKET_ACTIVITY
    assert delivery.TEST_SMTP_ACTIVITY == api.TEST_SMTP_ACTIVITY
    assert delivery.TEST_POSTGRES_ACTIVITY == api.TEST_POSTGRES_ACTIVITY
    assert delivery.SEND_EMAIL_ACTIVITY == api.SEND_EMAIL_ACTIVITY
    assert delivery.DELIVER_POSTGRES_ACTIVITY == api.DELIVER_POSTGRES_ACTIVITY
    assert delivery.UNDO_POSTGRES_ACTIVITY == api.UNDO_POSTGRES_ACTIVITY


def test_both_sides_agree_on_the_bucket_attempt_budgets():
    assert delivery.BUCKET_DELIVER_ATTEMPT_SECONDS == api.BUCKET_DELIVER_ATTEMPT_SECONDS
    assert delivery.BUCKET_UNDO_ATTEMPT_SECONDS == api.BUCKET_UNDO_ATTEMPT_SECONDS
    assert delivery.EMAIL_SEND_ATTEMPT_SECONDS == api.EMAIL_SEND_ATTEMPT_SECONDS
    assert delivery.POSTGRES_DELIVER_ATTEMPT_SECONDS == api.POSTGRES_DELIVER_ATTEMPT_SECONDS
    assert delivery.POSTGRES_UNDO_ATTEMPT_SECONDS == api.POSTGRES_UNDO_ATTEMPT_SECONDS


def test_both_sides_agree_on_the_not_written_detail():
    # A mismatch is silent: every failed try reads as maybe written and is undone.
    assert delivery.NOT_WRITTEN == api.NOT_WRITTEN


@pytest.mark.parametrize("prefix", ["", "runs", "/runs/", "a/b/"])
def test_the_workflow_derives_the_key_the_worker_writes(prefix):
    # A lost reply's key is rebuilt in the workflow to undo it (ADR-015 §5).
    args = (prefix, "4c1f0e7a", "9d2e1b3c", "store", "json")
    assert api.object_key(*args) == delivery.object_key(*args)


BUCKET_LOCATION = {
    "endpoint": "https://s3.example.com/",
    "bucket": "results",
    "region": "eu-west-1",
    "key": "runs/4c1f0e7a/9d2e1b3c/store.json",
}


@pytest.mark.parametrize("ext", ["html", "md", "json"])
def test_the_bucket_delivery_input_reaches_the_delivery_worker_whole(ext):
    address, _ = STORED_DESTINATIONS["bucket"]
    sent = api.DeliverBucketInput(
        address=api_destinations.stored(address),
        encrypted_secret="gAAAAAB_secret",
        source_path="scrapeflow-results/history/3f8b7a12/scrape.html",
        pipeline_id="4c1f0e7a-0000-4000-8000-000000000001",
        run_id="9d2e1b3c-0000-4000-8000-000000000002",
        block_id="store",
        ext=ext,
    )
    received = _decode(_encode(sent), delivery.DeliverBucketInput)

    assert received.model_dump() == sent.model_dump()
    # The stored address goes through the worker's own parser as it is.
    assert delivery.KIND_MODELS["bucket"][0].model_validate(received.address).prefix == "runs"


def test_the_api_parses_the_bucket_delivery_output():
    out = delivery.DeliverBucketOutput(
        location=delivery.BucketLocation(**BUCKET_LOCATION), size=4_300_000
    )
    assert _decode(_encode(out), api.DeliverBucketOutput).model_dump() == out.model_dump()


def test_a_recorded_location_reaches_the_undo_whole():
    # The workflow hands the undo the location the delivery returned (ADR-014 §7).
    recorded = _decode(
        _encode(delivery.DeliverBucketOutput(location=delivery.BucketLocation(**BUCKET_LOCATION), size=1)),
        api.DeliverBucketOutput,
    ).location
    sent = api.UndoBucketInput(location=recorded, encrypted_secret="gAAAAAB_secret")

    received = _decode(_encode(sent), delivery.UndoBucketInput)

    assert received.location.model_dump() == BUCKET_LOCATION


@pytest.mark.parametrize("already_gone", [False, True])
def test_the_api_parses_the_undo_output(already_gone):
    out = delivery.UndoBucketOutput(already_gone=already_gone)
    assert _decode(_encode(out), api.UndoBucketOutput).already_gone is already_gone


RUN_ID = "9d2e1b3c-0000-4000-8000-000000000002"


def test_the_postgres_delivery_input_reaches_the_delivery_worker_whole():
    address, _ = STORED_DESTINATIONS["postgres"]
    sent = api.DeliverPostgresInput(
        address=api_destinations.stored(address),
        encrypted_secret="gAAAAAB_secret",
        source_path="scrapeflow-results/history/3f8b7a12/llm.json",
        pipeline_id="4c1f0e7a-0000-4000-8000-000000000001",
        run_id=RUN_ID,
        block_id="db",
    )
    received = _decode(_encode(sent), delivery.DeliverPostgresInput)

    assert received.model_dump() == sent.model_dump()
    parsed = delivery.KIND_MODELS["postgres"][0].model_validate(received.address)
    assert (parsed.db_schema, parsed.table, parsed.port) == ("sf", "rows", 5432)


def test_the_workflow_derives_the_row_the_worker_writes():
    # A lost reply's row is rebuilt in the workflow to undo it (ADR-015 §5).
    stored = api_destinations.stored(STORED_DESTINATIONS["postgres"][0])
    built = api.row_location(stored, RUN_ID, "db").model_dump(mode="json", by_alias=True)
    assert built == delivery.row_location(stored, RUN_ID, "db").model_dump(
        mode="json", by_alias=True
    )
    assert built["schema"] == "sf" and built["block_id"] == "db"


def test_a_recorded_row_reaches_the_undo_whole():
    stored = api_destinations.stored(STORED_DESTINATIONS["postgres"][0])
    out = delivery.DeliverPostgresOutput(
        location=delivery.row_location(stored, RUN_ID, "db"), size=12
    )
    # The workflow records the location by alias and hands that dict to the undo.
    recorded = _decode(_encode(out), api.DeliverPostgresOutput).location.model_dump(
        mode="json", by_alias=True
    )
    sent = api.UndoPostgresInput(
        location=recorded, encrypted_secret="gAAAAAB_secret", ca_certificate="PEM"
    )

    received = _decode(_encode(sent), delivery.UndoPostgresInput)

    assert received.location.model_dump(mode="json", by_alias=True) == recorded
    assert received.ca_certificate == "PEM"


@pytest.mark.parametrize("already_gone", [False, True])
def test_the_api_parses_the_postgres_undo_output(already_gone):
    out = delivery.UndoPostgresOutput(already_gone=already_gone)
    assert _decode(_encode(out), api.UndoPostgresOutput).already_gone is already_gone


@pytest.mark.parametrize("run_id", ["0b1f6c1e-6a1d-4c39-9f0b-6f1d2b3c4d5e", None])
def test_the_email_input_reaches_the_delivery_worker_whole(run_id):
    stored = STORED_DESTINATIONS["smtp"]
    sent = api.SendEmailInput(
        address=api_destinations.stored(stored[0]),
        encrypted_secret="gAAAAAB_secret",
        message_id="<0b1f.notify.failed@scrapeflow.example.org>",
        subject="[ScrapeFlow] prices: pipeline.failed",
        body="pipeline.failed\n{}",
        run_id=run_id,
    )
    received = _decode(_encode(sent), delivery.SendEmailInput)
    assert received.model_dump() == sent.model_dump()
    address, _ = delivery.KIND_MODELS["smtp"]
    assert address.model_validate(received.address).sender == stored[0].sender


def test_an_email_blocks_input_reaches_the_delivery_worker_with_its_source():
    run_id = "0b1f6c1e-6a1d-4c39-9f0b-6f1d2b3c4d5e"
    stored = STORED_DESTINATIONS["smtp"]
    sent = api.SendEmailInput(
        address=api_destinations.stored(stored[0]),
        encrypted_secret="gAAAAAB_secret",
        message_id="<0b1f.mail@scrapeflow.example.org>",
        subject=f"[ScrapeFlow] prices: run {run_id}",
        body="pipeline.result\n",
        run_id=run_id,
        source_path="scrapeflow-results/history/0b1f/llm.json",
        recipients=["oncall@example.org"],
    )
    received = _decode(_encode(sent), delivery.SendEmailInput)
    assert received.model_dump() == sent.model_dump()
    assert received.source_path == "scrapeflow-results/history/0b1f/llm.json"
    assert received.recipients == ["oncall@example.org"]


def test_the_api_parses_the_email_output():
    out = delivery.SendEmailOutput(refused=["oncall@example.org"], deferred=["ops@example.org"])
    parsed = _decode(_encode(out), api.SendEmailOutput)
    assert (parsed.refused, parsed.deferred) == (["oncall@example.org"], ["ops@example.org"])
