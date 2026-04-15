# Software Architect — ScrapeFlow Onboarding Document

> **Purpose:** Bring a new Software Architect persona up to speed on everything done, every decision made, and every pattern established through Phase 3 design. Read this before touching any design work.
> **Last updated:** 2026-04-15
> **Covers:** Phase 1 context, all Phase 2 architectural decisions, all Phase 3 architectural decisions, documents produced, and what Phase 4 will require from you.

---

## 1. What ScrapeFlow Is

A self-hosted, multi-tenant web scraping platform. The primary use case is structured data extraction and change detection to feed ML/data pipelines. It is a portfolio project built to production-grade standards — not a toy. **Phase 3 is not homelab-scoped** — the platform is designed to serve multiple users beyond a single developer deployment.

**The invariant that drives every architectural decision:** the API is the brain, workers are dumb executors. Workers touch only NATS and MinIO — never Postgres. All business logic, state management, and error recovery lives in the API.

---

## 2. The Stack You Are Working With

| Layer | Technology | Notes |
|-------|-----------|-------|
| API | FastAPI (Python) | Async, SQLAlchemy 2.0, Alembic migrations |
| Scrape worker | Go | HTTP worker; pull consumer pattern |
| Playwright worker | Python | New in Phase 2; headless Chromium |
| LLM worker | Python | New in Phase 2; BYOK extraction |
| BFS Coordinator | Python | New in Phase 3; site crawl only (`coordinator/`) |
| Queue | NATS JetStream | WorkQueue retention; pull consumers |
| DB | PostgreSQL | Source of truth for all state |
| Object storage | MinIO | Raw scrape results and structured outputs |
| Cache / rate limiting | Redis | Sliding window (Phase 3); previously fixed window |
| Auth | Clerk | JWT + API keys; users synced to local DB |
| MCP Server | Python | New in Phase 3; standalone user-run process (`mcp/`) |
| Deployment | Docker Compose (dev), k3s (prod) | Domain: scrapeflow.govindappa.com |

---

## 3. Phase 1 — What Was Already Done When You Arrived

Phase 1 delivered a working MVP and 18 pre-Phase-2 cleanup items. Key things already in place:

- Full job CRUD with Clerk auth (JWT + API keys)
- Go HTTP scraper worker consuming NATS JetStream
- MinIO result storage
- Redis-backed per-user rate limiting
- ADR-001 defining the original API↔worker contract
- SSRF protection (`_validate_no_ssrf()`) in `api/app/core/security.py`
- Atomic rate limiter, graceful shutdown, correlation IDs

**ADR-001 principles you must preserve** (these are load-bearing):
- Fat message dispatch — worker needs no DB lookup
- Worker publishes results to `scrapeflow.jobs.result`; API result consumer updates DB
- Cancellation: API sets status; result consumer enforces correctness; worker is unaware
- NATS stream created outside API/worker (init container); API asserts existence at startup

---

## 4. Phase 2 Architectural Decisions — The Full Record

This is the core of your onboarding. Every decision below was made deliberately. Do not revisit them without understanding the rationale.

---

### 4.1 Job Model Split: `jobs` as Templates, `job_runs` as Executions

**Decision:** Remove `status`, `result_path`, and `error` from the `jobs` table. Create a new `job_runs` table to hold every execution record.

**Why:** Phase 2 introduces recurring/scheduled jobs. A job is now a *definition* (URL, engine, schedule, options) that produces multiple runs over time. Storing execution state on the template row collapses the history — you can only know the latest outcome, not the full run history needed for diff-based change detection.

**What this means in practice:**
- `jobs` row = the template. Never deleted by cancellation. Mutable only for fields that don't break diff history (see §4.10).
- `job_runs` row = one execution. Created at dispatch. Status lifecycle: `pending → running → processing → completed/failed/cancelled`.
- `GET /jobs/{id}` JOINs `jobs` with its latest `job_runs` row via a LATERAL join.
- `DELETE /jobs/{id}` = cancel the active run (sets `job_runs.status = 'cancelled'`). Hard delete of the `jobs` row is a separate operation.
- All result consumer logic references `run_id`, not `job_id`, for state transitions.

**Phase 3 extension:** `job_runs.job_id` is now nullable (ADR-006). A run belongs to either a `jobs` row (regular job) or a `batch_items` row (batch scraping). Never both. Enforced by a Postgres check constraint. See §5.3.

---

### 4.2 NATS Subject Routing: One Subject Per Worker Type

**Decision:** Replace the single `scrapeflow.jobs.run` subject with separate subjects per worker type.

```
scrapeflow.jobs.run.http       → Go HTTP worker
scrapeflow.jobs.run.playwright → Python Playwright worker
scrapeflow.jobs.llm            → Python LLM worker (conditional second stage)
scrapeflow.jobs.result         → All workers → API result consumer (unchanged)
```

Stream wildcard: `scrapeflow.jobs.>`.

**Why:** Workers should be dumb. Separate subjects mean each worker consumes only its messages; the API chooses the subject at dispatch time based on `job.engine`.

**ADR-002** is the authoritative reference. When ADR-001 and ADR-002 conflict, ADR-002 takes precedence.

---

### 4.3 Two-Stage LLM Pipeline

**Decision:** Go/Playwright worker handles ALL scrape jobs. Python LLM worker is a conditional second stage — only activated when `job.llm_config` is set.

**Discriminator invariant** (critical — do not break): The result consumer uses `job_run.status` to distinguish source:
- `status = 'running'` when `completed` arrives → came from a scrape worker → check llm_config
- `status = 'processing'` when `completed` arrives → came from the LLM worker → skip LLM routing

No code path other than the LLM dispatch branch may set `job_run.status = 'processing'`.

---

### 4.4 Pull Consumer Pattern

**Decision:** All workers use NATS pull consumers with a fixed worker pool. No push subscriptions.

**Critical AckWait safety pattern:**
```go
available := cap(sem) - len(sem)
if available == 0 {
    time.Sleep(100 * time.Millisecond)
    continue
}
msgs, err := sub.Fetch(available, nats.MaxWait(5*time.Second))
```
Fetching more messages than available slots causes in-process queueing while NATS's AckWait timer runs, triggering redelivery and duplicate processing.

---

### 4.5 Scheduler Design: DB Polling Loop

**Decision:** Recurring job scheduling is an asyncio background task in the API, polling Postgres every 60 seconds.

**Multi-instance safety:** `SELECT ... FOR UPDATE SKIP LOCKED`. Two API instances grab different jobs; neither grabs the same job twice.

**Atomicity rule:** DB commit **before** NATS publish. If NATS publish fails, the `job_runs` row stays `pending`. Stale-pending recovery re-dispatches on next cycle (any run with `status = 'pending'` and `created_at < NOW() - 10 minutes`).

---

### 4.6 Change Detection: MinIO Dual-Path + Diff Strategy

**MinIO path convention:**
- `latest/{job_id}.{ext}` — always overwritten
- `history/{job_id}/{timestamp}.{ext}` — append-only, one object per run

`job_runs.result_path` always stores the `history/` path.

**Diff strategies:**
- Non-LLM jobs: `compute_text_diff` — normalised text diff
- LLM jobs: `compute_json_diff` — field-by-field structured JSON comparison

---

### 4.7 Webhook Delivery: Postgres-Backed, Not NATS

**Decision:** Webhook deliveries in a `webhook_deliveries` Postgres table, retried by an asyncio background loop.

Retry schedule: immediate → 30s → 5min → 30min → 2h → exhausted. Configurable via `WEBHOOK_MAX_ATTEMPTS`. Backoff index capped with `min(attempts, len(BACKOFF_SECONDS) - 1)`.

HMAC signing: `X-ScrapeFlow-Signature: sha256=<hmac_hex>`. Per-job Fernet-encrypted secret.

---

### 4.8 LLM Key Storage

LLM API keys in `user_llm_keys` table, Fernet-encrypted. `jobs.llm_config` JSONB stores `llm_key_id` reference only — never the key value.

**Critical:** Always add `return` after the `llm_key is None` failure path in the result consumer — without it, execution falls through to diff computation and fires `"job.completed"` for a failed run.

---

### 4.9 `nats_stream_seq` — MaxDeliver Advisory Bridge

`job_runs.nats_stream_seq BIGINT NULL` is set on the first `status: "running"` result message. Bridges the NATS MaxDeliver advisory (which only carries stream sequence number) to the correct `job_runs` row. Without this column, NATS-abandoned jobs are stuck in `running` permanently.

---

### 4.10 Immutable Job Fields

| Field | Why immutable |
|-------|--------------|
| `url` | Changing URL invalidates all historical diffs |
| `engine` | Mixing HTTP/Playwright in same diff chain breaks diff semantics |
| `output_format` | Same reason |

Mutable fields: `schedule_status`, `schedule_cron`, `webhook_url`, `llm_config`, `playwright_options`, `respect_robots`, `actions`, `webhook_events`.

---

### 4.11 Admin Panel: Same FastAPI App

**Decision (Phase 2 and Phase 3):** Admin routes stay on the same FastAPI app. Not a separate service.

`get_current_admin_user` middleware is the enforcement boundary. A VPN-restricted `admin.scrapeflow.*` subdomain (which would justify a service split) is a Phase 4 consideration if the platform requires it. For Phase 3, the application-layer check is sufficient.

---

### 4.12 SSRF Protection

`_validate_no_ssrf()` is in `api/app/core/security.py`. Applied at:
- `POST /jobs` — job URL and webhook_url
- `POST /users/llm-keys` — base_url for openai_compatible providers
- **Phase 3 addition:** Re-validated on every webhook delivery attempt (not just at creation). DNS rebinding mitigation. If SSRF check fails at delivery time: mark delivery as `exhausted` immediately, no retry.

---

### 4.13 Alembic Migration Approach

Two things autogenerate cannot handle — always append manually:
1. Data migrations (INSERT/UPDATE inside `upgrade()`)
2. `ALTER TYPE ... ADD VALUE` — requires `COMMIT`/`BEGIN` trick. **Do not use `transaction = False` at module level** — Alembic silently ignores it.

```python
def upgrade() -> None:
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'processing' AFTER 'running'"))
    op.execute(sa.text("BEGIN"))
```

---

## 5. Phase 3 Architectural Decisions

Phase 3 design work is complete. ADRs 004–007 are accepted. The engineering spec is at `docs/phase3/phase3-engineering-spec.md`.

---

### 5.1 Scale Change — Phase 3 Is Not Homelab-Scoped

**Decision made 2026-04-15:** Phase 3 targets multi-user production deployments, not a single-developer homelab. This change was reflected in all affected documents (`CLAUDE.md`, `BACKLOG.md`, `PRD-001`, `PRD-011`, `PRD-012`, `PHASE3_DEFERRED.md`, this document).

**Consequence:** Several decisions that were acceptable at homelab scale are not acceptable at multi-tenant scale. The primary affected decision was BFS coordinator placement (§5.5). PRD-011 and PRD-012 priorities were flagged for PM reassessment — billing/quota enforcement is more urgent on a real multi-tenant platform.

---

### 5.2 Fat Message Schema v2 (ADR-004)

**Decision:** Add `schema_version: 2` and group Phase 3 fields into sub-objects.

```json
{
  "schema_version": 2,
  "job_id": "...", "run_id": "...", "url": "...",
  "output_format": "...", "engine": "...",
  "llm_config": null, "playwright_options": null,
  "credentials": { "proxy_url": null, "cookies": null },
  "options": { "respect_robots": false, "actions": null },
  "crawl_context": null
}
```

**Backward compatibility:** Workers treat absent `schema_version` as version 1. Workers receiving unknown versions log a warning and process best-effort (additive fields).

**Deployment order:** Workers must be deployed before the API when rolling out Phase 3. Workers must handle v2 messages before the API starts sending them.

**Go worker:** Use pointer structs for `Credentials`, `Options`, `CrawlContext` so they deserialise to `nil` when absent.

---

### 5.3 Batch Scraping Data Model (ADR-006)

**Decision:** New `batches` + `batch_items` tables. `job_runs.job_id` is nullable. `job_runs` gains `batch_item_id FK → batch_items`. Mutual exclusion enforced by Postgres check constraint.

**Why not synthetic `jobs` rows:** ADR-003 defines `jobs` as templates. Batch items are not templates. Forcing them into `jobs` requires a discriminator column spreading through every existing `GET /jobs` query.

**Result consumer routing:** Branch on which FK is set. `job_id IS NOT NULL` → existing path. `batch_item_id IS NOT NULL` → update batch counter, detect completion, fire `batch.completed` webhook.

**Workers are unchanged:** Batch item dispatches are standard NATS messages. Workers never know they're processing a batch.

**Caution for future engineers:** Any `JOIN jobs ON job_runs.job_id = jobs.id` must be changed to `LEFT JOIN` or filtered with `WHERE job_runs.job_id IS NOT NULL` to avoid excluding batch runs from aggregate queries.

---

### 5.4 Job Secrets Storage (ADR-007)

**Decision:** New `job_secrets` table with `secret_type ENUM('proxy', 'cookies')`. Encrypted at rest using the same Fernet key as LLM API keys (`LLM_KEY_ENCRYPTION_KEY`).

**Why not columns on `jobs`:** Phase 4 roadmap adds form-login credentials, OAuth tokens, custom auth headers. Adding encrypted columns per type leads to a wide table. Building the abstraction in Phase 3 avoids a migration under production load in Phase 4.

**Pattern:**
- `UNIQUE (job_id, secret_type)` — one row per type per job
- API responses: `has_proxy: bool`, `has_cookies: bool` — values never returned
- Dispatch: API decrypts and injects into `credentials` sub-object of fat message
- Workers receive plaintext — never touch encryption layer

**Phase 4 note:** `LLM_KEY_ENCRYPTION_KEY` env var name is now a misnomer — it also encrypts proxy and cookie secrets. Rename it to `JOB_SECRET_ENCRYPTION_KEY` in Phase 4 to avoid confusion.

---

### 5.5 Site Crawl BFS Coordinator (ADR-005)

**Decision:** Option B — dedicated Python coordinator process at `coordinator/` in the monorepo.

**Why not Option A (API background task):** API rolling deploys happen frequently on a multi-tenant platform. Option A would interrupt all in-progress crawls for all users on every deploy. Recovering requires persisting the entire BFS frontier to Postgres on every enqueue — at which point you have a worse version of Option B.

**Why not Option C (workers self-enqueue):** Breaks ADR-001 invariant. Workers don't own topology, depth limits, or visited-URL deduplication.

**BFS queue storage:** Postgres `crawl_queue` table (not Redis). Survives coordinator restart, API restart, and full node reboot. Redis outage would silently drop all active crawl frontiers.

**New tables:** `crawls`, `crawl_pages`, `crawl_queue`. See ADR-005 for full schemas.

**Coordinator startup recovery:** On startup, re-enqueue `crawl_queue` rows with `status = 'dispatched'` and `dispatched_at < NOW() - 10 minutes` — these were in-flight when the coordinator last restarted.

**Workers unchanged:** Crawl page dispatches reuse existing NATS subjects. `crawl_context` sub-object in fat message identifies the message as a crawl page. The API result consumer routes messages with non-null `crawl_context` to the coordinator's result handler.

---

### 5.6 Rate Limiting: Sliding Window (PRD-002)

**Decision:** Replace fixed-window Redis counter with Redis sorted set + Lua script.

**Why:** Fixed window has a known 2x burst exploit (fire `limit` requests at end of window N and start of window N+1). Unacceptable for billing enforcement on a multi-tenant platform.

**Implementation:** Lua script for atomic check-and-increment on a ZSET keyed by `rate:user:<user_id>`. Score = timestamp (ms). Remove entries outside the window with `ZREMRANGEBYSCORE`, count remaining, add new entry if under limit.

---

### 5.7 `jobs.updated_at`: DB Trigger (PRD-011 housekeeping)

**Decision:** Option C — Postgres trigger, not SQLAlchemy `onupdate`, not explicit assignment at each mutation path.

**Why:** `onupdate` silently doesn't fire for `db.execute(update(...))` paths (scheduler `next_run_at` updates, cancel route). Explicit assignment (Option B) is a maintenance contract that future engineers will miss. A trigger fires on every `UPDATE` regardless of path — no application-layer discipline required.

**Migration note:** The migration file must include a comment explaining why the trigger exists (SQLAlchemy `onupdate` bypass). Remove `onupdate=lambda: datetime.now(UTC)` from the SQLAlchemy model after the migration is applied.

---

### 5.8 Other Phase 3 Decisions (No ADR Required)

| Decision | Outcome | Rationale |
|----------|---------|-----------|
| Admin SPA service split | Stay on same FastAPI app | No VPN requirement; `get_current_admin_user` is sufficient enforcement |
| Cleanup CronJob image | Share API image, override entrypoint | Script imports API models; separate image duplicates dependencies |
| User-facing hard delete | `DELETE /jobs/{id}?permanent=true` | Users need dashboard cleanup; MinIO first, then cascade |
| Crawl export endpoint | Deferred to Phase 4 | Paginated per-page endpoints sufficient; ZIP generation adds async infra not otherwise needed |
| `execute_js` sandboxing | Ships with CSP mitigation; full sandbox Phase 4 | JS runs in Chromium sandbox; CSP blocks outbound fetch to non-target origins |
| MCP server transport | stdio only, Phase 3 | Most MCP clients (Claude Desktop, CLI) use stdio; SSE is Phase 4 |
| MCP server location | `mcp/` in monorepo | Thin HTTP client; no internal modules shared; separate repo adds Git complexity for no benefit |
| `proxy_provider` field | Stored on `jobs` table (not `job_secrets`) | It's a label, not a credential; no encryption needed |
| Cookie `domain` inference | Worker infers from job URL if not provided | Playwright requires domain; Go HTTP worker does not need it |

---

## 6. Documents You Produced

| Document | Location | Status |
|----------|----------|--------|
| Phase 2 concepts | `docs/phase2/phase2-concepts.md` | Complete |
| Phase 2 engineering spec v3 | `docs/phase2/phase2-engineering-spec-v3.md` | Current — approved |
| Architect review v1 (23 issues) | `docs/phase2/phase2-spec-review-v1.md` | All resolved in v2 |
| Architect review v2 (4 issues) | `docs/phase2/phase2-spec-review-v2.md` | All resolved in v3 |
| ADR-001 (Phase 1 worker contract) | `docs/adr/ADR-001-worker-job-contract.md` | Partially superseded by ADR-002 |
| ADR-002 (Phase 2 worker contract) | `docs/adr/ADR-002-phase2-worker-contract.md` | Accepted |
| ADR-003 (Job/Run data model split) | `docs/adr/ADR-003-job-run-split.md` | Accepted |
| **ADR-004 (Phase 3 fat message schema v2)** | `docs/adr/ADR-004-phase3-fat-message-schema.md` | **Accepted — Phase 3** |
| **ADR-005 (Site crawl BFS coordinator)** | `docs/adr/ADR-005-site-crawl-bfs-coordinator.md` | **Accepted — Phase 3** |
| **ADR-006 (Batch scraping data model)** | `docs/adr/ADR-006-batch-scraping-data-model.md` | **Accepted — Phase 3** |
| **ADR-007 (Job secrets storage)** | `docs/adr/ADR-007-job-secrets-storage.md` | **Accepted — Phase 3** |
| **Phase 3 engineering spec v1** | `docs/phase3/phase3-engineering-spec.md` | **Approved for implementation** |

---

## 7. The Review Process — What Happened

### Phase 2 review summary (two rounds)

**v1 → v2 (23 issues):** Critical bugs, logic errors, implementation gaps — all resolved.

**v2 → v3 (4 issues):**
- Issue A: `cleanup_old_runs.py` — `continue` on MinIO failure did not exclude run from DB deletion
- Issue B: Missing `return` after `llm_key is None` — fell through to diff and fired `job.completed` on failed run
- Issue C: `transaction = False` silently ignored by Alembic
- Issue D: `BACKOFF_SECONDS` IndexError when `WEBHOOK_MAX_ATTEMPTS > 5`

### Phase 3 design process

Phase 3 followed a structured Q&A process before any ADR was written. Ten clarifying questions were resolved with the project owner:

| Q | Topic | Decision |
|---|-------|----------|
| Q1 | Fat message structure | `schema_version: 2` + nested sub-objects |
| Q2 | BFS coordinator + queue | Option B (coordinator process) + Postgres `crawl_queue` |
| Q3 | Batch data model | New tables + nullable `job_runs.job_id` + check constraint |
| Q4 | Credentials storage | New `job_secrets` table (not columns on `jobs`) |
| Q5 | Cleanup CronJob image | API image with overridden entrypoint |
| Q6 | Admin SPA service split | Stay on same FastAPI app |
| Q7 | `jobs.updated_at` | Postgres trigger (Option C) |
| Q8 | Crawl export endpoint | Deferred to Phase 4 |
| Q9 | User-facing hard delete | `DELETE /jobs/{id}?permanent=true` |
| Q10 | `execute_js` sandboxing | CSP mitigation in Phase 3; full sandbox Phase 4 |

A mid-session scope change was also applied: Phase 3 was redefined as not homelab-scoped. This changed the BFS coordinator recommendation from Option A to Option B, and flagged PRD-011/012 priority for PM reassessment.

---

## 8. Architectural Principles Applied Throughout

Carry all of these into Phase 4.

1. **Database is source of truth; NATS is delivery mechanism.** DB commit before NATS publish, always.
2. **Workers are dumb.** No DB access, no business logic, no routing decisions. The coordinator is not a worker — it reads/writes Postgres and dispatches to NATS, but contains no scraping logic.
3. **Delete external state before internal state.** MinIO first, Postgres second.
4. **Fail fast and visibly.** Log structured warnings with job_id/run_id/crawl_id context.
5. **Idempotency.** Every operation that could be retried is safe to re-run.
6. **`SELECT FOR UPDATE SKIP LOCKED`** on every background polling loop that could run on multiple instances.
7. **Fernet for secrets at rest and in transit.** LLM API keys, proxy credentials, cookies — same cipher, same key (`LLM_KEY_ENCRYPTION_KEY`, to be renamed in Phase 4).
8. **404 not 403 for cross-tenant access.**

---

## 9. What Phase 4 Will Require From You

Phase 3 design is complete. The Tech Lead has the engineering spec and all ADRs. You have no outstanding Phase 3 design work.

**Known open items deferred explicitly to Phase 4:**

| Item | Context |
|------|---------|
| Rename `LLM_KEY_ENCRYPTION_KEY` → `JOB_SECRET_ENCRYPTION_KEY` | Env var name is now a misnomer (also encrypts proxy/cookie secrets) |
| Admin SPA VPN restriction | `admin.scrapeflow.*` behind VPN requires service split — revisit if platform requires it |
| Crawl export endpoint (`GET /crawls/{id}/export`) | ZIP archive of all crawled pages; async generation |
| Adaptive/BFS/DFS crawl strategy selection | Phase 3 ships BFS only |
| `execute_js` full sandboxing | Phase 3 ships with CSP mitigation only |
| Form-based login / OAuth automation | Phase 3 ships cookie injection only |
| Session capture and reuse across runs | Requires session state storage design |
| HPA for worker deployments | Phase 3 ships static replica counts |
| SSE transport for MCP server | Phase 3 ships stdio only |
| Batch/crawl MCP tools | Phase 3 MCP covers single-URL only |
| Billing / Stripe integration | Phase 3 ships quota enforcement only (no payment) |
| PRD-011 / PRD-012 priority reassessment | PM flagged to reconsider given multi-tenant scope |
| Per-user MinIO storage reconciliation cron | `storage_bytes_used` counter can drift if objects deleted outside API |
| `proxy_provider` provider-specific request formatting | Phase 3 stores the enum but adds no provider-specific logic |

**When Phase 4 PRDs arrive:**
- Read them before designing — do not re-litigate decisions already made
- The `job_secrets` ENUM migration pattern (ADR-007) is the template for adding new credential types
- The coordinator service (`coordinator/`) is the template for any future multi-step coordination needs
- Do not revisit ADR-003 (jobs/runs split) or ADR-006 (batch data model) without a strong reason — the `job_runs` check constraint and nullable `job_id` are load-bearing

---

## 10. Files to Read Before Starting Any Design Work

In this order:

1. `CLAUDE.md` — project goals, stack, key decisions table, Phase 3 build process conventions
2. `docs/adr/ADR-001-worker-job-contract.md` — Phase 1 worker contract (partially superseded but still authoritative for ack timing and cancellation)
3. `docs/adr/ADR-002-phase2-worker-contract.md` — Phase 2 worker contract (supersedes ADR-001 §2, §3, §8)
4. `docs/adr/ADR-003-job-run-split.md` — Jobs/runs data model split
5. `docs/adr/ADR-004-phase3-fat-message-schema.md` — Fat message schema v2
6. `docs/adr/ADR-005-site-crawl-bfs-coordinator.md` — BFS coordinator placement and queue design
7. `docs/adr/ADR-006-batch-scraping-data-model.md` — Batch data model and `job_runs` changes
8. `docs/adr/ADR-007-job-secrets-storage.md` — Job secrets table
9. `docs/phase3/phase3-engineering-spec.md` — Complete Phase 3 engineering spec (approved)
10. `docs/phase2/phase2-engineering-spec-v3.md` — Phase 2 spec (for Phase 2 implementation detail)

Do not read Phase 2 spec v1/v2 or the review documents unless investigating a specific decision's history.
