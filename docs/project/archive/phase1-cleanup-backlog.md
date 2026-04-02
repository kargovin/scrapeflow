# Pre-Phase 2 Cleanup — Completed Backlog

> **ARCHIVED — 2026-04-02.** All 18 cleanup steps are complete. This file is kept for historical context. Active Phase 2 work is tracked in `docs/project/PHASE2_BACKLOG.md`.

> **Purpose:** Close all open items from `suggestions.md` before Phase 2 begins.
> Work through these one step at a time. Each step is independently completable and verifiable.
>
> **Status: ALL 18 STEPS COMPLETE** — codebase is ready for Phase 2.
>
> **Test commands:**
> - Python: `docker compose exec api python -m pytest`
> - Go: `docker compose exec worker go test ./...`

---

## Steps (ordered by risk — do in sequence)

### ~~Step 1 — Commit Already-Fixed Worker (suggestion #18)~~
~~- [ ] `git add worker/cmd/worker/main.go worker/internal/worker/worker.go`~~
~~- [ ] Commit: "wire NATS_MAX_DELIVER through to worker subscription (suggestion #18)"~~

~~**What was done:** `Run(ctx, maxDeliver int)` now accepts `maxDeliver` and passes it to `nats.MaxDeliver(maxDeliver)`. `main.go` passes `cfg.NATSMaxDeliver`. Already in unstaged changes.~~

✅ Done — committed as "Use the NATS_MAX_DELIVER through to worker subscription"

---

### ~~Step 2 — Worker: `publishResult` Error Propagation (suggestion #19)~~

~~**File:** `worker/internal/worker/worker.go`~~

~~**Problem:** If NATS publish fails after scraping, the job is acked and stuck permanently in `running`.~~

~~**Changes:**~~
~~- Change `publishResult` signature: `func (w *Worker) publishResult(result resultMessage) error`~~
~~- On marshal or `js.Publish` failure: return the error (don't silently return)~~
~~- In `handleMessage`:~~
~~  - "running" publish failure → log + **continue** (fire-and-forget; job can still complete)~~
~~  - "completed"/"failed" publish failure → log + **do NOT call `msg.Ack()`** + return~~
~~  - Only call `msg.Ack()` after a successful terminal-result publish~~
~~- Update `worker_test.go` to cover the no-ack path on publish failure~~

~~**Why:** NATS will redeliver after `AckWait` (5 min), causing a re-scrape. Idempotent per ADR-001 §5. A duplicate scrape is better than a permanently stuck job.~~

~~**Verify:** `go test ./internal/worker/... -v`~~

✅ Done — `publishResult` returns `error`; terminal failures call `msg.NakWithDelay(30 * time.Second)` instead of ack.

---

### ~~Step 3 — API: Atomic Rate Limiter + Retry-After (suggestion #17)~~

~~**File:** `api/app/core/rate_limit.py`~~

~~**Problem:** `INCR` + `EXPIRE` as two commands — a crash between them leaves a key with no TTL.~~

~~**Changes:**~~
~~- Move `import time` from inside `_current_window()` to module top~~
~~- Replace two-step INCR/EXPIRE with pipeline:~~
~~  ```python~~
~~  pipe = redis.pipeline()~~
~~  pipe.incr(key)~~
~~  pipe.expire(key, settings.rate_limit_window_seconds)~~
~~  count, _ = await pipe.execute()~~
~~  # Remove the "if count == 1:" block — pipeline always sends both commands~~
~~  ```~~
~~- Add `Retry-After` header to the 429 response~~

~~**Verify:** `pytest tests/test_rate_limit.py -v` (no behavioral change, all tests pass)~~

✅ Done — implemented with a **Lua script** (stronger than pipeline: truly atomic in Redis). `import time` moved to module top. `Retry-After` header added.

---

### ~~Step 4 — API: SSRF Protection (suggestion #16)~~

~~**File:** `api/app/routers/jobs.py`~~

~~**Problem:** Any authenticated user can submit `http://169.254.169.254/`, `http://redis:6379/` etc. Worker fetches and stores the response in MinIO.~~

~~**Changes:** Add `_validate_no_ssrf(url: str) -> None` using stdlib only (`ipaddress`, `socket`, `urllib.parse`):~~
~~1. Parse hostname from URL~~
~~2. `socket.getaddrinfo(hostname, None)` → list of IPs (resolves Docker service names like `redis`, `minio`)~~
~~3. For each IP: if `is_loopback`, `is_private`, `is_link_local`, or `is_reserved` → raise `HTTPException(400, "URL resolves to a private address")`~~
~~4. `socket.gaierror` (DNS failure) → raise `HTTPException(422, "URL hostname could not be resolved")`~~

~~Call before DB insert: `await asyncio.get_event_loop().run_in_executor(None, _validate_no_ssrf, str(body.url))`~~

~~Add tests in `test_jobs.py`: mock `socket.getaddrinfo` to return private IPs, assert 400.~~

~~**Verify:** `pytest tests/test_jobs.py -v`~~

✅ Done — `_validate_no_ssrf()` in `jobs.py`, called via `run_in_executor`. DNS-resolves hostnames to catch Docker service names.

---

### ~~Step 5 — API: Migration Check at Startup (suggestion #1)~~

~~**Files:** `api/app/main.py`, `api/migrations/env.py`~~

~~**Problem:** API can start with a stale schema (Postgres healthy ≠ schema current).~~

~~**Changes:**~~
~~- Restructure `migrations/env.py` to expose `run_migrations_online()` as a callable without `asyncio.run(...)` at module bottom~~
~~- In `main.py` lifespan, call `await run_migrations_online()` **first**, before connecting Redis/NATS/MinIO~~
~~- Wrap in `try/except` — migration failure → log + re-raise (crash startup hard)~~

~~**Verify:** `docker compose up api` — observe migration log line at startup before first request~~

✅ Done — `_run_migrations_online()` called via `run_in_executor` in `lifespan` before all other clients.

---

### ~~Step 6 — Docker: Multi-Stage Dockerfile + .dockerignore (suggestions #4, #15)~~

~~**File:** `api/Dockerfile`~~

~~Replace single-stage build with three stages:~~

~~```dockerfile~~
~~FROM python:3.12-slim AS base~~
~~# Install uv, copy pyproject.toml~~
~~RUN uv sync --frozen          # use uv.lock for reproducible builds (#15)~~
~~COPY app/ app/~~
~~COPY migrations/ migrations/~~
~~COPY alembic.ini .~~

~~FROM base AS test~~
~~RUN uv sync --frozen --extra dev~~
~~COPY tests/ tests/~~

~~FROM base AS production~~
~~EXPOSE 8000~~
~~CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]~~
~~```~~

~~**New file:** `api/.dockerignore`~~

~~**Verify:**~~
~~```bash~~
~~docker build --target production -t scrapeflow-api:prod api/~~
~~docker build --target test -t scrapeflow-api:test api/~~
~~docker images scrapeflow-api  # prod image noticeably smaller~~
~~```~~

✅ Done — three-stage Dockerfile (`base`/`test`/`production`). `uv.lock` committed. `api/.dockerignore` added.

---

### ~~Step 7 — API: Replace `assert` Guards with `RuntimeError` (minor)~~

~~**Files:** `api/app/core/redis.py`, `api/app/core/nats.py`, `api/app/core/minio.py`~~

~~Python's `-O` flag strips `assert`. Replace each:~~
~~```python~~
~~# Before:~~
~~assert _pool is not None, "Redis pool not initialized"~~
~~# After:~~
~~if _pool is None:~~
~~    raise RuntimeError("Redis pool not initialized")~~
~~```~~

~~**Verify:** `pytest tests/test_redis.py tests/test_nats.py tests/test_minio.py -v`~~

✅ Done — module-level globals eliminated entirely; all clients live on `app.state`. No asserts remain.

---

### ~~Step 8 — API: Infrastructure Clients → `app.state` (suggestion #6)~~

~~**Files:** `api/app/main.py`, `api/app/core/redis.py`, `api/app/core/nats.py`, `api/app/core/minio.py`, `api/app/core/rate_limit.py`, `api/app/routers/jobs.py`, `api/app/core/result_consumer.py`, `api/tests/conftest.py`~~

~~**Problem:** Module-level globals make test isolation depend on ordering; can't inject test fakes.~~

~~**Pattern:**~~
~~`main.py` lifespan assigns to `app.state`, dependency functions accept `Request`.~~

~~`result_consumer.py` receives JetStream via parameter (called from lifespan).~~

~~`conftest.py`: set `app.state.*` directly on the imported `app` object in fixtures.~~

~~**Also fix:** `test_rate_limit.py` mutates `settings.rate_limit_requests` — pass limit as parameter instead.~~

~~> ⚠ Highest regression risk. Run full test suite after: `pytest tests/ -v`~~

✅ Done — `app.state.redis_pool`, `.minio`, `.nats_client`, `.nats_js`. Deps read via `Request`. `start_result_consumer(app.state.nats_js)` called from lifespan.

> Note: `test_rate_limit.py` still mutates `settings.rate_limit_requests` directly — minor, deferred.

---

### ~~Step 9 — API: CORS Config from Env Var (suggestion #5)~~

~~**Files:** `api/app/settings.py`, `api/app/main.py`~~

~~`settings.py` — add `allowed_origins_raw` + `allowed_origins` property.~~
~~`main.py` — replace hardcoded `allow_origins=["*"]`. Remove the TODO comment.~~

~~**Verify:** `pytest tests/ -v`~~

✅ Done — `ALLOWED_ORIGINS` env var, `allow_credentials` dynamic based on whether origins is `["*"]`.

---

### ~~Step 10 — API: `last_used_at` Tracking (suggestion #8)~~

~~**File:** `api/app/auth/api_key.py`~~

~~After successful key lookup in `verify_api_key()`:~~
~~```python~~
~~await db.execute(~~
~~    update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=datetime.now(timezone.utc))~~
~~)~~
~~```~~

~~Add test `test_verify_api_key_updates_last_used_at` in `test_api_keys.py`.~~

~~**Verify:** `pytest tests/test_api_keys.py tests/test_auth.py -v`~~

✅ Done — `update(ApiKey).values(last_used_at=datetime.now(timezone.utc))` in `verify_api_key`.

---

### ~~Step 11 — API: Fix `result_consumer.py` Logging (suggestion #20)~~

~~**File:** `api/app/core/result_consumer.py`~~

~~Two-line fix — replace `import logging` → `import structlog`, `logger = logging.getLogger(__name__)` → `logger = structlog.get_logger()`~~

~~**Verify:** `pytest tests/test_jobs.py -v`~~

✅ Done.

---

### ~~Step 12 — Worker: Response Body Size Limit (suggestion #21)~~

~~**File:** `worker/internal/fetcher/fetcher.go`~~

~~```go~~
~~const maxBodyBytes = 10 * 1024 * 1024 // 10 MB~~
~~body, err := io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes))~~
~~```~~

~~**Verify:** `go test ./internal/fetcher/... -v`~~

✅ Done — `const maxBodySize = 10 * 1024 * 1024` and `io.LimitReader(resp.Body, maxBodySize)`.

---

### ~~Step 13 — Docker: Fix NATS Stream Retention (suggestion #10d)~~

~~**File:** `docker/docker-compose.yml`~~

~~Change `--retention limits` → `--retention work` in `nats-init` command to match ADR-001 §1.~~

~~> ⚠ Existing dev stacks must `docker compose down -v` to recreate the NATS volume. Add to `PROGRESS.md` gotchas.~~

~~**Verify:**~~
~~```bash~~
~~docker compose down -v && docker compose up -d nats nats-init~~
~~nats stream info SCRAPEFLOW  # should show: Retention Policy: Work Queue~~
~~```~~

✅ Done — `--retention work` confirmed in docker-compose.yml.

---

### ~~Step 14 — Worker: Thread Shutdown Context (suggestion #23)~~

~~**File:** `worker/internal/worker/worker.go`~~

~~**Problem:** `processJob` uses `context.Background()` — in-flight HTTP fetches ignore SIGTERM.~~

~~**Change:** Use a closure to thread `ctx` into the NATS callback:~~
~~```go~~
~~handler := func(msg *nats.Msg) {~~
~~    w.handleMessage(ctx, msg)~~
~~}~~
~~```~~

~~**Verify:** `go test ./internal/worker/... -v`~~

✅ Done — `handler` closure passes `ctx` into `handleMessage(ctx, msg)` → `processJob(ctx, &job)` → `fetcher.Fetch(ctx, ...)`.

---

### ~~Step 15 — API: Health Readiness Endpoint + Version from Metadata (suggestions #12, minor)~~

~~**File:** `api/app/routers/health.py`~~

~~Fix hardcoded version with `importlib.metadata`. Keep `GET /health` as liveness probe. Add `GET /health/ready` readiness probe: DB `SELECT 1`, Redis `PING`, NATS `is_closed`. Return 200 ok / 503 degraded.~~

~~> Requires Step 8 (`app.state`) to be done first.~~

~~**Verify:** `pytest tests/test_health.py -v`~~

✅ Done — `GET /health/ready` checks DB, Redis, NATS. Returns 503 if any degraded. Version from `importlib.metadata`.

---

### ~~Step 16 — API: Request Correlation ID Middleware (suggestion #14)~~

~~**New files:** `api/app/middleware/__init__.py`, `api/app/middleware/correlation.py`~~

~~```python~~
~~class CorrelationIDMiddleware(BaseHTTPMiddleware):~~
~~    async def dispatch(self, request, call_next):~~
~~        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())~~
~~        clear_contextvars()~~
~~        bind_contextvars(request_id=request_id)~~
~~        ...~~
~~```~~

~~Register in `api/app/main.py` **before** `CORSMiddleware`.~~

~~**Verify:** `pytest tests/ -v`; manual: `curl -sI http://localhost:8000/health | grep X-Request-ID`~~

✅ Done — `CorrelationIdMiddleware` in `app/middleware/correlation.py`, registered before `CORSMiddleware` in `main.py`.

---

### ~~Step 17 — API: Structured Logging in Routes (suggestion #13)~~

~~**Files:** `api/app/routers/jobs.py`, `api/app/routers/users.py`, `api/app/auth/dependencies.py`~~

~~Add `logger = structlog.get_logger()` to each. Key log points: `job_created`, `job_cancelled`, `api_key_created`, `api_key_revoked`, `auth_failed`.~~

~~**Verify:** `pytest tests/ -v`~~

✅ Done — structured logging with job_id, user_id context in all route handlers and auth dependency.

---

### ~~Step 18 — Docker: Dev Hot-Reload (suggestion #9)~~

~~**File:** `docker/docker-compose.yml`~~

~~Add `command:` override to `api` service:~~
~~```yaml~~
~~command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload~~
~~```~~

~~**Verify:** Edit any file in `api/app/` while stack is running — uvicorn should log "Detected change in ..., reloading"~~

✅ Done — `command: uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` in docker-compose.yml.

---

## ~~Final End-to-End Smoke Test~~

~~Run after all steps complete:~~

~~```bash~~
~~docker compose down -v   # required: Step 13 changed NATS retention (needs volume wipe)~~
~~docker compose up -d~~
~~sleep 5~~
~~curl -s http://localhost:8000/health/ready   # all deps should show "ok"~~
~~# Create a job via scripts/dev_token.sh, poll until completed, verify result in MinIO~~
~~```~~

✅ All cleanup steps complete — smoke test can be run to verify the full stack.

---

## Deferred Items (not needed before Phase 2)

| # | Item | Reason |
|---|------|--------|
| 10e | MaxDeliver advisory subscription | Complex NATS advisory parsing; low-frequency failure; tackle in Phase 2 |
| 22 | Unbounded worker concurrency | Acceptable at Phase 1 volume; Phase 2 pull-based worker addresses it |
| 11 | Postgres ENUM migration pain | Process advisory; no code change — just be careful when adding new statuses |
| Go slog | Structured logging in worker | Standard `log` is readable; not blocking Phase 2 |
| Rate limit test mutation | `test_rate_limit.py` mutates `settings` | Tests are sequential; minor risk; not blocking Phase 2 |

---

## Phase 2 Architectural Decisions (document in ADR-002 before writing any Phase 2 migrations)

These decisions need to be made before writing Phase 2 code — they affect the migration schema.

**1. Job model new fields:**
- `engine VARCHAR(20) DEFAULT 'http'` — use CHECK constraint, not Postgres ENUM (avoids ALTER TYPE pain)
- `schedule_cron VARCHAR(100) NULL` — cron string; null = one-shot
- `webhook_url TEXT NULL`
- `llm_config JSONB NULL` — user API key + schema; JSONB allows schema evolution without migrations

**2. Worker routing strategy:** Use separate NATS subjects:
- `scrapeflow.jobs.run.http` → consumed by HTTP worker
- `scrapeflow.jobs.run.playwright` → consumed by Playwright worker

Avoids routing logic in workers. WorkQueue retention works cleanly with separate subjects.

**3. Change detection storage (MinIO path convention):**
- Latest result (overwritten): `{bucket}/latest/{job_id}.{ext}`
- History (append-only): `{bucket}/history/{job_id}/{timestamp}.{ext}`
- Diff computed in the API; workers write to both paths on every run.

**4. Webhook retry mechanism:** Use a Postgres `webhook_deliveries` retry table (not a NATS subject). Reasons: visible in admin panel, natural exponential backoff schedule in SQL, not latency-sensitive (job already complete when webhook fires).
