# ScrapeFlow - Apify Clone

> **Status: Phases 1–3 complete. Phase 4 in progress — Phase 4 *is* the Temporal durable-workflows migration.**
> **All Phase 4 scope lives in `docs/project/phase4-backlog.md` (single source of truth). Read it before starting any Phase 4 work — in particular §3, which lists bugs the migration deletes and that must therefore NOT be fixed.**
> Session context: `scrapeflow-session-handoff.md`.

## Goal

A self-hosted, multi-tenant web scraping platform. Primary use case: structured data extraction and change detection to feed ML/data pipelines. Built as a production-grade portfolio project, designed to scale beyond single-user deployments.

---

## Architecture

### Core stack
- **API**: FastAPI (Python)
- **Workers**: Go **http-worker**; Python **Playwright** + **LLM** workers; Python **crawl coordinator**
- **Queue**: NATS JetStream
- **DB**: PostgreSQL (metadata), MinIO (object storage / raw output)
- **Cache / rate limiting**: Redis
- **Gateway**: Traefik
- **Auth**: Clerk (OAuth — Google, GitHub; JWT issued by Clerk, verified by API)
- **MCP server**: LLM-callable interface (scrape_url, get_result, list_jobs)

### Worker contract
See `docs/adr/README.md` for the full ADR index and current status of each decision record.
- **ADR-001** — Phase 1 worker contract (partially superseded)
- **ADR-002** — Phase 2 worker contract (current authoritative reference for NATS subjects, message schemas, MinIO path convention)

When ADR-001 and ADR-002 conflict, **ADR-002 takes precedence**.

### Deployment
- **Local dev**: Docker Compose (Postgres, Redis, NATS, MinIO)
- **Production**: k3s cluster — namespace `scrapeflow`, domain `scrapeflow.govindappa.com`
  - Traefik ingress, ExternalDNS (Cloudflare), cert-manager (letsencrypt-prod)
  - GitOps via FluxCD — infra repo at `/home/karthik/Documents/govindappa/govindappa-k8s-config`
  - **Auth = Clerk PRODUCTION instance** (not dev). Frontend API custom domain `clerk.scrapeflow.govindappa.com`; its DNS CNAMEs were added **manually in Cloudflare (grey-cloud, DNS-only)** — ExternalDNS only manages `ingress`/`service`-derived records, so it can't (and won't touch) these. Production requires **own Google/GitHub OAuth credentials** (Clerk's shared demo OAuth app is dev-only — a prod login attempt without them fails with Google `Error 400: invalid_request, Missing required parameter: client_id`). Key split, do not confuse: backend **`sk_live`** lives in k8s secret `scrapeflow-app-secrets/clerk-secret-key`; frontend **`pk_live`** is baked into the api image at build time via GH Actions secret `VITE_CLERK_PUBLISHABLE_KEY`. `pk_live` and `sk_live` must be from the **same instance** or every request 401s; a `pk_live` mistakenly placed in the `sk` slot surfaces as `TokenVerificationErrorReason.JWK_FAILED_TO_LOAD`. Rotation/cutover flow: infra repo `clusters/k3s-server/scrapeflow/README.md` → "Rotating the Clerk secret".

---

## Components

### Phase 1 — MVP [COMPLETE]
- **Auth**: Clerk OAuth login/signup, JWT verification middleware, user sync to local DB, API key management
- **Job CRUD**: create scrape job (URL + options), get status, list jobs, cancel job
- **HTTP scraper worker**: plain HTTP requests, returns raw HTML / cleaned Markdown / JSON
- **Output storage**: raw results stored in MinIO, metadata in Postgres
- **Rate limiting**: Redis-backed per-user quotas
- **Docker Compose**: full local dev stack

### Phase 2 — Core features [COMPLETE]
- **Playwright worker**: opt-in JS rendering for dynamic/SPA sites, configurable per job
- **LLM processing**: user provides their own Anthropic/OpenAI API key + output schema; worker extracts structured data
- **Change detection**: recurring/scheduled jobs, diff detection, notify on change
- **Webhook delivery**: configurable webhooks with exponential backoff retry
- **Admin panel API**: manage users, view all jobs, usage stats

### Phase 3 — Production hardening [COMPLETE]
- **Proxy rotation**: pluggable proxy provider config (Bright Data, Oxylabs, etc.)
- **robots.txt compliance**: respect/ignore toggle per job
- **Billing/quotas**: per-user job limits, usage tracking
- **Admin SPA**: React dashboard for user and job management
- **MCP server**: expose scrape_url, get_result, list_jobs as LLM-callable tools
- **K8s manifests**: production deployment manifests for k3s, added to infra repo

### Phase 4 — Durable Workflows [IN PROGRESS]
A new **Workflows** layer on a durable-execution engine, built *alongside* the existing NATS job
path (nothing ripped out).

- **Backlog:** `docs/project/phase4-backlog.md` — **single source of truth.** Four sections:
  Pre-Phase 4 → the migration → **dissolved by Temporal (do NOT fix)** → survives-Temporal.
  §3 exists because roughly half the Phase 3→4 triage list is deleted outright by the migration;
  check it before fixing any orchestration bug.
- **Pre-Phase 4 queue: two open items — P6 / BUG-005 and P7 (crawl quota)** (reopened 2026-08-04;
  was empty 2026-07-28). **P7, filed 2026-08-08:** crawls consume **zero of all three quota
  meters** — every meter is keyed on `job_runs` and a crawl never creates one, so a 500-page crawl
  costs no monthly runs, no concurrent slots and no counted bytes; `routers/crawls.py` has no quota
  check at all, and **nothing frees crawl artifacts** (even deleting a user orphans them in MinIO).
  Same family as BUG-005 — a lane invisible because the contract named a table. PM decided in
  PRD-016 OQ-4 round 3 (✅ owner-confirmed 2026-08-08): crawls join ADR-009 §3's run-counting view,
  **per page** for monthly runs and storage, **per crawl** for concurrency; reclaim ships with
  counting; accounting starts at cutover with no backfill. Decision blocks ADR-009 §3/§8;
  implementation is pre-migration, after P6.
  Q6, Q5/Q7, BUG-003, UF-001, UF-003, BUG-002 and Q1–Q4 are all closed and verified in production;
  tagged **`prephase4`** (`1965953`). **BUG-005 — batch is broken on all three execution paths**,
  silently: playwright batch drops every message as malformed and hangs at `pending`; http batch
  "succeeds" but writes every item to `latest/.html` + `history//{ts}.{ext}`, so items overwrite
  each other and two tenants can be served the same object; batch + LLM hangs at `processing` and
  the batch never completes. One root cause — **`job_id` is NULL for batch runs (correct, per
  ADR-006) while the message schemas and the ADR-002 §8 artifact-path convention both assume it is
  not.** Fixed pre-migration on the Q6 precedent despite §3 (live, silent, shipped feature). Latent
  in prod — batch appears unused. Full writeup: `open-bugs.md` → BUG-005.
  Per-item detail on the closed items lives in `phase4-backlog.md` §1. Two carry-forwards that are
  **not** history, because they are business logic living
  inside plumbing the migration deletes (backlog §3, PRD-016 OQ-6): the **LLM cold-start handling**
  (`ensure_ready()` + 180s timeout) and the **transient/terminal storage-fault classifier** on all
  three workers. Both must be **ported into the Temporal activities, not deleted with NATS.**
  Deferred and not blocking: **BUG-004** (screenshots orphaned on every path) and **BUG-006**
  (**Dependabot scans 3 of 6 manifests** — `coordinator/`, `llm-worker/` and `playwright-worker/`
  have no lockfile and are unmonitored, so the true advisory count is unknown; the visible aiohttp
  high is the *unreachable* copy while the reachable one — `coordinator/sitemap.py` fetching
  robots.txt/sitemaps from user-supplied target sites — is in a service nothing scans. **Do not
  close it as dissolved by the migration:** the coordinator is deleted, but sitemap discovery
  *ports into a `CrawlWorkflow` activity* and carries the exposure unless the port uses **httpx**,
  as every other untrusted-target fetch already does). Alert count as of 2026-08-05: **51 open —
  2 high, 34 medium, 15 low**, all against the three scanned manifests.
  **`develop` and `main` are level and deployed.**
- **Engine: Temporal** (chosen over DBOS/Restate for portfolio value + Python/Go SDKs). Grounded in the **Q8** incident — the hand-rolled `result_consumer` state machine that caused a live feedback loop.
- **Feature (nested layers):** user-defined **Pipelines** (scrape → clean → LLM → validate → deliver) → **Delivery sinks** (S3/DB/Sheet/email, saga rollback) → long-lived **Monitors** (durable sleep + human-approval, absorbing the dormant scheduled-crawl path).
- **Rollout:** one product, two engines — route new work to v2 (Temporal), drain + cut v1 (NATS) per-flow when proven; reversible each step. End state retires `result_consumer`/`scheduler`/`webhook_loop`/`advisory`/`coordinator` + NATS, and makes the API thin/horizontally scalable.
- **Docs:** `docs/project/workflows-scoping.md` (feature + engine comparison — §7's "prototype on DBOS first" is **superseded**, the engine is settled), `docs/project/temporal-full-migration.md` (complete change inventory + migration sequence), `docs/project/phase4-prd/` (PRDs).
- **PRD-016 — Workflows: Pipelines ✅ written + PM-reviewed, ready for Architect** (`docs/project/phase4-prd/PRD-016-workflows-pipelines.md`). Covers layer **A only**; Delivery (C) and Monitors (B) get their own PRDs. **R6 is the acceptance gate:** reproduce today's `scrape → LLM → webhook` recipe as a pipeline *before* designing any new block type — equivalence judged on **structure and mechanics, not byte-equality** (the LLM block is nondeterministic), and **four** divergences from the job path are named up front as known exclusions. **R4 makes "retry lives in exactly one visible layer" a hard requirement** — the Q5/Q6/Q7 lesson written into the spec — and now also covers **time budgets** (they must compose; a run ceiling shorter than the sum of block ceilings *is Q6 again*) and the **fail-closed** rule (unknown error → terminal). **PM review round 2 (2026-08-04)** settled three Architect escalations in place: **no change-detection / cost gate in layer A** (both halves go to Monitors — "the previous run of this same thing" is undefinable once R1 run inputs exist; **B cannot ship without the gate**), **at most one Webhook block per pipeline** rejected at save time (multi-destination is layer C's, and arrives with saga rollback), and **cancellation never aborts a block mid-execution** (the Scrape exception is dropped).
- **ADR-009 — Workflow Engine: Temporal + v1/v2 coexistence — 📝 DRAFT, section-by-section review IN PROGRESS** (`docs/adr/ADR-009-workflow-engine-temporal.md`). **Still not Accepted — do not implement against it or cite the document as settled.** Individual sections *are* being settled as the review passes over them; the ADR's **Review log** (status block, top of file) is authoritative for which. **Reviewed so far: §1 (taken as settled pre-ADR), §2, §3 — plus §12, reversed as a knock-on. Next: §4 (block model).** What the review changed, none of it cosmetic: **§2** gained four sub-decisions (**2a** Temporal persistence is a **separate Postgres instance** — not a second DB on the app instance, which buys no isolation; note Temporal wants **two** DBs inside it and its schema is `temporal-sql-tool`'s, a **second migration mechanism** alongside Alembic; **2b** the **Web UI is NOT exposed** — `kubectl port-forward` only, because it *terminates/cancels/signals/resets* workflows and so is a write-capable control plane, not a dashboard; **2c** namespace retention **30 days**, cheap *only because* §5's references-not-payloads holds; **2d** capacity — the +1.5–2 CPU figure **is the coexistence peak**, and the node's **162% CPU limit overcommit** means throttling that looks like a workflow bug). **§3 took two factual corrections found against live code:** crawls are **already** an uncounted lane (so this is a live gap, not migration prep — filed as **P7**), and the **`storage_bytes` exemption was wrong** — all **three** meters are blind to crawls, not two. **§12 was reversed:** "user identity in the workflow ID" is withdrawn — Temporal never parses a workflow ID, so the property was never structural; **the API's ownership check is the only tenant boundary**, with nothing backing it at the engine. Answers all 11 of PRD-016's open questions. Load-bearing: pipeline runs get **their own table** and quota counting moves onto a **view** (today's meters recount and hardcode `FROM job_runs`, so a new lane is invisible by construction — BUG-005's lesson generalised); **explicit named block wiring**, not implicit chaining; blocks pass **references, never content** (workflow history is retained after completion; real pages run 291 KiB–4.1 MiB), with the v2 artifact path keyed on the **run** and `latest/` dropped; in-flight edits **pin** the definition version (adopting mid-run breaks replay determinism); **one run = one unit**, shared pools, **only the final artifact charged**; webhooks take OQ-11 **option (c)** on a horizon matching today's ≈2.6 h reach, with **no delivery table** on the v2 lane. **Two warnings:** §9 — integration option (a) puts **two retry layers** on the same work and recreates Q5/Q6/Q7 unless NATS-side retry is neutralised for workflow-originated messages; §10 — `diff.py` + content-hash are **relocated, not deleted, and not yet re-homed**, so they must outlive `result_consumer.py` and wait for Monitors.

### Phase 3 — Build Process
Phase 3 simulates how a larger engineering organization works by dividing the build process across distinct Claude personas. Each persona owns a specific part of the process and produces defined outputs before handing off to the next.

| Persona | Responsibilities | Outputs |
|---------|-----------------|---------|
| **Product Manager** | Defines scope, priorities, success criteria, and stakeholder requirements for each feature | PRD per feature, prioritized backlog |
| **Software Architect** | Translates PRDs into technical design decisions, system contracts, and ADRs | Design docs, ADRs, updated engineering spec |
| **Tech Lead** | Breaks the engineering spec into an ordered implementation backlog with dependencies and sequencing | Task breakdown, sprint plan, dependency graph |
| **Engineer(s)** | Implements tasks from the backlog, writes tests, raises blockers to Tech Lead | Code, tests, implementation notes |

Each persona operates with only the outputs from the persona before them — the Engineer does not read the PRD; the Architect does not second-guess the PM's priorities. This mirrors how information flows in real organizations and surfaces the communication gaps between roles.

---

## Key decisions

| Concern | Decision | Rationale |
|---|---|---|
| Auth provider | Clerk | Handles OAuth, JWT, user mgmt out of the box |
| Tenancy | Multi-tenant | Each user has isolated jobs/data |
| Scraping engine | HTTP first, Playwright opt-in later | Most structured data sites are server-rendered |
| LLM output | User provides own API key + schema | Avoids shared LLM cost; users control their models |
| Proxy rotation | Skip for MVP (done — Phase 3) | Not needed for MVP scale; added as pluggable provider in Phase 3 (PRD-005) |
| Change detection | Yes, Phase 2 | Key feature for ML data pipeline use cases |
| Output formats | Raw HTML, cleaned Markdown, JSON | Feed directly into ML pipelines |
| Worker design | Light worker — NATS + MinIO only, no DB access | Keeps worker DB-ignorant; all business logic in API |
| Job dispatch message | Fat message `{job_id, url, output_format}` | Worker needs no DB lookup to execute the scrape |
| Worker→API result | Worker publishes to `scrapeflow.jobs.result`; API background consumer updates DB | Decoupled; worker never touches Postgres |
| Cancellation | API sets `status=cancelled`; result consumer discards worker results for cancelled jobs | Worker is unaware of cancellations; API enforces correctness |
| NATS stream creation | Outside API/worker (init container / infra); API asserts stream exists at startup | API has no infra concerns |
| Cross-tenant access | 404 (not 403) for jobs belonging to other users | 403 leaks resource existence; 404 is safer for multi-tenant |
| NATS subject constants | `app/constants.py` (not `settings.py`) | Subject names are part of the worker contract, not env-configurable |
| Rate limiting | Fixed window counter (Redis `INCR` + `EXPIRE`) per user; sliding window planned for Phase 2 **[superseded — see Phase 3 sliding-window row below]** | Simple, 2–3 Redis ops; adequate for MVP quotas |
| Cancellation (Phase 2) | Cancel active `job_runs` rows (not `jobs.status`); result consumer discards by checking `run.status == "cancelled"` | `jobs` no longer has a `status` column after migration 2.4 |
| MinIO path convention | Dual write: `latest/{job_id}.{ext}` (overwritten) + `history/{job_id}/{unix_ts}.{ext}` (immutable); `job_runs.result_path` always stores the `history/` path | history path enables per-run diff; latest path for convenience access |
| Worker routing (Phase 2) | Subject-based: `scrapeflow.jobs.run.http` for Go worker, `scrapeflow.jobs.run.playwright` for Playwright worker | Workers subscribe to their own subject; wrong engine never receives the message |
| `nats_stream_seq` | Stored on `job_runs` from the worker's "running" result message | MaxDeliver advisory carries only stream seq — used to identify stalled runs (Step 22) |
| Rate limiting (Phase 3) | Sliding window via Redis sorted set + Lua atomic script | Fixes 2× burst exploit in fixed-window; PRD-002 |
| SSRF re-validation | Re-validated on every webhook delivery attempt, not just job creation | DNS rebinding attack — URL can resolve differently at delivery time; PRD-003 |
| Batch data model | New `batches`/`batch_items` tables; nullable `job_id` on `job_runs` + `batch_item_id` FK; mutual exclusion CHECK constraint | ADR-006 Option B — keeps `jobs` as template-only, workers unchanged |
| BFS crawl coordinator | Dedicated Python process at `coordinator/`; BFS queue persisted in `crawl_queue` Postgres table | ADR-005 Option B — API rolling deploys must not abort in-progress crawls |
| `jobs.updated_at` | Postgres BEFORE UPDATE trigger, not SQLAlchemy `onupdate` | `onupdate` silently skips `db.execute(update(...))` paths (scheduler, cancel route); trigger fires on every UPDATE regardless of path |
| Batch `job_runs` routing | `job_runs.batch_item_id` FK set; `job_id = NULL` for batch runs; result consumer routes by checking which FK is set | ADR-006 — workers are unchanged, result consumer gains a routing branch |
| API keys uniqueness | `UniqueConstraint(user_id, name)` on `api_keys`; `POST /users/api-keys` returns 409 on duplicate | Revoked keys still hold their name — names are identifiers, not recycled |
| Disabling a scheduled job (Q4) | Its **own operation**, not a side effect of `DELETE`: `PATCH /jobs/{id}` `{"schedule_status": "paused"}`. `jobs.schedule_status` is **tri-state** — nullable, `CHECK IN ('active','paused')`, where `NULL` = not a scheduled job at all. The scheduler selects only `schedule_status = 'active'` (`scheduler.py`), backed by a partial index. `DELETE /jobs/{id}` keeps soft-cancel (active `job_runs` only, **never** touches `schedule_status`); hard delete is the explicit `?permanent=true` mode | Cancelling a run and retiring a schedule are different intents — folding them into `DELETE` would make a one-off cancel silently permanent for a recurring job. Splitting them is what lets one route back both the user-facing soft **Cancel** and the admin permanent-delete. A bare `is_active` boolean (the originally recommended option) could not express "not scheduled at all". **Migration-critical:** this flag is how cutover gotcha #2 is enforced — a job moved to a Temporal Schedule must be paused in v1 or it fires on **both** lanes |
| Page actions field naming | Schema field `actions` maps to model column `playwright_actions`; popped from PATCH updates dict and set explicitly (same pattern as `proxy_url`/`cookies`) | Consistent with `playwright_options` naming convention on the model |
| Action warnings persistence | Worker publishes warnings in NATS `ResultMessage`; result consumer persists to `job_runs.warnings JSONB`; `GET /jobs/{id}/result` reads from DB column | Warnings are not stored in MinIO content — they live in the result message and are captured by the consumer |
| Webhook event filter | `jobs.webhook_events TEXT[]` (null = all events); filter checked at every `create_webhook_delivery` call site in `result_consumer.py` | Null means no filter (backward compatible); validated against known event set at API boundary |
| Content deduplication | `job_runs.content_hash VARCHAR(16)` — xxh64 of raw MinIO bytes, truncated to 16 hex chars; checked in result consumer before LLM/diff dispatch; on match: `diff_detected=False`, history/ object deleted, no LLM, no webhook | Only on regular job path (not batch, not crawl); hash stored even when no previous run exists (first-run baseline); fail-open — hash error skips dedup silently |
| Admin result content | Admins read another user's scraped output via a dedicated `GET /admin/jobs/{id}/result`; the user-facing `GET /jobs/{id}/result` stays owner-scoped (404 for others). Both share `load_completed_result()` in `routers/jobs.py` | Owner check can't be relaxed on the user route; admin needs its own endpoint. `user_email` also joined into admin `JobResponse` (list + detail) — the `User` model has `email` only, no `name` |
| Frontend asset bundling | Admin SPA is served from `/app/` with no external-CDN dependency — all assets bundled locally by Vite. Monaco (result viewer) is configured in `frontend/src/lib/monaco.ts` to load from bundled workers, not jsdelivr, and is lazy-loaded so it stays out of the initial bundle | A CDN-loaded dependency would break offline / under CSP; future frontend deps must bundle locally too. Frontend is on **vite 8 + `@vitejs/plugin-react` 6** (4.x peers only vite ≤7) |
| Admin vs user job views (frontend) | Same `Jobs`, `JobDetail`, `ResultViewer` components serve both the admin panel (`/app/admin/jobs*`, all users' jobs via `/admin/jobs*`) and the user dashboard (`/app/dashboard/jobs*`, own jobs via owner-scoped `/jobs*`), switched by a `mode: 'admin' \| 'user'` prop — not duplicated. `mode` derives the API base, route/link base, and result endpoint; admin-only UI (User column, user filter, User detail row) renders only when `mode==='admin'`. User Job Detail exposes soft-cancel (`DELETE /jobs/{id}`), admin exposes permanent delete | Differences are ~5 localized conditionals per file; duplicating would drift. Backend needed no change — owner-scoped `/jobs*` already 404 cross-tenant. `user_email` is `null` on user routes, so the shared column degrades cleanly when hidden |
| Frontend admin detection | No role claim on the Clerk JWT — admin status is inferred by probing `/admin/users?limit=1` (200 = admin), in the shared `lib/useIsAdmin.ts` hook (query key `['admin-check']`). Used by `RequireAdmin` (route gate) and `Layout` (nav cross-link) | Backend `/admin/*` authorization is the real gate; the SPA just reacts to 200 vs error. Shared query key means gate + nav read one cached result, no duplicate request |
| App shell height | `Layout` root is `h-screen` (fixed viewport), not `min-h-screen`, so the sidebar is pinned and `main` (`overflow-auto`) scrolls internally | `min-h-screen` let tall pages (Jobs/Stats) grow the whole page, pushing the sidebar footer (nav cross-link + Sign out) below the fold |
| Playwright anti-bot (stealth) | `playwright-worker` runs **Patchright** (drop-in Playwright fork) driving **real Google Chrome** (`channel="chrome"`), **truly headed under Xvfb**; launch args `--disable-blink-features=AutomationControlled` (+ `--no-sandbox`, `--disable-dev-shm-usage`); `new_context(no_viewport=True)`; **no UA spoofing**. Container start is `entrypoint.sh` (start Xvfb → wait for socket → `exec python` as pid 1), **not** `xvfb-run`. All env-tunable in `worker/config.py` | Stock legacy-headless Chromium failed BrowserScan on **webdriver / User-Agent / CDP**. Verified: only headed real Chrome yields a clean UA (even `--headless=new` leaks `HeadlessChrome`); `--disable-blink-features=AutomationControlled` clears `navigator.webdriver` (Patchright's `launch()` alone didn't); Patchright patches the CDP `Runtime.enable` leak. **Verified in prod: BrowserScan `Normal`, 0 Robot / 18 checks.** k8s resources bumped (infra `538fba5`). Full record: **ADR-008** + `docs/guides/anti-bot-hardening.md` |
| Playwright worker container start | `entrypoint.sh` (`exec python` as pid 1), **never** `xvfb-run` as the entrypoint | `xvfb-run` as pid 1 stays alive after the Python worker dies → container looks healthy, k8s never restarts a dead worker. `exec python` makes crashes surface (CrashLoopBackOff). Also: `ENV PYTHONUNBUFFERED=1` (else crash logs are lost) and pre-create `/tmp/.X11-unix 1777` (Xvfb won't as non-root) |
| Playwright NATS consumer `ack_wait` | Explicit `ConsumerConfig(ack_wait=120)` on the `python-playwright-worker` pull consumer + `msg.in_progress()` heartbeat every 30s during a job | JetStream default `ack_wait=30s` < a headed-Chrome scrape (~37s) → redelivery mid-job, no-op late `ack()`, and with `max_deliver=-1` an **infinite re-scrape loop** (Q6, fired in prod 2026-07-03). Heartbeat covers jobs longer than `ack_wait`. **JetStream won't change `ack_wait` on an existing durable consumer — update/recreate out-of-band** (`add_consumer` with modified config; this nats-py has no `update_consumer`). **LLM worker audited and fixed — see next row.** |
| LLM NATS consumer `ack_wait` + SDK retry pin | Explicit `ConsumerConfig(ack_wait=120)` on the `python-llm-worker` pull consumer + `msg.in_progress()` heartbeat every 30s, **plus `max_retries=settings.llm_max_retries` (default `0`) pinned on both the `AsyncAnthropic` and `AsyncOpenAI` clients** | Same Q6 bug as playwright, but worse: `llm_request_timeout_seconds` (60) is **2× the 30s default `ack_wait`**, so redelivery fired on ordinary slow calls, not just heavy ones — and each redelivery re-bills the **user's own** API key, unbounded (`max_deliver` unset → `-1`). Critically, **the playwright numbers do not transfer**: both provider SDKs default to `max_retries=2`, so one `call_llm()` could make 3 billable attempts ≈ 210s wall-clock, well past a 120s `ack_wait`. Hence the pin — retry now lives in exactly one visible layer, which drops the real ceiling back to ~70s. Here **`ack_wait` is the orphan-recovery window, not a job-duration budget**; the heartbeat is what actually covers long calls. Raising `llm_max_retries` re-hides retries *beneath* whatever retry layer sits above it (today NATS; Phase 4 Temporal `RetryPolicy`) and silently multiplies both cost and wall-clock. Same out-of-band consumer-recreate caveat as the row above |
| NATS consumer config changes | **JetStream never applies a changed `ConsumerConfig` to an existing durable.** Procedure: deploy image → confirm pod → `nats consumer rm <STREAM> <DURABLE> -f` → `kubectl rollout restart` → verify with `nats consumer info`. **Delete before restart**; **never scale to 0** | The worker only calls `pull_subscribe` at startup, so deleting under a running pod leaves it holding a subscription it won't rebuild; scaling to 0 loses to Flux, which reconciles the declared `replicas: 1` straight back. Letting the *worker* recreate the consumer keeps **code as the source of truth** — no hand-applied setting to forget. Safe because the `SCRAPEFLOW` stream is `--retention work`: acked messages are deleted, so a fresh consumer cannot replay completed jobs (verify `Unprocessed`/`Outstanding Acks` are 0 first). **Verification trap (two layers):** the worker's `subscribed` log line prints values from *config*, not from the live consumer — it logged `ack_wait=120` while JetStream ran `30`, and later `max_deliver=3` while JetStream ran `-1`. Only `nats consumer info` is authoritative — and for `max_deliver`, only **`nats consumer info --json`**: the table output **omits the `Max Deliver` row entirely when the value is `-1`**, so an uncapped consumer and a correctly capped one look identical. **Between the delete and the restart there is a ~30s window where the old pod is `1/1 Running` and healthy to k8s but consuming nothing** (it logs `fetch_error` with backoff, then self-shuts-down cleanly); verify `Unprocessed`/`Outstanding Acks` are 0 first so nothing is dispatched into that gap |
| LLM cold starts (scale-to-zero) | `llm_request_timeout_seconds=180` (Modal cold start is 90–110s) **+** `ensure_ready()` warm-up probe against the OpenAI-compatible **`/models`** endpoint before the real call — `openai_compatible` **with** a `base_url` only, 60s process-local warm cache, any HTTP response (incl. 401/404) counts as awake | A cold start exceeded the old 60s read timeout, and the worker acked on failure, so the job failed **permanently**. This was masked until `llm_max_retries` was pinned to `0` (Q6) — the SDK's default `max_retries=2` meant a later attempt landed after Modal booted. **The Q6 pin and the timeout bump are safe together and unsafe apart.** `/models` is in the OpenAI spec and implemented by vLLM/Modal; `/health` is not standardised. Anthropic/hosted OpenAI are never probed — no health endpoint, no cold start. `ack_wait` stays 120s: the 30s heartbeat covers the long call |
| Bot-wall detection (BUG-003) | `playwright-worker/worker/blocking.py` classifies the rendered page **after `final_url`, before `format_output`**; on a block the worker publishes **`failed` with `error="blocked:<vendor>"`** (existing status — no new state value) and does **not** upload the wall. Tiered: **Tier 1** = vendor challenge harnesses (Akamai, Cloudflare, PerimeterX, DataDome, Imperva, Sucuri, Kasada, Amazon), decisive alone at any size; **Tier 2** = generic challenge language, gated to pages **< 20 KB**; **Tier 3** (structural integrity) deliberately unimplemented. Vendor lives in the **error string plus a `block_detected` log line**, not a column | **A block is the server deliberately serving something else because it identified us as a bot — status is evidence, not definition.** The canonical Amazon wall is a **200** with valid HTML, which is why it flowed through as `completed`. Operative test for any new signal: *would a human on a normal browser have gotten the real content?* If yes → block; if no → that page is the site's honest answer, so **paywalls / login walls / geo-blocks / age gates / genuine 404s are NOT blocks**. **Posture is the inverse of `llm-worker/worker/errors.py`**: that fails *closed* (a wrong "transient" re-bills the user's key); here a wrong "blocked" fails a working job, so be conservative about *claiming* a block — hence the Tier 2 size gate (those phrases are legitimate content at full page size). Prod audit 2026-07-22: **6 of 15 completed runs were walls**, all `engine=playwright`; the Go worker's `fetcher.go:72` non-2xx check already covers hard walls. **`final_url` is NOT a signal** — Amazon serves the wall *at the requested URL*; `/errors/validateCaptcha` is only a form action. Detection must see **raw HTML** — markdown conversion strips every HTML-level signal. No column for vendor: nothing branches on it yet, so it would be data written and never read, and the Temporal migration would have to carry it. Tier 1/2 patterns adapted from **Crawl4AI** (Apache-2.0, attribution in `README.md`); Amazon is our own addition. Getting *past* walls (retry-on-fresh-IP, unblocker providers) is a **later phase**, gated on UF-002 |
| LLM transient vs terminal failures | `worker/errors.py` classifies the exception; transient (timeout, 429, 5xx, MinIO backend fault, **MinIO unreachable via `aiohttp.ClientConnectionError`/`ServerTimeoutError`**, warm-up timeout) → `msg.nak(delay=…)` with exponential backoff, **no `failed` published**; terminal (bad/undecryptable key, 400/401/403/404) → publish `failed` + ack. Attempt cap enforced in-worker via `metadata.num_delivered`, with `max_deliver` as the consumer-side backstop. **Unknown exceptions default to terminal** | The worker used to ack on *every* exception, so a cold-start timeout was as permanent as a bad API key — the queue could not retry because the worker preempted it. Publishing `failed` on a retry would be locked in by the API's terminal-status guard (`result_consumer.py:125`), which then discards the successful retry's `completed` — hence only the final attempt reports an outcome, and the worker (not JetStream) decides when that is, so a run can't dangle in `processing`. Fails closed because the risk is asymmetric: a wrong "transient" guess retries against the **user's own** API key (the Q6 failure mode); a wrong "terminal" guess fails one job. Retry must stay in **one visible layer** — do not restore `llm_max_retries=2` on top of this (3 × 3 = 9 billable calls). Backoff state resets on worker restart; durable retry is Temporal `RetryPolicy`'s job in Phase 4 |
| Transient MinIO failure = `nak`, not `ack` (all workers) (UF-003 3a) | The LLM worker's transient/terminal classifier is the template, ported to **each** worker: a MinIO **write** fault (result/screenshot upload) is *transient* → `nak` with exponential backoff up to a per-worker attempt cap, publishing terminal `failed` only on the last attempt. Playwright = `playwright-worker/worker/errors.py` (`2432be7`); Go worker = `http-worker/internal/worker/errors.go` (`fbce01f`); LLM already had it (Q5). Failures against a *dead target site* (navigation/fetch) stay **terminal** — only *infra* faults retry | The ack-on-failure bug was latent on all three workers; Q5 fixed only the LLM one, so a momentary MinIO outage permanently failed a job whose expensive work (headed-Chrome render / paid LLM call) had already succeeded. **The non-obvious trap: "MinIO down" (connection refused) is a *different exception class* from "MinIO returned a 5xx".** miniopy-async's backend is aiohttp, so unreachable → `aiohttp.ClientConnectionError` (no `.code`), which the `S3Error.code`-only match misses — this bit the LLM worker too (`6ad95e3`). Both Python classifiers now cover both; **Go's `minio-go` got the same split** — `net.Error`/`*url.Error` (unreachable) *vs* `minio.ErrorResponse.Code` (5xx). **Go-specific divergence from the Python port:** in Python a connection error can only come from MinIO, so classifying by exception *type* is safe. In Go both the `net/http` fetcher and `minio-go` use the net stack, so a dead *site* and a dead *MinIO* both raise `*net.OpError`/`*url.Error` — a bare `net.Error` is ambiguous. So transient-eligibility is scoped to the **upload step**: `processJob` wraps the `Upload()` error in a typed `*uploadError`, and only inside that does `classifyMinIO` apply. A fetcher net error stays terminal. Also note the Go consumer **already sets `NATS_MAX_DELIVER` (3)** as the attempt cap — the Python workers had it `-1`. Terminal-for-dead-sites is deliberate: a re-scrape costs a headed-Chrome render + proxy bandwidth, and a dead site is its own answer. Fail-closed default (unknown → terminal), same as the LLM row. Deleted by the Temporal migration, but the transient/terminal split is **domain knowledge** that ports into the activity `RetryPolicy` (backlog §3) |

---

## MVP definition

> "Submit a URL via API → get back raw or cleaned data (HTML/Markdown/JSON) → check job status → usable in an ML pipeline"
