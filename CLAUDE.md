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
- **Pre-Phase 4 queue:** Q6 LLM `ack_wait` ✅ **closed in prod** → Q5 cold starts ✅ **closed in
  prod** (`fbcf254`; consumer recreated, `max_deliver: 3` verified) → **next: UF-001 MinIO health
  check / BUG-003 bot walls** → Q1–Q4 close-out → BUG-002 Dependabot crit+highs (now 8 crit / 13
  high, drifting up).
- **Engine: Temporal** (chosen over DBOS/Restate for portfolio value + Python/Go SDKs). Grounded in the **Q8** incident — the hand-rolled `result_consumer` state machine that caused a live feedback loop.
- **Feature (nested layers):** user-defined **Pipelines** (scrape → clean → LLM → validate → deliver) → **Delivery sinks** (S3/DB/Sheet/email, saga rollback) → long-lived **Monitors** (durable sleep + human-approval, absorbing the dormant scheduled-crawl path).
- **Rollout:** one product, two engines — route new work to v2 (Temporal), drain + cut v1 (NATS) per-flow when proven; reversible each step. End state retires `result_consumer`/`scheduler`/`webhook_loop`/`advisory`/`coordinator` + NATS, and makes the API thin/horizontally scalable.
- **Docs:** `docs/project/workflows-scoping.md` (feature + engine comparison), `docs/project/temporal-full-migration.md` (complete change inventory + migration sequence). Next artifacts: PRD + engine ADR (ADR-009).

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
| LLM transient vs terminal failures | `worker/errors.py` classifies the exception; transient (timeout, 429, 5xx, MinIO backend fault, warm-up timeout) → `msg.nak(delay=…)` with exponential backoff, **no `failed` published**; terminal (bad/undecryptable key, 400/401/403/404) → publish `failed` + ack. Attempt cap enforced in-worker via `metadata.num_delivered`, with `max_deliver` as the consumer-side backstop. **Unknown exceptions default to terminal** | The worker used to ack on *every* exception, so a cold-start timeout was as permanent as a bad API key — the queue could not retry because the worker preempted it. Publishing `failed` on a retry would be locked in by the API's terminal-status guard (`result_consumer.py:125`), which then discards the successful retry's `completed` — hence only the final attempt reports an outcome, and the worker (not JetStream) decides when that is, so a run can't dangle in `processing`. Fails closed because the risk is asymmetric: a wrong "transient" guess retries against the **user's own** API key (the Q6 failure mode); a wrong "terminal" guess fails one job. Retry must stay in **one visible layer** — do not restore `llm_max_retries=2` on top of this (3 × 3 = 9 billable calls). Backoff state resets on worker restart; durable retry is Temporal `RetryPolicy`'s job in Phase 4 |

---

## MVP definition

> "Submit a URL via API → get back raw or cleaned data (HTML/Markdown/JSON) → check job status → usable in an ML pipeline"
