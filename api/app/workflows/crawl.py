import asyncio
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.workflows import steps
    from app.workflows.activities.contracts import (
        DISCOVER_SITEMAPS_ACTIVITY,
        EXTRACT_LINKS_ACTIVITY,
        FETCH_SITEMAP_ACTIVITY,
        DiscoverSitemapsInput,
        DiscoverSitemapsOutput,
        ExtractLinksInput,
        ExtractLinksOutput,
        FetchSitemapInput,
        FetchSitemapOutput,
        ScrapeInput,
        ScrapeOptions,
        StoredObject,
    )
    from app.workflows.queues import CRAWL_QUEUE

CRAWL_STARTED_ACTIVITY = "crawl_started"
ADMIT_URLS_ACTIVITY = "admit_urls"
VET_URLS_ACTIVITY = "vet_urls"
CLAIM_CRAWL_PAGES_ACTIVITY = "claim_crawl_pages"
RECORD_CRAWL_PAGE_ACTIVITY = "record_crawl_page"
FINISH_CRAWL_ACTIVITY = "finish_crawl"

# ADR-010 §2: sitemap indexes are expanded as indexes, within bounds; redirects are re-vetted.
MAX_SITEMAP_FILES = 20
MAX_SITEMAP_INDEX_DEPTH = 2
MAX_SITEMAP_REDIRECTS = 3

STORAGE_LIMIT_NOTE = "storage limit reached: the crawl stopped early"
# v1's error for the same failure (the result consumer's).
ACCOUNTING_FAILED = "storage_accounting_failed"

# How a URL reached the frontier; admission's rules depend on it.
Source = Literal["seed", "link", "sitemap", "sitemap_file"]


def workflow_id(crawl_id: object) -> str:
    return f"crawl-{crawl_id}"


class CrawlInput(BaseModel):
    crawl_id: str
    seed_url: str
    engine: steps.Engine
    max_depth: int
    max_pages: int
    respect_robots: bool
    ignore_sitemap: bool
    # E.0: pages in flight at once.
    max_in_flight: int
    # ADR-009 13b: pages per run before continue-as-new (history size).
    pages_per_run: int
    # Set on every run after the first: the crawl is running and its seed admitted.
    continued: bool = False


class CrawlRef(BaseModel):
    crawl_id: str


class UrlBatch(BaseModel):
    crawl_id: str
    urls: list[str]
    source: Source
    # The depth admitted URLs get.
    depth: int = 0


class Vetted(BaseModel):
    urls: list[str] = []


class Admitted(BaseModel):
    """The crawl's status after the call, and what this call admitted and refused."""

    state: str
    admitted: int = 0
    dropped: dict[str, int] = {}


class ClaimInput(BaseModel):
    crawl_id: str
    limit: int
    # Pages this run is fetching: a claimed page not among them is handed back (a retried claim).
    in_flight: list[str] = []


class ClaimedPage(BaseModel):
    page_id: str
    url: str
    depth: int


class Claim(BaseModel):
    state: str
    pages: list[ClaimedPage] = []
    # The owner is at the storage limit: no page was claimed (ADR-009 §8d, per page for crawls).
    storage_full: bool = False


class RecordCrawlPageInput(BaseModel):
    crawl_id: str
    page_id: str
    status: Literal["completed", "failed", "cancelled"]
    objects: list[StoredObject] = []
    error: str | None = None


class PageRecorded(BaseModel):
    state: str
    storage_full: bool = False


class FinishCrawlInput(BaseModel):
    crawl_id: str
    status: Literal["completed", "failed", "cancelled"]
    error: str | None = None


async def _crawl_activity(activity: str, arg: BaseModel, result_type: type) -> Any:
    return await workflow.execute_activity(
        activity,
        arg,
        task_queue=CRAWL_QUEUE,
        result_type=result_type,
        schedule_to_start_timeout=steps.SCHEDULE_TO_START,
        start_to_close_timeout=timedelta(seconds=60),
        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3),
    )


@workflow.defn
class CrawlWorkflow:
    """A crawl on the v2 lane: BFS over a frontier held in Postgres (ADR-009 13b).

    The body never reads the frontier itself: ``claim_crawl_pages`` hands it the next pages, at
    most ``max_in_flight`` in flight, each scraped inside its host's slot and charged per page.
    Extracted links and the seed's sitemaps go back through ``admit_urls``, the one gate every URL
    passes (13d). Ends when the frontier is empty, the owner hits the storage limit (the page that
    crossed it is kept and charged), or the API cancels the row; continues as new every
    ``pages_per_run`` pages.
    """

    def __init__(self) -> None:
        self._in_flight: dict[str, asyncio.Task] = {}
        self._finished = 0
        self._cancel_requested = False
        # Why no more pages are claimed: the storage note, or "stopped" (the row is terminal).
        self._stop: str | None = None
        self._failure: BaseException | None = None

    @workflow.run
    async def run(self, input: CrawlInput) -> str:
        try:
            outcome = await self._crawl(input)
        except workflow.NondeterminismError:
            raise
        except asyncio.CancelledError:
            self._cancel_requested = True
            await self._drain()
            outcome = ("cancelled", None)
        except Exception as exc:
            # Left to propagate it would fail the workflow task, retried forever: the crawl would
            # stay running and hold its slot.
            await self._drain()
            error = exc.message if isinstance(exc, ApplicationError) else None
            outcome = ("failed", error or f"internal error: {type(exc).__name__}: {exc}")
        if outcome is None:
            workflow.continue_as_new(input.model_copy(update={"continued": True}))
        status, error = outcome
        state = await self._db(
            FINISH_CRAWL_ACTIVITY,
            FinishCrawlInput(crawl_id=input.crawl_id, status=status, error=error),
            str,
        )
        if state == "cancelled" and self._cancel_requested:
            raise asyncio.CancelledError()
        if state == "failed":
            raise ApplicationError(error or "the crawl failed", non_retryable=True)
        return state

    async def _crawl(self, input: CrawlInput) -> tuple[str, str | None] | None:
        """The crawl's outcome for ``finish_crawl``; None = continue as new."""
        ref = CrawlRef(crawl_id=input.crawl_id)
        if not input.continued:
            state = await self._db(CRAWL_STARTED_ACTIVITY, ref, str)
            if state != "running":
                # Cancelled before it started, or deleted: finish_crawl leaves it as it is.
                return "cancelled", None
            seed = await self._admit(input, [input.seed_url], "seed", 0)
            if seed.admitted == 0 and seed.dropped:
                return "failed", f"the seed URL was refused: {', '.join(sorted(seed.dropped))}"

        while True:
            self._raise_if_cancelled()
            if self._failure is not None:
                raise self._failure
            free = input.max_in_flight - len(self._in_flight)
            if self._stop is None and not self._run_full(input) and free > 0:
                claim: Claim = await self._db(
                    CLAIM_CRAWL_PAGES_ACTIVITY,
                    ClaimInput(
                        crawl_id=input.crawl_id, limit=free, in_flight=list(self._in_flight)
                    ),
                    Claim,
                )
                self._raise_if_cancelled()
                if claim.state != "running":
                    self._stop = "stopped"
                elif claim.storage_full:
                    self._stop = STORAGE_LIMIT_NOTE
                for page in claim.pages:
                    if page.page_id not in self._in_flight:
                        self._in_flight[page.page_id] = asyncio.create_task(self._page(input, page))
            if not self._in_flight:
                # Nothing claimed and nothing left to finish: the frontier is empty, or stopped.
                if self._stop is None and self._run_full(input):
                    return None
                break
            done = self._finished
            await workflow.wait_condition(lambda done=done: self._finished > done)

        if self._failure is not None:
            raise self._failure
        if self._stop == "stopped":
            return "cancelled", None
        return "completed", self._stop

    def _run_full(self, input: CrawlInput) -> bool:
        return (
            self._finished >= input.pages_per_run or workflow.info().is_continue_as_new_suggested()
        )

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested:
            raise asyncio.CancelledError()

    async def _page(self, input: CrawlInput, page: ClaimedPage) -> None:
        try:
            scrape_input = ScrapeInput(
                artifact_id=page.page_id,
                url=page.url,
                # Always HTML: links are extracted from the stored page (v1's rule).
                output_format="html",
                options=ScrapeOptions(respect_robots=input.respect_robots),
            )
            try:
                held = await steps.wait_for_host_slot(
                    page.url, steps.host_lease_ttl(scrape_input, input.engine)
                )
            except asyncio.CancelledError:
                # Cancelled while waiting for the host: nothing was fetched.
                await self._through_cancel(self._record(input, page, "cancelled"))
                return
            await self._through_cancel(self._fetch(input, page, scrape_input, held))
        except Exception as exc:
            self._failure = self._failure or exc
        finally:
            self._in_flight.pop(page.page_id, None)
            self._finished += 1

    async def _fetch(
        self,
        input: CrawlInput,
        page: ClaimedPage,
        scrape_input: ScrapeInput,
        held: steps.HostSlotRelease | None,
    ) -> None:
        try:
            scraped = await steps.scrape(scrape_input, input.engine)
        except ActivityError as exc:
            await self._record(input, page, "failed", error=steps.activity_error(exc))
            return
        finally:
            await steps.release_host_slot(held)
        recorded = await self._record(
            input, page, "completed", objects=[scraped.result, *scraped.screenshots]
        )
        if recorded.state != "running" or self._cancel_requested:
            return
        if page.depth < input.max_depth:
            await self._follow_links(input, page, scraped.result.path)
        if page.depth == 0 and not input.ignore_sitemap:
            await self._follow_sitemaps(input)

    async def _record(
        self,
        input: CrawlInput,
        page: ClaimedPage,
        status: str,
        objects: list[StoredObject] | None = None,
        error: str | None = None,
    ) -> PageRecorded:
        try:
            recorded: PageRecorded = await steps.db(
                RECORD_CRAWL_PAGE_ACTIVITY,
                RecordCrawlPageInput(
                    crawl_id=input.crawl_id,
                    page_id=page.page_id,
                    status=status,
                    objects=objects or [],
                    error=error,
                ),
                PageRecorded,
            )
        except ActivityError:
            raise ApplicationError(ACCOUNTING_FAILED, non_retryable=True) from None
        if recorded.state != "running":
            self._stop = "stopped"
        elif recorded.storage_full and self._stop is None:
            self._stop = STORAGE_LIMIT_NOTE
        return recorded

    async def _follow_links(self, input: CrawlInput, page: ClaimedPage, path: str) -> None:
        try:
            out: ExtractLinksOutput = await _crawl_activity(
                EXTRACT_LINKS_ACTIVITY,
                ExtractLinksInput(path=path, base_url=page.url),
                ExtractLinksOutput,
            )
        except ActivityError as exc:
            # The page is kept; only what it links to is lost.
            workflow.logger.warning("Link extraction failed for %s: %s", page.url, exc.cause)
            return
        if out.links:
            await self._admit(input, out.links, "link", page.depth + 1)

    async def _follow_sitemaps(self, input: CrawlInput) -> None:
        """The seed's sitemaps, every file vetted before it is fetched; entries admitted at depth 1."""
        try:
            found: DiscoverSitemapsOutput = await _crawl_activity(
                DISCOVER_SITEMAPS_ACTIVITY,
                DiscoverSitemapsInput(seed_url=input.seed_url),
                DiscoverSitemapsOutput,
            )
        except ActivityError as exc:
            workflow.logger.warning(
                "Sitemap discovery failed for %s: %s", input.seed_url, exc.cause
            )
            return
        # (url, index depth, redirect hops)
        queue = [(url, 0, 0) for url in await self._vet(input, found.sitemaps)]
        fetched = 0
        entries = 0
        while queue and fetched < MAX_SITEMAP_FILES and entries < input.max_pages:
            if self._stop is not None or self._cancel_requested:
                return
            url, depth, hops = queue.pop(0)
            fetched += 1
            try:
                out: FetchSitemapOutput = await _crawl_activity(
                    FETCH_SITEMAP_ACTIVITY,
                    FetchSitemapInput(url=url, max_entries=input.max_pages - entries),
                    FetchSitemapOutput,
                )
            except ActivityError as exc:
                workflow.logger.warning("Sitemap %s not read: %s", url, exc.cause)
                continue
            if out.kind == "redirect" and out.location and hops < MAX_SITEMAP_REDIRECTS:
                queue += [(loc, depth, hops + 1) for loc in await self._vet(input, [out.location])]
            elif out.kind == "index" and depth < MAX_SITEMAP_INDEX_DEPTH:
                queue += [(loc, depth + 1, 0) for loc in await self._vet(input, out.locs)]
            elif out.kind == "urlset" and out.locs:
                entries += len(out.locs)
                await self._admit(input, out.locs, "sitemap", 1)
        if queue:
            workflow.logger.info("Sitemap expansion stopped with %d file(s) unread", len(queue))

    async def _vet(self, input: CrawlInput, urls: list[str]) -> list[str]:
        if not urls:
            return []
        vetted: Vetted = await self._db(
            VET_URLS_ACTIVITY,
            UrlBatch(crawl_id=input.crawl_id, urls=urls, source="sitemap_file"),
            Vetted,
        )
        return vetted.urls

    async def _admit(
        self, input: CrawlInput, urls: list[str], source: Source, depth: int
    ) -> Admitted:
        admitted: Admitted = await self._db(
            ADMIT_URLS_ACTIVITY,
            UrlBatch(crawl_id=input.crawl_id, urls=urls, source=source, depth=depth),
            Admitted,
        )
        if admitted.state != "running":
            self._stop = "stopped"
        return admitted

    async def _drain(self) -> None:
        """Ends the host waits; a page past its wait finishes, and its record drops what it got."""
        tasks = list(self._in_flight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _through_cancel(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Awaits ``coro`` to completion even if cancelled meanwhile."""
        task = asyncio.ensure_future(coro)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    raise
        return task.result()

    async def _db(self, activity: str, arg: BaseModel, result_type: type) -> Any:
        """An app-DB activity that always finishes; a workflow cancel meanwhile is noted."""
        task = asyncio.ensure_future(steps.db(activity, arg, result_type))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    raise
                self._cancel_requested = True
        return task.result()
