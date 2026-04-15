# ScrapeFlow — Phase 3 Deferred Items

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
