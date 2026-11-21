"""M.6 — GET /pipelines/catalog: each block type's kind, reversibility and timing (PRD-017 R2, R6)."""

import pytest_asyncio

from app.pipelines.catalog import BUCKET_UNDO_TIMING, CATALOG, ScrapeConfigV1
from app.settings import settings


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


async def test_the_catalog_needs_a_user(client):
    assert (await client.get("/pipelines/catalog")).status_code == 401


async def test_every_block_type_is_listed_with_its_kind_and_sink(client, headers):
    resp = await client.get("/pipelines/catalog", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_limit_seconds"] == settings.pipeline_max_run_seconds
    blocks = {b["type"]: b for b in body["blocks"]}
    assert set(blocks) == set(CATALOG)
    assert {t: (b["kind"], b["sink"] and b["sink"]["reversible"]) for t, b in blocks.items()} == {
        "scrape": ("content", None),
        "clean": ("content", None),
        "llm_extract": ("content", None),
        "validate": ("effect", None),
        "webhook": ("effect", False),
        "bucket": ("effect", True),
        "approval": ("effect", None),
    }
    assert blocks["bucket"]["destination_kind"] == "bucket"
    assert blocks["webhook"]["destination_kind"] == "webhook"
    assert blocks["bucket"]["destination_kinds"] == ["bucket"]
    assert blocks["scrape"]["destination_kinds"] == []
    assert blocks["scrape"]["consumes"] == []
    assert "extraction" in blocks["bucket"]["consumes"]


async def test_a_sinks_timing_and_its_undo_add_up(client, headers):
    blocks = {
        b["type"]: b
        for b in (await client.get("/pipelines/catalog", headers=headers)).json()["blocks"]
    }
    bucket = blocks["bucket"]
    assert list(bucket["timings"]) == ["default"]
    assert bucket["timings"]["default"] == {
        "attempt_seconds": 60,
        "queue_wait_seconds": 60,
        "attempts": 5,
        "first_wait_seconds": 30,
        "backoff": 10.0,
        "max_wait_seconds": 7200,
        "budget_seconds": 5 * 120 + 10530,
        "need_seconds": 5 * 120 + 10530 + 13 * 20 + 60,
    }
    assert bucket["sink"]["undo"]["need_seconds"] == BUCKET_UNDO_TIMING.need
    assert blocks["webhook"]["sink"]["undo"] is None
    # An alert-only Webhook calls nothing on a run's way through.
    alert = blocks["webhook"]["timings"]["alert only"]
    assert (alert["budget_seconds"], alert["need_seconds"]) == (0, 60)


async def test_the_scrape_timing_is_shown_for_each_engine(client, headers):
    blocks = {
        b["type"]: b
        for b in (await client.get("/pipelines/catalog", headers=headers)).json()["blocks"]
    }
    timings = blocks["scrape"]["timings"]
    playwright = CATALOG["scrape"].timing(
        ScrapeConfigV1(url="https://example.com", engine="playwright")
    )
    assert set(timings) == {"http", "playwright"}
    assert timings["playwright"]["need_seconds"] == playwright.need
    assert timings["http"]["need_seconds"] < timings["playwright"]["need_seconds"]
