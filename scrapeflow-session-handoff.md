# ScrapeFlow Session Handoff

You are a **coding mentor** helping the user build ScrapeFlow, a self-hosted multi-tenant web scraping platform (Apify clone). Read @CLAUDE.md for the full architecture.

## Your role in this session

**Do not write code unless the user explicitly says "build it", "implement it", or similar.**

Instead:
- Explain what needs to be built and why
- Walk through design decisions, trade-offs, and patterns
- Point out relevant existing code the user should look at before writing
- Review code the user writes and give feedback
- Answer questions about the spec, architecture, or implementation approach

When the user is ready to build something, they will say so. Until then, guide and explain.

---

## Project reference

| What | Where |
|------|-------|
| Architecture + key decisions | `CLAUDE.md` |
| Docs index (ADRs, reference, archive) | `docs/README.md` |
| ADRs (ADR-001 through ADR-007) | `docs/adr/` |
| Operational reference (commands, devops, usages) | `docs/project/` |
| Multi-persona process starter prompts | `docs/process/` |
| Phase 3 engineering spec (historical) | `docs/archive/phase3/phase3-engineering-spec.md` |
| Phase 3 ordered backlog (historical) | `docs/archive/phase3/PHASE3_BACKLOG.md` |
| Phase 3 deferred items → Phase 4 candidates | `docs/archive/phase3/PHASE3_DEFERRED.md` |
| Progress tracker (historical) | `docs/archive/PROGRESS.md` |
| Phase 2 spec (historical) | `docs/archive/phase2/phase2-engineering-spec-v3.md` |
| Phase 2 production readiness review | `docs/archive/phase2/production-review.md` |
| Phase 3 production readiness review | `docs/archive/phase3/production-review.md` |
| Idempotency audit (NATS redelivery) | `docs/archive/phase3/idempotency-checks.md` — 7 findings; all fixed |
| Service failure & recovery audit | `docs/archive/phase3/service-failure-recovery.md` — all findings fixed |

---

## Commands

**Tests** (must run inside Docker — `uv` manages the venv inside the container):
```bash
# from ./docker
docker compose exec api uv run pytest tests/ -v
docker compose exec api uv run pytest tests/test_jobs.py -v
```

**MCP server tests** (built and run as a standalone Docker image — not in docker-compose):
```bash
# from repo root
docker build -t scrapeflow-mcp mcp/
docker run --rm -e SCRAPEFLOW_API_KEY=test-key scrapeflow-mcp python -m pytest tests/ -v
```

**Migrations** (Alembic auto-run is enabled in `main.py` — runs on API startup):
```bash
# from ./docker
docker compose exec api uv run alembic upgrade head
docker compose exec api uv run alembic current
docker compose exec api uv run alembic revision --autogenerate -m "migration_3_N_description"
```

---

## Current state

- Branch: `main` (develop is in sync). Latest commit `67ba983` (2026-07-03).
- Phase 1 + Phase 2 + Phase 3 complete and production-verified at `scrapeflow.govindappa.com`
- **Auth on production Clerk instance** as of 2026-07-03 (was dev instance). See "Clerk production cutover" below.
- **In Phase 4 — investigation/triage + small feature work.** No formal spec yet; the backlog is being assembled from real production usage. Open findings cluster around **LLM-worker reliability** (Q5/Q6/Q7) and a **state-machine design flaw** (Q8) that already caused a live incident. Shipped Phase 4 work: **admin result viewer + user-email surfacing**, the **user-facing job dashboard**, and the **Playwright anti-bot hardening (ADR-008)** — Patchright + real headed Chrome under Xvfb, verified passing BrowserScan in prod (see Post-Phase-3 changes + ADR-008).
- **⚠ Q6 (`ack_wait` redelivery loop) is now CONFIRMED, not just latent** — it fired in prod on the playwright worker (headed Chrome is slower than the old headless Chromium, so scrapes cross the default 30s). Fixed there (`67ba983`). **The LLM worker's consumer almost certainly has the same bug — audit its `ack_wait` next (see Phase 4 triage Q6).**
- 243 API tests passing (deterministic — first-run clean); **70** playwright-worker tests passing; 14 MCP tests passing.
- Alembic auto-migration enabled in `api/app/main.py`
- **Smoke test completed 2026-05-12** — core job pipeline confirmed working (google.com → markdown output)
- **Docs reorganised 2026-05-12** — `docs/` now has four sections: `adr/` (decisions), `project/` (reference), `process/` (multi-persona starters), `archive/` (completed phase history)

### Post-Phase-3 changes (since handoff, 2026-05-13 → 2026-07-03)

| Commit | Change |
|--------|--------|
| `5cb8c7f` | **Incident fix** — LLM dispatch loop. The regular + batch result handlers had un-source-guarded `if worker_status == "running"` branches; the LLM stage's `running` clobbered `run.status` from `processing` back to `running`, so the next `completed` re-matched the scrape-completed branch and re-dispatched to `scrapeflow.jobs.llm` — a tight feedback loop that burned ~200 LLM API calls in ~5 min before the worker was stopped. Fix: `if source == "scrape":` guards on the `running` transition in both `_handle_job_result` and `_handle_batch_result`. Root cause + permanent fix tracked as **Q8** (status-value overloading — needs a total state machine, not more guards). |
| `63b2dfc` | playwright-worker: split proxy URL userinfo into `server`/`username`/`password` for `browser.new_context(proxy=…)` (Playwright rejects credentials embedded in the URL). |
| `4b2d1d6`, `d9e1edb` | Added top-level `README.md`; replaced ASCII architecture diagram with Mermaid; updated `docs/project/COMMANDS.md`. |
| `70ce8bc` | **Admin result viewer + user email** (first Phase 4 feature). Backend: `user_email` added to `JobResponse` (admin jobs query joins `User`); new `GET /admin/jobs/{id}/result` (admin-scoped, no owner check) via shared `load_completed_result()` helper extracted from `get_job_result`. Frontend: read-only Monaco viewer on Job Detail (Source/Preview toggle — markdown via react-markdown, HTML via sandboxed iframe, JSON pretty-printed), User email row, and a User column + user filter on the Jobs list. Monaco is bundled locally (config in `frontend/src/lib/monaco.ts`) and lazy-loaded so the main admin bundle stays ~93 KB gzip. New deps: `@monaco-editor/react`, `monaco-editor`, `react-markdown`, `remark-gfm`. 4 new admin tests → 243. |
| `d8a6ce1` | **CI fix** for `70ce8bc`. The frontend's committed `package.json` uses **vite `^8`** (a working-tree bump that rode along in `70ce8bc`), but `@vitejs/plugin-react@4.7.0` only peers vite ≤7 — so the API Docker build's `npm install` failed with ERESOLVE. Bumped `@vitejs/plugin-react` → `^6.0.3` (peers vite `^8`), so the tree resolves with **no `--legacy-peer-deps` needed** locally or in CI. Build output/bundle sizes unchanged. |
| `d0a64b5` | **Docs** — handoff + CLAUDE.md updated for the admin result viewer; Phase 4 triage docs (`open-questions.md`, `open-bugs.md`, `usage-findings.md`) added. |
| _(user-dashboard commit)_ | **User-facing job dashboard + admin/user nav cross-link** (frontend-only; no backend/API/schema change — the owner-scoped `/jobs*` routes already existed). New user routes `/app/dashboard/jobs` + `/app/dashboard/jobs/:jobId` (nav item added; `/app/dashboard` now lands on Jobs). The admin `Jobs`, `JobDetail`, and `ResultViewer` are **shared, not duplicated** — each takes a `mode: 'admin' \| 'user'` prop that swaps the API base (`/admin/jobs` ↔ `/jobs`), the route/link base, and the result endpoint. Admin-only bits (User column, user filter which calls `/admin/users`, User detail row) render only in `mode='admin'`; the user Job Detail gets a **soft Cancel** button (`DELETE /jobs/{id}`) in place of the admin permanent-delete Danger Zone. `AdminJob` type renamed to `Job` (back-compat alias kept). Admin-detection extracted from `RequireAdmin` into a shared `lib/useIsAdmin.ts` hook (same `['admin-check']` cache key — no extra request); `Layout` gained a `variant` prop that renders the cross-link ("← My dashboard" in admin; "Admin panel →" in user, only if the user is an admin). **Layout bug fixed**: shell `min-h-screen` → `h-screen` so the sidebar footer (cross-link + Sign out) no longer falls below the fold on tall pages (Jobs/Stats) — `main` scrolls internally instead of the whole page. Bundle unchanged (~93 KB gzip main; Monaco stays a lazy chunk). No new tests (backend untouched; 243 API tests still green). |
| `92df7ea` | **Playwright anti-bot hardening (ADR-008 + `docs/guides/anti-bot-hardening.md`).** Scrapes were blocked despite residential proxies; BrowserScan diagnosed 3 fingerprint fails (`navigator.webdriver`, `HeadlessChrome` UA, CDP `Runtime.enable` leak). Fix: swap Playwright → **Patchright**; run **real Google Chrome** (`channel="chrome"`) **truly headed under Xvfb** (only mode with a clean UA — even `--headless=new` leaks `HeadlessChrome`); `--disable-blink-features=AutomationControlled` (clears `webdriver`; Patchright alone didn't) + `--no-sandbox`/`--disable-dev-shm-usage`; `new_context(no_viewport=True)`; no UA spoofing. All env-tunable in `worker/config.py`. k8s: `patchright install chrome` in image; playwright-worker Deployment resources bumped (infra repo `538fba5`). 70 playwright-worker tests. **Verified in prod: BrowserScan now `Normal`, 0 Robot / 18 checks.** |
| `4257183` | **Entrypoint fix** — first stealth deploy looked healthy (pod 1/1, 0 restarts) but ran nothing. `xvfb-run` as pid 1 masked worker crashes (container never exited → k8s never restarted), a cold-start race killed Chrome before Xvfb was ready, and `PYTHONUNBUFFERED` unset hid the logs. Replaced with `playwright-worker/entrypoint.sh` (start Xvfb → wait for its socket → `exec python` as pid 1) + `PYTHONUNBUFFERED=1` + pre-create `/tmp/.X11-unix 1777`. Now: python is pid 1, logs flow, crashes surface as CrashLoopBackOff. |
| `67ba983` | **NATS `ack_wait` + heartbeat (Q6 — now CONFIRMED & FIXED for playwright worker).** A headed-Chrome scrape (~37s) exceeded the pull consumer's default 30s `ack_wait`, so NATS redelivered mid-scrape; the late `ack()` was a no-op and, with `max_deliver=-1`, the job **looped forever** (re-scrape + re-upload every ~20s). Live incident mitigated by purging the subject + raising the live consumer to `ack_wait=120` (via `add_consumer` — this nats-py has no `update_consumer`). Permanent fix: `pull_subscribe(config=ConsumerConfig(ack_wait=120))` + `msg.in_progress()` heartbeat every 30s (covers jobs longer than `ack_wait`). **Caveat: JetStream won't apply a new `ack_wait` to an existing durable consumer — must update/recreate out-of-band.** |

### Clerk production cutover (2026-07-03)

Moved auth from the Clerk **dev** instance to a **production** instance. No app code changed — Clerk is derived entirely from keys/config (backend `jwt.py` gets the instance from the secret key; frontend `pk` is a build-time env). Work was dashboard + Cloudflare + cluster secret only.

- **DNS**: production Frontend API `clerk.scrapeflow.govindappa.com` + account portal + mail CNAMEs added **manually in Cloudflare, grey-cloud (DNS-only)**. Not via ExternalDNS — its `sources` are `ingress`/`service` only, so it neither creates these nor prunes them (they carry no `txtOwnerId` TXT registry record, so `policy: sync` ignores them).
- **OAuth**: production needs **own** Google (and GitHub, if used) OAuth credentials — Clerk's shared demo app is dev-only. Symptom when missing: Google `Error 400: invalid_request / Missing required parameter: client_id`. Google client created, creds pasted into Clerk → login works.
- **Keys** (easy to mix up):
  - backend **`sk_live`** → k8s secret `scrapeflow-app-secrets/clerk-secret-key` (patched via the README "Rotating the Clerk secret" flow + `rollout restart`)
  - frontend **`pk_live`** → GH Actions secret `VITE_CLERK_PUBLISHABLE_KEY`, baked into the api image at build (CI rebuilds api on `api/**`/`frontend/**` changes — the `a0c905b` `index.html` title tweak was the trigger for that rebuild).
  - **Incident during cutover**: `pk_live` was accidentally pasted into the `clerk-secret-key` slot → backend couldn't auth to Clerk's Backend API to load JWKS → `TokenVerificationErrorReason.JWK_FAILED_TO_LOAD` (HTTP 401 in the SPA). Fixed by patching the real `sk_live`. Verified: pod env is `sk_live`, `api.clerk.com/v1/jwks` + Frontend-API `/.well-known/jwks.json` both 200, no verification errors in logs.
- **Fresh start**: prod **Postgres app tables truncated + MinIO `scrapeflow-results` emptied** on 2026-07-03 (schema + `alembic_version 8f4b6eb47abb` preserved; bucket kept). Rationale: a prod Clerk instance issues **new `sub` (user) IDs**, so old `users` rows keyed on the dev `clerk_id` would be orphaned — clean slate instead. Fernet keys (`llm-key-encryption-key`, `credentials-encryption-key`) were **not** rotated (would orphan encrypted-at-rest data).
- **Loose end**: GitHub OAuth custom credentials only need setup if GitHub sign-in is offered (Google done).

### Uncommitted working tree (not part of handoff scope)

- `frontend/tsconfig.tsbuildinfo`, `tmp/` — build artifact / scratch, untracked (should not be committed; `tsbuildinfo` is a candidate for `.gitignore`)

### Phase 3 steps done

| Step | Description | Notes |
|------|-------------|-------|
| 1 | K8s manifests: playwright-worker, llm-worker, cleanup CronJob | Written to infra repo (`govindappa-k8s-config`) |
| 2 | Sliding window rate limiter (PRD-002) | Redis sorted set + Lua; `api/app/core/rate_limit.py` |
| 3 | SSRF re-validation on webhook delivery (PRD-003) | `security.py` split into ValueError core + HTTPException adapter; `api/app/core/webhook_loop.py` |
| 4 | Migration 3.1: `jobs.respect_robots BOOLEAN NOT NULL DEFAULT false` | Autogenerated |
| 5 | Migration 3.2: `jobs.proxy_provider VARCHAR(50) NULL` | Autogenerated |
| 6 | Migration 3.3: `jobs.playwright_actions JSONB`, `webhook_url TEXT`, `webhook_events TEXT[]` | Autogenerated; `playwright_actions` named for consistency with `playwright_options` |
| 7 | Migration 3.4: `job_secrets` table + `job_secret_type` ENUM (`proxy`, `cookies`) | Autogenerated (ENUM inline in `create_table` — no COMMIT/BEGIN needed for CREATE TYPE, only ALTER TYPE ADD VALUE); `downgrade()` manually drops the type |
| 8 | Migration 3.5: `batches` + `batch_items` tables (ADR-006) | Autogenerated; `status` is VARCHAR(20) not ENUM (status values may grow without ALTER TYPE); composite index `idx_batch_items_batch_id ON (batch_id, status)` for result consumer queries |
| 9 | Migration 3.6: `job_runs` nullable `job_id`, `batch_item_id`, check constraint, `content_hash` (ADR-006, PRD-015) | Autogenerated columns + nullable change; CHECK constraint and partial index hand-appended; FK named `fk_job_runs_batch_item_id` (autogenerate emits `None` — always name it explicitly); `job_id` keeps full index (non-NULL ratio stays high); `batch_item_id` partial index `WHERE batch_item_id IS NOT NULL` |
| 10 | Migration 3.7: `crawls`, `crawl_pages`, `crawl_queue` tables (ADR-005) | Autogenerated; `crawl_queue` has two special indexes: partial `WHERE status = 'pending'` for dispatch loop performance, unique on `(crawl_id, url)` for silent deduplication; create order matters (`crawls → crawl_pages → crawl_queue` — queue FKs both) |
| 11 | Migration 3.8: `user_quotas` table (PRD-012) | Autogenerated; `user_id` is the PK (natural key — strict 1:1 with users, no separate UUID needed); limits are nullable (NULL = use env var default); no `created_at` (settings record, not an event) |
| 12 | Migration 3.9: `jobs.updated_at` DB trigger (spec §2.3) | Hand-written; asyncpg rejects multi-statement `op.execute()` — split `CREATE FUNCTION` and `CREATE TRIGGER` into two separate calls; `onupdate` hook removed from `Job` model (trigger is now authoritative); trigger verified via `information_schema.triggers` |
| 13 | Migration 3.10: `api_keys (user_id, name)` uniqueness constraint (PRD-011) | Autogenerated; `POST /users/api-keys` catches `IntegrityError` → 409; revoked keys still hold their name (intentional — names are identifiers, not recycled) |
| 14 | Go HTTP worker: schema_version 2 + proxy routing + robots.txt (ADR-004, PRD-004, PRD-005) | `ScrapeMessage` v2 struct with `Credentials`/`Options`/`CrawlContext`; `fetcher.WithProxy()` builds proxy transport reusing original timeout; `internal/robots` package — `IsDisallowed()` direct fetch (never via proxy), `isPathDisallowed()` pure parser (ScrapeFlow > `*` precedence, longest-match wins); `storageClient` interface added to Worker for test injection; 4 `handleMessage` unit tests (disallowed, allowed-proceeds, skip-when-false, malformed-proxy) |
| 15 | Playwright worker: schema_version 2 + proxy + cookies + actions + robots.txt (ADR-004, PRD-004/005/008/009) | `handle_message` extracted to `worker/worker.py` (mirrors LLM worker + Go worker pattern); `Credentials`/`Options`/`CrawlContext` models added; robots check fires **before** `"running"` publish matching Go contract; proxy via `browser.new_context(proxy={"server": url})`; cookies injected before `page.goto()` with domain inferred from job URL; CSP `connect-src` header set before goto when actions present; 8-action executor in `worker/actions.py` — partial-failure loop, screenshots stored to MinIO; `worker/robots.py` direct-fetch client (httpx, never proxy); 69 tests passing (up from 28); Dockerfile updated to include `tests/` + `pyproject.toml` |
| 16 | PRD-004: robots.txt — API integration | `respect_robots: bool = False` added to `_MutableJobFields` (exposed on `JobCreate`/`JobPatch`); persisted onto `Job` model in `create_job()`; all three dispatch sites (router `create_job`, scheduler `_dispatch_due_jobs`, scheduler `_recover_stale_pending`) upgraded from v1 flat payload to schema_version 2 with `engine`, `credentials: null`, `options: {respect_robots}`, `crawl_context: null`; 2 new tests — 145 API tests passing |
| 17 | PRD-005: proxy rotation — API integration | `default_proxy_url` added to settings; `proxy_url` (write-only) + `proxy_provider` added to `_MutableJobFields`; `has_proxy: bool` added to `JobResponse`; `_resolve_credentials()` helper (per-job `job_secrets` row > `DEFAULT_PROXY_URL` env > `None`) called at all 3 dispatch sites; `pg_insert(...).on_conflict_do_update()` upsert on create/PATCH; `proxy_url: null` on PATCH deletes the secret; EXISTS correlated subquery for `has_proxy` in `_jobs_with_latest_run_stmt` (callers now unpack 3-tuple); `_resolve_credentials` duplicated inline in `scheduler.py` to avoid circular import; 4 new tests; **conftest fix**: `client` fixture now overrides `check_rate_limit` to no-op (shared mock user exhausted the 60-req window mid-suite); `rate_limited_client` fixture for HTTP rate-limit integration test; phantom `client` dep removed from `mock_clerk_auth` (was silently activating the override) — 149 tests passing, first-run deterministic |
| 18 | PRD-008: authenticated scraping — API integration | `cookies` (write-only) on `JobCreate`/`JobPatch`; `job_secrets` upsert/delete with `secret_type='cookies'`; `has_cookies: bool` on `JobResponse` via EXISTS subquery; `_resolve_credentials()` updated in both `routers/jobs.py` and `scheduler.py` to decrypt + inject cookies alongside proxy; 4 new tests — 153 tests passing |
| 19 | PRD-009: page actions — API integration | `actions: list[dict] | None = None` on `_MutableJobFields` with `field_validator` (max 20; valid type; `wait` ms 1–10000; `click`/`type`/`wait_for_selector` selector non-empty); engine-actions guard (422 if `engine != playwright`) in `create_job` and `patch_jobs`; `playwright_actions=body.actions` persisted to `Job`; `options.actions` in NATS payload at all 3 dispatch sites; `actions` on `JobResponse` (not write-only); Migration 3.11 adds `job_runs.warnings JSONB` (autogenerated); result consumer persists `warnings` from NATS message to DB; `GET /jobs/{id}/result` returns `JobResultResponse {content, output_format, result_path, warnings}`; 7 new tests — 160 tests passing |
| 20 | PRD-013: webhook event filter | `_VALID_WEBHOOK_EVENTS` frozenset + `webhook_events: list[str] \| None = None` on `_MutableJobFields` with `field_validator` (rejects unknown event names); `webhook_events` on `JobResponse`; `webhook_events=body.webhook_events` persisted to `Job` in `create_job()`; all 5 `JobResponse` builders updated; filter guard `not job.webhook_events or event_name in job.webhook_events` added to all 4 `create_webhook_delivery` call sites in `result_consumer.py` (non-LLM completed, LLM-key-not-found failed, LLM completed, generic failed); no new migration (column already existed from Migration 3.3); 5 new tests — 165 tests passing |
| 21 | PRD-006: batch scraping — API + result consumer | `api/app/routers/batch.py` + `api/app/schemas/batch.py`; `POST /batch` SSRF-checks all URLs then atomically deducts `len(urls)` quota units via `_SLIDING_WINDOW_BATCH_SCRIPT` in `rate_limit.py`; dispatches one NATS fat-message per item with `job_id=null` (ADR-006 §4 — workers unchanged); result consumer routes by `run.batch_item_id IS NOT NULL`, updates `batch_items` + increments `batches.completed/failed`, transitions to `completed`/`partial_failure` when done; `batch.completed` webhook implemented in Migration 3.12 (`webhook_deliveries.job_id` nullable + `batch_id` FK + `CHECK num_nonnulls=1`), `create_batch_webhook_delivery()` in `webhooks.py`; admin.py: 5 query fixes — `engine_stmt`/`engine_7d_stmt` use `outerjoin` through `batch_items → batches` + `COALESCE(Job.engine, Batch.engine)`; `top_stmt` adds batch user path via aliased User; per-user status/run-count guards add `job_id IS NOT NULL`; 12 new tests — 177 passing |
| 22 | PRD-007: site crawl — API routes | `api/app/routers/crawls.py` + `api/app/schemas/crawls.py`; `POST /crawls` SSRF-checks seed_url + webhook_url, enforces one-active-crawl-per-domain via `LIKE origin%` query (409 `{error, crawl_id}` if found), creates `crawls` row + seeds `crawl_queue` with `depth=0` — API does not dispatch (coordinator picks up); `GET /crawls/{id}` + `GET /crawls/{id}/pages` (paginated, `?status=` filter via Query alias); `DELETE /crawls/{id}` sets `cancelled` + bulk-updates pending queue rows to `skipped`; `import status as http_status` alias in router avoids `status` query param shadowing the FastAPI status module; `bypass_ssrf` monkeypatch fixture for test isolation (fake `.local` domains don't resolve DNS); UUID-based domain names per test prevent cross-run conflicts; 13 new tests — 190 passing |
| 23 | PRD-007: coordinator service + Docker Compose | `coordinator/` Python service — BFS dispatch loop + NATS result subscriber; polls `crawl_queue WHERE status = 'pending'`; creates `crawl_pages` row, dispatches fat NATS message with `crawl_context`; extracts links via BeautifulSoup; UNIQUE constraint on `(crawl_id, url)` handles dedup silently; crawl completion fires `crawl.completed` webhook; startup re-enqueues stalled dispatched items; separate durable NATS consumer (not shared with API); added to `docker-compose.yml`; `coordinator/coordinator/config.py` uses pydantic-settings |
| 24 | PRD-010: MCP server | `mcp/` standalone Python service (stdio transport); four tools: `scrape_url` (submit + poll + truncate at 50 KB), `get_result`, `get_job_status`, `list_jobs`; auth via `SCRAPEFLOW_API_KEY` env → `Authorization: Bearer`; tool descriptions explicitly state "single URL only"; not deployed in k3s (user-run); tests mock the HTTP API via custom `AsyncBaseTransport` — patch target is `tools.<module>.make_client` (not `client.make_client`); 14 tests passing |
| 25 | PRD-012: billing/quotas — enforcement + admin endpoint | `api/app/core/quota.py` — three quota dimensions: `monthly_runs`, `concurrent_jobs`, `storage_bytes`; limits from `user_quotas` row (NULL = env var default: 500 runs, 5 concurrent, 5 GB); `check_user_quota()` raises 429 with structured detail; `is_quota_exceeded()` bool variant for scheduler; `check_storage_quota()` + `increment_storage_bytes()` for result consumer; quota checks as FastAPI deps (`check_job_quota`, `check_batch_quota`) — overridable via `dependency_overrides`; `PATCH /admin/users/{id}/quota` upsert endpoint; `cleanup_old_runs.py` decrements `storage_bytes_used` on delete; `quota_client` fixture for quota integration tests; 202 tests passing |
| 26 | PRD-014: WebSocket real-time job tracking | `GET /jobs/{id}/watch` WebSocket endpoint in `routers/jobs.py`; `GET /batch/{id}/watch` in `routers/batch.py`; auth via `?token=` query param (browsers cannot set `Authorization` on WS upgrade); `job_notifier` on `app.state` — pg_notify listener (`job_status`, `batch_status` channels) fans out to per-subscriber asyncio queues; endpoint sends initial status on connect, streams updates until terminal status, then closes; close codes 4001 (unauthorized) / 4004 (not found) |
| 27 | PRD-015: content deduplication | `_compute_content_hash()` helper in `result_consumer.py` — xxh64 of raw MinIO bytes, stored as `job_runs.content_hash VARCHAR(16)`; dedup check inserted before LLM/diff branch: on hash match sets `diff_detected=False`, deletes redundant `history/` MinIO object, skips LLM dispatch + webhook; `xxhash>=3.0.0` added to `pyproject.toml`; 3 new tests in `tests/test_deduplication.py` (same content short-circuits, different content proceeds, first run has no previous) — 205 tests passing |

### Phase 3 complete

All 28 steps done. Full record in `docs/archive/phase3/production-review.md`.

#### Production review progress

| # | Severity | Description | Status |
|---|----------|-------------|--------|
| 1 | CRITICAL | Batch counter race condition (`completed += 1` read-modify-write) | `[x]` done |
| 2 | CRITICAL | `zip(strict=False)` silently truncates batch dispatch | `[x]` done |
| 3 | CRITICAL | Batch webhooks always unsigned (HMAC over empty key) — Migration 3.13 adds `batches.webhook_secret` | `[x]` done |
| 4 | CRITICAL | Alembic auto-migration commented out | `[x]` done (temporarily re-disabled for Migration 3.13; re-enable before ship) |
| 5 | CRITICAL | Dedup deletes `history/` object already stored in `job_runs.result_path` | `[x]` done |
| 35 | CRITICAL | `authorized_parties=None` in both JWT paths — accepts JWTs from any Clerk app on the same instance | `[x]` done |
| 36 | HIGH | `execute_js` Playwright action executes arbitrary user JavaScript — CSP only blocks `connect-src` | `[x]` done |
| 6 | HIGH | Batch jobs with LLM output silently return raw HTML — Migration 3.14 adds `batches.llm_config` | `[x]` done |
| 7 | HIGH | `decrement_storage_bytes` never called on delete | `[x]` done |
| 8 | HIGH | PATCH upserts secrets before all validations pass — sentinel pattern defers writes after loop | `[x]` done |
| 9 | HIGH | `increment_storage_bytes` not atomic — savepoint helper + explicit fail-run on error | `[x]` done |
| 10 | HIGH | MinIO orphan on LLM key deleted mid-schedule — delete object before marking failed | `[x]` done |
| 11 | HIGH | Admin user delete leaks MinIO — stat/remove/decrement before cascade | `[x]` done |
| 12 | HIGH | `cancelled` status never emits `pg_notify` from router — WebSocket hangs if NATS down | `[x]` done |
| 13/49 | HIGH | Coordinator `_dispatch_batch` has no `FOR UPDATE SKIP LOCKED` — duplicate dispatch under multi-replica | `[x]` done |
| 40/32 | HIGH | Coordinator hardcodes NATS subjects — `coordinator/coordinator/constants.py` created; `bfs.py`, `result_handler.py`, `main.py` import from it | `[x]` done |
| 38 | MEDIUM | Proxy URL plaintext in NATS — new `CREDENTIALS_ENCRYPTION_KEY` (Fernet); per-job ciphertext forwarded directly; `default_proxy_url` encrypted at dispatch; field renamed `encrypted_proxy_url`/`encrypted_cookies`; all three workers updated; **k8s TODO: add key to sealed secret + all three Deployment env arrays** | `[x]` done |
| 14 | MEDIUM | Crawl page `?status=` filter accepts any string — whitelist check added | `[x]` done |
| 15 | MEDIUM | Coordinator fires crawl completion webhook with no retry — Migration 3.15 + `_enqueue_crawl_webhook` + 2 new tests | `[x]` done |
| 16 | MEDIUM | WebSocket not wired into Admin SPA — `JobDetail.tsx` WS hook + live badge; `Jobs.tsx` refetchInterval; `vite-env.d.ts` added | `[x]` done |
| 39 | MEDIUM | WS rate limiting — per-user cap in `JobNotifier`; close 4029 | `[x]` done |
| 41 | MEDIUM | `result_consumer.py` monolith — `handle_storage_quota_exceeded` moved to `quota.py`; `delete_minio_object` + `stat_minio_size` extracted to new `api/app/core/storage.py`; admin.py inline stat+delete loops replaced; bool return on delete for conditional accounting | `[x]` done |
| 44 | LOW | `JobNotifier` subscriber queues unbounded — `asyncio.Queue(maxsize=100)` on both `subscribe_job`/`subscribe_batch`; `await queue.put` → `put_nowait` + `QueueFull` drop+log in both notify callbacks | `[x]` done |
| 46 | LOW | `import os` at module bottom in `main.py` — already fixed (line 2) | `[x]` done |
| 47 | LOW | `email.ilike` search has no index — Migration 3.17: `pg_trgm` extension + GIN index `idx_users_email_trgm ON users USING gin (email gin_trgm_ops)`; declared in `User.__table_args__` | `[x]` done |
| 50 | MEDIUM | `_check_completion` after-dispatch block in `bfs.py` ran in a fresh session with no `FOR UPDATE` — moved `check_completion` + `enqueue_crawl_webhook` to `result_handler.py` (public); called inside `_process_crawl_result` within the same transaction; removed after-dispatch block from `bfs.py`; `bfs.py` imports from `result_handler.py` for safety-net scan | `[x]` done |
| 51 | MEDIUM | Coordinator restart mid-crawl: `reenqueue_stalled` nulled `crawl_page_id` but left orphaned `CrawlPage` rows — SELECT stale IDs → DELETE pages → then UPDATE queue | `[x]` done |
| 52 | MEDIUM | `_process_crawl_result` not idempotent on NATS redelivery + `"running"` message could overwrite terminal page status under multi-replica — terminal-status early-return + running-branch terminal check | `[x]` done |
| 25 | LOW | Stale WHAT comments in `result_consumer.py` and `result_handler.py` — 7 removed across both files; WHY comments (idempotency guard, deferred LLM accounting, dedup early-return, ADR references, crawl-ack note) retained | `[x]` done |
| 29 | LOW | No test for batch item storage quota exceeded — `test_result_consumer_batch_item_storage_quota_exceeded` added to `test_batch.py`; asserts run + item marked `failed`, `batch.failed == 1`, MinIO object removed | `[x]` done |
| 43 | LOW | Content hash re-reads freshly-written MinIO object — deferred; fix requires schema_version 3 worker contract change; bundle with other Phase 4 worker changes | `[ ]` deferred to Phase 4 |
| 21 | MEDIUM | `SCHEDULE_MIN_INTERVAL_MINUTES` missing from k8s API manifest + `CREDENTIALS_ENCRYPTION_KEY` (#38) + coordinator deployment — all applied in k8s repo (Phase 3 DevOps pass) | `[x]` done |
| 31 | LOW | Cron schedule validation assumes server timezone — document UTC assumption | `[x]` done (`_MutableJobFields.schedule_cron` now has `Field(description=...)` stating UTC; Phase 4 TODO left on field) |
| I-1 | CRITICAL | Redelivered scrape "completed" misidentified as LLM completion — `source: "scrape"\|"llm"` discriminator added to all three workers + API consumer gates `_handle_llm_completed` on `source == "llm"` | `[x]` done |
| I-2 | CRITICAL | Terminal run flipped back to `failed` on redelivery — terminal guard `if run.status in ("completed","failed"): return` at top of `_handle_job_result` | `[x]` done |
| I-3 | CRITICAL | Batch counters double-incremented on redelivery — same terminal guard at top of `_handle_batch_result` | `[x]` done |
| I-4 | HIGH | `"running"` message overwrites terminal run — closed as side effect of I-2 terminal guard | `[x]` done |
| I-5 | HIGH | Storage quota increments non-idempotent — `job_runs.storage_accounted_at` column (Migration 3.18); `_try_increment_storage` skips if already set | `[x]` done |
| I-6 | HIGH | Duplicate webhook delivery rows on redelivery — `webhook_deliveries.event` column + `idx_webhook_deliveries_dedup UNIQUE (run_id, event) WHERE run_id IS NOT NULL` (Migration 3.18); both helpers use `ON CONFLICT DO NOTHING` | `[x]` done |
| I-7 | LOW | `check_completion` no terminal guard — `crawl.status not in ("completed","cancelled")` added | `[x]` done |
| 17+ | MEDIUM–LOW | See `docs/phase3/production-review.md` for full list | all items done or deferred; **all idempotency findings done — Phase 3 review complete** |
| [?] 42 | LOW | `JobNotifier` uses blocking `asyncpg.connect()` at startup? — `asyncpg.connect()` is async (awaited at startup); raw connection is intentional (LISTEN requires a dedicated non-pooled connection for its lifetime); `+asyncpg` stripped from URL because asyncpg only accepts plain `postgresql://` | `[x]` closed |
| [?] 43 | LOW | Can `_handle_batch_result` receive `worker_status` other than `completed`/`failed`? — workers only publish `running`, `completed`, `failed`; `processing` is API-side only; `else: return` at end of function is defensive coverage for redelivery combos, not a gap | `[x]` closed |
| [?] 44 | LOW | Should content hash be computed when `schedule_cron` is not set? — intentional; establishes baseline for users who add scheduling later via PATCH | `[x]` closed |
| [?] 40 | LOW | Custom robots.txt parsers (Go + Python) vs established packages — needs explicit decision: evaluate edge cases or document choice to keep hand-rolled | `[ ]` deferred to Phase 4 |
| [?] 41 | LOW | Should `hiredis` be installed for redis-py? — one-line addition, no API change, low risk; recommended for production | `[x]` done — `redis[hiredis]>=5.0.0` in `pyproject.toml` |

---

### Post-deploy fixes applied (2026-05-12)

| Fix | Detail |
|-----|--------|
| `http-worker/internal/worker/worker.go` — `sub.Unsubscribe()` → `sub.Drain()` | `Unsubscribe()` sends `CONSUMER.DELETE` to NATS on every pod shutdown (SIGTERM), deleting the durable `go-worker` consumer. With `RollingUpdate`, the new pod creates the consumer first then the old pod deletes it — causing `"nats: consumer deleted"` errors on the new pod. `Drain()` closes the client-side subscription without touching server state; the durable consumer survives restarts. Commit `ef22a17` on `develop` + `main`. |
| `govindappa-k8s-config` — `strategy: Recreate` on `scrapeflow-http-worker` deployment | Belt-and-suspenders fix: old pod is fully terminated before new pod starts, eliminating the rolling-update overlap window where the old pod's shutdown could delete the consumer the new pod just created. Commit `55d2928` in infra repo. |

### Phase 4 entry point

Phase 4 scope is **still not formally specced**, but real production usage has surfaced a concrete triage list (below). When returning:

1. Read the **Phase 4 triage** table below — these are captured findings, not yet a backlog
2. Read the source docs in full before acting: `docs/project/open-questions.md` (Q5–Q8 have detailed options + recommendations), `docs/project/open-bugs.md`, `docs/project/usage-findings.md`
3. Check `docs/archive/phase3/PHASE3_DEFERRED.md` for items already scoped and deferred
4. Decide whether to run the full PM → Architect → Tech Lead → Engineer process (see `docs/process/`) or a lighter spec approach. The LLM-worker cluster (Q5/Q6/Q7) is tightly coupled and should be designed together, not piecemeal.

---

### Phase 4 triage — captured findings (what bugs we still have)

The dominant theme is **LLM-worker reliability**: Q5, Q6, Q7 are three faces of the same problem and their recommendations explicitly say they resolve together. Q8 is the design flaw behind the incident already fixed in `5cb8c7f`.

**Open questions needing a decision** (`docs/project/open-questions.md`):

| # | Severity | Summary | State |
|---|----------|---------|-------|
| Q5 | High | LLM worker can't survive scale-to-zero cold starts (90–120s). `LLM_REQUEST_TIMEOUT_SECONDS=60` → `httpx.ReadTimeout`; worker acks-on-failure so NATS never retries → job permanently `failed`. Recommend A+B (bump timeout + classify exceptions, nak transient). | Needs decision |
| Q6 | High (**CONFIRMED in prod**) | No `ack_wait` on consumer → default 30s. Any job >30s → NATS silently redelivers → duplicate processing / **double MinIO upload** / (for LLM) double billing. **Fired live on the playwright worker** on 2026-07-03 (headed Chrome scrapes cross 30s) — infinite loop, `max_deliver=-1`. **FIXED on the playwright worker** (`67ba983`): `ConsumerConfig(ack_wait=120)` + `msg.in_progress()` heartbeat every 30s; live consumer updated out-of-band. **TODO: the LLM worker's pull consumer has the same default — audit + apply the same fix (ack_wait floor above `LLM_REQUEST_TIMEOUT_SECONDS` + heartbeat).** Caveat learned: JetStream won't change `ack_wait` on an existing durable consumer — update/recreate it out-of-band (this nats-py has no `update_consumer`; use `add_consumer` with the modified config). | **Playwright done; LLM worker pending** |
| Q7 | Medium | No worker-level retry on transient LLM failures (instance death mid-call, 503, conn reset). Only SDK default `max_retries=2`, invisible + doesn't cover conn errors. Recommend B (SDK + worker retry with an explicit exception-classification table). | Needs decision; blocked on Q5/Q6 |
| Q8 | Medium (root cause of the incident) | `job_runs.status` values overloaded across pipeline stages — `running`/`completed` mean different things for scrape vs LLM, disambiguated only by `source`. Caused the `5cb8c7f` dispatch loop. Source-guards are a patch; recommend **Option B** (distinct per-stage status values → total state machine) as a Phase 4 refactor. Should land *after* Q5/Q6/Q7 so retry logic isn't rebuilt twice. | Needs decision |
| Q1–Q4 | — | Phase 1/2 questions — Q1 (api_keys uniqueness) already resolved in Phase 3; Q2–Q4 largely superseded. Re-confirm and close out. | Mostly stale |

**Open bugs** (`docs/project/open-bugs.md`):

| # | Severity | Summary | Fix |
|---|----------|---------|-----|
| BUG-001 | Low (noisy, harmless) | Scheduler `_recover_stale_pending` selects batch runs (`job_id IS NULL`) → `db.get(Job, None)` emits `SELECT ... WHERE jobs.id IS NULL` every 60s per stuck batch run, flooding logs. No data corruption. | Add `JobRun.job_id.is_not(None)` to the stale-pending query. One-liner. |
| BUG-002 | Mixed (1 critical, 11 high, 41 moderate, 20 low) | **73 GitHub Dependabot vulnerability alerts** on `kargovin/scrapeflow` default branch (surfaced 2026-07-03 on push). Not yet triaged — unknown which are in prod paths vs transitive/dev-only. | Review at https://github.com/kargovin/scrapeflow/security/dependabot ; triage the critical + highs first (likely a mix of Python/`api`, Go/workers, and frontend/npm). Bump or accept per-advisory. |

**Usage findings** (`docs/project/usage-findings.md`):

| # | Summary |
|---|---------|
| UF-001 | `/health/ready` checks DB/Redis/NATS but **not MinIO** — endpoint reports `200 ok` while every job silently fails to store output if MinIO is down. Add a MinIO ping to the degraded check. |
| UF-002 | `DEFAULT_PROXY_URL` is a single platform-wide proxy — one user's behaviour can get the shared IP banned for everyone. Decision: per-user proxy model (`user_proxies` table, provider-side rotation, no platform default). Larger change: schema + secrets + dispatch + frontend UI. |

**Deferred from Phase 3** (already scoped):

| Item | Detail |
|------|--------|
| `[?] 40` | Custom robots.txt parsers (Go + Python) vs established packages — deferred to Phase 4 |
| `[?] 43` | Content hash re-reads freshly-written MinIO object — bundle with Phase 4 schema_version 3 worker contract changes |
| NATS pull consumers | API result consumer uses a push consumer (durable); limits to one replica and requires `Recreate` strategy. Phase 4: migrate to pull consumer model. (Note: overlaps with Q6 — both touch JetStream consumer config.) |
