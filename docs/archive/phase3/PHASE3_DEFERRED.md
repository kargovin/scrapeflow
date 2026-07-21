# ScrapeFlow — Phase 3 Deferred Items

> **Still-open items are tracked in the consolidated Phase 4 view: [`../../project/phase4-backlog.md`](../../project/phase4-backlog.md)** (§4 — "Survives Temporal").

> **Purpose:** Living dump of everything explicitly deferred out of Phase 2 into Phase 3.
> This doc is intentionally messy — it's a tracking list, not a spec. Items get promoted to
> PRDs/ADRs/backlog steps when Phase 3 planning begins.
>
> **Last updated:** 2026-04-12
> **Owner:** Tech Lead

---

## How to use this doc

- Add an item here the moment something is deferred — not at the end of a phase
- Include *why* it was deferred so Phase 3 planning starts with context, not archaeology
- Items here are NOT in any priority order

---

## Security / Hardening

### SSRF re-validation on every webhook delivery attempt

**What:** Re-validate `webhook_url` via SSRF check on every delivery attempt, not just at job creation time.

**Why deferred:** DNS rebinding attack vector — an attacker registers a domain that resolves to a public IP at creation time but later rebinds to an internal IP (169.254.x.x, 10.x.x.x). Phase 2 SSRF-checks the URL at `POST /jobs` time only. A rebinding attack between creation and delivery bypasses this.

**Why not done in Phase 2:** Requires passing the SSRF validator into the webhook delivery loop, and adds latency to every delivery attempt. Acceptable risk for MVP scale with known users.

**Source:** `docs/project/PHASE2_BACKLOG.md` — Deferred to Phase 3 section

---

### Rate limiting: Sliding window

**What:** Replace the current fixed window counter (Redis `INCR` + `EXPIRE`) with a sliding window implementation.

**Why deferred:** Fixed window has a known edge case — a user can burst 2x the quota limit by firing requests at the end of one window and the start of the next. Acceptable for MVP quotas, not for production billing.

**Why not done in Phase 2:** Fixed window is working, low volume. Sliding window requires a Redis sorted set or Lua script — more ops complexity for marginal benefit at current scale.

**Source:** `CLAUDE.md` decisions table ("sliding window planned for Phase 2")

---

## API / Routes

### User-facing hard delete for jobs

**What:** `DELETE /jobs/{id}` currently cancels the active run and pauses scheduling. It does NOT delete the `jobs` row or any `job_runs` history. A true hard-delete route (removes the template row + cascades to all runs + deletes MinIO objects) is not exposed to regular users in Phase 2.

**Why deferred:** Dashboard cleanliness (the primary driver) can be achieved with status filtering instead. Hard delete also destroys run history, which is valuable for ML pipeline use cases. Admin hard delete already exists via `DELETE /admin/jobs/{id}`.

**What Phase 3 needs to decide:**
- Expose hard delete to users at all? (vs. admin-only forever)
- If yes: `DELETE /jobs/{id}?permanent=true` or a separate endpoint?
- Soft delete (archive flag) might be the right middle ground

**Raised during:** ADR-003 Q&A, 2026-04-09

---

### Per-event webhook subscriptions

**What:** Let users configure *which* events trigger their webhook — e.g. `job.completed` only, or `job.failed` only, or all events.

**Current Phase 2 behaviour:** Webhook fires on all events (`job.completed`, `job.failed`) if `webhook_url` is set. No per-event filtering.

**Why deferred:** Phase 2 webhook delivery is already complex (backoff, HMAC signing, delivery table). Per-event subscriptions adds a filter config field on `jobs` and a check in the result consumer and webhook loop. Low demand until there's a frontend to configure it.

**Source:** `docs/project/PHASE2_BACKLOG.md` — Deferred to Phase 3 section

---

### Batch and crawl webhooks have no `webhook_events` filter

**What:** Jobs expose a `webhook_events: list[str] | None` field — users can subscribe selectively to `"job.completed"` or `"job.failed"`. Batches and crawls have no equivalent field. `batch.completed` fires unconditionally when `batch.webhook_url` is set; `crawl.completed` fires unconditionally when `crawl.webhook_url` is set.

**Why deferred:** Both are currently single-event resources — there is only one lifecycle event to subscribe to, so a filter field adds no value today. The gap becomes real if additional events are added (e.g. `batch.partial_failure`, `crawl.page_failed`, `crawl.depth_reached`), because users would have no way to subscribe selectively.

**What Phase 4 needs to decide:**
- Add `webhook_events TEXT[]` to `batches` and `crawls` with the same null-means-all-events semantics as jobs?
- Or keep batch/crawl webhooks simple (one event, no filter) and document the intentional asymmetry?

**Source:** Phase 3 production review item #27 audit, 2026-04-28.

---

### Crawl webhooks bypass `create_webhook_delivery` — coordinator writes directly to DB

**What:** Job webhooks go through `create_webhook_delivery` in `api/app/core/webhooks.py`. Crawl webhooks are enqueued by the coordinator — a separate process — via `enqueue_crawl_webhook` in `coordinator/coordinator/result_handler.py`, which inserts into `webhook_deliveries` directly. The HTTP delivery loop (`webhook_loop.py`) picks up and signs both, so the delivery path is shared. But any future validation or instrumentation added to `create_webhook_delivery` will not automatically apply to crawl webhooks.

**Why deferred:** The coordinator cannot import from the API package (different service, different Docker image). Sharing the insertion logic would require extracting it into a shared library or duplicating it — neither is worth the cost for one event type.

**What Phase 4 needs to decide:**
- Extract `create_webhook_delivery` logic into a shared `scrapeflow-common` package importable by both the API and coordinator?
- Or accept the duplication and document the coordinator path as a deliberate exception with a test asserting the payload shape?

**Source:** Phase 3 production review item #27 audit, 2026-04-28.

---

### `api_keys` — `(user_id, name)` uniqueness constraint

**What:** Currently two API keys can share the same name within a user's account. A `UniqueConstraint("user_id", "name")` would prevent this and return 409 on duplicate name.

**Why deferred:** No functional bug — just confusing UX when there's no frontend. When the Admin SPA ships (Phase 3), duplicate key names become a real usability problem.

**What Phase 3 needs:**
- `UniqueConstraint("user_id", "name", name="uq_api_keys_user_name")` on `ApiKey`
- Alembic migration
- `POST /api-keys` catches `IntegrityError` → 409 Conflict

**Source:** `docs/project/open-questions.md` Q1

---

### `jobs.updated_at` — maintenance not guaranteed

**What:** `jobs.updated_at` was added in Phase 1 with `onupdate=lambda: datetime.now(UTC)`. After Migration 2.4 drops `jobs.status`/`result_path`/`error`, the only remaining mutable fields on `jobs` are Phase 2 additions (`schedule_cron`, `schedule_status`, `next_run_at`, `last_run_at`, `webhook_url`, etc.). Some mutation paths (cancel route, scheduler updates to `next_run_at`) may bypass ORM assignment and go through `db.execute(update(...))` — in which case `onupdate` silently does not fire.

**Why deferred:** Not queried anywhere in Phase 2. Admin stats use `job_runs.created_at`, not `jobs.updated_at`. Becomes important for Admin SPA sort order in Phase 3.

**What Phase 3 needs to decide:**
- Option A: Remove the column entirely (no misleading stale data)
- Option B: Wire it up — ensure all mutation paths touch at least one field, or assign `job.updated_at` explicitly
- Option C: DB trigger (more reliable than ORM `onupdate`)

**Source:** `docs/project/open-questions.md` Q2

---

## Workers / Processing

### Proxy rotation

**What:** Pluggable proxy provider config (Bright Data, Oxylabs, etc.) for the Go HTTP worker and Playwright worker. Each scrape request routes through a rotating proxy to avoid IP-based blocking.

**Why deferred:** Low volume personal use — direct requests are fine at MVP scale. Proxy providers add cost and integration complexity.

**What Phase 3 needs:**
- Provider config in worker env (PROXY_URL, PROXY_PROVIDER)
- Proxy injection in Go HTTP worker (`http.Transport`) and Playwright worker (`browser.new_context(proxy=...)`)
- Retry-on-proxy-failure logic separate from NATS retry

**Source:** `CLAUDE.md` Phase 3 section

---

### Authenticated scraping — login flows via Playwright

**What:** Allow users to scrape pages that require authentication. Two sub-features:

1. **Storage state** — user provides login credentials (or a pre-captured session); the Playwright worker authenticates once, captures `cookies + localStorage`, and reuses the saved session on subsequent runs via `browser.new_context(storage_state=...)`.
2. **Cookie injection** — user provides a raw session cookie value; worker injects it into the context before navigating.

**Why deferred:** Playwright can do this natively — the blocker is the data model and security surface, not the browser automation. Implementing it requires:
- Encrypted credential storage (username/password sensitivity is higher than LLM API keys — needs separate threat model)
- A `session_state` storage layer (MinIO or DB JSONB) — per-user, per-domain or per-job
- Session refresh logic (detect redirect-to-login mid-run, re-authenticate, retry)
- Multi-tenant isolation guarantee (User A's session state must never be accessible during User B's job)

None of this is in the Phase 2 schema or spec. Adding it mid-Phase 2 would require an Architect review of the credential storage design before a line of code is written.

**Narrow alternative (lower scope):** Skip credential storage entirely — let users pass a raw cookie string as a `playwright_options.cookies` field. Worker injects it via `context.add_cookies()`. No storage, no refresh, no re-auth. This may be small enough to spec and add to Phase 3 backlog without a full PRD, but that call belongs to the Architect.

**What Phase 3 needs (full version):**
- PM PRD: which auth patterns to support (form login, cookie injection, OAuth?)
- Architect ADR: credential storage design and threat model
- New fields on `jobs` or a separate `job_credentials` table (encrypted at rest)
- Playwright worker session management: capture → store → reuse → refresh on expiry

**Raised during:** Tech Lead Q&A, 2026-04-12

---

### robots.txt compliance

**What:** Per-job toggle: respect or ignore `robots.txt`. Currently the workers make no `robots.txt` check.

**Why deferred:** Primary use case (internal/known sites, data pipelines) doesn't need it. Becomes important if the platform is opened to broader usage.

**What Phase 3 needs:**
- `respect_robots` boolean field on `jobs`
- Worker fetches and parses `robots.txt` before scraping if enabled
- Cache `robots.txt` per domain (Redis, short TTL)

**Source:** `CLAUDE.md` Phase 3 section

---

## Infrastructure / Deployment

### k3s manifests for Phase 2 services

**What:** Kubernetes Deployment + Service manifests for:
- `playwright-worker` (needs larger memory limits — Chromium)
- `llm-worker`
- CronJob for `cleanup_old_runs.py`

**Why deferred:** No k3s deployment target exists yet for Phase 2 (FluxCD is on main branch = Phase 1 only). Manifests belong in the infra repo (`govindappa-k8s-config`) and are added when Phase 2 is deployed to production.

**What Phase 3 needs:**
- Playwright worker Deployment — namespace `scrapeflow`, domain `scrapeflow.govindappa.com`
- LLM worker Deployment
- CronJob for cleanup script (weekly or nightly)
- Resource limits: playwright-worker needs 512MB+ RAM for Chromium

**Source:** `docs/project/PHASE2_BACKLOG.md` — Deferred to Phase 3 section

---

## Frontend / UX

### Admin SPA

**What:** React dashboard for user and job management. Wraps the `/admin/*` API routes (Steps 23–24) in a UI.

**Why deferred:** Admin API routes are built in Phase 2 — the SPA is the Phase 3 consumer of those routes.

**What Phase 3 needs:**
- React app (likely in `frontend/` or a separate repo)
- User list, job list, stats dashboard
- Force-cancel and webhook retry controls

**Source:** `CLAUDE.md` Phase 3 section

---

## Integrations

### MCP server

**What:** Expose `scrape_url`, `get_result`, `list_jobs` as LLM-callable tools via MCP (Model Context Protocol).

**Why deferred:** Phase 1 + 2 build the data plane. MCP is a consumption layer — needs a stable API to wrap.

**What Phase 3 needs:**
- MCP server process (Python or Node)
- Tool definitions: `scrape_url(url, output_format, engine?)`, `get_result(job_id)`, `list_jobs(status?)`
- Auth: API key passed through MCP tool calls

**Source:** `CLAUDE.md` Phase 3 section

---

## Billing / Quotas

### Per-user job limits and usage tracking

**What:** Hard limits on concurrent jobs, total runs per month, MinIO storage per user. Billing integration (Stripe or usage-based).

**Why deferred:** Single-user homelab deployment for now. Billing adds significant product complexity.

**What Phase 3 needs:**
- `user_quotas` table or quota fields on `users`
- Usage tracking (run count, storage bytes) per user
- 429 enforcement in scheduler and `POST /jobs` when quota exceeded
- Admin UI for quota management

**Source:** `CLAUDE.md` Phase 3 section

---

## Real-time Job Tracking — SSE vs WebSocket

**What:** `GET /jobs/{job_id}/stream` — push live job status updates (`{status, updated_at}`) to the client as the job progresses, closing automatically on terminal state.

**Why deferred:** The spec (PRD-014) calls for WebSocket, but the communication is strictly unidirectional (server → client). Server-Sent Events (SSE) is purpose-built for this pattern and removes the need for a WebSocket-specific auth workaround (token query param) since SSE rides normal HTTP and supports the `Authorization` header directly. Error signalling also becomes standard HTTP status codes (401, 404) rather than custom WebSocket close codes (4001, 4004).

**What Phase 3 needs to decide:**
- Adopt SSE (`text/event-stream`) instead of WebSocket for this endpoint?
- If SSE: use `StreamingResponse` or the `sse-starlette` library?
- If staying with WebSocket: document the query-param auth pattern as the project convention (it will recur for crawl streaming)

**Trade-off summary:**
| | SSE | WebSocket |
|---|---|---|
| Direction fit | Exact — unidirectional only | Overkill — bidirectional unused |
| Auth | Normal `Authorization` header | Token query param workaround |
| Error signalling | HTTP 401 / 404 | Custom 4001 / 4004 close codes |
| Reconnect | Built into protocol | Manual |
| Future bidirectionality (e.g. cancel-over-stream) | Not possible | Supported |

**Decision needed from:** Architect

**Raised during:** Step 26 planning, 2026-04-23

---

## Security / `execute_js` — sandboxing or removal

**What:** `execute_js` is the only Playwright page action that executes arbitrary caller-controlled code. All other actions (`click`, `type`, `scroll`, `wait`, `wait_for_selector`, `screenshot`) take bounded parameters. The short-term fix (Phase 3 production review item 36) tightens the CSP injected into document responses to cover `connect-src`, `img-src`, `form-action`, and `frame-src`. This limits the exfiltration surface but does not eliminate it — a sufficiently creative script can still abuse same-origin channels (e.g. `navigator.sendBeacon`, `fetch` with `no-cors` mode, WebRTC data channels, DNS prefetch tricks).

**Why deferred:** The short-term CSP tightening is an acceptable risk reduction for a known-user deployment. Eliminating the risk entirely requires a larger architectural decision.

**Options:**
1. **Drop `execute_js`** — remove the action type from the schema and API validator. This is a breaking change for any user who relies on it, but eliminates the risk class entirely.
2. **Sandbox via isolated browser context** — run `execute_js` in a dedicated context with no stored credentials, no cookies, and a restrictive network filter. Adds complexity to the worker.
3. **Allowlist-only actions** — replace the freeform `script` field with a parameterised action set (e.g. `extract_text(selector)`, `extract_attribute(selector, attr)`). No user-controlled code strings enter the worker at all.

**Decision needed from:** PM (should `execute_js` be a supported feature?) → Architect (if yes, which sandboxing model?).

**Source:** Phase 3 production review item 36, 2026-04-25.

---

## Open Questions Still Unresolved

These are from `docs/project/open-questions.md` and have not been decided yet:

| Q | Summary | Blocking |
|---|---------|---------|
| Q1 | `api_keys` `(user_id, name)` uniqueness | No — deferred to Phase 3 |
| Q2 | `jobs.updated_at` maintenance | No — decide in Phase 3 before Admin SPA |
| Q3 | `jobs.webhook_url` column type should be `Text` | No — low risk, fix opportunistically |

> Q3 is a low-risk fix (`VARCHAR` and `TEXT` are functionally identical in Postgres) — can be done as a housekeeping migration at any point. Not worth a dedicated Phase 3 step.

---

## Phase 3 Build Process Note

Phase 3 uses the full persona chain (PM → Architect → Tech Lead → Engineer). Each item above
needs to go through that chain before implementation begins. Don't start coding Phase 3 items
from this list directly — surface them to the PM persona first for prioritization.

See `CLAUDE.md` §Phase 3 — Build Process for the full persona responsibilities table.
