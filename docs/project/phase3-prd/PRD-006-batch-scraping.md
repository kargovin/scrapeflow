# PRD-006 — Batch Scraping

**Priority:** P2
**Source:** NEW — identified from firecrawl (`POST /batch/scrape`) and crawl4ai (`arun_many()`)
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

ScrapeFlow currently processes one URL per job. A user who wants to scrape 50 product pages, or monitor a list of competitor URLs for changes, must create 50 separate jobs, poll 50 job IDs, and assemble results manually. Both firecrawl and crawl4ai treat multi-URL batch processing as a primary primitive — not an afterthought.

The absence of batch scraping is the most significant capability gap between ScrapeFlow and its direct competitors for ML pipeline use cases.

---

## Goals

1. Allow a user to submit a list of URLs in a single API call and get back a single batch job ID.
2. Individual URLs within a batch are scraped concurrently (up to a configurable worker concurrency limit).
3. The user can poll the batch job for overall progress and retrieve results per-URL.
4. Webhook fires once when the entire batch completes (not on each individual URL).
5. Each URL in the batch uses the same scrape options (output format, engine, etc.) — per-URL option overrides are Phase 4.

---

## Non-goals

- Per-URL option overrides within a batch (all URLs share the same job config)
- Streaming results as individual URLs complete (Phase 4 — see PRD-014 WebSocket for the streaming future)
- Batch size > 500 URLs (Phase 3 limit; larger batches are a Phase 4 concern with pagination)
- Resumable batches (if a batch job is cancelled, it cannot be resumed)

---

## User stories

**As a user** building a price-monitoring pipeline, I want to submit a list of 200 product URLs in one API call and get back a single job ID I can poll for completion.

**As a user**, I want to see which URLs in my batch succeeded, which failed, and retrieve each result independently by URL.

**As a user**, I want the same webhook I configured on the job to fire once when all URLs in the batch are done, with a summary payload.

---

## Requirements

### New API endpoint

`POST /batch`

Request body:
```json
{
  "urls": ["https://example.com/page1", "https://example.com/page2"],
  "output_format": "markdown",
  "engine": "http",
  "webhook_url": "https://my-service.com/webhook",
  "respect_robots": true
}
```

Constraints:
- `urls`: 1–500 items; each must pass URL validation and SSRF check
- All other fields: same validation as `POST /jobs`
- Returns `{"batch_id": "uuid", "status": "queued", "total_urls": N}`

### Data model

The Architect will decide the exact schema, but the PM requires these semantics:

- A **batch** is a parent entity tied to a user
- Each URL in the batch creates a **batch item** (child) that tracks: `url`, `status`, `result_path`, `error`, `run_id`
- The batch has aggregate fields: `total`, `completed`, `failed`, `status` (queued/running/completed/partial_failure)
- Batch items reuse or reference the existing `job_runs` machinery where possible — the Architect decides whether a batch item IS a job_run or LINKS to one

### Worker dispatch

- Each URL in the batch is dispatched as an individual NATS message (same subject as a regular job run)
- The dispatch message includes a `batch_id` and `batch_item_id` so the result consumer can update the correct batch item
- Workers do not need to know they are processing a batch — the result consumer handles aggregation

### Status endpoint

`GET /batch/{batch_id}`

Response:
```json
{
  "batch_id": "uuid",
  "status": "running",
  "total": 50,
  "completed": 32,
  "failed": 3,
  "created_at": "...",
  "items": [
    {"url": "https://...", "status": "completed", "result_url": "/batch/{id}/items/{item_id}/result"},
    {"url": "https://...", "status": "failed", "error": "timeout"}
  ]
}
```

### Result retrieval

`GET /batch/{batch_id}/items/{item_id}/result` — returns the scraped content for a single URL (same format as `GET /jobs/{id}/result` today)

### Cancellation

`DELETE /batch/{batch_id}` — cancels all in-flight batch items (same semantics as cancelling individual job runs; result consumer discards results for cancelled items)

### Webhook

When all batch items reach a terminal state (completed or failed), fire the batch-level webhook with:
```json
{
  "event": "batch.completed",
  "batch_id": "...",
  "total": 50,
  "completed": 47,
  "failed": 3
}
```

### Rate limiting

Batch submissions count against the per-user rate limit. A batch of 50 URLs consumes 50 quota units (not 1). Enforce at `POST /batch` time, rejecting if the user's remaining quota is less than `len(urls)`.

### Batch size limit

- Default maximum: 500 URLs per batch
- Configurable via env `MAX_BATCH_SIZE` (default 500)
- Return 422 if exceeded

---

## Success criteria

- [ ] `POST /batch` with 10 URLs returns a batch_id and begins processing
- [ ] `GET /batch/{id}` shows accurate counts as URLs complete
- [ ] Results are retrievable per-URL via the item endpoint
- [ ] Webhook fires exactly once when all items reach terminal state
- [ ] `DELETE /batch/{id}` stops in-flight items; already-completed results are preserved
- [ ] Submitting 501 URLs returns 422 with a clear error message
- [ ] Rate limit deducts N credits for N URLs at submission time
- [ ] End-to-end test: 10 URLs batch completes fully; 3-URL batch with 1 failing URL shows `partial_failure` status

---

## Open questions for Architect

1. Should batches be represented as a new `batches` + `batch_items` table pair, or should a batch be modeled as a special `jobs` row with a `job_runs` entry per URL? The latter reuses more existing machinery but may make the data model confusing.
2. Should `POST /batch` validate and SSRF-check all 500 URLs synchronously in the request handler, or dispatch validation asynchronously (accepting the job first, failing items individually)? Synchronous validation on 500 URLs could add 2–5 seconds of latency to the request.
3. Should batch items share a single NATS consumer group with regular single-URL jobs, or use a separate subject (e.g. `scrapeflow.jobs.run.batch.http`)? Separate subjects would allow workers to prioritize or rate-limit batch work independently.
