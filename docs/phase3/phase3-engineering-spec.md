# ScrapeFlow Phase 3 — Engineering Specification

> **Status:** Approved for implementation (v1)
> **Date:** 2026-04-15
> **Audience:** Tech Lead and Engineers
> **Prerequisites:** Phase 2 complete (all 26 steps done), ADR-004 through ADR-007 accepted

---

## 1. Context & Pre-Conditions

Phase 2 delivered: Playwright worker, LLM processing, change detection, webhook delivery, admin panel API. All 26 steps complete. Production readiness review done — all CRITICAL/HIGH/MEDIUM issues resolved.

Phase 3 adds 15 features across four priority tiers. The PM's prioritized backlog is at `docs/project/phase3-prd/BACKLOG.md`. This spec translates those PRDs into implementable engineering work.

**Before any Phase 3 code is written:**
- ADR-004 (fat message schema v2) must be accepted — it governs every worker message change
- ADR-005 (BFS coordinator) must be accepted — it defines the new coordinator service
- ADR-006 (batch data model) must be accepted — it requires a migration on `job_runs`
- ADR-007 (job secrets) must be accepted — it defines the credential storage pattern

**Deployment order constraint (from ADR-004):**
When rolling out Phase 3 to production, deploy workers before the API. Workers must be able to handle schema_version 2 messages before the API starts sending them.

---

## 2. Cross-Cutting Changes

These changes affect multiple features and must be implemented before the features that depend on them.

### 2.1 Fat Message Schema v2 (ADR-004)

All workers must be updated to handle the new message structure before any Phase 3 API changes are deployed.

**Go HTTP worker changes (`worker/`):**
- Add `SchemaVersion`, `Credentials`, `Options`, `CrawlContext` fields to the message struct (pointer types, nullable)
- Extract `proxy_url` from `Credentials` if present; configure `http.Transport.Proxy`
- Extract `respect_robots` from `Options`; enforce robots.txt compliance (PRD-004)
- Extract `cookies` from `Credentials` if present; inject as `Cookie` header

**Playwright worker changes (`playwright-worker/`):**
- Add `schema_version`, `credentials`, `options`, `crawl_context` to message parsing
- Extract `proxy_url` from `credentials` if present; pass to `browser.new_context(proxy=...)`
- Extract `cookies` from `credentials` if present; call `context.add_cookies(...)` before navigation
- Extract `actions` from `options` if present; execute action loop before extraction
- Extract `respect_robots` from `options`; enforce robots.txt compliance

### 2.2 `job_runs.job_id` nullable migration

Required before batch scraping can ship. This is a migration on a critical table — run it carefully.

```python
# Alembic migration — autogenerate will not catch the constraint addition
# After autogenerate handles the column nullability change, append manually:

op.execute(sa.text("""
    ALTER TABLE job_runs
        ADD COLUMN batch_item_id UUID REFERENCES batch_items(id),
        ADD CONSTRAINT chk_job_runs_single_parent
            CHECK ((job_id IS NOT NULL) != (batch_item_id IS NOT NULL))
"""))
op.create_index(
    'idx_job_runs_batch_item_id',
    'job_runs',
    ['batch_item_id'],
    postgresql_where=sa.text('batch_item_id IS NOT NULL')
)
```

**Existing rows:** All existing `job_runs` rows have `job_id IS NOT NULL` and `batch_item_id IS NULL`. The check constraint is satisfied — no backfill needed.

### 2.3 `jobs.updated_at` DB trigger (Q7 decision)

Add a Postgres trigger to maintain `jobs.updated_at` on every row update, regardless of whether the update came through the ORM or raw SQL.

```python
# Hand-written Alembic migration — autogenerate cannot produce trigger DDL
def upgrade() -> None:
    op.execute(sa.text("""
        -- jobs.updated_at is maintained by this trigger, NOT by SQLAlchemy's onupdate.
        -- Reason: mutation paths that use db.execute(update(...)) bypass the ORM
        -- onupdate hook silently. The trigger fires on every UPDATE regardless of path.
        CREATE OR REPLACE FUNCTION set_jobs_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_jobs_updated_at
        BEFORE UPDATE ON jobs
        FOR EACH ROW EXECUTE FUNCTION set_jobs_updated_at();
    """))

def downgrade() -> None:
    op.execute(sa.text("""
        DROP TRIGGER IF EXISTS trg_jobs_updated_at ON jobs;
        DROP FUNCTION IF EXISTS set_jobs_updated_at();
    """))
```

Remove `onupdate=lambda: datetime.now(UTC)` from the `jobs.updated_at` SQLAlchemy column definition after this migration is applied — the trigger is now authoritative.

---

## 3. P1 Features — Must Ship

### 3.1 PRD-001: K8s Manifests — Phase 2 Services

> **PRD:** [PRD-001-k8s-manifests.md](../project/phase3-prd/PRD-001-k8s-manifests.md)

**Problem:** Phase 2 introduced playwright-worker, llm-worker, and the cleanup cron script — but none have Kubernetes manifests. FluxCD's GitOps pipeline on `main` still deploys only Phase 1 services. Production is running Phase 1 code. Phase 3 cannot ship until Phase 2 is promoted to production.

**What to build:** Kubernetes manifests for `playwright-worker`, `llm-worker`, and `cleanup` CronJob in the infra repo at `/home/karthik/Documents/govindappa/govindappa-k8s-config`.

**playwright-worker Deployment:**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: playwright-worker
  namespace: scrapeflow
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: playwright-worker
          resources:
            requests: { memory: 512Mi, cpu: 250m }
            limits:   { memory: 1.5Gi, cpu: 1000m }
          volumeMounts:
            - name: dshm
              mountPath: /dev/shm
      volumes:
        - name: dshm
          emptyDir:
            medium: Memory
            sizeLimit: 512Mi
```

**Why `emptyDir: medium: Memory` for `/dev/shm`:** Chromium uses shared memory for inter-process communication between its renderer processes. The default `/dev/shm` in a k8s container is 64Mi — insufficient for Chromium, causing tab crashes. `emptyDir` with `medium: Memory` mounts a tmpfs of the specified size, giving Chromium the shared memory it needs. `hostPath` is not appropriate — it shares the host's `/dev/shm` across all pods, which is a security concern on a multi-tenant node.

**Why `Deployment` not `StatefulSet`:** NATS pull consumers have no ordering requirement — each pull consumer fetches messages independently. A stable pod identity (StatefulSet) is only needed when a consumer must resume from a specific stream position after restart. Pull consumers with WorkQueue retention re-fetch unacknowledged messages automatically; pod identity is irrelevant.

**llm-worker Deployment:**

```yaml
resources:
  requests: { memory: 128Mi, cpu: 100m }
  limits:   { memory: 512Mi, cpu: 500m }
```

No `/dev/shm` needed — the LLM worker makes HTTP API calls, no browser.

**cleanup CronJob:**

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: cleanup-old-runs
  namespace: scrapeflow
spec:
  schedule: "0 3 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: cleanup
              image: <same image as API>
              command: ["python", "scripts/cleanup_old_runs.py"]
```

**Why API image for CronJob:** `cleanup_old_runs.py` imports SQLAlchemy models and settings from the API codebase. A separate image would duplicate those dependencies. One image to build and push; the CronJob overrides the entrypoint.

**FluxCD integration:** Follow the existing Kustomization pattern in the infra repo. No manual `kubectl apply` should be required.

---

### 3.2 PRD-002: Rate Limiting — Sliding Window

> **PRD:** [PRD-002-sliding-window-rate-limit.md](../project/phase3-prd/PRD-002-sliding-window-rate-limit.md)

**Problem:** The current fixed-window Redis counter has a known 2x burst exploit — a user can fire `limit` requests at the end of window N and `limit` requests at the start of window N+1, effectively getting 2x the quota.

**Solution:** Replace with a sliding window using a Redis sorted set + Lua script for atomicity.

**Location:** `api/app/core/rate_limit.py` (replace the existing `check_rate_limit` function)

**Lua script approach:**

```lua
-- KEYS[1] = rate limit key (e.g. "rate:user:<user_id>")
-- ARGV[1] = current timestamp (milliseconds)
-- ARGV[2] = window size (milliseconds)
-- ARGV[3] = limit (max requests per window)
-- ARGV[4] = TTL for the key (seconds)

local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local cutoff = now - window

-- Remove entries outside the window
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)

-- Count remaining entries
local count = redis.call('ZCARD', KEYS[1])

if count >= limit then
    return 0  -- rate limited
end

-- Add this request with current timestamp as score
redis.call('ZADD', KEYS[1], now, now .. '-' .. math.random(1000000))
redis.call('EXPIRE', KEYS[1], ttl)
return 1  -- allowed
```

**Why Lua:** The check-and-increment must be atomic. Without Lua, two concurrent requests can both read `count < limit` and both proceed, exceeding the limit. The Lua script runs atomically on the Redis server.

**API change:** `check_rate_limit()` in `core/rate_limit.py` is replaced. The function signature and return type remain the same (`raises HTTPException 429 if limited`). No changes to callers.

**Window parameters:** 60-second sliding window, configurable via `RATE_LIMIT_WINDOW_SECONDS` env var (default 60). Existing `RATE_LIMIT_MAX_REQUESTS` is unchanged.

---

### 3.3 PRD-003: SSRF Re-validation on Webhook Delivery

> **PRD:** [PRD-003-ssrf-revalidation.md](../project/phase3-prd/PRD-003-ssrf-revalidation.md)

**Problem:** DNS rebinding attack — a domain resolves to a public IP at `POST /jobs` time but later rebinds to an internal IP (`169.254.x.x`, `10.x.x.x`). The Phase 2 SSRF check runs only at job creation, not at delivery time.

**Fix:** Call `_validate_no_ssrf()` in `core/security.py` at the start of each webhook delivery attempt in the delivery loop.

**Location:** `api/app/services/webhook_delivery.py` — the `_attempt_delivery()` function.

```python
async def _attempt_delivery(delivery: WebhookDelivery, session: AsyncSession) -> bool:
    # Re-validate webhook_url on every attempt — DNS rebinding can change
    # what IP a hostname resolves to after the initial check at POST /jobs.
    try:
        await _validate_no_ssrf(delivery.webhook_url)
    except ValueError as e:
        # Treat SSRF validation failure as a permanent delivery failure
        await _mark_exhausted(delivery, error=f"ssrf_blocked: {e}", session=session)
        return False

    # ... existing delivery logic
```

**Behaviour on SSRF block:** Mark the delivery as `exhausted` (not `failed`) immediately — no retry. Log a structured warning with `job_id`, `delivery_id`, and the blocked URL. This prevents an attacker from using retries to repeatedly probe internal addresses.

**No new migrations needed.** This is a pure code change to the delivery loop.

---

### 3.4 PRD-004: robots.txt Compliance

> **PRD:** [PRD-004-robots-txt.md](../project/phase3-prd/PRD-004-robots-txt.md)

**Problem:** ScrapeFlow makes no attempt to honour `robots.txt` directives. As the platform opens beyond a single user, ignoring robots.txt creates legal and ethical exposure — sites explicitly disallow scraping via this mechanism. A per-job toggle is needed so compliant scraping is opt-in without breaking existing jobs.

**New job field:** `respect_robots: BOOLEAN NOT NULL DEFAULT FALSE`

The field is mutable (can be changed via `PATCH /jobs/{id}`).

**API validation:** No validation beyond type check — `true`/`false` is the full domain.

**Fat message:** `options.respect_robots` (ADR-004 `options` sub-object).

**Worker behaviour (both Go HTTP and Playwright):**

1. If `respect_robots = false` (default): proceed as normal — no change to existing behaviour
2. If `respect_robots = true`:
   - Fetch `{origin}/robots.txt` (with a 5-second timeout; if fetch fails, treat as "no restrictions")
   - Parse the `User-agent: *` and `User-agent: ScrapeFlow` blocks
   - Check if the target URL path is disallowed
   - If disallowed: fail the run with `error = "robots_txt_disallowed"` — do not scrape
   - If allowed (or robots.txt fetch failed): proceed with scrape

**robots.txt fetch:** The robots.txt request itself is NOT routed through the job's proxy (if any). It is a direct request from the worker. This avoids the proxy being used to bypass robots.txt enforcement on the target origin.

**Alembic migration:** Add `respect_robots` column to `jobs` table. Autogenerate handles this.

---

## 4. P2 Features — Core Phase 3

### 4.1 PRD-005: Proxy Rotation

> **PRD:** [PRD-005-proxy-rotation.md](../project/phase3-prd/PRD-005-proxy-rotation.md)

**Problem:** Both workers make outbound requests from the server's public IP. High-frequency scraping of the same domain triggers IP-based blocks and CAPTCHAs. Proxy rotation is the standard mitigation and is treated as table-stakes by both crawl4ai and firecrawl.

**Storage:** `job_secrets` table, `secret_type = 'proxy'` (ADR-007).

**New job fields (API request body only — not stored on `jobs` table):**
- `proxy_url: str | null` — stored in `job_secrets`
- `proxy_provider: str | null` — stored on `jobs` table as `proxy_provider VARCHAR(50) NULL`

**Why `proxy_provider` stays on `jobs`:** It is not sensitive — it's a label (`generic`, `brightdata`, `oxylabs`), not a credential. No encryption needed.

**Fat message:** `credentials.proxy_url` (ADR-004). Decrypted by API at dispatch time; worker receives plaintext.

**Go HTTP worker:**
```go
if msg.Credentials != nil && msg.Credentials.ProxyURL != "" {
    proxyURL, _ := url.Parse(msg.Credentials.ProxyURL)
    transport := &http.Transport{Proxy: http.ProxyURL(proxyURL)}
    client = &http.Client{Transport: transport}
}
```

**Playwright worker:**
```python
context_options = {}
if credentials := message.get("credentials"):
    if proxy_url := credentials.get("proxy_url"):
        context_options["proxy"] = {"server": proxy_url}
context = await browser.new_context(**context_options)
```

**Error handling:** Proxy connection failure → NATS redelivery (existing mechanism). After max retries: `job_run.error = "proxy_connection_failed: {error}"`. No fallback to direct request — a silent fallback could leak the server IP unexpectedly.

**API response:** `has_proxy: bool` presence flag only. `proxy_url` value never returned.

---

### 4.2 PRD-006: Batch Scraping

> **PRD:** [PRD-006-batch-scraping.md](../project/phase3-prd/PRD-006-batch-scraping.md)

**Problem:** ScrapeFlow processes one URL per job. A user scraping 50 product pages must create 50 jobs, poll 50 IDs, and assemble results manually. Both firecrawl and crawl4ai treat multi-URL batch processing as a primary API primitive. This is the most significant capability gap for ML pipeline use cases.

See ADR-006 for the full data model. This section covers the API and dispatch logic.

**New endpoint:** `POST /batch`

**Request validation:**
- `urls`: 1–500 items; each URL validated and SSRF-checked synchronously
- Rate limit check: deduct `len(urls)` quota units upfront at submission time; reject with 429 if insufficient quota

**Why synchronous SSRF validation for up to 500 URLs:** SSRF checks are CPU-bound string operations (regex + DNS lookup). A 500-URL batch adds at most 2–3 seconds of latency. Async validation (accept first, fail items individually) creates a UX problem — the user gets a `batch_id` but some items silently fail validation. Synchronous rejection with a clear 422 error is a better UX.

**Dispatch:**
After creating the `batches` row and all `batch_items` rows, the API dispatches each item as an individual NATS message (same subject as regular jobs — `scrapeflow.jobs.run.http` or `scrapeflow.jobs.run.playwright` based on `engine`). Each `job_runs` row has `batch_item_id` set and `job_id = null` (ADR-006).

**Result consumer batch branch:**
```python
if run.batch_item_id is not None:
    # Update batch item
    item.status = result.status
    item.result_path = result.result_path
    item.error = result.error
    item.completed_at = now()

    # Atomic counter increment
    db.execute(
        update(Batch)
        .where(Batch.id == item.batch_id)
        .values(
            completed=Batch.completed + (1 if result.status == 'completed' else 0),
            failed=Batch.failed + (1 if result.status == 'failed' else 0)
        )
    )
    db.commit()

    # Check if batch is complete
    batch = db.get(Batch, item.batch_id)
    if batch.completed + batch.failed == batch.total:
        batch.status = 'completed' if batch.failed == 0 else 'partial_failure'
        batch.completed_at = now()
        # Fire batch.completed webhook
```

**Cancellation:** `DELETE /batch/{id}` sets all `pending`/`running` `batch_items` to `cancelled`; result consumer discards results for cancelled runs (same check as regular job cancellation, extended to `batch_item_id` path).

---

### 4.3 PRD-007: Site Crawl

> **PRD:** [PRD-007-site-crawl.md](../project/phase3-prd/PRD-007-site-crawl.md)

**Problem:** ScrapeFlow scrapes one URL at a time. A user who wants to extract all pages of a documentation site or monitor a competitor's entire blog must manually enumerate URLs. Whole-site crawling is the headline feature of both firecrawl and crawl4ai. Without it, ScrapeFlow cannot serve the "feed an entire website into my ML pipeline" use case.

See ADR-005 for coordinator placement, BFS queue design, and table schemas. This section covers the API surface and coordinator implementation details.

**New endpoint:** `POST /crawls`

**One-active-crawl-per-domain enforcement:**
```sql
SELECT id FROM crawls
WHERE user_id = $1
  AND seed_url LIKE $2  -- same origin prefix
  AND status NOT IN ('completed', 'failed', 'cancelled')
LIMIT 1
```
If a row is found: return 409. This check uses a DB query (not Redis) for durability.

**Coordinator startup sequence:**
1. Subscribe to `scrapeflow.jobs.result` (NATS pull consumer, separate consumer group from API result consumer)
2. On startup: re-enqueue any `crawl_queue` rows with `status = 'dispatched'` and `dispatched_at < NOW() - 10 minutes` — these were in-flight when the coordinator last restarted
3. Start the dispatch loop: poll `crawl_queue WHERE status = 'pending'` and dispatch up to N messages (configurable `COORDINATOR_DISPATCH_BATCH_SIZE`, default 10)

**Link extraction:** After a crawl page result arrives, the coordinator fetches the raw HTML from MinIO, parses `<a href>` tags, and applies filters:
- Same-origin only (scheme + host must match seed URL)
- Pass `include_paths` allow-list (if set)
- Pass `exclude_paths` block-list
- Not already in `crawl_queue` for this `crawl_id` (UNIQUE constraint handles deduplication)
- `depth + 1 <= max_depth`
- `crawl_queue` row count for this crawl < `max_pages`

**Sitemap discovery** (when `ignore_sitemap = false`):
- On receiving the seed URL result, also fetch `{origin}/robots.txt` → extract `Sitemap:` directives
- Fetch each sitemap XML → extract `<loc>` entries
- Insert into `crawl_queue` with `depth = 1`

**Crawl completion detection:**
```sql
SELECT COUNT(*) FROM crawl_queue
WHERE crawl_id = $1 AND status = 'pending'
```
When this returns 0 AND all dispatched items are terminal, set `crawls.status = 'completed'`.

**Scheduling integration:** The existing API scheduler loop checks for crawls with `schedule_cron` set and `next_run_at <= NOW()`. Each scheduled trigger creates a new `crawls` row — it does not resume the previous crawl.

---

### 4.4 PRD-008: Authenticated Scraping

> **PRD:** [PRD-008-authenticated-scraping.md](../project/phase3-prd/PRD-008-authenticated-scraping.md)

**Problem:** Many useful scraping targets are behind authentication — internal dashboards, paywalled content, SaaS tools. ScrapeFlow has no way to scrape authenticated pages. This PRD implements the narrow version: cookie injection only. Full credential storage (form login, OAuth, session capture) is Phase 4.

**Storage:** `job_secrets` table, `secret_type = 'cookies'` (ADR-007).

**Cookie format stored:** JSON array of cookie objects. At dispatch time, the API decrypts and includes the array in `credentials.cookies` in the fat message.

**Playwright worker:**
```python
if credentials := message.get("credentials"):
    if cookies := credentials.get("cookies"):
        await context.add_cookies(cookies)
        # Called after context creation, before page.goto()
```

**Go HTTP worker:**
```python
if credentials := message.get("credentials"):
    if cookies := credentials.get("cookies"):
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        request.headers["Cookie"] = cookie_header
```

**Domain inference:** If a cookie object has no `domain` field, the Playwright worker infers it from the job's target URL host. The Go worker does not need to infer domain — the `Cookie` header is domain-agnostic.

**Validation at `POST /jobs`:**
- Cookie `name` and `value` must be non-empty strings
- Cookie `domain` (if provided) must match the job URL domain or a parent domain
- Maximum 50 cookies per job
- If `has_cookies: true` in `GET /jobs/{id}` response — no values exposed

---

### 4.5 PRD-009: Pre-crawl Page Actions

> **PRD:** [PRD-009-page-actions.md](../project/phase3-prd/PRD-009-page-actions.md)

**Problem:** The Playwright worker navigates to a URL and immediately extracts content. Many real-world pages require interaction before useful content is visible: dismissing cookie banners, waiting for lazy-loaded components, scrolling to trigger infinite scroll, or filling a search box. Without action choreography, the Playwright worker is significantly limited for dynamic sites.

**Storage:** `actions JSONB NULL` column on `jobs` table. Included in fat message as `options.actions`.

**Alembic migration:** Add `actions JSONB NULL` to `jobs`. Autogenerate handles this.

**Validation at `POST /jobs`:**
- Maximum 20 actions
- If `actions` is set and `engine = 'http'`: return 422 — actions require Playwright
- `wait.milliseconds`: 1–10000ms
- `selector` fields: non-empty string
- Each action must have a known `type`

**Playwright worker execution loop:**

```python
async def execute_actions(page, actions: list[dict]) -> list[str]:
    warnings = []
    for action in actions:
        try:
            if action["type"] == "wait":
                await asyncio.sleep(action["milliseconds"] / 1000)
            elif action["type"] == "wait_for_selector":
                await page.wait_for_selector(
                    action["selector"],
                    timeout=action.get("timeout", 5000)
                )
            elif action["type"] == "click":
                await page.click(action["selector"],
                    timeout=action.get("timeout", 5000))
            elif action["type"] == "type":
                await page.fill(action["selector"], action["text"])
            elif action["type"] == "press":
                await page.keyboard.press(action["key"])
            elif action["type"] == "scroll":
                for _ in range(action.get("amount", 1)):
                    await page.evaluate(
                        f"window.scrollBy(0, {'window.innerHeight' if action['direction'] == 'down' else '-window.innerHeight'})"
                    )
            elif action["type"] == "execute_js":
                await page.evaluate(action["script"])
            elif action["type"] == "screenshot":
                screenshot_bytes = await page.screenshot()
                # Store to MinIO alongside job result
                await store_screenshot(screenshot_bytes, run_id)
        except Exception as e:
            warnings.append(f"action {action['type']} failed: {str(e)}")
            continue  # Partial failure — continue with next action
    return warnings
```

**CSP mitigation for `execute_js` (Q10 decision):**
Before executing the action list, inject a Content Security Policy that blocks outbound fetch/XHR to non-target-domain origins:

```python
target_origin = urlparse(job_url).scheme + "://" + urlparse(job_url).netloc
await page.set_extra_http_headers({
    "Content-Security-Policy": f"connect-src 'self' {target_origin}"
})
```

This runs before `page.goto()` to ensure the CSP is in effect for the initial page load and all subsequent actions.

**Warnings field:** The job result payload gains an optional `warnings: list[str]` field populated by the action executor. Stored in the MinIO result JSON alongside content. Surfaced in `GET /jobs/{id}/result`.

---

### 4.6 PRD-010: MCP Server

> **PRD:** [PRD-010-mcp-server.md](../project/phase3-prd/PRD-010-mcp-server.md)

**Problem:** ScrapeFlow's primary use case is feeding data into LLM pipelines, but an LLM agent wanting to trigger a scrape must make raw HTTP calls — requiring custom integration code in every agent. Firecrawl now ships native MCP support, making its platform callable from Claude, Cursor, or any MCP-compatible client with zero integration code. ScrapeFlow needs the same capability.

**Location:** `mcp/` directory in the monorepo.

**Why monorepo:** The MCP server calls the ScrapeFlow API (HTTP client only — no DB, no NATS, no MinIO). It shares no internal Python modules with the API. A separate repository adds Git complexity for what is essentially a thin client wrapper. Monorepo under `mcp/` is simpler.

**Transport:** stdio only in Phase 3. The `mcp` Python SDK uses stdio transport by default — this is what Claude Desktop and the majority of MCP clients expect. SSE (HTTP-based) transport is Phase 4.

**Implementation:**

```
mcp/
  server.py          # MCP server entrypoint
  tools/
    scrape_url.py    # scrape_url tool
    get_result.py    # get_result tool
    list_jobs.py     # list_jobs tool
    get_job_status.py # get_job_status tool
  client.py          # ScrapeFlow API HTTP client
  config.py          # env var config (SCRAPEFLOW_API_URL, SCRAPEFLOW_API_KEY)
  Dockerfile
  pyproject.toml
```

**`scrape_url` with `wait_for_result: true`:**
- Submit `POST /jobs` → get `job_id`
- Poll `GET /jobs/{job_id}` every 2 seconds (configurable `MCP_POLL_INTERVAL_SECONDS`)
- Return when status is `completed` or `failed`, or after `MCP_MAX_WAIT_SECONDS` (default 120)
- Fetch content from `GET /jobs/{job_id}/result` on completion
- Truncate at `MCP_MAX_CONTENT_BYTES` (default 50KB) with notice

**Authentication:** `SCRAPEFLOW_API_KEY` env var → `Authorization: Bearer <key>` header on all API calls. No separate MCP auth mechanism.

**Tool descriptions:** Tool descriptions in the MCP schema should note "single URL only" to set expectations clearly. Do not leave the scope ambiguous — LLM agents will infer batch/crawl capability if the description is silent about it.

**Docker distribution:**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY mcp/ .
RUN pip install -e .
CMD ["python", "server.py"]
```

**K8s deployment:** The MCP server is user-run (each user runs their own instance pointed at the ScrapeFlow API). It does not run inside the k3s cluster. No k8s manifest needed for Phase 3.

---

## 5. P3 Features — Enhancements

### 5.1 PRD-011: Admin SPA

> **PRD:** [PRD-011-admin-spa.md](../project/phase3-prd/PRD-011-admin-spa.md)

**Problem:** The admin API routes exist but there is no UI. Operating the platform requires knowing the API surface and crafting curl commands — not acceptable for a multi-user deployment. The Admin SPA wraps the existing `/admin/*` endpoints in a React dashboard covering user management, job oversight, quota controls, and webhook retry.

**Stack:** React 18 + TypeScript + Vite + React Query + Tailwind CSS + shadcn/ui (Radix UI primitives). Located at `frontend/` in the monorepo.

**Served by:** The FastAPI app serves the built static files under `/admin` using `StaticFiles`. No separate Nginx deployment in Phase 3.

```python
# api/app/main.py — after all route registrations
from fastapi.staticfiles import StaticFiles
app.mount("/admin", StaticFiles(directory="frontend/dist", html=True), name="admin-spa")
```

**Auth:** The SPA reads the Clerk JWT from the Clerk JS SDK session and attaches it as `Authorization: Bearer <jwt>` on all API requests. Same-origin requests (SPA and API on the same domain) mean no CORS configuration is needed.

**Missing API endpoint — `PATCH /admin/users/{id}/quota`:**
This endpoint does not exist in Phase 2. It must be added before the Admin SPA quota management feature is implemented. The endpoint accepts partial quota overrides:
```json
{ "monthly_runs_limit": 1000, "concurrent_jobs_limit": 10, "storage_bytes_limit": 10737418240 }
```

**Housekeeping items resolved by this PRD:**

1. **`api_keys (user_id, name)` uniqueness constraint:**
```python
# SQLAlchemy model addition
UniqueConstraint("user_id", "name", name="uq_api_keys_user_name")
# POST /api-keys: catch IntegrityError → 409 Conflict
```

2. **`jobs.webhook_url VARCHAR → TEXT`:** Opportunistic migration — apply in the same Alembic revision as the `jobs.actions` column addition (§4.5 migration).

3. **User-facing hard delete `DELETE /jobs/{id}?permanent=true`:**
```python
@router.delete("/jobs/{job_id}")
async def delete_job(job_id: UUID, permanent: bool = False, ...):
    if permanent:
        # Delete MinIO objects first (external state before internal state)
        await delete_job_minio_objects(job_id)
        # Cascade handles job_runs
        db.delete(job)
        db.commit()
        return Response(status_code=204)
    else:
        # Existing soft-cancel behaviour
```

**CI:** Add a frontend build step to the CI pipeline: `npm run build` in `frontend/`, output to `frontend/dist/`. The API Docker image should include `frontend/dist/` — the `StaticFiles` mount expects the built output.

---

### 5.2 PRD-012: Billing and Per-user Quotas

> **PRD:** [PRD-012-billing-quotas.md](../project/phase3-prd/PRD-012-billing-quotas.md)

**Problem:** Without enforcement, any multi-user deployment risks uncontrolled consumption exhausting worker capacity, MinIO storage, and LLM API budgets. This PRD ships quota enforcement only — monthly run limits, concurrent job limits, and storage limits. Stripe billing integration is Phase 4.

**New table — `user_quotas`:**

```sql
CREATE TABLE user_quotas (
    user_id                UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    monthly_runs_limit     INT NULL,   -- null = use platform default
    concurrent_jobs_limit  INT NULL,
    storage_bytes_limit    BIGINT NULL,
    storage_bytes_used     BIGINT NOT NULL DEFAULT 0,
    -- storage_bytes_used is a Postgres counter updated on each MinIO write/delete.
    -- monthly_runs and concurrent_jobs are computed from job_runs (always accurate).
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Usage tracking — three dimensions, three strategies:**

| Dimension | Strategy | Rationale |
|-----------|----------|-----------|
| `monthly_runs` | Computed from `job_runs` on demand | `SELECT COUNT(*) FROM job_runs WHERE user_id = $1 AND created_at >= first_of_month`. Always accurate; acceptable at current scale. |
| `concurrent_jobs` | Computed from `job_runs` on demand | `SELECT COUNT(*) FROM job_runs WHERE user_id = $1 AND status IN ('queued', 'running')`. Already indexed. |
| `storage_bytes` | Postgres counter on `user_quotas` | MinIO object listing is slow. Counter updated on each result write (+) and cleanup delete (-). Risk: drift if objects are deleted outside the API — reconcile via cron if needed in Phase 4. |

**Enforcement points:**
1. `POST /jobs` and `POST /batch`: check all three dimensions before creating the job/batch
2. Scheduler dispatch: re-check `monthly_runs` and `concurrent_jobs` before dispatching a scheduled run; if exceeded, skip and log (do not fail the job template)
3. MinIO result write path (in result consumer): check `storage_bytes_used + result_size <= limit` before writing; if exceeded, fail the run with `error = "storage_quota_exceeded"`

**Batch quota:** A batch of N URLs deducts N from `monthly_runs` at submission time. Batch items count toward `concurrent_jobs` (each dispatched `job_runs` row is an active run).

**Platform defaults:** Configurable via env vars:
- `DEFAULT_QUOTA_MONTHLY_RUNS` (default: 500)
- `DEFAULT_QUOTA_CONCURRENT_JOBS` (default: 5)
- `DEFAULT_QUOTA_STORAGE_BYTES` (default: 5368709120 — 5GB)

**429 response format:**
```json
{
  "error": "quota_exceeded",
  "quota_type": "monthly_runs",
  "message": "Monthly run limit reached (500/500). Quota resets on 2026-05-01.",
  "resets_at": "2026-05-01T00:00:00Z"
}
```

---

### 5.3 PRD-013: Per-event Webhook Subscriptions

> **PRD:** [PRD-013-webhook-event-filter.md](../project/phase3-prd/PRD-013-webhook-event-filter.md)

**Problem:** Currently all webhook events (`job.completed`, `job.failed`) fire every webhook unconditionally. A user who only wants to be notified on failures, or only on successful completions, cannot filter — every event fires their endpoint. Per-event subscription filtering was deferred from Phase 2 until a frontend existed to configure it.

**New job field:** `webhook_events: TEXT[] NULL` — array of event names to subscribe to.

Supported events: `job.completed`, `job.failed`, `crawl.completed`, `batch.completed`

If `webhook_events` is `null` or empty: all events fire (existing behaviour, backward compatible).

**Enforcement in result consumer and delivery loop:**
```python
if job.webhook_events and event_name not in job.webhook_events:
    return  # Skip webhook for this event
```

**Alembic migration:** Add `webhook_events TEXT[] NULL` to `jobs`. Autogenerate handles this.

---

### 5.4 PRD-014: WebSocket Real-time Job Tracking

> **PRD:** [PRD-014-websocket-tracking.md](../project/phase3-prd/PRD-014-websocket-tracking.md)

**Problem:** Integrations and the Admin SPA must poll `GET /jobs/{id}` repeatedly to track job progress. Polling adds unnecessary load and latency. Firecrawl ships WebSocket endpoints for live crawl status. A WS endpoint eliminates polling from any integration that needs real-time feedback.

**New endpoint:** `GET /jobs/{job_id}/stream` — WebSocket connection for live job status updates.

**Implementation:** FastAPI native WebSocket support.

```python
@router.websocket("/jobs/{job_id}/stream")
async def job_status_stream(websocket: WebSocket, job_id: UUID, ...):
    await websocket.accept()
    while True:
        run = db.query(latest_run_for_job(job_id))
        await websocket.send_json({"status": run.status, "updated_at": run.updated_at})
        if run.status in ('completed', 'failed', 'cancelled'):
            break
        await asyncio.sleep(2)
    await websocket.close()
```

**Auth:** WebSocket connections pass the API key or JWT as a query parameter (`?token=...`) since WebSocket clients cannot set custom headers in the browser. The existing auth middleware is extended to accept `token` query parameter for WebSocket routes.

**Scope:** Single-job tracking only in Phase 3. Batch and crawl streaming is Phase 4.

---

### 5.5 PRD-015: Content Deduplication

> **PRD:** [PRD-015-content-dedup.md](../project/phase3-prd/PRD-015-content-dedup.md)

**Problem:** ML pipeline consumers running high-frequency change-detection jobs re-process and re-store identical content on every run, even when the page hasn't changed. crawl4ai uses xxhash content fingerprinting to skip unchanged pages. Without deduplication, these jobs waste worker capacity, LLM API calls, and MinIO storage on content that is bit-for-bit identical to the previous run.

**New `job_runs` field:** `content_hash VARCHAR(16) NULL` — xxhash64 fingerprint of the normalised page content.

**Deduplication check in result consumer:**
```python
import xxhash

content_hash = xxhash.xxh64(normalised_content).hexdigest()[:16]

# Check previous run for this job
previous_run = db.query(JobRun)\
    .filter(JobRun.job_id == job_id, JobRun.status == 'completed')\
    .order_by(JobRun.created_at.desc())\
    .offset(1).first()  # second-most-recent (current run is most recent)

if previous_run and previous_run.content_hash == content_hash:
    current_run.content_hash = content_hash
    current_run.change_detected = False
    # No diff computation, no MinIO write for structured output, no LLM dispatch
    return
```

**MinIO deduplication:** If content is unchanged, the `latest/{job_id}.{ext}` MinIO object is still updated (to maintain the "latest" contract). The `history/` write is skipped — no new history object is stored for unchanged content. This reduces MinIO storage for high-frequency jobs on stable pages.

**`xxhash` dependency:** Add `xxhash` to `api/requirements.txt`. It is a pure-C library with Python bindings, significantly faster than hashlib MD5/SHA for content fingerprinting.

---

## 6. Database Migration Plan

Migrations must run in this order. Never combine migrations — each is independently rollbackable.

| # | Migration | PRD | Type |
|---|-----------|-----|------|
| 3.1 | Add `respect_robots BOOLEAN` to `jobs` | PRD-004 | Autogenerate |
| 3.2 | Add `proxy_provider VARCHAR(50)` to `jobs` | PRD-005 | Autogenerate |
| 3.3 | Add `actions JSONB`, `webhook_url TEXT` (type change), `webhook_events TEXT[]` to `jobs` | PRD-009, PRD-011, PRD-013 | Autogenerate |
| 3.4 | Create `job_secrets` table + `job_secret_type` ENUM | PRD-007 | Hand-written (ENUM creation) |
| 3.5 | Create `batches` + `batch_items` tables | PRD-006 | Autogenerate |
| 3.6 | Alter `job_runs`: nullable `job_id`, add `batch_item_id`, check constraint, `content_hash` | PRD-006, PRD-015 | Autogenerate + manual constraint append |
| 3.7 | Create `crawls` + `crawl_pages` + `crawl_queue` tables | PRD-007 | Autogenerate |
| 3.8 | Create `user_quotas` table | PRD-012 | Autogenerate |
| 3.9 | Add `jobs.updated_at` trigger | Cross-cutting §2.3 | Hand-written |
| 3.10 | Add `UniqueConstraint` on `api_keys (user_id, name)` | PRD-011 | Autogenerate |

---

## 7. New Services Summary

| Service | Location | Language | K8s | Notes |
|---------|----------|----------|-----|-------|
| BFS Coordinator | `coordinator/` | Python | Deployment | New in Phase 3; required for site crawl |
| MCP Server | `mcp/` | Python | None (user-run) | Standalone; not deployed in k3s |

---

## 8. Architectural Principles — Unchanged from Phase 2

All Phase 2 architectural principles (from `docs/personas/architect.md §7`) carry forward unchanged:

1. Database is source of truth; NATS is delivery mechanism
2. Workers are dumb — no DB access, no business logic
3. Delete external state before internal state (MinIO before Postgres)
4. Fail fast and visibly
5. Idempotency on all retryable operations
6. `SELECT FOR UPDATE SKIP LOCKED` on all background polling loops
7. Fernet for secrets at rest and in transit
8. 404 not 403 for cross-tenant access

The coordinator service follows rule 2: it reads from `crawl_queue` (Postgres) and dispatches to NATS, but it does not contain scraping logic. Scraping remains entirely in the workers.
