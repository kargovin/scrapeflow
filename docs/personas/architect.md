# Software Architect — ScrapeFlow Onboarding Document

> **Purpose:** Bring a new Software Architect persona up to speed on everything done, every decision made, and every pattern established through Phase 2. Read this before touching any design work.
> **Last updated:** 2026-04-02
> **Covers:** Phase 1 context, all Phase 2 architectural decisions, documents produced, review outcomes, and what Phase 3 will require from you.

---

## 1. What ScrapeFlow Is

A self-hosted, multi-tenant web scraping platform. The primary use case is structured data extraction and change detection to feed ML/data pipelines. It is a portfolio project built to production-grade standards — not a toy.

**The invariant that drives every architectural decision:** the API is the brain, workers are dumb executors. Workers touch only NATS and MinIO — never Postgres. All business logic, state management, and error recovery lives in the API.

---

## 2. The Stack You Are Working With

| Layer | Technology | Notes |
|-------|-----------|-------|
| API | FastAPI (Python) | Async, SQLAlchemy 2.0, Alembic migrations |
| Scrape worker | Go | HTTP worker; pull consumer pattern |
| Playwright worker | Python | New in Phase 2; headless Chromium |
| LLM worker | Python | New in Phase 2; BYOK extraction |
| Queue | NATS JetStream | WorkQueue retention; pull consumers |
| DB | PostgreSQL | Source of truth for all state |
| Object storage | MinIO | Raw scrape results and structured outputs |
| Cache / rate limiting | Redis | Fixed window counters per user |
| Auth | Clerk | JWT + API keys; users synced to local DB |
| Deployment | Docker Compose (dev), k3s homelab (prod) | Domain: scrapeflow.govindappa.com |

---

## 3. Phase 1 — What Was Already Done When You Arrived

Phase 1 delivered a working MVP and 18 pre-Phase-2 cleanup items. You inherited a clean codebase. Key things already in place:

- Full job CRUD with Clerk auth (JWT + API keys)
- Go HTTP scraper worker consuming NATS JetStream
- MinIO result storage
- Redis-backed per-user rate limiting
- ADR-001 defining the original API↔worker contract
- SSRF protection (`_validate_no_ssrf()`) in `api/app/routers/jobs.py`
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

---

### 4.2 NATS Subject Routing: One Subject Per Worker Type

**Decision:** Replace the single `scrapeflow.jobs.run` subject with separate subjects per worker type.

```
scrapeflow.jobs.run.http       → Go HTTP worker
scrapeflow.jobs.run.playwright → Python Playwright worker
scrapeflow.jobs.llm            → Python LLM worker (conditional second stage)
scrapeflow.jobs.result         → All workers → API result consumer (unchanged)
```

Stream wildcard changed from explicit subject list to `scrapeflow.jobs.>`.

**Why:** If all worker types share one subject, each worker must contain routing logic to decide whether a given message is for it. Workers should be dumb. Separate subjects mean each worker consumes only its messages; the API chooses the subject at dispatch time based on `job.engine`.

**ADR-002** formalises this — it is embedded in `docs/phase2/phase2-engineering-spec-v3.md §3`. Extract it to `docs/adr/ADR-002-phase2-worker-contract.md` before Phase 3 begins.

---

### 4.3 Two-Stage LLM Pipeline

**Decision:** Go/Playwright worker handles ALL scrape jobs (with or without LLM). Python LLM worker is a conditional second stage — only activated when `job.llm_config` is set.

**Flow:**
```
ALL jobs:
  API → [scrapeflow.jobs.run.http] → Go worker → MinIO (raw) → [scrapeflow.jobs.result: completed]

LLM jobs (second stage, triggered by API result consumer):
  result consumer sees completed scrape + llm_config set
    → sets job_run.status = 'processing'
    → publishes to [scrapeflow.jobs.llm]
    → LLM worker: reads raw from MinIO → calls LLM → writes structured JSON
    → publishes to [scrapeflow.jobs.result: completed]
    → result consumer finalises
```

**Why Go scrapes and Python extracts:** The official Playwright package is Python-only. LLM SDKs (Anthropic, OpenAI) are better maintained in Python. Go is faster for raw HTTP scraping. Splitting responsibilities by language lets each worker use the best tool.

**Discriminator invariant** (critical — do not break): Both scrape workers and the LLM worker publish to `scrapeflow.jobs.result` with identical schemas. The result consumer uses `job_run.status` to distinguish source:
- `status = 'running'` when `completed` arrives → came from a scrape worker → check llm_config
- `status = 'processing'` when `completed` arrives → came from the LLM worker → skip LLM routing, go straight to diff

No code path other than the LLM dispatch branch may set `job_run.status = 'processing'`.

---

### 4.4 Pull Consumer Pattern

**Decision:** All three workers use NATS pull consumers with a fixed worker pool. No push subscriptions.

**Why:** Push subscriptions deliver messages as fast as NATS can send them. For the Playwright worker, which runs a real Chromium browser, this means unbounded goroutine/task concurrency. A pull consumer lets the worker control its own ingestion rate — it fetches only as many messages as it has capacity to process (`available = cap(sem) - len(sem)`).

**Critical pattern for AckWait safety:**
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

**Decision:** Recurring job scheduling is implemented as an asyncio background task in the API that polls Postgres every 60 seconds.

**Why not APScheduler:** Creates in-memory state that doesn't survive restarts. Two API instances would double-dispatch jobs.

**Why not k8s CronJob:** Coarse granularity (1-minute minimum), creates a separate service, and still requires a DB row update to track next_run_at.

**Multi-instance safety:** `SELECT ... FOR UPDATE SKIP LOCKED` on the `jobs` table. Two API instances running the scheduler simultaneously will each grab different jobs; neither will grab the same job twice.

**Atomicity rule:** DB commit happens **before** NATS publish. If NATS publish fails after a successful commit, the `job_runs` row remains `pending`. Stale-pending recovery re-dispatches on the next cycle (any run with `status = 'pending'` and `created_at < NOW() - 10 minutes`). The inverse — NATS has an in-flight message but no DB row — has no recovery path.

---

### 4.6 Change Detection: MinIO Dual-Path + Diff Strategy

**MinIO path convention:**
- `latest/{job_id}.{ext}` — always overwritten, reflects current state
- `history/{job_id}/{timestamp}.{ext}` — append-only, one object per run

`job_runs.result_path` always stores the `history/` path. The `latest/` path is derivable and not stored.

**Diff strategies — two, not one:**
- **Non-LLM jobs:** `compute_text_diff` — strip HTML, collapse whitespace, `difflib.unified_diff` on normalised text
- **LLM jobs:** `compute_json_diff` — field-by-field deep comparison of structured JSON output

**Why different strategies:** For LLM jobs, comparing raw HTML (the scrape output) is meaningless — the LLM may produce identical structured output from slightly different HTML. Diff must run on the LLM's structured output, not the raw content. The result consumer defers diff computation for LLM jobs until after the LLM worker completes.

---

### 4.7 Webhook Delivery: Postgres-Backed, Not NATS

**Decision:** Webhook deliveries are stored in a `webhook_deliveries` Postgres table and retried by an asyncio background loop.

**Why not NATS:** Webhook delivery is not latency-sensitive (failure and retry over hours is fine). The admin panel needs visibility into delivery state and manual retry capability. Postgres gives both naturally; NATS does not.

**Retry schedule (exponential backoff):**
```
attempt 1: immediate
attempt 2: 30s
attempt 3: 5min
attempt 4: 30min
attempt 5: 2h → status = exhausted
```
Configurable via `WEBHOOK_MAX_ATTEMPTS` (default 5). The backoff table index is capped with `min(attempts, len(BACKOFF_SECONDS) - 1)` so operators can safely increase `WEBHOOK_MAX_ATTEMPTS` beyond 5.

**HMAC signing:** Every delivery includes `X-ScrapeFlow-Signature: sha256=<hmac_hex>`. The secret is per-job, Fernet-encrypted in the DB (`jobs.webhook_secret`). Secret shown once at job creation, never returned again. Rotation endpoint: `POST /jobs/{id}/webhook-secret/rotate`.

**Webhook event design (Option A — implemented):** Single `job.completed` event per run with `diff_detected` and `diff_summary` embedded in the payload. Consumer inspects `diff_detected` to decide whether to act. Option B (separate `job.change_detected` event) was documented but not implemented — reconsider in Phase 3 if per-event subscription filtering is added.

---

### 4.8 LLM Key Storage

**Decision:** LLM API keys are stored once per user in a `user_llm_keys` table, Fernet-encrypted at rest. `jobs.llm_config` JSONB stores a reference (`llm_key_id`) — never the key itself.

**At dispatch:** API resolves the key by ID, passes the Fernet ciphertext in the NATS message. LLM worker decrypts with `LLM_KEY_ENCRYPTION_KEY` env var (shared secret between API and LLM worker).

**Key deletion:** Hard delete. Jobs referencing a deleted key fail at dispatch with `error: "LLM key not found"`. No FK constraint is possible (llm_key_id lives inside JSONB — Postgres cannot enforce FK on JSONB fields). Application enforces: resolve `llm_key_id` at dispatch, fail the run gracefully if missing. **Critical:** add `return` after the failure path in the result consumer — without it, execution falls through to diff computation and fires `"job.completed"` for a failed run (Issue B from v2 review).

---

### 4.9 `nats_stream_seq` — MaxDeliver Advisory Bridge

**Decision:** Add `nats_stream_seq BIGINT NULL` to `job_runs`. Set by the result consumer on first `status: "running"` message.

**Why:** When NATS exhausts redeliveries for a message, it publishes an advisory to `$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SCRAPEFLOW.*`. The advisory contains only the stream sequence number — no `job_id` or `run_id`. `nats_stream_seq` is the bridge that lets the advisory handler find the correct `job_runs` row and mark it `failed`. Without this column, jobs that NATS gives up on are stuck in `running` permanently.

The advisory handler also targets `status IN ('pending', 'running', 'processing')` — `processing` is included because LLM jobs can exhaust redeliveries on the `scrapeflow.jobs.llm` subject while in that state.

---

### 4.10 Immutable Job Fields

Some `jobs` fields are intentionally immutable after creation. This is enforced at `PATCH /jobs/{id}`:

| Field | Why immutable |
|-------|--------------|
| `url` | Changing URL invalidates all historical diffs — history becomes meaningless. Create a new job instead. |
| `engine` | Mixing HTTP and Playwright results in the same diff chain breaks diff semantics. |
| `output_format` | Same reason — mixed format history breaks diffs. |

Mutable fields: `schedule_status`, `schedule_cron`, `webhook_url`, `llm_config`, `playwright_options`.

---

### 4.11 Admin Panel: Same FastAPI App

**Decision:** Admin routes live in a new `/admin` router on the same FastAPI app. Not a separate service.

**Why:** Phase 2 admin is internal tooling. A separate service adds k8s complexity (new Deployment, new ingress, new auth service) for no benefit at this scale. Revisit in Phase 3 — the plan file notes a Phase 3 item to evaluate whether admin needs its own FastAPI app and separate API key type (`sfa_...` prefix).

---

### 4.12 SSRF Protection

`_validate_no_ssrf()` must be moved from `api/app/routers/jobs.py` to `api/app/core/security.py` before Phase 2 code is written. It is applied at:
- `POST /jobs` — job URL
- `POST /jobs` — webhook_url
- `POST /users/llm-keys` — base_url for openai_compatible providers

**Known gap (documented, deferred to Phase 3):** SSRF is not re-validated at webhook delivery time. DNS rebinding can change what IP a hostname resolves to after the initial check at `POST /jobs`. The fix (call `_validate_no_ssrf()` again before each HTTP POST in the delivery loop) is straightforward — the infrastructure is already in `core/security.py`. It was deferred because the project is dev-only with no external attack surface.

---

### 4.13 Alembic Migration Approach

Two `alembic revision` runs for Phase 2:

1. **Autogenerated revision** — covers all structural changes (new tables, new columns, dropped columns, indexes). Two things appended manually:
   - Data migration: copy existing job outcomes into `job_runs`
   - No other manual appends

2. **Hand-written revision** — `ALTER TYPE jobstatus ADD VALUE 'processing'`. Must use the `COMMIT`/`BEGIN` trick inside `upgrade()`. **Do not use `transaction = False` at the module level** — Alembic silently ignores it, and the migration will fail at runtime with `ProgrammingError: ALTER TYPE ... cannot run inside a transaction block`.

```python
import sqlalchemy as sa

def upgrade() -> None:
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'processing' AFTER 'running'"))
    op.execute(sa.text("BEGIN"))
```

---

## 5. Documents You Produced

| Document | Location | Status |
|----------|----------|--------|
| Phase 2 concepts (educational "why" document) | `docs/phase2/phase2-concepts.md` | Complete |
| Phase 2 engineering spec v1 | `docs/phase2/phase2-engineering-spec-v1.md` | Superseded by v3 |
| Phase 2 engineering spec v2 | `docs/phase2/phase2-engineering-spec-v2.md` | Superseded by v3 |
| Phase 2 engineering spec v3 | `docs/phase2/phase2-engineering-spec-v3.md` | **Current — approved for implementation** |
| Architect review v1 (23 issues) | `docs/phase2/phase2-spec-review-v1.md` | All 23 issues resolved in v2 |
| Architect review v2 (4 issues) | `docs/phase2/phase2-spec-review-v2.md` | All 4 issues resolved in v3 |
| ADR-001 (Phase 1 worker contract) | `docs/adr/ADR-001-worker-job-contract.md` | Unchanged — still authoritative |
| ADR-002 (Phase 2 worker contract) | Embedded in spec v3 §3 | **Extract to `docs/adr/` before Phase 3** |

---

## 6. The Review Process — What Happened

The spec went through two architect review rounds:

**v1 → v2 (23 issues):** Covered critical bugs (TaskGroup hang, webhook race condition, cancellation enforcement gap, missing `nats_stream_seq`), logic errors (diff on wrong content type, AckWait race, scheduler split-brain), and implementation gaps (undefined LLM implementation, no content truncation, wrong migration workflow).

**v2 → v3 (4 issues):**
- **Issue A (P1):** `cleanup_old_runs.py` built `run_ids` from all batch rows after the loop — `continue` on MinIO failure did not exclude the run from DB deletion. Fixed: build `successful_ids` inside the loop.
- **Issue B (P1):** Missing `return` after `llm_key is None` in result consumer — execution fell through to diff computation and fired `"job.completed"` on a failed run. Fixed: add `return` + `"job.failed"` webhook.
- **Issue C (P2):** `transaction = False` at module level is silently ignored by Alembic. Fixed: `COMMIT`/`BEGIN` trick in `upgrade()`.
- **Issue D (P3):** `BACKOFF_SECONDS` hardcoded to 5 entries but `WEBHOOK_MAX_ATTEMPTS` is operator-configurable — `IndexError` if set above 5. Fixed: `min(attempts, len(BACKOFF_SECONDS) - 1)` index cap.

---

## 7. Architectural Principles Applied Throughout

These are the recurring patterns you established. Carry them into Phase 3.

1. **Database is source of truth; NATS is delivery mechanism.** DB commit before NATS publish, always.
2. **Workers are dumb.** No DB access, no business logic, no routing decisions.
3. **Delete external state before internal state.** MinIO first, Postgres second. If external delete fails, the DB row survives and the next run retries. The inverse leaves orphaned objects with no reference.
4. **Fail fast and visibly.** Truncate LLM content rather than silently hitting API limits. Log structured warnings with job_id/run_id context.
5. **Idempotency.** Every operation that could be retried (cleanup, NATS init, migration) is safe to re-run.
6. **`SELECT FOR UPDATE SKIP LOCKED`** on every background polling loop that could run on multiple API instances.
7. **Fernet for secrets at rest and in transit.** LLM API keys and webhook secrets — same pattern, same key (`LLM_KEY_ENCRYPTION_KEY`).
8. **404 not 403 for cross-tenant access.** 403 leaks resource existence in a multi-tenant system.

---

## 8. What Phase 3 Will Require From You

Phase 3 features are listed in `CLAUDE.md`. None of them have design work yet. Before the Tech Lead can do task breakdown, you need to produce:

**For each Phase 3 feature:**
- Architectural decisions (how does it fit with what exists?)
- ADRs for any load-bearing choices
- Updated engineering spec sections
- Dependencies on Phase 2 completion

**Known open questions you will need to answer:**

| Question | Context |
|----------|---------|
| Does admin need its own FastAPI app? | Noted in plan — evaluate separate service + `sfa_...` API key type |
| Tier-based rate limiting vs quota-based? | Phase 3 billing/quotas — replaces current fixed-window Redis approach |
| SSRF re-validation at webhook delivery | Known gap from Phase 2 — simple fix, needs to be scheduled |
| Proxy rotation interface | Pluggable provider config — define the abstraction |
| MCP server auth model | Does the MCP server use existing Clerk JWTs or its own key type? |
| k8s manifest structure | How do Phase 2's two new workers (Playwright, LLM) get added to the infra repo? |

**Phase 3 also follows the full persona chain:** PM defines scope → you design → Tech Lead breaks down → Engineers implement. Do not start design until the PM has produced a PRD for the feature you are designing.

---

## 9. Files to Read Before Starting Any Design Work

In this order:

1. `CLAUDE.md` — project goals, stack, key decisions table, Phase 3 build process conventions
2. `docs/adr/ADR-001-worker-job-contract.md` — the original worker contract (still authoritative)
3. `docs/phase2/phase2-engineering-spec-v3.md` — the complete Phase 2 spec (current approved version)
4. `docs/phase2/phase2-concepts.md` — the "why we built it this way" explanations for each Phase 2 decision

Do not read the v1/v2 specs or the review documents unless you are investigating a specific decision's history.
