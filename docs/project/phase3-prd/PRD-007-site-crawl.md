# PRD-007 — Site Crawl: Multi-page Crawl from Seed URL

**Priority:** P2
**Source:** NEW — identified from firecrawl (`POST /crawl`) and crawl4ai (BFS/DFS/BestFirst deep crawl strategies)
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

ScrapeFlow scrapes one URL at a time. A user who wants to extract all product listings from an e-commerce site, build a full-text index of a documentation site, or monitor all pages of a competitor's blog must manually enumerate URLs. Firecrawl and crawl4ai both treat whole-site crawling as their headline feature. Without it, ScrapeFlow cannot serve a meaningful slice of the "feed a website into my ML pipeline" use case.

---

## Goals

1. Given a seed URL, ScrapeFlow discovers and scrapes all reachable pages within the same domain, up to a configurable depth and page count limit.
2. URL discovery uses both link following (href extraction from scraped HTML) and optional sitemap parsing.
3. Each discovered page is scraped with the same options as the seed job (output format, engine, etc.).
4. Results are stored per-URL and retrievable individually or as a paginated list.
5. The crawl can be cancelled in-flight.

---

## Non-goals

- Cross-domain crawling (only pages under the same origin as the seed URL)
- Strategy selection (BFS vs DFS vs best-first) — Phase 3 uses BFS only; strategy selection is Phase 4
- Adaptive crawling with convergence detection (crawl4ai feature; Phase 4)
- Concurrent crawls of the same domain from the same user (only one active crawl per seed domain per user at a time — prevents runaway crawls)
- Resumable crawls after cancellation

---

## User stories

**As a user** building a documentation chatbot, I want to submit my docs site URL and have ScrapeFlow crawl all pages and return them as Markdown — so I can feed the entire knowledge base into my LLM.

**As a user** monitoring a competitor's blog, I want to set up a scheduled crawl of their blog index so I get all new posts automatically.

**As a user**, I want to limit a crawl to pages under `/blog/` only, so I don't accidentally crawl the entire site.

**As a platform operator**, I want a global maximum page count per crawl to prevent runaway crawls consuming all worker capacity.

---

## Requirements

### New API endpoint

`POST /crawls`

Request body:
```json
{
  "url": "https://example.com",
  "max_depth": 3,
  "max_pages": 100,
  "include_paths": ["/blog/", "/docs/"],
  "exclude_paths": ["/login", "/admin", "/api/"],
  "ignore_sitemap": false,
  "output_format": "markdown",
  "engine": "http",
  "webhook_url": "https://...",
  "respect_robots": true
}
```

Field constraints:
- `max_depth`: 1–10, default 3
- `max_pages`: 1–1000, default 100; operator-configurable ceiling via `MAX_CRAWL_PAGES` env (default 1000)
- `include_paths`: optional allow-list; if set, only URLs whose path starts with one of these prefixes are followed
- `exclude_paths`: optional block-list; URLs matching are not enqueued
- `ignore_sitemap`: if false (default), seed URL's domain sitemap is checked for URL discovery in addition to link following

Returns: `{"crawl_id": "uuid", "status": "queued", "seed_url": "https://example.com"}`

### Crawl engine behavior (BFS)

1. Enqueue seed URL
2. Scrape the page (using existing worker machinery)
3. Extract all `<a href>` links from the result; filter to same-origin, include/exclude path rules
4. Deduplicate against already-visited URLs (per crawl, in Redis or DB)
5. Enqueue new URLs if `depth < max_depth` and `total_enqueued < max_pages`
6. Repeat until queue is empty or limits reached

Sitemap discovery (when `ignore_sitemap = false`):
- Fetch `{origin}/robots.txt` → extract `Sitemap:` directive URLs
- Fetch each sitemap XML; extract `<loc>` entries
- Add to the crawl queue if they pass include/exclude and depth rules
- Sitemap URLs all get `depth = 1` (treated as direct children of seed)

### Concurrency

- Pages within a crawl are dispatched as individual NATS messages (same worker infrastructure as single-URL jobs)
- Crawl coordinator (in the API) manages the BFS queue and dispatches pages as workers become available
- One active crawl per user per seed domain (enforced at `POST /crawls` time; returns 409 if violated)

### Data model

The Architect decides the schema. PM requirements:
- A crawl has: `crawl_id`, `user_id`, `seed_url`, `status`, `max_depth`, `max_pages`, `total_queued`, `total_completed`, `total_failed`, `created_at`, `completed_at`
- Each crawled page has: `url`, `depth`, `status`, `result_path`, `error`
- Results per page are stored in MinIO (same path convention as existing job runs)

### Status endpoint

`GET /crawls/{crawl_id}` — aggregate status + summary counts

`GET /crawls/{crawl_id}/pages` — paginated list of crawled pages with per-page status
- Pagination: cursor-based, 50 per page
- Filter by status: `?status=failed`

`GET /crawls/{crawl_id}/pages/{page_id}/result` — content for a single crawled page

### Cancellation

`DELETE /crawls/{crawl_id}` — stops dispatching new pages; in-flight pages complete; result is stored

### Webhook

When crawl reaches terminal state (all pages done or cancelled):
```json
{
  "event": "crawl.completed",
  "crawl_id": "...",
  "seed_url": "...",
  "total_pages": 87,
  "completed": 84,
  "failed": 3
}
```

### Scheduling integration

A crawl can be scheduled (same `schedule_cron` mechanism as regular jobs). Each scheduled trigger starts a new crawl from the seed URL with the same config.

### Rate limiting

A crawl of `N` pages consumes `N` quota units. Enforce at dispatch time per page (not upfront, since the total is unknown). When the user's quota is exhausted mid-crawl, the crawl pauses with status `quota_exceeded`; remaining pages are not dispatched.

---

## Success criteria

- [ ] `POST /crawls` with a 10-page documentation site crawls all reachable pages within `max_depth`
- [ ] `include_paths` correctly filters out pages outside the allowed prefixes
- [ ] `exclude_paths` skips configured paths
- [ ] Sitemap URL discovery adds pages not reachable via link following
- [ ] `GET /crawls/{id}` shows accurate counts in real time as pages complete
- [ ] Results are retrievable per-page
- [ ] Second `POST /crawls` to the same seed domain from the same user returns 409
- [ ] `DELETE /crawls/{id}` stops new dispatches; in-flight page results are preserved
- [ ] A crawl that hits `max_pages` stops cleanly with status `completed` (not an error)
- [ ] Scheduled crawl restarts from seed URL on each scheduled trigger

---

## Open questions for Architect

1. **BFS coordinator placement (elevated to BACKLOG.md Q-ARCH-2):** Who owns the BFS queue and dispatches next-page messages? The existing orchestration model (API as orchestrator, thin workers) works for single-URL jobs but this is the first feature requiring multi-step coordination. See `BACKLOG.md § Q-ARCH-2` for the three options (API background task / dedicated coordinator / workers self-enqueue) and the full trade-off table. This question must be answered before PRD-007 ADR work begins.

2. Where does the BFS queue itself live? Options: (a) Redis `ZSET` per `crawl_id` (score = depth, fast dequeue), (b) a `crawl_queue` Postgres table (durable, survives API restart, slower), (c) NATS subject per crawl (consistent with existing infra but adds per-crawl subject management). The right answer depends on the coordinator placement decision above.

3. How should crawl results be aggregated for download? A user who crawls a 1000-page site may want a single Markdown ZIP archive, not 1000 individual API calls. Is a `GET /crawls/{id}/export` endpoint (async ZIP generation) in scope for Phase 3 or Phase 4?
