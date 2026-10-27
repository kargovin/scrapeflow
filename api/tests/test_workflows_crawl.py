"""E.5 — CrawlWorkflow over a mocked frontier (time-skipping env); the DB side is
tests/test_crawl_activities.py."""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.activities.contracts import (
    DISCOVER_SITEMAPS_ACTIVITY,
    EXTRACT_LINKS_ACTIVITY,
    FETCH_SITEMAP_ACTIVITY,
    SCRAPE_ACTIVITY,
    DiscoverSitemapsInput,
    DiscoverSitemapsOutput,
    ExtractLinksInput,
    ExtractLinksOutput,
    FetchSitemapInput,
    FetchSitemapOutput,
    ScrapeInput,
    ScrapeOutput,
)
from app.workflows.crawl import (
    ACCOUNTING_FAILED,
    ADMIT_URLS_ACTIVITY,
    CLAIM_CRAWL_PAGES_ACTIVITY,
    CRAWL_STARTED_ACTIVITY,
    FINISH_CRAWL_ACTIVITY,
    MAX_SITEMAP_FILES,
    RECORD_CRAWL_PAGE_ACTIVITY,
    STORAGE_LIMIT_NOTE,
    VET_URLS_ACTIVITY,
    Admitted,
    Claim,
    ClaimedPage,
    ClaimInput,
    CrawlInput,
    CrawlRef,
    CrawlWorkflow,
    FinishCrawlInput,
    PageRecorded,
    RecordCrawlPageInput,
    UrlBatch,
    Vetted,
    workflow_id,
)
from app.workflows.queues import CRAWL_QUEUE, SCRAPE_HTTP_QUEUE, WORKFLOW_QUEUE
from app.workflows.steps import (
    ACQUIRE_HOST_SLOT_ACTIVITY,
    RELEASE_HOST_SLOT_ACTIVITY,
    HostSlot,
    HostSlotInput,
    HostSlotRelease,
)

SEED = "https://site.example/"
HASH = "00c0ffee12345678"


def u(path: str) -> str:
    return f"https://site.example/{path}"


def crawl_input(**overrides) -> CrawlInput:
    fields = {
        "crawl_id": "c1",
        "seed_url": SEED,
        "engine": "http",
        "max_depth": 3,
        "max_pages": 100,
        "respect_robots": True,
        "ignore_sitemap": True,
        "max_in_flight": 3,
        "pages_per_run": 100,
    }
    return CrawlInput(**(fields | overrides))


@dataclass
class Frontier:
    """An in-memory crawl_queue + crawl_pages with admission's dedup and cap, and the crawl row."""

    links: dict[str, list[str]] = field(default_factory=dict)
    max_pages: int = 100
    refused: set[str] = field(default_factory=set)
    state: str = "running"
    storage_full_after: int | None = None
    pending: list[tuple[str, int]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    pages: dict[str, ClaimedPage] = field(default_factory=dict)
    outcomes: dict[str, RecordCrawlPageInput] = field(default_factory=dict)
    admits: list[UrlBatch] = field(default_factory=list)
    claims: list[ClaimInput] = field(default_factory=list)
    finishes: list[FinishCrawlInput] = field(default_factory=list)
    started: int = 0

    def admit(self, batch: UrlBatch) -> Admitted:
        self.admits.append(batch)
        if self.state != "running":
            return Admitted(state=self.state)
        admitted, dropped = 0, {}
        for url in batch.urls:
            if url in self.seen:
                continue
            self.seen.add(url)
            if url in self.refused:
                dropped["ssrf"] = dropped.get("ssrf", 0) + 1
            elif len(self.seen - self.refused) <= self.max_pages:
                self.pending.append((url, batch.depth))
                admitted += 1
        return Admitted(state="running", admitted=admitted, dropped=dropped)

    def claim(self, input: ClaimInput) -> Claim:
        self.claims.append(input)
        if self.state != "running":
            return Claim(state=self.state)
        if self.storage_full_after is not None and len(self.outcomes) >= self.storage_full_after:
            return Claim(state="running", storage_full=True)
        pages = []
        while self.pending and len(pages) < input.limit:
            url, depth = self.pending.pop(0)
            page = ClaimedPage(page_id=f"p{len(self.pages)}", url=url, depth=depth)
            self.pages[page.page_id] = page
            pages.append(page)
        return Claim(state="running", pages=pages)

    def record(self, input: RecordCrawlPageInput) -> PageRecorded:
        self.outcomes.setdefault(input.page_id, input)
        full = self.storage_full_after is not None and len(self.outcomes) >= self.storage_full_after
        return PageRecorded(state=self.state, storage_full=full)

    def statuses(self) -> dict[str, str]:
        return {self.pages[p].url: o.status for p, o in self.outcomes.items()}


def _scraped(artifact_id: str) -> ScrapeOutput:
    return ScrapeOutput.model_validate(
        {
            "result": {"path": f"b/history/{artifact_id}/scrape.html", "size": 10},
            "content_hash": HASH,
        }
    )


class Harness:
    def __init__(self, frontier: Frontier, scrape=None, sitemaps=None, extract=None, record=None):
        self.f = frontier
        self.scraping = 0
        self.max_scraping = 0
        self.scraped: list[str] = []
        self.extracted: list[str] = []
        self.acquired: list[str] = []
        self.sitemaps = sitemaps or {}
        self.fetched: list[str] = []
        self.discovered = 0
        self._scrape = scrape
        self._extract = extract
        self._record = record

    def activities(self):
        f = self

        @activity.defn(name=CRAWL_STARTED_ACTIVITY)
        async def started(input: CrawlRef) -> str:
            f.f.started += 1
            return f.f.state

        @activity.defn(name=ADMIT_URLS_ACTIVITY)
        async def admit(input: UrlBatch) -> Admitted:
            return f.f.admit(input)

        @activity.defn(name=VET_URLS_ACTIVITY)
        async def vet(input: UrlBatch) -> Vetted:
            return Vetted(urls=[x for x in input.urls if x not in f.f.refused])

        @activity.defn(name=CLAIM_CRAWL_PAGES_ACTIVITY)
        async def claim(input: ClaimInput) -> Claim:
            return f.f.claim(input)

        @activity.defn(name=RECORD_CRAWL_PAGE_ACTIVITY)
        async def record(input: RecordCrawlPageInput) -> PageRecorded:
            if f._record:
                await f._record(input)
            return f.f.record(input)

        @activity.defn(name=FINISH_CRAWL_ACTIVITY)
        async def finish(input: FinishCrawlInput) -> str:
            f.f.finishes.append(input)
            return f.f.state if f.f.state != "running" else input.status

        @activity.defn(name=ACQUIRE_HOST_SLOT_ACTIVITY)
        async def acquire(input: HostSlotInput) -> HostSlot:
            f.acquired.append(input.url)
            return HostSlot(domain="site.example")

        @activity.defn(name=RELEASE_HOST_SLOT_ACTIVITY)
        async def release(input: HostSlotRelease) -> None:
            return None

        @activity.defn(name=SCRAPE_ACTIVITY)
        async def scrape(input: ScrapeInput) -> ScrapeOutput:
            assert input.output_format == "html"
            f.scraping += 1
            f.max_scraping = max(f.max_scraping, f.scraping)
            try:
                url = f.f.pages[input.artifact_id].url
                f.scraped.append(url)
                if f._scrape:
                    await f._scrape(url)
                else:
                    await asyncio.sleep(0.02)
                return _scraped(input.artifact_id)
            finally:
                f.scraping -= 1

        @activity.defn(name=EXTRACT_LINKS_ACTIVITY)
        async def extract(input: ExtractLinksInput) -> ExtractLinksOutput:
            f.extracted.append(input.base_url)
            if f._extract:
                await f._extract(input.base_url)
            return ExtractLinksOutput(links=f.f.links.get(input.base_url, []))

        @activity.defn(name=DISCOVER_SITEMAPS_ACTIVITY)
        async def discover(input: DiscoverSitemapsInput) -> DiscoverSitemapsOutput:
            f.discovered += 1
            return DiscoverSitemapsOutput(sitemaps=f.sitemaps.get("robots", []))

        @activity.defn(name=FETCH_SITEMAP_ACTIVITY)
        async def fetch(input: FetchSitemapInput) -> FetchSitemapOutput:
            f.fetched.append(input.url)
            return f.sitemaps.get(input.url, FetchSitemapOutput(kind="unavailable"))

        return (
            [started, admit, vet, claim, record, finish, acquire, release],
            [scrape],
            [extract, discover, fetch],
        )

    async def run(self, input: CrawlInput | None = None, during=None):
        db, scrape, crawl = self.activities()
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            async with (
                Worker(
                    env.client,
                    task_queue=WORKFLOW_QUEUE,
                    workflows=[CrawlWorkflow],
                    activities=db,
                ),
                Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=scrape),
                Worker(env.client, task_queue=CRAWL_QUEUE, activities=crawl),
            ):
                handle = await env.client.start_workflow(
                    CrawlWorkflow.run,
                    input or crawl_input(),
                    id=workflow_id(uuid.uuid4()),
                    task_queue=WORKFLOW_QUEUE,
                    execution_timeout=timedelta(days=1),
                )
                if during:
                    await during(handle)
                try:
                    return await handle.result()
                except WorkflowFailureError as exc:
                    return exc
                finally:
                    # The replay corpus records from here (tests/replay/record_harness.py).
                    self.history = await handle.fetch_history()
                    first = env.client.get_workflow_handle(
                        handle.id, run_id=handle.first_execution_run_id
                    )
                    self.first_run = await first.describe()
                    self.first_history = await first.fetch_history()


SITE = {
    SEED: [u("a"), u("b")],
    u("a"): [u("c"), SEED],
    u("b"): [u("c")],
    u("c"): [],
}


# ---------------------------------------------------------------------------
# The frontier
# ---------------------------------------------------------------------------


async def test_crawl_runs_until_the_frontier_is_empty():
    f = Frontier(links=SITE)
    h = Harness(f)
    assert await h.run() == "completed"

    assert f.started == 1
    assert sorted(h.scraped) == sorted([SEED, u("a"), u("b"), u("c")])
    assert set(f.statuses().values()) == {"completed"}
    assert f.finishes == [FinishCrawlInput(crawl_id="c1", status="completed")]
    # Every page through its host's slot.
    assert sorted(h.acquired) == sorted(h.scraped)
    # The seed through admission too, at depth 0.
    assert (f.admits[0].urls, f.admits[0].source, f.admits[0].depth) == ([SEED], "seed", 0)
    assert {(a.source, a.depth) for a in f.admits[1:]} <= {("link", 1), ("link", 2)}


async def test_links_are_not_followed_past_max_depth():
    f = Frontier(links=SITE)
    h = Harness(f)
    await h.run(crawl_input(max_depth=1))

    # Depth-1 pages are fetched but not read for links.
    assert sorted(h.scraped) == sorted([SEED, u("a"), u("b")])
    assert h.extracted == [SEED]


async def test_never_more_than_max_in_flight_pages():
    f = Frontier(links={SEED: [u(str(i)) for i in range(9)]})
    h = Harness(f)
    await h.run(crawl_input(max_in_flight=2))

    assert len(h.scraped) == 10
    assert h.max_scraping == 2
    assert all(c.limit <= 2 for c in f.claims)


async def test_claims_tell_the_engine_what_is_in_flight():
    f = Frontier(links={SEED: [u(str(i)) for i in range(5)]})
    await Harness(f).run(crawl_input(max_in_flight=2))
    # A claim never asks for more than the free slots, and names the pages it still holds.
    assert all(len(c.in_flight) + c.limit <= 2 for c in f.claims)
    assert any(c.in_flight for c in f.claims)


async def test_continue_as_new_every_pages_per_run():
    f = Frontier(links={SEED: [u(str(i)) for i in range(6)]})
    h = Harness(f)
    assert await h.run(crawl_input(pages_per_run=2, max_in_flight=1)) == "completed"

    assert h.first_run.status == WorkflowExecutionStatus.CONTINUED_AS_NEW
    # The seed is admitted once and the crawl started once, across every run.
    assert f.started == 1
    assert [a.source for a in f.admits].count("seed") == 1
    assert sorted(h.scraped) == sorted([SEED] + [u(str(i)) for i in range(6)])
    assert len(f.finishes) == 1


async def test_a_failed_scrape_fails_its_page_and_the_crawl_goes_on():
    async def scrape(url):
        if url == u("a"):
            raise ApplicationError("blocked:akamai", type="Blocked", non_retryable=True)

    f = Frontier(links=SITE)
    h = Harness(f, scrape=scrape)
    assert await h.run() == "completed"

    outcomes = {f.pages[p].url: (o.status, o.error) for p, o in f.outcomes.items()}
    assert outcomes[u("a")] == ("failed", "blocked:akamai")
    assert outcomes[u("c")] == ("completed", None)  # still reached through b
    assert u("a") not in h.extracted


async def test_a_link_extraction_failure_keeps_the_page():
    async def extract(url):
        if url == SEED:
            raise ApplicationError("minio down", non_retryable=True)

    f = Frontier(links=SITE)
    h = Harness(f, extract=extract)
    assert await h.run() == "completed"
    assert f.statuses() == {SEED: "completed"}


async def test_a_refused_seed_fails_the_crawl():
    f = Frontier(links=SITE, refused={SEED})
    h = Harness(f)
    result = await h.run()

    assert isinstance(result, WorkflowFailureError)
    assert f.finishes == [
        FinishCrawlInput(crawl_id="c1", status="failed", error="the seed URL was refused: ssrf")
    ]
    assert h.scraped == []


async def test_an_accounting_failure_fails_the_crawl():
    async def record(input):
        raise ApplicationError("db down", non_retryable=True)

    f = Frontier(links=SITE)
    result = await Harness(f, record=record).run()

    assert isinstance(result, WorkflowFailureError)
    assert f.finishes == [FinishCrawlInput(crawl_id="c1", status="failed", error=ACCOUNTING_FAILED)]


# ---------------------------------------------------------------------------
# The storage wall (ADR-009 §8d, per page)
# ---------------------------------------------------------------------------


async def test_the_storage_wall_stops_the_crawl_and_keeps_what_crossed_it():
    f = Frontier(links={SEED: [u(str(i)) for i in range(9)]}, storage_full_after=3)
    h = Harness(f)
    assert await h.run(crawl_input(max_in_flight=2)) == "completed"

    # Pages in flight when the wall was hit finished and were recorded; nothing new was claimed.
    assert len(f.outcomes) <= 4
    assert set(f.statuses().values()) == {"completed"}
    assert f.finishes == [
        FinishCrawlInput(crawl_id="c1", status="completed", error=STORAGE_LIMIT_NOTE)
    ]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_a_cancel_lets_the_pages_in_flight_finish_and_claims_no_more():
    scraping, release = asyncio.Event(), asyncio.Event()

    async def scrape(url):
        if url != SEED:
            scraping.set()
            await release.wait()

    f = Frontier(links={SEED: [u(str(i)) for i in range(6)]})

    async def cancel(handle):
        await scraping.wait()
        f.state = "cancelled"  # the API cancels the row first
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(f, scrape=scrape)
    result = await h.run(crawl_input(max_in_flight=2), during=cancel)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, CancelledError)
    # Seed + the two in flight; each in-flight page recorded once (the fake keeps the first).
    assert len(h.scraped) == 3
    assert f.finishes == [FinishCrawlInput(crawl_id="c1", status="cancelled")]
    claims_after = [c for c in f.claims if c.in_flight]
    assert all(c.limit < 2 for c in claims_after)


async def test_a_row_cancelled_without_the_engine_stops_at_the_next_claim():
    async def scrape(url):
        if url == SEED:
            f.state = "cancelled"

    f = Frontier(links=SITE)
    h = Harness(f, scrape=scrape)
    # The workflow ends normally: the crawl is cancelled, not the workflow.
    assert await h.run() == "cancelled"
    assert h.scraped == [SEED]
    assert f.finishes == [FinishCrawlInput(crawl_id="c1", status="cancelled")]


async def test_a_cancel_during_the_host_wait_records_the_page_cancelled():
    f = Frontier(links=SITE)
    h = Harness(f)
    busy = HostSlot(domain="site.example", wait_ms=60_000)

    db, scrape, crawl = h.activities()

    @activity.defn(name=ACQUIRE_HOST_SLOT_ACTIVITY)
    async def acquire(input: HostSlotInput) -> HostSlot:
        h.acquired.append(input.url)
        return busy

    db = [a for a in db if activity._Definition.from_callable(a).name != ACQUIRE_HOST_SLOT_ACTIVITY]
    db.append(acquire)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[CrawlWorkflow], activities=db),
            Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=scrape),
            Worker(env.client, task_queue=CRAWL_QUEUE, activities=crawl),
        ):
            handle = await env.client.start_workflow(
                CrawlWorkflow.run,
                crawl_input(),
                id=workflow_id(uuid.uuid4()),
                task_queue=WORKFLOW_QUEUE,
            )
            while not h.acquired:
                await asyncio.sleep(0.01)
            f.state = "cancelled"
            await handle.cancel()
            try:
                await handle.result()
            except WorkflowFailureError:
                pass

    assert h.scraped == []
    assert [o.status for o in f.outcomes.values()] == ["cancelled"]
    assert f.finishes == [FinishCrawlInput(crawl_id="c1", status="cancelled")]


# ---------------------------------------------------------------------------
# Sitemaps: every file vetted before it is fetched (ADR-010 §2)
# ---------------------------------------------------------------------------

BAIT = "http://169.254.169.254/latest/meta-data/"


def _sitemaps():
    return {
        "robots": [u("index.xml"), BAIT],
        u("index.xml"): FetchSitemapOutput(kind="index", locs=[u("s1.xml"), u("moved.xml")]),
        u("s1.xml"): FetchSitemapOutput(kind="urlset", locs=[u("p1"), u("p2")]),
        u("moved.xml"): FetchSitemapOutput(kind="redirect", location=u("s2.xml")),
        u("s2.xml"): FetchSitemapOutput(kind="urlset", locs=[u("p3"), BAIT]),
    }


async def test_sitemaps_are_expanded_through_the_gate():
    f = Frontier(links={SEED: []}, refused={BAIT})
    h = Harness(f, sitemaps=_sitemaps())
    assert await h.run(crawl_input(ignore_sitemap=False)) == "completed"

    assert BAIT not in h.fetched
    assert h.fetched == [u("index.xml"), u("s1.xml"), u("moved.xml"), u("s2.xml")]
    assert sorted(h.scraped) == sorted([SEED, u("p1"), u("p2"), u("p3")])
    sitemap_admits = [a for a in f.admits if a.source == "sitemap"]
    assert {a.depth for a in sitemap_admits} == {1}
    # The bait as an entry reaches admission, which refuses it.
    assert any(BAIT in a.urls for a in sitemap_admits)


async def test_ignore_sitemap_skips_discovery():
    h = Harness(Frontier(links={SEED: []}), sitemaps=_sitemaps())
    await h.run(crawl_input(ignore_sitemap=True))
    assert (h.discovered, h.fetched) == (0, [])


async def test_sitemap_expansion_is_bounded():
    # An index that names itself and a fresh child every time: bounded by depth and file count.
    sitemaps = {"robots": [u("loop.xml")]}
    for i in range(100):
        sitemaps[u("loop.xml") if i == 0 else u(f"loop{i}.xml")] = FetchSitemapOutput(
            kind="index", locs=[u(f"loop{j}.xml") for j in range(i + 1, i + 30)]
        )
    h = Harness(Frontier(links={SEED: []}), sitemaps=sitemaps)
    assert await h.run(crawl_input(ignore_sitemap=False)) == "completed"
    assert len(h.fetched) <= MAX_SITEMAP_FILES


async def test_history_size_per_page_is_within_budget():
    f = Frontier(links={SEED: [u(str(i)) for i in range(19)]})
    h = Harness(f)
    await h.run(crawl_input(pages_per_run=1000))
    events = len(h.history.events)
    per_page = events / 20
    # ADR-009 13b: pages_per_run (100) x per-page cost must stay far below 51,200 events.
    assert per_page < 60, per_page
    assert not any(
        e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
        for e in h.history.events
    )
