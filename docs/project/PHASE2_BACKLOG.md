# ScrapeFlow Phase 2 — Implementation Backlog

> **Owner:** Tech Lead
> **Status:** Ready for implementation
> **Spec source:** `docs/phase2/phase2-engineering-spec-v3.md` (approved, v3 — all architect review issues resolved)
> **Test command (Python):** `docker compose exec api python -m pytest`
> **Test command (Go):** `docker compose exec worker go test ./...`

---

## How to Use This Backlog

Pick the lowest-numbered incomplete step that has no incomplete dependencies.
Each step is independently completable and verifiable before the next begins.

Steps marked with ⚠ have a sequencing constraint — read the note before starting.

---

## Critical Sequencing Notes

1. **Migration 2.4** (Step 12) drops `jobs.status`, `jobs.result_path`, `jobs.error`. It cannot run until every piece of code that reads those columns is updated. Steps 9–11 do that work. **Do not run Step 12 before Steps 9–11 are merged.**
2. **Steps 1–3** (SSRF refactor, admin dependency, encryption setup) are short but unlock everything else — do them first.
3. **Steps 4–11** are schema + model changes. Do them in order — each migration depends on the previous table state.
4. **Steps 13–18** (workers + background tasks) can proceed in parallel once Step 7 (NATS constants) is done.

---

## Backlog

### Step 1 — Refactor `_validate_no_ssrf()` to `core/security.py`

**Why first:** Phase 2 calls this from three places (`POST /jobs` url, webhook_url, LLM key base_url). It must be importable before writing any of those routes.

**Files:**
- New: `api/app/core/security.py` — move `_validate_no_ssrf()` here (unchanged logic)
- Edit: `api/app/routers/jobs.py` — import from `core.security` instead of defining locally

**Verify:** `pytest tests/test_jobs.py -v` — all existing SSRF tests still pass, no behavior change

**Spec ref:** §5.1

---

### Step 2 — Add `get_current_admin_user` dependency

**Files:**
- Edit: `api/app/auth/dependencies.py` — add `get_current_admin_user(user=Depends(get_current_user))` that raises 403 if `not user.is_admin`

> Note: `is_admin` column doesn't exist yet (that's Step 4). Add the field to the `User` model now but skip the migration — Step 4 runs it. The dependency can be written and tested independently once the model has the attribute.

**Verify:** Unit test — mock a user with `is_admin=False`, assert 403; with `is_admin=True`, assert passes through

**Spec ref:** §5.2

---

### Step 3 — Fernet encryption setup in settings + dependencies

**What:** Phase 2 needs a `LLM_KEY_ENCRYPTION_KEY` Fernet key for encrypting LLM API keys and webhook secrets. Wire it into settings and expose a `get_fernet` dependency.

**Files:**
- Edit: `api/app/settings.py` — add `llm_key_encryption_key: str` field (required, no default)
- Edit: `api/app/main.py` lifespan — validate Fernet key is valid on startup (call `Fernet(settings.llm_key_encryption_key)` and crash fast if invalid)
- Edit: `api/app/.env.example` — add `LLM_KEY_ENCRYPTION_KEY=` with generation command in comment
- New: `api/app/core/encryption.py` — `get_fernet(request: Request) -> Fernet` dependency

**Generate a dev key:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Verify:** `docker compose up api` — startup should crash with a clear message if key is missing or invalid

**Spec ref:** §9

---

### Step 4 — Migration 2.1: Add `is_admin` to `users`

**Files:**
- Edit: `api/app/models/user.py` — add `is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")`
- Run: `alembic revision --autogenerate -m "add_is_admin_to_users"` inside the api container
- Verify the generated migration, then run: `alembic upgrade head`

**Verify:**
```bash
docker compose exec api python -m pytest tests/ -v  # full suite, no regression
docker compose exec db psql -U scrapeflow -c "\d users"  # verify is_admin column exists
```

**Spec ref:** §2.1

---

### Step 5 — Migration 2.2: Add Phase 2 fields to `jobs`

**Files:**
- Edit: `api/app/models/job.py` — add new columns:
  - `engine: Mapped[str]` (VARCHAR 20, default `'http'`, CHECK constraint `IN ('http', 'playwright')`)
  - `schedule_cron: Mapped[str | None]`
  - `schedule_status: Mapped[str | None]` (CHECK constraint `IN ('active', 'paused')`)
  - `next_run_at: Mapped[datetime | None]`
  - `last_run_at: Mapped[datetime | None]`
  - `webhook_url: Mapped[str | None]`
  - `webhook_secret: Mapped[str | None]` (Fernet-encrypted at rest)
  - `llm_config: Mapped[dict | None]` (JSONB)
  - `playwright_options: Mapped[dict | None]` (JSONB)
- Run: `alembic revision --autogenerate -m "add_phase2_fields_to_jobs"`
- Manually append the partial index on `next_run_at` (autogenerate misses conditional indexes):
  ```python
  op.create_index("idx_jobs_next_run_at", "jobs", ["next_run_at"],
                  postgresql_where=sa.text("schedule_cron IS NOT NULL AND schedule_status = 'active'"))
  ```
- Run: `alembic upgrade head`

> ⚠ Do NOT remove `status`, `result_path`, `error` yet — that is Step 12.

**Verify:** `pytest tests/ -v`

**Spec ref:** §2.2

---

### Step 6 — Migration 2.3: Create `job_runs` table + `JobRun` model

**Files:**
- New: `api/app/models/job_run.py` — `JobRun` model with all columns from spec §2.3
- Edit: `api/app/models/__init__.py` — import `JobRun` so Alembic detects it
- Run: `alembic revision --autogenerate -m "create_job_runs"`
- **Manually append** the data migration inside `upgrade()` after `op.create_table(...)`:
  ```python
  op.execute("""
      INSERT INTO job_runs (id, job_id, status, result_path, error, completed_at, created_at)
      SELECT gen_random_uuid(), id, status, result_path, error, updated_at, created_at
      FROM jobs WHERE status != 'pending'
  """)
  ```
- Run: `alembic upgrade head`

**Verify:**
```bash
docker compose exec db psql -U scrapeflow -c "SELECT COUNT(*) FROM job_runs;"
# Should equal number of non-pending jobs before migration
pytest tests/ -v
```

**Spec ref:** §2.3

---

### Step 7 — Migration 2.5: Create `user_llm_keys` table + `UserLLMKey` model

**Files:**
- New: `api/app/models/llm_key.py` — `UserLLMKey` model (id, user_id FK, name, provider, encrypted_api_key, base_url, created_at)
- Edit: `api/app/models/__init__.py` — import `UserLLMKey`
- Run: `alembic revision --autogenerate -m "create_user_llm_keys"` + `alembic upgrade head`

**Verify:** `pytest tests/ -v`

**Spec ref:** §2.5

---

### Step 8 — Migration 2.6: Add `processing` status to `JobStatus` enum

> ⚠ This migration cannot run inside a transaction. Do NOT use `transaction = False` at module level — it is silently ignored. Use the COMMIT/BEGIN pattern below.

**Files:**
- New hand-written migration file (do NOT use autogenerate):
  ```python
  import sqlalchemy as sa

  def upgrade() -> None:
      op.execute(sa.text("COMMIT"))
      op.execute(sa.text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'processing' AFTER 'running'"))
      op.execute(sa.text("BEGIN"))

  def downgrade() -> None:
      pass  # Postgres cannot remove an ENUM value
  ```
- Run: `alembic upgrade head`

**Verify:**
```bash
docker compose exec db psql -U scrapeflow -c "SELECT unnest(enum_range(NULL::jobstatus));"
# Should include: pending, running, processing, completed, failed, cancelled
```

**Spec ref:** §2.6

---

### Step 9 — Migration 2.7 + 2.8: `webhook_deliveries` + `nats_stream_seq`

Two migrations, run in sequence.

**Migration 2.7 — `webhook_deliveries`:**
- New: `api/app/models/webhook_delivery.py` — `WebhookDelivery` model
- Edit: `api/app/models/__init__.py` — import `WebhookDelivery`
- `alembic revision --autogenerate -m "create_webhook_deliveries"` + `upgrade head`

**Migration 2.8 — `nats_stream_seq`:**
- Edit: `api/app/models/job_run.py` — add `nats_stream_seq: Mapped[int | None]`
- `alembic revision --autogenerate -m "add_nats_stream_seq_to_job_runs"` + `upgrade head`
- Manually append the sparse index to the generated file:
  ```python
  op.create_index("idx_job_runs_nats_stream_seq", "job_runs", ["nats_stream_seq"],
                  postgresql_where=sa.text("nats_stream_seq IS NOT NULL"))
  ```

**Verify:** `pytest tests/ -v`

**Spec ref:** §2.7, §2.8

---

### Step 10 — Update `POST /jobs` for Phase 2

**Dependencies:** Steps 1, 3, 6, 7

**Files:**
- New: `api/app/schemas/jobs.py` — add `Engine`, `WaitStrategy`, `PlaywrightOptions`, `LLMJobConfig` Pydantic models; update `JobCreate` with new fields
- Edit: `api/app/routers/jobs.py` — update `POST /jobs` handler:
  1. SSRF check on `webhook_url` (if provided) using `core.security._validate_no_ssrf`
  2. SSRF check on LLM key `base_url` (resolve from `llm_key_id`, return 404 if not found)
  3. Validate `schedule_cron` with `croniter`; enforce `SCHEDULE_MIN_INTERVAL_MINUTES`
  4. Generate webhook secret before DB insert (if `webhook_url` set): `Fernet.generate_key()`, encrypt
  5. Insert `jobs` row + insert `job_runs` row (status=pending) in same transaction
  6. Commit, then publish to NATS (`scrapeflow.jobs.run.http` or `.playwright` based on engine) with `{job_id, run_id, url, output_format, playwright_options}`
  7. Return `JobResponse` including `run_id` + `webhook_secret` (shown once if generated)
- Add `settings.schedule_min_interval_minutes` to `settings.py`

**Verify:** `pytest tests/test_jobs.py -v` — all existing tests pass; add new tests:
- 422 for invalid cron
- 404 for unknown `llm_key_id`
- 400 for private webhook_url
- `job_runs` row created on job creation

**Spec ref:** §5.3

---

### Step 11 — Update `GET /jobs`, `GET /jobs/{id}`, `DELETE /jobs/{id}` for Phase 2

**Dependencies:** Step 6

**What changes:**
- `GET /jobs` and `GET /jobs/{id}`: add LATERAL JOIN to get latest `job_runs` row; return `status`, `result_path`, `diff_detected`, `error`, `completed_at` from the run row, not from `jobs`
- `DELETE /jobs/{id}`: Phase 2 cancellation — find the latest non-terminal `job_runs` row and set `status='cancelled'` on it (not `jobs.status`)
  - Return `{"message": "Job run cancelled"}` if an active run was found
  - Return `{"message": "Job has no active run to cancel"}` if all runs are terminal

**Files:**
- Edit: `api/app/routers/jobs.py` — update all three handlers
- Edit: `api/app/schemas/jobs.py` — update `JobResponse` to include `run_id`, `diff_detected`, `completed_at`

**Verify:** `pytest tests/test_jobs.py -v` — existing cancel/get tests updated; add:
- Cancel with no active run → 200 with "no active run" message
- GET /jobs/{id} returns current run status from `job_runs`

**Spec ref:** §5.4, §5.5

---

### Step 12 — Migration 2.4: Drop run-state columns from `jobs`

> ⚠ **ADR-003 required before this step.** Write `docs/adr/ADR-003-job-run-split.md` documenting the job/run data model split before this migration runs. Step 12 is the point of no return — once `jobs.status`, `jobs.result_path`, and `jobs.error` are dropped, there is no downgrade path without restoring from backup. The ADR captures the rationale so future engineers understand why the schema looks the way it does.

> ⚠ **Only run after Steps 10 and 11 are merged and deployed.** This is a one-way migration — no downgrade. Verify `job_runs` is correctly populated (Step 6) before proceeding.

**Pre-check:**
```bash
docker compose exec db psql -U scrapeflow -c "SELECT COUNT(*) FROM job_runs;"
# Confirm count > 0 if you have existing data
```

**Migration:**
- New hand-written migration (autogenerate will not generate DROP COLUMN correctly in all cases):
  ```python
  def upgrade():
      op.drop_column("jobs", "status")
      op.drop_column("jobs", "result_path")
      op.drop_column("jobs", "error")
  ```
- Edit: `api/app/models/job.py` — remove `status`, `result_path`, `error` fields
- Run: `alembic upgrade head`

**Verify:** `pytest tests/ -v` — entire test suite passes with the columns gone

**Spec ref:** §2.4

---

### Step 13 — Update NATS constants + docker-compose nats-init

**Dependencies:** None (can be done alongside schema steps)

**Files:**
- Edit: `api/app/constants.py` — replace `NATS_JOBS_RUN_SUBJECT` with:
  ```python
  NATS_JOBS_RUN_HTTP_SUBJECT       = "scrapeflow.jobs.run.http"
  NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT = "scrapeflow.jobs.run.playwright"
  NATS_JOBS_LLM_SUBJECT            = "scrapeflow.jobs.llm"
  NATS_JOBS_RESULT_SUBJECT         = "scrapeflow.jobs.result"  # unchanged
  ```
- Edit: `docker/docker-compose.yml` — update `nats-init` command to idempotent create-or-edit with `scrapeflow.jobs.>` wildcard subject:
  ```yaml
  command: >-
    sh -c "
      nats stream info SCRAPEFLOW --server nats:4222 > /dev/null 2>&1
      && nats stream edit SCRAPEFLOW --subjects 'scrapeflow.jobs.>' --server nats:4222
      || nats stream add SCRAPEFLOW
           --subjects 'scrapeflow.jobs.>'
           --retention work --max-deliver 3
           --storage file --replicas 1
           --server nats:4222
    "
  ```
- Edit: `docker/docker-compose.yml` `worker` service env — rename `NATS_JOBS_RUN_SUBJECT` → `NATS_JOBS_RUN_HTTP_SUBJECT: "scrapeflow.jobs.run.http"`

> ⚠ Dev stacks may need `docker compose down -v && docker compose up -d` to recreate the NATS stream with the new subject. The idempotent `stream edit || stream add` form handles all cases after that.

**Verify:**
```bash
docker compose down -v && docker compose up -d nats nats-init
# nats stream info SCRAPEFLOW should show subjects: scrapeflow.jobs.>
```

**Spec ref:** §3.1, §3.2, §8.1

---

### Step 14 — Update Go HTTP worker for Phase 2

**Dependencies:** Step 13

**Changes:**
1. Subscribe subject: `scrapeflow.jobs.run` → `scrapeflow.jobs.run.http`
2. Message schema: accept `run_id` field in dispatch message; include `run_id` in all published results
3. MinIO paths: write to BOTH `latest/{job_id}.{ext}` AND `history/{job_id}/{unix_timestamp}.{ext}`; publish the `history/` path in the result event
4. Pull consumer: replace push subscription with pull consumer + semaphore worker pool (`cfg.WorkerPoolSize`, default `runtime.NumCPU()`)
5. Structured logging: replace `log.Printf` calls with `slog.Info/slog.Error` (stdlib since Go 1.21)
6. Result message: include `nats_stream_seq` (from `msg.Metadata().Sequence.Stream`) on `status: "running"` messages only

**Files to modify:**
- `worker/internal/worker/worker.go` — pull consumer loop, run_id threading, nats_stream_seq
- `worker/internal/storage/storage.go` — dual MinIO path writes
- `worker/cmd/worker/config.go` — add `WorkerPoolSize int`
- `worker/cmd/worker/main.go` — pass WorkerPoolSize to worker

**Verify:**
```bash
docker compose exec worker go test ./... -v
```

**Spec ref:** §4.1

---

### Step 15 — Update result consumer for Phase 2

**Dependencies:** Steps 6, 8, 9, 13

**This is the most complex single task.** The result consumer gains:
1. **Cancellation guard** (run-based, not job-based): check `job_run.status == 'cancelled'` at top of handler using `run_id`
2. **`status: "running"` handler**: update `job_run.started_at` + store `nats_stream_seq`
3. **`status: "completed"` from scrape worker** (discriminated by `job_run.status == 'running'`):
   - If `job.llm_config` set → transition to `processing`, dispatch to `scrapeflow.jobs.llm`, `return`
   - If `llm_key` not found → `status='failed'`, fire `job.failed` webhook, `return`
   - Else → compute text diff against previous completed run, create webhook delivery, `return`
4. **`status: "completed"` from LLM worker** (discriminated by `job_run.status == 'processing'`):
   - Compute JSON diff against previous completed run
   - Create webhook delivery
5. **`status: "failed"` handler**: update `job_run` error + create `job.failed` webhook delivery (if configured)

**Files:**
- Edit: `api/app/core/result_consumer.py` — full rewrite of handler logic
- New: `api/app/core/diff.py` — `compute_text_diff(path_a, path_b)` and `compute_json_diff(path_a, path_b)` (fetch from MinIO, diff, return `{detected: bool, summary: dict | None}`)
- New: `api/app/core/webhooks.py` — `create_webhook_delivery(db, job, run_id, event, minio_path, diff)` helper

**Verify:** `pytest tests/test_jobs.py -v` — add integration tests:
- `status: "running"` → `started_at` set, `nats_stream_seq` stored
- `status: "completed"` (no LLM) → diff computed, webhook delivery row created
- `status: "completed"` with `llm_config` → `processing` transition, LLM dispatch, no diff yet
- `status: "completed"` with missing LLM key → `status='failed'`, `job.failed` webhook, no fallthrough
- `status: "completed"` from LLM worker → JSON diff computed, webhook delivery created
- Cancelled run result → discarded (no DB update)

**Spec ref:** §7

---

### Step 16 — New job routes: `PATCH /jobs/{id}`, `GET /jobs/{id}/runs`, webhook-secret rotate

**Dependencies:** Steps 6, 10, 11

**Files:**
- Edit: `api/app/routers/jobs.py` — add three new handlers:
  - `GET /jobs/{id}/runs` — paginated `job_runs` for this job (user-scoped ownership check)
  - `PATCH /jobs/{id}` — partial update of mutable fields only (see spec §5.6 for mutable/immutable table); enforce 409 if `llm_config` update while latest run is `processing`
  - `POST /jobs/{id}/webhook-secret/rotate` — generate new Fernet key, encrypt, update `jobs.webhook_secret`, return new secret once

**Key rules for `PATCH`:**
- `url`, `engine`, `output_format` are immutable — return 422 if included
- On `schedule_cron` change: recalculate `next_run_at = croniter(new_cron, now()).get_next(datetime)` immediately
- On `webhook_url` removal: set `webhook_secret = NULL`
- On `webhook_url` change: SSRF-validate the new URL

**Verify:** `pytest tests/test_jobs.py -v`

**Spec ref:** §5.6

---

### Step 17 — LLM key management routes

**Dependencies:** Steps 3, 7

**Files:**
- New: `api/app/routers/llm_keys.py` — three routes:
  - `POST /users/llm-keys` — SSRF-check `base_url`; Fernet-encrypt `api_key`; return `{id, name, provider, api_key: "sk-***"}` (first 5 chars + `***`)
  - `GET /users/llm-keys` — list active keys, never return raw key; return `{id, name, provider, base_url, created_at}`
  - `DELETE /users/llm-keys/{id}` — hard delete (404 for missing or cross-user)
- Edit: `api/app/main.py` — register `llm_keys` router

**Verify:**
```bash
pytest tests/test_llm_keys.py -v
```
Tests:
- Create key → DB stores encrypted (raw key not visible)
- List keys → api_key field masked
- Delete key → hard deleted
- Create job referencing deleted key → 404

**Spec ref:** §5.7

---

### Step 18 — Python Playwright worker (new service)

**Dependencies:** Step 13

**Location:** `playwright-worker/`

**Files to create:**
- `playwright-worker/Dockerfile` — base: `mcr.microsoft.com/playwright/python:v1.44.0-jammy`
- `playwright-worker/pyproject.toml` — deps: `playwright`, `nats-py`, `miniopy-async`, `structlog`, `html2text`
- `playwright-worker/app/config.py` — env vars: NATS_URL, MINIO_*, PLAYWRIGHT_MAX_WORKERS, PLAYWRIGHT_DEFAULT_TIMEOUT_SECONDS
- `playwright-worker/app/worker.py` — pull consumer on `scrapeflow.jobs.run.playwright`, per-job lifecycle as per spec §4.2
- `playwright-worker/app/formatter.py` — same HTML→markdown/JSON logic as Go worker (share the pattern, not the code)
- `playwright-worker/app/main.py` — startup sequence: config → NATS → MinIO → browser → worker loop
- `playwright-worker/tests/test_worker.py` — mock Chromium; verify context close on both success and failure paths

**Key implementation details:**
- Each job gets an isolated `browser_context` (never share contexts across jobs)
- `finally: await context.close()` — always runs even on exception
- `block_images` route: abort pattern/image/font/css routes before `page.goto()`
- Dual MinIO writes: `latest/{job_id}.{ext}` + `history/{job_id}/{timestamp}.{ext}` (same as Go worker)

**Verify:**
```bash
docker compose build playwright-worker
docker compose run --rm playwright-worker python -m pytest tests/ -v
```

**Spec ref:** §4.2

---

### Step 19 — Python LLM worker (new service)

**Dependencies:** Step 13

**Location:** `llm-worker/`

**Files to create:**
- `llm-worker/Dockerfile` — base: `python:3.12-slim`
- `llm-worker/pyproject.toml` — deps: `openai`, `anthropic`, `nats-py`, `miniopy-async`, `cryptography`, `structlog`, `httpx`
- `llm-worker/app/config.py` — env vars: NATS_URL, MINIO_*, LLM_KEY_ENCRYPTION_KEY, LLM_REQUEST_TIMEOUT_SECONDS, LLM_MAX_CONTENT_CHARS
- `llm-worker/app/llm.py` — `call_anthropic()` and `call_openai_compatible()` as defined in spec §4.3
- `llm-worker/app/worker.py` — pull consumer on `scrapeflow.jobs.llm`, per-job lifecycle
- `llm-worker/app/main.py`
- `llm-worker/tests/test_worker.py` — mock LLM API responses

**Key implementation details:**
- Decrypt `encrypted_api_key` with Fernet at call time — key never persisted or logged
- Truncate content to `LLM_MAX_CONTENT_CHARS` BEFORE the LLM call (log a warning)
- `httpx.Timeout` applied at transport layer (covers both connect and streaming read)
- Anthropic: use tool_use with `tool_choice={"type": "tool", "name": "extract"}` — forces structured output
- OpenAI-compatible: use `response_format` with `json_schema` — works for OpenAI, vLLM, Groq, Together

**Verify:**
```bash
docker compose build llm-worker
docker compose run --rm llm-worker python -m pytest tests/ -v
```

**Spec ref:** §4.3

---

### Step 20 — Scheduler loop background task

**Dependencies:** Steps 6, 10, 13

**Files:**
- New: `api/app/core/scheduler.py` — `scheduler_loop(db_factory, js)` as defined in spec §6.1
  - Polls every 60s for jobs with `next_run_at <= NOW()` using `FOR UPDATE SKIP LOCKED`
  - DB commit before NATS publish (source-of-truth ordering)
  - Stale-pending recovery: re-dispatch `job_runs` with `status='pending'` AND `created_at < NOW() - 10 minutes`
  - `asyncio.CancelledError` re-raised; all other exceptions logged + loop continues
- Edit: `api/app/main.py` lifespan — launch `asyncio.create_task(scheduler_loop(...))` and cancel on shutdown

**Verify:** `pytest tests/test_scheduler.py -v`
- Seed job with `next_run_at = NOW() - 1m` → verify `job_runs` row created, NATS message published, `next_run_at` updated
- Stale-pending recovery test: seed `job_runs` with `status='pending'` and old `created_at` → verify re-dispatched

**Spec ref:** §6.1

---

### Step 21 — Webhook delivery loop background task

**Dependencies:** Steps 9, 15

**Files:**
- New: `api/app/core/webhook_loop.py` — `webhook_delivery_loop(db_factory, http_client, fernet)` as defined in spec §6.2
  - Polls every 15s; `FOR UPDATE SKIP LOCKED LIMIT 50`
  - HMAC-SHA256 signature on every delivery (`X-ScrapeFlow-Signature: sha256=...`)
  - Backoff: `BACKOFF_SECONDS = [0, 30, 300, 1800, 7200]`; index capped at `len(BACKOFF_SECONDS)-1` to handle `WEBHOOK_MAX_ATTEMPTS > 5`
  - `status=delivered` on 2xx, `status=pending` with incremented attempts + backoff on non-2xx, `status=exhausted` after max attempts
  - `asyncio.CancelledError` re-raised; all other exceptions logged + loop continues
- Edit: `api/app/main.py` lifespan — launch `asyncio.create_task(webhook_delivery_loop(...))` with shared `httpx.AsyncClient` and Fernet instance; cancel on shutdown

**Verify:** `pytest tests/test_webhook_delivery.py -v`
- 2xx response → `status=delivered`
- 500 response → `attempts++`, backoff applied
- 5 failures → `status=exhausted`
- HMAC header present and verifiable with the job's webhook_secret

**Spec ref:** §6.2

---

### Step 22 — MaxDeliver advisory subscriber

**Dependencies:** Steps 9, 13

**Files:**
- New: `api/app/core/advisory.py` — `maxdeliver_advisory_subscriber(nats_client, db_factory)` as defined in spec §6.3
  - Subscribes to `$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SCRAPEFLOW.*`
  - On advisory: parse `stream_seq` from payload, look up `job_runs.nats_stream_seq`, mark `status='failed'`
  - Status filter: `IN ('pending', 'running', 'processing')` — includes `processing` for LLM jobs exhausting `.llm` subject retries
- Edit: `api/app/main.py` lifespan — launch as `asyncio.create_task(maxdeliver_advisory_subscriber(...))` and cancel on shutdown

**Verify:** `pytest tests/test_advisory.py -v`
- Simulate advisory JSON message, verify `job_runs.status='failed'` and `error='Max NATS redeliveries exceeded'`

**Spec ref:** §6.3

---

### Step 23 — Admin panel API routes

**Dependencies:** Steps 2, 4, 6, 9

**Files:**
- New: `api/app/routers/admin.py` — all `/admin/*` routes, all protected with `Depends(get_current_admin_user)`:
  - `GET /admin/users` — paginated, filterable
  - `GET /admin/users/{id}` — user detail + usage stats (job count by status)
  - `DELETE /admin/users/{id}` — hard delete + CASCADE (confirm: also deletes all jobs/runs)
  - `GET /admin/jobs` — all jobs, filterable by user_id/status/engine
  - `GET /admin/jobs/{id}` — any job regardless of ownership
  - `DELETE /admin/jobs/{id}` — force cancel latest run + optionally hard delete
  - `GET /admin/webhooks/deliveries` — filterable by status
  - `POST /admin/webhooks/deliveries/{id}/retry` — reset `attempts=0`, `status='pending'`
- Edit: `api/app/main.py` — register `admin` router

**Verify:** `pytest tests/test_admin.py -v`
- Every route: 403 for non-admin; 200 for admin
- Admin can read/cancel jobs belonging to other users
- Retry endpoint resets delivery correctly

**Spec ref:** §5.8

---

### Step 24 — Admin stats endpoint

**Dependencies:** Step 23

**Files:**
- Edit: `api/app/routers/admin.py` — add `GET /admin/stats` and `GET /admin/stats/users/{id}`
  - Operational stats: live counts from DB (running jobs, pending webhook deliveries, next scheduled run)
  - Historical stats: aggregate queries over `job_runs.created_at` (7-day, monthly windows)
  - `minio_storage_bytes`: MinIO `stat_object` or bucket stats API; cache in Redis with 5-minute TTL (`scrapeflow:cache:minio_storage`)
  - All historical queries target `job_runs.created_at`, NOT `jobs.created_at`

**Verify:** Seed known data, query stats endpoint, verify counts match

**Spec ref:** §5.9

---

### Step 25 — `scripts/cleanup_old_runs.py`

**Dependencies:** Steps 6, 9

**Files:**
- New: `api/scripts/cleanup_old_runs.py` — standalone async script as defined in spec §8.3
  - Batches of 500 rows
  - MinIO delete BEFORE DB delete (MinIO failure leaves DB row for retry; inverse is unrecoverable)
  - `successful_ids` built inside loop — only append on successful MinIO delete
  - Deletes: `job_runs`, their `webhook_deliveries`, their `history/` MinIO objects
  - Never deletes: `latest/` MinIO objects, `jobs` rows

**Verify:**
```bash
# Seed data with old job_runs, run script, verify cleanup
docker compose exec api python scripts/cleanup_old_runs.py
```

**Spec ref:** §8.3

---

### Step 26 — Docker Compose: add Playwright + LLM worker services

**Dependencies:** Steps 18, 19

**Files:**
- Edit: `docker/docker-compose.yml` — add `playwright-worker` and `llm-worker` services per spec §8.1
- Edit: `docker/.env.example` — add `LLM_KEY_ENCRYPTION_KEY=` with generation note

**Verify:**
```bash
docker compose up --build -d
docker compose ps  # all 8 services healthy
curl -s http://localhost:8000/health/ready  # all deps "ok"
```

**Spec ref:** §8.1

---

## Phase 2 End-to-End Smoke Test

Run after all steps complete:

```bash
# 1. Start fresh
docker compose down -v && docker compose up -d
sleep 10

# 2. Health check
curl -s http://localhost:8000/health/ready  # all deps "ok"

# 3. HTTP job (using existing dev_token.sh)
TOKEN=$(./scripts/dev_token.sh --api-key sf_...)
JOB=$(curl -s -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"url": "https://example.com", "output_format": "markdown"}')
RUN_ID=$(echo $JOB | jq -r '.run_id')

# 4. Poll until completed
curl -s http://localhost:8000/jobs/$(echo $JOB | jq -r '.id') \
  -H "Authorization: Bearer $TOKEN" | jq .status

# 5. Playwright job
curl -s -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"url": "https://example.com", "engine": "playwright", "output_format": "html"}'

# 6. Scheduled job (runs every 5 min)
curl -s -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"url": "https://example.com", "schedule_cron": "*/5 * * * *"}'
```

---

## Deferred to Phase 3

| Item | Reason |
|------|--------|
| Re-validate `webhook_url` on every delivery attempt (SSRF re-check) | DNS rebinding gap; Phase 3 hardening |
| k3s manifests for playwright-worker + llm-worker + CronJob | No k3s deployment yet; add with Phase 3 infra |
| Per-event webhook subscriptions | Phase 3 UX — users configure which events to receive |
