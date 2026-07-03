# Playwright Primer — What It Is and How We Use It in ScrapeFlow

> **Audience:** Engineer assigned to Step 18 (Python Playwright worker).
> **Read this before writing a single line of code for `playwright-worker/`.**
> **Spec reference:** `docs/phase2/phase2-engineering-spec-v3.md §4.2`
> **Backlog entry:** `docs/project/PHASE2_BACKLOG.md → Step 18`

---

## Part 1 — Playwright Fundamentals

### Why Playwright Exists (The Problem)

When you use Python's `requests` library (or Go's `http.Get`), you get back whatever the server's HTML response contains — raw bytes, done. That is our Go HTTP worker, and it works fine for ~80% of sites.

The other ~20% are "JS-heavy" sites: React SPAs, Angular dashboards, infinite-scroll feeds, sites behind Cloudflare's JS challenge. When a browser hits these pages it:

1. Downloads a skeletal HTML shell (often just `<div id="root"></div>`)
2. Downloads and **executes** JavaScript bundles
3. The JavaScript makes XHR/fetch calls, receives data, and **renders** the real content into the DOM

A plain HTTP client never runs step 2 or 3. It sees the empty shell. Playwright solves this by driving a real browser engine (Chromium, Firefox, or WebKit) programmatically, letting all the JavaScript run, and then extracting the fully-rendered page.

---

### The Core Object Hierarchy

Playwright has three nested objects. Understanding them is essential because our isolation model depends on it:

```
Browser
  └── BrowserContext  (an isolated "incognito session")
        └── Page      (a browser tab)
```

**Browser** — the Chromium process. Shared across all workers. Created once at startup. Expensive to create; do it once.

**BrowserContext** — a complete, isolated browser profile: its own cookies, localStorage, cache, and session state. Think of it as a fresh incognito window. Creating a new context is cheap (milliseconds). **Every job in ScrapeFlow gets its own context.** This is why state from one scrape (cookies, auth tokens, cached responses) cannot leak into another.

**Page** — a browser tab inside a context. We create one page per job. `page.goto()`, `page.content()`, and `page.route()` all operate on this page.

The key rule: **never share a BrowserContext across jobs.** Always create one per job, and always close it in `finally`.

---

### Navigation and the Waiting Problem

Calling `page.goto(url)` tells the browser to start loading the page. But "loaded" means different things:

| Wait strategy | When it resolves | Use case |
|---|---|---|
| `load` | When the `load` event fires — all resources (images, scripts) downloaded | Default. Good for most sites. |
| `domcontentloaded` | When the DOM is parsed, before images/scripts complete | Faster. Use if you only need HTML structure, not data fetched by JS. |
| `networkidle` | When there are no network requests for 500ms | Slowest. Use only for SPAs that fetch all their data asynchronously after load. |

After `page.goto()` returns, we call `page.wait_for_load_state(strategy)` to confirm the page has reached the desired state. This two-step combination is the standard Playwright pattern.

**Why timeout matters:** If a site hangs, Playwright will wait forever by default. We always set a timeout. The spec bounds this between 5s and 300s (`timeout_seconds` in `PlaywrightOptions`). Playwright takes milliseconds, so `timeout_seconds * 1000` is the correct conversion.

---

### Extracting Content

After navigation:

```python
html = await page.content()    # returns the fully-rendered DOM as HTML string
final_url = page.url           # the URL after any redirects
```

`page.content()` returns the DOM as Playwright sees it after all JavaScript has run — this is the content a user would see if they View Source *after* the page loaded. This is what makes it valuable for JS-heavy sites.

---

### Resource Interception (`page.route`)

Playwright lets you intercept network requests before they're sent. We use this to abort image/font/CSS requests:

```python
await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}",
                 lambda r: r.abort())
```

This glob pattern matches any URL ending in those extensions. `r.abort()` tells the browser not to make the request. The result: pages load faster because we skip binary assets we don't need for content extraction.

This must be set up **before** `page.goto()` — routes registered after navigation has started do not apply retroactively.

---

### Async Model

Playwright's Python library (`playwright-async`) is fully async. Every browser operation is a coroutine:

```python
browser = await playwright.chromium.launch(headless=True)
context = await browser.new_context()
page    = await context.new_page()
await   page.goto(url, timeout=60_000)
html    = await page.content()
await   context.close()
```

This means the worker's entire I/O path — NATS message fetch, browser operations, MinIO upload, NATS publish — is a single async chain. No threads needed. The `asyncio` event loop handles concurrency between multiple simultaneous job coroutines.

---

### Concurrency: How Multiple Jobs Run in Parallel

We use a semaphore to cap simultaneous Playwright jobs:

```python
semaphore = asyncio.Semaphore(settings.playwright_max_workers)  # default 3

async def process_with_limit(msg, job):
    async with semaphore:
        await process_job(msg, job)
```

The pull consumer fetches a message, then `async with semaphore` blocks until a slot is available. Up to `PLAYWRIGHT_MAX_WORKERS` jobs run concurrently; the rest wait. This prevents the process from spawning unlimited browser contexts and exhausting memory.

Why 3 as the default (not more)? Each Chromium context uses ~50–100MB RAM. 3 contexts = ~150–300MB, safe on a modest machine. HTTP scraping has lower memory cost, which is why the Go worker pool is `runtime.NumCPU()`.

---

## Part 2 — How Playwright Fits Into ScrapeFlow

### When Does the Playwright Worker Get Invoked?

When a user creates a job with `"engine": "playwright"`:

```http
POST /jobs
{
  "url": "https://app.example.com/dashboard",
  "engine": "playwright",
  "output_format": "markdown",
  "playwright_options": {
    "wait_strategy": "networkidle",
    "timeout_seconds": 90,
    "block_images": true
  }
}
```

The API publishes the dispatch message to **`scrapeflow.jobs.run.playwright`** (not `.run.http`). The Playwright worker has a pull consumer on this subject. The Go worker is subscribed to `.run.http` and never sees Playwright messages. This routing is purely subject-based — no content inspection needed.

---

### The Dispatch Message the Worker Receives

```json
{
  "job_id": "uuid",
  "run_id": "uuid",
  "url": "https://app.example.com/dashboard",
  "output_format": "markdown",
  "playwright_options": {
    "wait_strategy": "networkidle",
    "timeout_seconds": 90,
    "block_images": true
  }
}
```

Both `job_id` and `run_id` are always present. `run_id` is how the result consumer knows which `job_runs` row to update. Always pass both through to every result message you publish.

---

### Per-Job Lifecycle (from spec §4.2)

This is the exact flow the worker must implement:

```
1. Publish  → result { status: "running" }           ← tells API the run has started
2. Create   → browser_context, page
3. (If block_images) Register route aborts
4. Navigate → page.goto(url, timeout)
             page.wait_for_load_state(wait_strategy)
5. Extract  → html = page.content(), final_url = page.url
6. Format   → html → markdown / json / raw (same logic as Go worker)
7. Upload   → MinIO: write latest/ AND history/ paths
8. Publish  → result { status: "completed", minio_path: "history/..." }
9. Ack      → msg.ack()                              ← ONLY after MinIO write succeeds
```

On failure (any exception):

```
Publish → result { status: "failed", error: str(e) }
Ack     → msg.ack()   ← still ack — NATS won't retry a failed job uselessly
```

And in **both** paths, in `finally`:

```python
await context.close()   # always, always, always
```

Missing the `finally` block is the most common Playwright bug. Without it, a context from a failed job leaks and holds memory until the process restarts.

---

### MinIO Path Convention (Same as Go Worker)

The Playwright worker writes to **two** MinIO paths per job:

| Path | Purpose | Overwritten? |
|---|---|---|
| `latest/{job_id}.{ext}` | Always has the most recent result | Yes — overwritten each run |
| `history/{job_id}/{unix_timestamp}.{ext}` | Immutable per-run record | Never |

The result message always reports the **`history/`** path:

```json
{ "minio_path": "history/abc-123/1710000000.md" }
```

This is what `job_runs.result_path` stores. The result consumer uses the `history/` path to compute diffs between consecutive runs. The `latest/` path is convenience access — "what did this job last return?" — but the history path is what enables change detection.

---

### The Ack Rule — Do Not Deviate

**Ack only after a successful MinIO upload.** Never before.

If you ack before MinIO write and then the upload fails:
- NATS considers the message delivered (it won't redeliver)
- The job run is permanently lost
- No error is published; no `job_runs` row is updated

If you ack after MinIO write but the ack call itself fails:
- NATS redelivers the message
- The worker runs the scrape again, overwrites MinIO paths, publishes a duplicate result
- The result consumer deduplicates by checking `run.status` — this is the safe path

Between these two failure modes, ack-after-write is the safe choice. The spec encodes this as a non-negotiable (`docs/adr/ADR-001-worker-job-contract.md §5`).

---

### The `playwright_options` Fields

| Field | Type | Default | What it controls |
|---|---|---|---|
| `wait_strategy` | `"load"` \| `"domcontentloaded"` \| `"networkidle"` | `"load"` | When navigation is considered complete |
| `timeout_seconds` | int, 5–300 | 60 | Max time before `TimeoutError` |
| `block_images` | bool | false | Whether to abort image/font/CSS requests |

These values are validated at the API layer before the message is published — the worker will always receive valid values. You do not need to re-validate them. If `playwright_options` is null (user sent an HTTP job routed incorrectly), treat missing fields as their defaults.

---

### What the Worker Must NOT Do

| Do not | Why |
|---|---|
| Touch Postgres directly | Workers are DB-ignorant by architectural contract (see `CLAUDE.md` key decisions table). All state lives in the API. |
| Share a `BrowserContext` across jobs | Session state (cookies, auth) would leak between tenants. |
| Ack before MinIO write | See above — permanent data loss on write failure. |
| Log the raw `playwright_options.api_key` or any user data | No secrets in logs. |
| Re-implement navigation without `wait_for_load_state` | `page.goto()` resolving does not mean the page content is ready. |

---

### Where This Worker Sits in the Larger Flow

```
User
  │  POST /jobs { engine: "playwright" }
  ▼
API (FastAPI)
  │  INSERT job_runs (status=pending)
  │  PUBLISH → scrapeflow.jobs.run.playwright
  ▼
Playwright Worker  ◄─── you are building this
  │  PULL from scrapeflow.jobs.run.playwright
  │  PUBLISH running → scrapeflow.jobs.result
  │  [browser renders page]
  │  WRITE → MinIO latest/ + history/
  │  PUBLISH completed → scrapeflow.jobs.result
  │  ACK message
  ▼
Result Consumer (API)
  │  UPDATE job_runs (status=completed, result_path=...)
  │  IF llm_config → dispatch to scrapeflow.jobs.llm
  │  ELSE → compute diff, create webhook delivery
  ▼
Webhook Delivery Loop
  │  POST → user's webhook_url
```

The Playwright worker is a pure processor: receive message, render page, write to MinIO, publish result, ack. It knows nothing about users, schedules, diffs, or webhooks. All of that is the API's job.

---

### File Layout to Create (Step 18)

```
playwright-worker/
├── Dockerfile                  # base: mcr.microsoft.com/playwright/python:v1.44.0-jammy
├── pyproject.toml              # deps: playwright, nats-py, miniopy-async, structlog, html2text
└── app/
    ├── config.py               # env: NATS_URL, MINIO_*, PLAYWRIGHT_MAX_WORKERS, PLAYWRIGHT_DEFAULT_TIMEOUT_SECONDS
    ├── main.py                 # startup: config → NATS → MinIO → browser → worker loop
    ├── worker.py               # pull consumer + per-job lifecycle (the core of this step)
    ├── formatter.py            # html → markdown/json/raw (mirror Go worker logic in Python)
    └── tests/
        └── test_worker.py      # mock Chromium; test success + exception paths; verify context.close() called in both
```

The Dockerfile base image (`mcr.microsoft.com/playwright/python:v1.44.0-jammy`) includes Playwright, Python 3.12, and all Chromium system dependencies pre-installed. You do not need to call `playwright install` — the image handles it. For the test environment (`docker compose run --rm playwright-worker python -m pytest tests/ -v`), mock the Chromium browser — do not launch a real browser in tests.

---

### Quick Reference — Spec Locations

| Topic | Where to look |
|---|---|
| Full per-job lifecycle pseudocode | spec §4.2 |
| NATS subject names | spec §3.1, `api/app/constants.py` |
| Dispatch message schema | spec §3.3 |
| `playwright_options` Pydantic model | spec §5.3 |
| MinIO dual-path convention | spec §4.1 (Go worker — same pattern) |
| Ack timing rule | `docs/adr/ADR-001-worker-job-contract.md §5` |
| Non-negotiables list | `docs/personas/tech-lead.md §7` |
