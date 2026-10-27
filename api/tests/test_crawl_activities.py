"""E.3 / E.5 — the crawl lane's DB activities on a real DB: admission (ADR-009 13d, ADR-010 §2),
the claim, the per-page record and charge, and the crawl's close."""

import asyncio
import io
import json
import uuid

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.crawl import Crawl, CrawlPage, CrawlQueueItem
from app.models.quota_views import quota_active_submissions, quota_run_units
from app.models.storage_object import StorageObject
from app.models.user_quota import UserQuota
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities import crawls, job_runs
from app.workflows.activities.contracts import StoredObject
from app.workflows.activities.crawls import (
    NO_OUTCOME,
    admit_urls,
    claim_crawl_pages,
    crawl_started,
    finish_crawl,
    record_crawl_page,
    vet_urls,
)
from app.workflows.crawl import (
    ClaimInput,
    CrawlRef,
    FinishCrawlInput,
    RecordCrawlPageInput,
    UrlBatch,
)

BUCKET = settings.minio_bucket
SEED = "https://shop.example.com/"
# Hosts the fake SSRF check refuses; everything else "resolves" publicly.
INTERNAL = "internal.shop.example.com"
NXDOMAIN = "nx.shop.example.com"


@pytest.fixture(autouse=True)
def _fake_ssrf(monkeypatch):
    def check(url: str) -> None:
        host = url.split("/")[2]
        if host == INTERNAL:
            raise ValueError("URL resolves to a private address: 10.0.0.7")
        if host == NXDOMAIN:
            raise ValueError("URL hostname could not be resolved")

    monkeypatch.setattr(crawls, "validate_no_ssrf_core", check)


@pytest_asyncio.fixture(autouse=True)
async def _test_minio(monkeypatch):
    monkeypatch.setattr(job_runs, "_minio", app.state.minio)


async def _crawl(user_id, *, status="running", seed=SEED, **fields) -> Crawl:
    async with AsyncSessionLocal() as db:
        crawl = Crawl(user_id=user_id, seed_url=seed, status=status, **fields)
        db.add(crawl)
        await db.commit()
        await db.refresh(crawl)
    return crawl


async def _reload(crawl_id) -> tuple[Crawl, dict[str, CrawlQueueItem], list[CrawlPage]]:
    async with AsyncSessionLocal() as db:
        crawl = await db.get(Crawl, crawl_id)
        items = (
            await db.scalars(select(CrawlQueueItem).where(CrawlQueueItem.crawl_id == crawl_id))
        ).all()
        pages = (
            await db.scalars(
                select(CrawlPage)
                .where(CrawlPage.crawl_id == crawl_id)
                .order_by(CrawlPage.created_at)
            )
        ).all()
    return crawl, {i.url: i for i in items}, list(pages)


def _batch(crawl, urls, source="link", depth=1) -> UrlBatch:
    return UrlBatch(crawl_id=str(crawl.id), urls=urls, source=source, depth=depth)


async def _listen(channel: str):
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(channel, lambda *a: received.put_nowait(json.loads(a[3])))
    return conn, received


async def _put(page_id, size: int = 10) -> str:
    key = f"history/{page_id}/scrape.html"
    await app.state.minio.put_object(BUCKET, key, io.BytesIO(b"x" * size), size)
    return f"{BUCKET}/{key}"


async def _exists(path: str) -> bool:
    bucket, _, key = path.partition("/")
    try:
        await app.state.minio.stat_object(bucket, key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# admit_urls — the one gate (13d)
# ---------------------------------------------------------------------------


async def test_the_seed_is_admitted_at_depth_zero_and_notified(db_user):
    crawl = await _crawl(db_user.id)
    conn, received = await _listen("crawl_status")
    try:
        admitted = await admit_urls(_batch(crawl, [SEED], "seed", 0))
        payload = await asyncio.wait_for(received.get(), 5)
    finally:
        await conn.close()

    assert (admitted.state, admitted.admitted, admitted.dropped) == ("running", 1, {})
    reloaded, items, _ = await _reload(crawl.id)
    assert (items[SEED].status, items[SEED].depth) == ("pending", 0)
    assert reloaded.total_queued == 1
    assert payload["total_queued"] == 1 and payload["status"] == "running"


async def test_seen_urls_are_skipped_and_a_retry_changes_nothing(db_user):
    crawl = await _crawl(db_user.id, exclude_paths=["/admin"])
    batch = _batch(crawl, [f"{SEED}a", f"{SEED}a#top", f"{SEED}b", f"{SEED}admin/x"])

    first = await admit_urls(batch)
    again = await admit_urls(batch)

    assert (first.admitted, first.dropped) == (2, {"filtered": 1})
    assert (again.admitted, again.dropped) == (0, {})
    reloaded, items, _ = await _reload(crawl.id)
    assert reloaded.total_queued == 2
    # Refusals are counted once — a rediscovered dropped URL is seen.
    assert reloaded.dropped == {"filtered": 1}
    assert (items[f"{SEED}admin/x"].status, items[f"{SEED}admin/x"].drop_reason) == (
        "dropped",
        "filtered",
    )
    # The fragment is not part of the key.
    assert f"{SEED}a#top" not in items


async def test_include_paths_apply_to_links_and_sitemap_entries_not_the_seed(db_user):
    crawl = await _crawl(db_user.id, include_paths=["/blog"])
    assert (await admit_urls(_batch(crawl, [SEED], "seed", 0))).admitted == 1
    links = await admit_urls(_batch(crawl, [f"{SEED}blog/1", f"{SEED}shop/1"]))
    entries = await admit_urls(_batch(crawl, [f"{SEED}blog/2", f"{SEED}about"], "sitemap"))
    assert (links.admitted, links.dropped) == (1, {"filtered": 1})
    assert (entries.admitted, entries.dropped) == (1, {"filtered": 1})


async def test_sitemap_entries_are_scoped_to_the_seeds_registrable_domain(db_user):
    crawl = await _crawl(db_user.id)
    out = await admit_urls(
        _batch(
            crawl,
            [
                "https://docs.example.com/guide",  # a subdomain: admitted (ADR-010 §2)
                "https://unrelated.org/page",
                "https://shop.example.com.evil.net/x",
            ],
            "sitemap",
        )
    )
    assert (out.admitted, out.dropped) == (1, {"out_of_scope": 2})


async def test_scope_keeps_private_suffix_siblings_apart(db_user):
    crawl = await _crawl(db_user.id, seed="https://alice.github.io/")
    out = await admit_urls(
        _batch(crawl, ["https://alice.github.io/a", "https://bob.github.io/b"], "sitemap")
    )
    assert (out.admitted, out.dropped) == (1, {"out_of_scope": 1})


async def test_internal_and_unresolvable_urls_are_dropped_not_failed(db_user):
    crawl = await _crawl(db_user.id)
    out = await admit_urls(
        _batch(
            crawl,
            [f"https://{INTERNAL}/a", f"https://{INTERNAL}/b", f"https://{NXDOMAIN}/", f"{SEED}ok"],
            "sitemap",
        )
    )
    assert (out.state, out.admitted, out.dropped) == ("running", 1, {"ssrf": 2, "unresolved": 1})


async def test_the_real_check_refuses_a_metadata_address(db_user, monkeypatch):
    monkeypatch.undo()  # the real validate_no_ssrf_core; an IP literal needs no DNS
    monkeypatch.setattr(job_runs, "_minio", app.state.minio)
    crawl = await _crawl(db_user.id, seed="http://169.254.169.254/")
    out = await admit_urls(_batch(crawl, ["http://169.254.169.254/latest/meta-data/"], "seed", 0))
    assert (out.admitted, out.dropped) == (0, {"ssrf": 1})


async def test_a_url_that_is_not_http_is_invalid(db_user):
    crawl = await _crawl(db_user.id)
    out = await admit_urls(
        _batch(crawl, ["javascript:alert(1)", "ftp://shop.example.com/f", "http://[::1"], "sitemap")
    )
    assert (out.admitted, out.dropped) == (0, {"invalid": 3})


async def test_admission_stops_at_max_pages(db_user):
    crawl = await _crawl(db_user.id, max_pages=3)
    await admit_urls(_batch(crawl, [SEED], "seed", 0))
    out = await admit_urls(_batch(crawl, [f"{SEED}{i}" for i in range(5)]))
    later = await admit_urls(_batch(crawl, [f"{SEED}late"]))

    assert (out.admitted, later.admitted) == (2, 0)
    reloaded, items, _ = await _reload(crawl.id)
    assert reloaded.total_queued == 3
    # Past the cap nothing is stored: those URLs were not refused, just not reached.
    assert len(items) == 3


async def test_a_stopped_crawl_admits_nothing(db_user):
    crawl = await _crawl(db_user.id, status="cancelled")
    out = await admit_urls(_batch(crawl, [f"{SEED}a"]))
    assert (out.state, out.admitted) == ("cancelled", 0)
    _, items, _ = await _reload(crawl.id)
    assert items == {}


async def test_vet_counts_refused_sitemap_files_and_stores_nothing(db_user):
    crawl = await _crawl(db_user.id)
    vetted = await vet_urls(
        _batch(
            crawl,
            [f"{SEED}sitemap.xml", f"https://{INTERNAL}/s.xml", "https://other.net/s.xml"],
            "sitemap_file",
        )
    )
    assert vetted.urls == [f"{SEED}sitemap.xml"]
    reloaded, items, _ = await _reload(crawl.id)
    assert reloaded.dropped == {"sitemap_ssrf": 1, "sitemap_out_of_scope": 1}
    assert items == {}


# ---------------------------------------------------------------------------
# claim_crawl_pages
# ---------------------------------------------------------------------------


async def _admitted(user_id, urls_by_depth: dict[int, list[str]], **fields) -> Crawl:
    crawl = await _crawl(user_id, **fields)
    for depth, urls in urls_by_depth.items():
        await admit_urls(_batch(crawl, urls, "seed" if depth == 0 else "link", depth))
    return crawl


async def test_claim_is_breadth_first_and_writes_one_page_row_each(db_user):
    crawl = await _admitted(db_user.id, {0: [SEED], 2: [f"{SEED}deep"], 1: [f"{SEED}a"]})

    claim = await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=2))

    assert [(p.url, p.depth) for p in claim.pages] == [(SEED, 0), (f"{SEED}a", 1)]
    _, items, pages = await _reload(crawl.id)
    assert [(p.url, p.status) for p in pages] == [(SEED, "running"), (f"{SEED}a", "running")]
    assert items[SEED].status == "dispatched"
    assert items[SEED].crawl_page_id == uuid.UUID(claim.pages[0].page_id)
    assert items[f"{SEED}deep"].status == "pending"


async def test_claimed_pages_are_the_run_units_and_the_crawl_holds_one_slot(db_user):
    crawl = await _admitted(db_user.id, {0: [SEED], 1: [f"{SEED}a", f"{SEED}b"]})
    await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=3))
    async with AsyncSessionLocal() as db:
        units = await db.scalar(
            select(func.count())
            .select_from(quota_run_units)
            .where(quota_run_units.c.user_id == db_user.id)
        )
        slots = await db.scalar(
            select(func.count())
            .select_from(quota_active_submissions)
            .where(quota_active_submissions.c.user_id == db_user.id)
        )
    assert (units, slots) == (3, 1)


async def test_a_page_claimed_but_not_in_flight_is_handed_back(db_user):
    crawl = await _admitted(db_user.id, {0: [SEED], 1: [f"{SEED}a"]})
    lost = await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=1))

    # A retried claim whose first answer the workflow never saw.
    retried = await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=2))
    assert retried.pages[0].page_id == lost.pages[0].page_id
    assert [p.url for p in retried.pages] == [SEED, f"{SEED}a"]

    held = [p.page_id for p in retried.pages]
    assert (
        await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=2, in_flight=held))
    ).pages == []


async def test_at_the_storage_limit_nothing_is_claimed(db_user):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=db_user.id, storage_bytes_limit=10, storage_bytes_used=10))
        await db.commit()
    crawl = await _admitted(db_user.id, {0: [SEED]})
    claim = await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=3))
    assert (claim.state, claim.pages, claim.storage_full) == ("running", [], True)


async def test_a_cancelled_crawl_claims_nothing(db_user):
    crawl = await _admitted(db_user.id, {0: [SEED]})
    async with AsyncSessionLocal() as db:
        (await db.get(Crawl, crawl.id)).status = "cancelled"
        await db.commit()
    claim = await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=3))
    assert (claim.state, claim.pages) == ("cancelled", [])


# ---------------------------------------------------------------------------
# record_crawl_page — the per-page charge P7 deferred
# ---------------------------------------------------------------------------


async def _claimed(user_id, n=2, **fields):
    crawl = await _admitted(user_id, {0: [SEED], 1: [f"{SEED}{i}" for i in range(n - 1)]}, **fields)
    claim = await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=n))
    return crawl, claim.pages


def _record(crawl, page, status, objects=(), error=None) -> RecordCrawlPageInput:
    return RecordCrawlPageInput(
        crawl_id=str(crawl.id),
        page_id=page.page_id,
        status=status,
        objects=list(objects),
        error=error,
    )


async def _ledger(paths) -> list[StorageObject]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.scalars(select(StorageObject).where(StorageObject.object_key.in_(paths)))
            ).all()
        )


async def test_a_completed_page_is_charged_to_the_crawls_owner(db_user):
    crawl, [page, _] = await _claimed(db_user.id)
    path = await _put(page.page_id, 10)
    conn, received = await _listen("crawl_status")
    try:
        out = await record_crawl_page(
            _record(crawl, page, "completed", [StoredObject(path=path, size=10)])
        )
        payload = await asyncio.wait_for(received.get(), 5)
    finally:
        await conn.close()

    assert (out.state, out.storage_full) == ("running", False)
    [row] = await _ledger([path])
    assert (row.user_id, row.crawl_page_id, row.bytes) == (db_user.id, uuid.UUID(page.page_id), 10)
    reloaded, items, pages = await _reload(crawl.id)
    assert (pages[0].status, pages[0].result_path) == ("completed", path)
    assert items[SEED].status == "completed"
    assert (reloaded.total_completed, reloaded.total_failed) == (1, 0)
    assert payload == {
        "crawl_id": str(crawl.id),
        "status": "running",
        "total_queued": 2,
        "total_completed": 1,
        "total_failed": 0,
        "page_url": SEED,
        "page_status": "completed",
    }

    # A retried activity: no second charge, counters unchanged.
    await record_crawl_page(_record(crawl, page, "completed", [StoredObject(path=path, size=10)]))
    async with AsyncSessionLocal() as db:
        assert (await db.get(UserQuota, db_user.id)).storage_bytes_used == 10
    assert (await _reload(crawl.id))[0].total_completed == 1


async def test_a_failed_page_keeps_its_error(db_user):
    crawl, [_, page] = await _claimed(db_user.id)
    await record_crawl_page(_record(crawl, page, "failed", error="robots_txt_disallowed"))
    reloaded, items, pages = await _reload(crawl.id)
    assert (pages[1].status, pages[1].error) == ("failed", "robots_txt_disallowed")
    assert items[f"{SEED}0"].status == "failed"
    assert (reloaded.total_completed, reloaded.total_failed) == (0, 1)


async def test_a_page_of_a_cancelled_crawl_is_dropped_uncharged(db_user):
    crawl, [page, _] = await _claimed(db_user.id)
    async with AsyncSessionLocal() as db:
        (await db.get(Crawl, crawl.id)).status = "cancelled"
        await db.commit()
    path = await _put(page.page_id)

    out = await record_crawl_page(
        _record(crawl, page, "completed", [StoredObject(path=path, size=10)])
    )

    assert out.state == "cancelled"
    assert not await _exists(path)
    assert await _ledger([path]) == []
    _, items, pages = await _reload(crawl.id)
    assert (pages[0].status, items[SEED].status) == ("cancelled", "skipped")


async def test_the_page_that_crosses_the_storage_limit_is_kept_and_charged(db_user):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=db_user.id, storage_bytes_limit=15, storage_bytes_used=10))
        await db.commit()
    crawl, [page, _] = await _claimed(db_user.id)
    path = await _put(page.page_id, 10)

    out = await record_crawl_page(
        _record(crawl, page, "completed", [StoredObject(path=path, size=10)])
    )

    # §8d: the run that crosses the ceiling finishes and is charged; the next page is not claimed.
    assert (out.state, out.storage_full) == ("running", True)
    assert await _exists(path)
    async with AsyncSessionLocal() as db:
        assert (await db.get(UserQuota, db_user.id)).storage_bytes_used == 20
    assert (await claim_crawl_pages(ClaimInput(crawl_id=str(crawl.id), limit=1))).storage_full


# ---------------------------------------------------------------------------
# crawl_started / finish_crawl
# ---------------------------------------------------------------------------


async def test_started_moves_queued_to_running_once(db_user):
    crawl = await _crawl(db_user.id, status="queued")
    assert await crawl_started(CrawlRef(crawl_id=str(crawl.id))) == "running"
    assert await crawl_started(CrawlRef(crawl_id=str(crawl.id))) == "running"
    assert await crawl_started(CrawlRef(crawl_id=str(uuid.uuid4()))) == "gone"


async def _deliveries(crawl_id) -> list[WebhookDelivery]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.scalars(
                    select(WebhookDelivery).where(WebhookDelivery.crawl_id == crawl_id)
                )
            ).all()
        )


async def test_finish_closes_the_crawl_with_its_webhook_row(db_user):
    crawl, [page, _] = await _claimed(db_user.id, n=2, webhook_url="https://hooks.example.com/c")
    await admit_urls(_batch(crawl, [f"{SEED}never"]))
    path = await _put(page.page_id)
    await record_crawl_page(_record(crawl, page, "completed", [StoredObject(path=path, size=10)]))

    out = await finish_crawl(FinishCrawlInput(crawl_id=str(crawl.id), status="completed"))

    assert out == "completed"
    reloaded, items, pages = await _reload(crawl.id)
    assert reloaded.completed_at is not None
    # The page claimed and never recorded, and the URL never claimed.
    assert (pages[1].status, pages[1].error) == ("failed", NO_OUTCOME)
    assert items[f"{SEED}never"].status == "skipped"
    assert (reloaded.total_completed, reloaded.total_failed) == (1, 1)
    [delivery] = await _deliveries(crawl.id)
    assert delivery.event == "crawl.completed"
    assert delivery.payload["total_completed"] == 1 and delivery.payload["status"] == "completed"

    # A retried activity: left as it is, no second row.
    assert (
        await finish_crawl(FinishCrawlInput(crawl_id=str(crawl.id), status="completed"))
        == "completed"
    )
    assert len(await _deliveries(crawl.id)) == 1


async def test_finish_keeps_the_storage_note(db_user):
    crawl = await _crawl(db_user.id)
    await finish_crawl(
        FinishCrawlInput(crawl_id=str(crawl.id), status="completed", error="storage limit reached")
    )
    assert (await _reload(crawl.id))[0].error == "storage limit reached"


async def test_a_crawl_the_api_cancelled_stays_cancelled_without_a_webhook(db_user):
    crawl, _ = await _claimed(db_user.id, webhook_url="https://hooks.example.com/c")
    async with AsyncSessionLocal() as db:
        (await db.get(Crawl, crawl.id)).status = "cancelled"
        await db.commit()

    out = await finish_crawl(FinishCrawlInput(crawl_id=str(crawl.id), status="completed"))

    assert out == "cancelled"
    reloaded, _, pages = await _reload(crawl.id)
    assert {p.status for p in pages} == {"cancelled"}
    assert reloaded.completed_at is not None
    assert await _deliveries(crawl.id) == []


async def test_finish_failed_writes_crawl_failed(db_user):
    crawl = await _crawl(db_user.id, webhook_url="https://hooks.example.com/c")
    out = await finish_crawl(
        FinishCrawlInput(
            crawl_id=str(crawl.id), status="failed", error="the seed URL was refused: ssrf"
        )
    )
    assert out == "failed"
    [delivery] = await _deliveries(crawl.id)
    assert (delivery.event, delivery.payload["error"]) == (
        "crawl.failed",
        "the seed URL was refused: ssrf",
    )
