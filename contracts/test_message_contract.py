"""The cross-service message contract test (ADR-011 §6).

Every suite in this repo was green throughout BUG-005. The API's tests proved the API
emitted what it emitted; each worker's tests proved that worker parsed well-formed
messages. **No test ever fed an API-produced message into a worker's parser**, so the
wire between the two services was severed while both sides reported healthy — and on
the Python→Go boundary the severance was silent, because Go's encoding/json turns a
JSON null into "" and returns no error.

This test closes that gap: it builds each dispatch payload through the model the API
actually publishes through, and parses it with each worker's real consumer model. The
Go parser is covered too, through the fixtures written to contracts/fixtures/ — see
http-worker/internal/worker/contract_test.go. Go is not optional here; it is the only
boundary on which the silent class exists at all.

Run it with the repo root mounted into an image that has pydantic:

    docker run --rm -v "$PWD:/repo" -w /repo docker-api \
        /app/.venv/bin/python -m pytest contracts -q

Regenerate the Go fixtures after an intentional contract change:

    ... -e UPDATE_CONTRACT_FIXTURES=1 ...
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str, relpath: str):
    """Import a module by file path.

    Each service builds from its own context with its own dependency manifest, so
    there is no package to import from. All four message modules depend on nothing
    but pydantic, which is what makes loading them side by side possible at all.
    """
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = _load("contract_api_messages", "api/app/messages.py")
coordinator = _load("contract_coordinator_messages", "coordinator/coordinator/messages.py")
playwright = _load("contract_playwright_models", "playwright-worker/worker/models.py")
llm = _load("contract_llm_models", "llm-worker/worker/models.py")


# ---------------------------------------------------------------------------
# The payloads each dispatcher builds, constructed through the producer's model
# ---------------------------------------------------------------------------

RUN_ID = "3f8b7a12-0c44-4e7a-9a1e-1b2c3d4e5f60"
JOB_PAGE_ID = "9c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f"
CRAWL_ID = "11111111-2222-3333-4444-555555555555"


def _scrape_lanes() -> dict[str, str]:
    """One payload per scrape dispatcher, keyed by lane name.

    The value is the exact JSON the dispatcher puts on the wire — these are built by
    calling to_nats_bytes() on the producer model, which is the only path the API has
    to NATS.
    """
    return {
        # routers/jobs.py, engine=http
        "job_http": api.ScrapeMessage(
            artifact_id=RUN_ID,
            run_id=RUN_ID,
            url="https://example.com/",
            output_format="markdown",
            engine="http",
            options=api.MessageOptions(respect_robots=False, actions=None),
        ).to_nats_bytes(),
        # routers/jobs.py, engine=playwright, with credentials and actions
        "job_playwright": api.ScrapeMessage(
            artifact_id=RUN_ID,
            run_id=RUN_ID,
            url="https://example.com/",
            output_format="html",
            engine="playwright",
            credentials=api.Credentials(
                encrypted_proxy_url="gAAAAAB_proxy", encrypted_cookies="gAAAAAB_cookies"
            ),
            options=api.MessageOptions(
                respect_robots=True, actions=[{"type": "wait", "milliseconds": 500}]
            ),
            playwright_options={
                "wait_strategy": "networkidle",
                "timeout_seconds": 90,
                "block_images": True,
            },
        ).to_nats_bytes(),
        # core/scheduler.py — both the cron dispatch and stale-pending recovery
        "scheduled": api.ScrapeMessage(
            artifact_id=RUN_ID,
            run_id=RUN_ID,
            url="https://example.com/",
            output_format="json",
            engine="http",
            credentials=api.Credentials(encrypted_proxy_url="gAAAAAB_proxy"),
            options=api.MessageOptions(respect_robots=True, actions=None),
        ).to_nats_bytes(),
        # routers/batch.py — the lane BUG-005 broke on all three execution paths
        "batch": api.ScrapeMessage(
            artifact_id=RUN_ID,
            run_id=RUN_ID,
            url="https://example.com/a",
            output_format="markdown",
            engine="http",
            options=api.MessageOptions(respect_robots=False),
        ).to_nats_bytes(),
        # coordinator/dispatcher.py — no run_id at all
        "crawl": coordinator.ScrapeMessage(
            artifact_id=JOB_PAGE_ID,
            url="https://example.com/page",
            output_format="html",
            engine="playwright",
            options=coordinator.MessageOptions(respect_robots=True),
            crawl_context=coordinator.CrawlContext(
                crawl_id=CRAWL_ID, crawl_page_id=JOB_PAGE_ID, depth=2
            ),
        ).to_nats_bytes(),
    }


def _llm_payload() -> bytes:
    """core/result_consumer.py — both LLM dispatch sites build this shape."""
    return api.LLMMessage(
        artifact_id=RUN_ID,
        run_id=RUN_ID,
        raw_minio_path=f"scrapeflow-results/history/{RUN_ID}/scrape.html",
        provider="openai_compatible",
        encrypted_api_key="gAAAAAB_key",
        base_url="https://llm.example.com/v1",
        model="qwen2.5-7b-instruct",
        output_schema={"type": "object", "properties": {"title": {"type": "string"}}},
    ).to_nats_bytes()


SCRAPE_LANES = _scrape_lanes()
LANES_WITH_RUN = ["job_http", "job_playwright", "scheduled", "batch"]


# ---------------------------------------------------------------------------
# Producer payload → the Playwright worker's real parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", sorted(SCRAPE_LANES))
def test_playwright_worker_parses_every_dispatcher_payload(lane):
    """Every lane's dispatch must parse. Path A of BUG-005 was this test's absence."""
    msg = playwright.JobMessage.model_validate_json(SCRAPE_LANES[lane])
    assert msg.schema_version == 3
    assert msg.url
    assert msg.output_format


@pytest.mark.parametrize("lane", sorted(SCRAPE_LANES))
def test_artifact_id_survives_the_wire_intact(lane):
    """
    The worker builds its object paths from this string verbatim, so a value that
    changes in transit silently relocates the artifact.
    """
    expected = json.loads(SCRAPE_LANES[lane])["artifact_id"]
    assert playwright.JobMessage.model_validate_json(SCRAPE_LANES[lane]).artifact_id == expected
    assert expected  # never empty — that is the whole point of the field


@pytest.mark.parametrize("lane", LANES_WITH_RUN)
def test_run_id_is_present_on_every_lane_that_has_a_run(lane):
    assert playwright.JobMessage.model_validate_json(SCRAPE_LANES[lane]).run_id == RUN_ID


def test_crawl_lane_omits_run_id_entirely():
    """
    Not null, not empty — absent. A crawl page creates no job_runs row, and the
    coordinator used to fabricate a uuid4() so a required field could be populated.
    """
    raw = json.loads(SCRAPE_LANES["crawl"])
    assert "run_id" not in raw
    assert playwright.JobMessage.model_validate_json(SCRAPE_LANES["crawl"]).run_id is None


def test_no_lane_puts_job_id_on_the_wire():
    for lane, payload in SCRAPE_LANES.items():
        assert "job_id" not in json.loads(payload), lane
    assert "job_id" not in json.loads(_llm_payload())


def test_playwright_worker_reads_the_nested_sub_objects_it_is_sent():
    """The fields only this worker consumes must survive the round trip."""
    msg = playwright.JobMessage.model_validate_json(SCRAPE_LANES["job_playwright"])
    assert msg.credentials.encrypted_cookies == "gAAAAAB_cookies"
    assert msg.options.actions == [{"type": "wait", "milliseconds": 500}]
    assert msg.playwright_options.wait_strategy == "networkidle"
    assert msg.playwright_options.block_images is True


def test_crawl_context_survives_for_result_routing():
    msg = playwright.JobMessage.model_validate_json(SCRAPE_LANES["crawl"])
    assert msg.crawl_context.crawl_id == CRAWL_ID
    assert msg.crawl_context.crawl_page_id == JOB_PAGE_ID
    assert msg.crawl_context.depth == 2


# ---------------------------------------------------------------------------
# Producer payload → the LLM worker's real parser
# ---------------------------------------------------------------------------


def test_llm_worker_parses_the_result_consumer_payload():
    """Path C of BUG-005: this dispatch carried job_id=None and was acked and dropped."""
    job = llm.JobMessage.model_validate_json(_llm_payload())
    assert job.artifact_id == RUN_ID
    assert job.run_id == RUN_ID
    assert job.base_url == "https://llm.example.com/v1"
    assert job.output_schema["type"] == "object"


def test_llm_stage_writes_beside_the_scrape_it_reads():
    """
    artifact_id ties the two stages together: the LLM worker reads the scrape's object
    and writes llm.json under the same artifact. If these diverged, the extraction
    would land under a different parent from the page it came from.
    """
    job = llm.JobMessage.model_validate_json(_llm_payload())
    assert job.raw_minio_path.endswith(f"history/{job.artifact_id}/scrape.html")


# ---------------------------------------------------------------------------
# The producer/consumer duplicate cannot drift
# ---------------------------------------------------------------------------


def test_api_and_coordinator_scrape_models_declare_the_same_fields():
    """
    ADR-011 §6 rejected a shared contracts package and accepted a duplicate instead.
    This is what holds the duplicate honest — nothing else does.
    """
    assert set(api.ScrapeMessage.model_fields) == set(coordinator.ScrapeMessage.model_fields)
    assert api.SCHEMA_VERSION == coordinator.SCHEMA_VERSION


def test_workers_accept_every_field_the_producers_declare():
    """
    A field the producer sends and the consumer silently ignores is how a contract
    rots: it looks delivered and is not. The Go worker deliberately omits fields it
    does not consume, so this is asserted for the Playwright worker, which consumes
    all of them.
    """
    assert set(api.ScrapeMessage.model_fields) <= set(playwright.JobMessage.model_fields)
    assert set(api.LLMMessage.model_fields) <= set(llm.JobMessage.model_fields)


# ---------------------------------------------------------------------------
# The BUG-005 regressions themselves
# ---------------------------------------------------------------------------


def test_the_old_batch_payload_no_longer_validates_anywhere():
    """
    The exact payload routers/batch.py used to publish. api/tests/test_batch.py
    asserted `payload["job_id"] is None` was correct, which is why every suite stayed
    green while three execution paths were broken.
    """
    legacy = json.dumps(
        {
            "schema_version": 2,
            "job_id": None,
            "run_id": RUN_ID,
            "url": "https://example.com/a",
            "output_format": "html",
            "engine": "http",
            "credentials": None,
            "options": {"respect_robots": False, "actions": None},
            "crawl_context": None,
        }
    )
    with pytest.raises(ValidationError):
        playwright.JobMessage.model_validate_json(legacy)


def test_a_producer_cannot_build_a_message_without_an_artifact_id():
    """
    The side effect ADR-011 §6 wanted: batch's missing identifier now fails at
    dispatch, in development, on the first batch anyone runs — a loud local error
    instead of a silent production one.
    """
    for model in (api.ScrapeMessage, coordinator.ScrapeMessage):
        with pytest.raises(ValidationError):
            model(url="https://example.com/", output_format="html", engine="http")
        with pytest.raises(ValidationError):
            model(
                artifact_id="",
                url="https://example.com/",
                output_format="html",
                engine="http",
            )


# ---------------------------------------------------------------------------
# Fixtures for the Go parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", sorted(SCRAPE_LANES))
def test_go_fixture_matches_what_the_producer_emits(lane):
    """
    The Go worker's parser lives in another runtime, so it is fed through committed
    fixtures (http-worker/internal/worker/contract_test.go). This test is what keeps
    those fixtures from going stale: they are regenerated here and compared.

    Set UPDATE_CONTRACT_FIXTURES=1 to rewrite them after an intended change.
    """
    path = FIXTURES / f"{lane}.json"
    current = json.dumps(json.loads(SCRAPE_LANES[lane]), indent=2, sort_keys=True) + "\n"

    if os.environ.get("UPDATE_CONTRACT_FIXTURES") == "1":
        path.write_text(current)

    assert path.exists(), f"missing fixture {path}; regenerate with UPDATE_CONTRACT_FIXTURES=1"
    assert path.read_text() == current, (
        f"{path.name} is stale — the Go worker is being tested against a payload the "
        f"API no longer sends. Regenerate with UPDATE_CONTRACT_FIXTURES=1."
    )
