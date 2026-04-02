# ScrapeFlow — Phase 1 Architect's Review

> **ARCHIVED — 2026-04-02.** All 23 issues in this review are resolved. This file is kept for historical context. Active work is tracked in `docs/project/PHASE2_BACKLOG.md`.

> Reviewed by: architect analysis pass
> Codebase state: ~~Phase 1 MVP in progress (steps 1–5 complete)~~ ~~Phase 1 MVP complete (all 9 steps done)~~ Phase 1 complete + pre-Phase 2 cleanup done (all 18 cleanup steps committed) + Phase 2 spec written (2026-04-01)
> Date: 2026-03-25 (updated 2026-03-27, updated 2026-03-31)

---

## How to Continue This Review in a New Session

Claude Code has no persistent session ID — each conversation starts fresh. To resume with full context, start a new session and say:

> "Read `suggestions.md`, `PROGRESS.md`, and `CLAUDE.md`, then continue the architect review from where we left off."

That gives the next session: the full issue list from this review, the current build progress, and the project architecture/goals. No context is lost.

Files that define the project state:
- `suggestions.md` — this document (architectural issues, ranked by severity)
- `PROGRESS.md` — build step tracking, gotchas, notes
- `CLAUDE.md` — architecture, stack, MVP definition, key decisions

---

## Overall Assessment

The foundation is solid. The technology choices are appropriate for the problem, the code is clean and readable, and the separation of concerns is sensible for a project at this stage. The patterns established early (async SQLAlchemy, structlog, dependency injection, Clerk for auth) are production-grade choices that will scale without requiring re-architecture.

The main gaps are not quality issues — they are the expected incompleteness of an MVP in flight. The notes below are ranked by impact and flag the things that will bite hardest if not addressed before the project grows.

---

## Phase 1 Complete — Overall Assessment Update

Phase 1 is well-executed. Both the Python API and Go worker are clean, the ADR is faithfully implemented, and test coverage is solid across unit and integration tests. The worker internals (fetcher, formatter, storage) are correctly separated into packages with their own tests. The NATS subject constants are correctly placed in `constants.py` — not `settings.py` — respecting the contract boundary.

New findings below are from a full read of the completed codebase. Several are security-relevant and should be addressed before Phase 2 begins.

---

## Pre-Phase 2 Cleanup Complete — Assessment Update (2026-03-31)

All 18 cleanup steps from `TODO.md` have been committed. The codebase is now production-hardened for Phase 2. Security-critical issues (SSRF, rate limiter atomicity, stuck jobs) are all resolved. Infrastructure is correctly wired via `app.state`. Observability (correlation IDs, structured logging) is in place. The project is ready to begin Phase 2 feature work.

---

## Critical — Address Before Phase 2

### ~~1. No migration safety net at startup~~ — ✅ Resolved (pre-Phase 2 cleanup step 5)

~~The API starts and immediately serves traffic with no guarantee that migrations have been applied. In Docker Compose this is handled by `depends_on: condition: service_healthy` for the DB, but Postgres being *ready* is not the same as the schema being *current*.~~

~~A single scrape job written to a table that doesn't yet have a column (e.g., `error` column added in a future migration) will corrupt data silently or throw a 500 with no clear cause.~~

~~**Recommendation:** Add a startup check in `lifespan` that runs `alembic upgrade head` (or at minimum checks `alembic current` == `alembic head`) before the app begins accepting requests. For k8s, this becomes an init container.~~

`main.py` `lifespan` now runs `alembic upgrade head` via `asyncio.get_event_loop().run_in_executor(None, _run_migrations_online)` before connecting Redis/NATS/MinIO. Migration failure crashes startup cleanly with a logged exception.

---

### 2. ~~Worker cancellation protocol undefined~~ — ✅ Resolved by ADR-001

~~The job model has a `cancelled` status and a NATS queue. When a job is `pending` or `running`, cancelling it means the worker may still be processing it — setting `status = cancelled` in Postgres alone does not stop the worker. The worker needs to either check job status before writing results (poll-before-write), or receive a cancellation signal via NATS. Without this, a cancelled job can still have its result written to MinIO and its status flipped back to `completed` by the worker, silently overwriting the cancellation.~~

ADR-001 §7 defines the cancellation protocol: `DELETE /jobs/{id}` sets `status = cancelled` in Postgres only. The API result consumer on `scrapeflow.jobs.result` discards the result if `status = cancelled`. The worker wastes one scrape; correctness is preserved. Decision is documented and explicit.

---

### ~~4. Tests ship inside the production Docker image~~ — ✅ Resolved (pre-Phase 2 cleanup step 6, also resolves #15)

~~`COPY tests/ tests/` in the Dockerfile puts all test code, fixtures, and test-only dependencies (pytest, httpx) into the production image. This increases image size and attack surface, and `.[dev]` installs dev dependencies in production.~~

~~**Recommendation:** Use a multi-stage build.~~

Multi-stage Dockerfile now has three stages: `base` (production deps only, `app/`, `migrations/`, `alembic.ini`), `test` (extends `base`, adds dev deps and `tests/`), `production` (extends `base`, sets CMD). Docker Compose builds with `target: test`. `uv.lock` committed; `uv sync --frozen` in Dockerfile — also resolves suggestion #15 (non-reproducible builds). `api/.dockerignore` added to exclude `.venv/`, `__pycache__`, etc.

---

### ~~16. SSRF vulnerability — no private IP blocklist on job URLs~~ — ✅ Resolved (pre-Phase 2 cleanup step 4)

~~The API accepts any `AnyHttpUrl` and dispatches it to the worker, which makes a real HTTP GET. There is no validation blocking private/internal addresses. In a multi-tenant deployment this means any authenticated user can submit:~~

~~- `http://169.254.169.254/latest/meta-data/` — AWS/GCP/Azure instance metadata service~~
~~- `http://redis:6379/` — internal Redis instance~~
~~- `http://minio:9000/` — internal MinIO API~~
~~- `http://postgres:5432/` — causes a TCP connect, leaks port state~~
~~- `http://192.168.0.1/` — router admin panel~~

~~The worker fetches the URL and writes the response to MinIO, where the owner can retrieve it. This is a textbook SSRF attack that could leak cloud credentials or internal service data.~~

`_validate_no_ssrf(url)` added in `jobs.py` (called via `run_in_executor` before DB insert). Uses `socket.getaddrinfo` to resolve the hostname (catches Docker service names like `redis`, `minio`) then checks `is_loopback`, `is_private`, `is_link_local`, `is_reserved` on every resolved IP. Returns 400 for private addresses, 422 for unresolvable hostnames.

---

### ~~17. Rate limiter INCR/EXPIRE is not atomic — key can live forever~~ — ✅ Resolved (pre-Phase 2 cleanup step 3)

~~In `rate_limit.py`, the fixed-window counter used two separate Redis commands (`INCR` then `EXPIRE`). If the process crashed between them, the key was created with no TTL and lived forever.~~

~~**Recommendation:** Use a Redis pipeline or a Lua script to make INCR + EXPIRE atomic.~~

Implemented using a **Lua script** (stronger than the pipeline suggestion — Lua executes atomically within Redis's single-threaded engine, not just batched):

```lua
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
```

`Retry-After` header also added to 429 responses with the seconds remaining until the window resets.

---

### ~~18. Worker `NATS_MAX_DELIVER` config is loaded but silently ignored~~ — ✅ Resolved (pre-Phase 2 cleanup step 1)

~~`config.go` reads `NATS_MAX_DELIVER` from the environment into `cfg.NATSMaxDeliver`. But `worker.go` hardcodes `nats.MaxDeliver(3)` in the subscription options — `cfg.NATSMaxDeliver` is never passed through.~~

~~**Recommendation:** Pass `cfg.NATSMaxDeliver` into `worker.New()` and use it in the subscription.~~

`Run(ctx context.Context, maxDeliver int)` now accepts `maxDeliver` as a parameter and passes it to `nats.MaxDeliver(maxDeliver)`. `main.go` passes `cfg.NATSMaxDeliver`. `NATS_MAX_DELIVER=3` in docker-compose is now honoured.

---

### ~~19. Worker `publishResult` failure leaves jobs permanently stuck~~ — ✅ Resolved (pre-Phase 2 cleanup step 2)

~~In `worker.go`, `publishResult` logs and returns silently on NATS publish failure.~~

~~If the result publish fails (NATS partition, server restart), `handleMessage` continues and calls `msg.Ack()`. The NATS job dispatch message is acknowledged — NATS will never redeliver it. But the API result consumer never received the result event, so the job stays in `running` (or `pending`) state forever.~~

~~**Recommendation:** `publishResult` should return an error. If the result publish fails, do **not** ack the NATS message — let NATS redeliver the job.~~

`publishResult` now returns `error`. On terminal-result publish failure (`completed`/`failed`), `handleMessage` calls `msg.NakWithDelay(30 * time.Second)` — NATS redelivers after 30s, triggering a re-scrape. The 30s delay avoids a tight retry storm. On `running` publish failure it logs and continues (fire-and-forget — job can still complete).

---

## Important — Address Before Production

### ~~5. CORS is wide open with credentials enabled~~ — ✅ Resolved (pre-Phase 2 cleanup step 9)

~~`allow_origins=["*"]` combined with `allow_credentials=True` — rejected by browsers and wrong for production.~~

~~**Recommendation:** Add `ALLOWED_ORIGINS` to `Settings` now, defaulting to `["*"]` for local dev.~~

`settings.py` now has `allowed_origins_raw: str = Field(default="*", alias="ALLOWED_ORIGINS")` with an `allowed_origins` property that splits on commas for production. `main.py` uses `allow_origins=settings.allowed_origins` and `allow_credentials=settings.allowed_origins != ["*"]` — credentials disabled for wildcard, enabled only when explicit origins are set.

---

### ~~6. Global mutable singletons for all infrastructure clients~~ — ✅ Resolved (pre-Phase 2 cleanup step 8)

~~`redis.py`, `minio.py`, and `nats.py` all use module-level globals (`_pool`, `_client`, `_nc`). This works fine for a single-process deployment but makes testing fragile.~~

~~**Recommendation:** Store infrastructure clients on `app.state` in the FastAPI lifespan.~~

All infrastructure clients now live on `app.state`: `app.state.redis_pool`, `app.state.minio`, `app.state.nats_client`, `app.state.nats_js`. Dependency functions accept `Request` and read from `request.app.state`. `conftest.py` sets `app.state.*` directly in session fixtures. Module-level globals and `assert` guards eliminated.

---

### 7. ~~No pagination on `GET /jobs`~~ — ✅ Resolved, tracked in PROGRESS.md 6d

~~When the jobs router is built, listing all jobs for a user with `SELECT * FROM jobs WHERE user_id = ?` will return unbounded results.~~

---

### ~~8. API key `last_used_at` is never updated~~ — ✅ Resolved (pre-Phase 2 cleanup step 10)

~~The `ApiKey` model has `last_used_at` but `verify_api_key()` never sets it. This field will always be `NULL`.~~

~~**Recommendation:** Update `last_used_at` in `verify_api_key()` after a successful lookup.~~

`verify_api_key()` in `api_key.py` now executes `update(ApiKey).values(last_used_at=datetime.now(timezone.utc))` after a successful key lookup. Fire-and-forget within the same DB session.

---

### ~~9. Add `--reload` to complete the `develop.watch` setup~~ — ✅ Resolved (pre-Phase 2 cleanup step 18)

~~`docker-compose.yml` already has a `develop.watch` block with `action: sync` on `app/` — but uvicorn is not started with `--reload`, so synced files are never picked up until the container restarts.~~

~~**Recommendation:** Add `--reload` to the uvicorn command for development.~~

`docker-compose.yml` now has `command: uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` on the `api` service. The production Dockerfile `CMD` is unchanged (no `--reload`). `develop.watch` sync → uvicorn reload loop is complete.

---

### ~~20. `result_consumer.py` uses `logging` instead of `structlog`~~ — ✅ Resolved (pre-Phase 2 cleanup step 11)

~~Every other Python module in the project uses `structlog`. The result consumer uses the standard `logging` module — structured kwargs silently dropped.~~

~~**Recommendation:** Replace `import logging` with `import structlog`.~~

`result_consumer.py` now uses `import structlog` and `logger = structlog.get_logger()`. The structured keyword arguments (`error=`, `data=`, `job_id=`) are now correctly included in log output.

---

### ~~21. No response body size limit in the Go fetcher~~ — ✅ Resolved (pre-Phase 2 cleanup step 12)

~~`fetcher.go` uses `io.ReadAll` with no size constraint. A malicious user can submit a URL pointing to a multi-gigabyte file.~~

~~**Recommendation:** Wrap the response body with `io.LimitReader` before reading.~~

`fetcher.go` now has `const maxBodySize = 10 * 1024 * 1024 // 10 MB` and uses `io.ReadAll(io.LimitReader(resp.Body, maxBodySize))`. Partial responses from large bodies are silently truncated — acceptable trade-off vs OOM under concurrent large fetches.

---

### ~~22. NATS push subscription creates unbounded concurrency in the worker~~ — ⏳ Deferred to Phase 2

NATS push subscriptions in Go dispatch each incoming message to a separate goroutine. At Phase 1 volume this is acceptable. Phase 2 will switch to a pull consumer with a fixed worker pool, which addresses concurrency limits and integrates cleanly with the Playwright worker design.

---

### ~~23. Worker graceful shutdown does not cancel in-flight HTTP fetches~~ — ✅ Resolved (pre-Phase 2 cleanup step 14)

~~`processJob` uses `context.Background()` instead of the shutdown context. When Docker sends SIGTERM, in-flight jobs continue running because they hold a `context.Background()`.~~

~~**Recommendation:** Pass a shutdown context into `handleMessage` and thread it through to `processJob`.~~

`Run()` now creates a closure that passes `ctx` into `handleMessage(ctx, msg)`. `handleMessage` passes `ctx` to `processJob(ctx, &job)`. `processJob` passes `ctx` to `fetcher.Fetch(ctx, ...)`. SIGTERM now cancels in-flight HTTP fetches via context cancellation.

---

## Design Observations — Worth Discussing

### 10. ~~Worker contract undocumented~~ — ✅ Resolved by ADR-001

ADR-001 covers stream lifecycle, subjects, message schemas, worker responsibilities, ack timing, retry policy, cancellation, and MinIO path convention. Well-structured and explicit.

---

### 10a. ~~NATS stream is never created in Docker Compose (gap from ADR-001)~~ — ✅ Resolved, tracked in PROGRESS.md 8a

**Docker Compose has no init containers — that is a Kubernetes concept.** The correct Docker Compose equivalent is a one-shot service with `restart: "no"` that exits after doing its work, and a `condition: service_completed_successfully` dependency on it.

The `nats-init` service uses `natsio/nats-box`, creates the stream with `--retention work`, and uses `|| nats stream info SCRAPEFLOW` as an idempotent fallback. `api` and `worker` both depend on `nats-init: condition: service_completed_successfully`.

---

### 10b. ~~The API result consumer doesn't exist yet~~ — ✅ Resolved, tracked in PROGRESS.md 6g

---

### 10c. ~~MaxDeliver advisory detection is non-trivial~~  — Partially superseded

The current implementation (item 19 above, now resolved) means `publishResult` failures already prevent the API from ever receiving a result event. The MaxDeliver advisory path is a separate concern and remains valid — but item 19 should be fixed first since it's a more common failure mode.

---

### ~~10d. NATS stream retention diverges from ADR-001~~ — ✅ Resolved (pre-Phase 2 cleanup step 13)

~~ADR-001 §1 specifies `WorkQueuePolicy` retention (`--retention work`). The actual `nats-init` command in `docker-compose.yml` creates the stream with `--retention limits`.~~

~~WorkQueue retention automatically deletes messages after they are acknowledged — semantically correct for a job dispatch queue. Limits retention keeps messages until they age out or hit configured size/count limits, accumulating all dispatched jobs indefinitely.~~

`docker-compose.yml` `nats-init` now uses `--retention work` per ADR-001 §1. Existing dev stacks require `docker compose down -v` to wipe the NATS volume on upgrade (noted in PROGRESS.md gotchas).

---

### 10e. MaxDeliver exhaustion leaves jobs silently stuck — ⏳ Deferred to Phase 2

ADR-001 §6 says "The API result consumer is responsible for detecting [MaxDeliver exhaustion] via the `MaxDeliver` advisory." NATS publishes exhaustion advisories to `$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SCRAPEFLOW.*`. The current result consumer has no subscription to this subject. A job that exceeds `MaxDeliver` retries will stay in `pending` state forever.

**Recommendation (deferred):** Subscribe to the MaxDeliver advisory subject in the result consumer and mark the job as `failed` when it fires. The simpler alternative: configure a NATS dead-letter subject and subscribe to that instead of parsing advisory metadata.

---

### 11. SQLAlchemy Enum types will cause migration pain — ⏳ Process advisory

Both `JobStatus` and `OutputFormat` are stored as native Postgres `ENUM` types. Adding a new status requires an `ALTER TYPE ... ADD VALUE` migration. In Postgres, this cannot be done inside a transaction — Alembic's default transactional migration behavior will fail.

**Recommendation:** Be aware of this limitation. New fields for Phase 2 (e.g., `engine`, `schedule_cron`) should use `VARCHAR` + `CHECK` constraint, not Postgres ENUMs.

---

### ~~12. Health endpoint doesn't check dependencies~~ — ✅ Resolved (pre-Phase 2 cleanup step 15)

~~`GET /health` returns `{"status": "ok"}` unconditionally. If Postgres is down, Redis is OOM, or NATS is disconnected, the health endpoint still returns 200.~~

~~**Recommendation:** Add a `/health/ready` endpoint that checks each dependency.~~

`GET /health/ready` added: checks DB (`SELECT 1`), Redis (`PING` via `app.state.redis_pool`), NATS (`nats_client.is_connected`). Returns 200 with all `"ok"` or 503 with `{"status": "degraded", "db": "error: ..."}`. `GET /health` is unchanged — liveness probe only. Version now read from `importlib.metadata.version("scrapeflow-api")` with `"dev"` fallback.

---

### ~~13. `structlog` is imported but barely used~~ — ✅ Resolved (pre-Phase 2 cleanup step 17)

~~`structlog` is in the dependency list and used in `main.py` for startup logs. None of the other modules use it.~~

~~**Recommendation:** Add structured logging at the key decision points.~~

`jobs.py`, `users.py`, and `dependencies.py` now all have `logger = structlog.get_logger()`. Key events logged: `job_created`, `job_cancelled`, `api_key_created`, `api_key_revoked`, `auth_failed`. All log lines carry `request_id=` automatically via the correlation middleware.

---

### ~~14. No request ID / correlation ID~~ — ✅ Resolved (pre-Phase 2 cleanup step 16)

~~In a multi-tenant async system, debugging requires correlating logs across a request's lifecycle. Currently there is no trace/request ID attached to logs.~~

~~**Recommendation:** Add a middleware that generates a `request_id` per request and uses `structlog.contextvars.bind_contextvars(request_id=...)`.~~

`CorrelationIdMiddleware` added in `api/app/middleware/correlation.py`. Uses `X-Request-ID` header if present (allows client-driven correlation), otherwise generates a UUID. Binds to `structlog` context vars — every log line in the request automatically carries `request_id=`. Echo'd back in `X-Request-ID` response header. Registered in `main.py` before `CORSMiddleware`.

---

### ~~15. `uv pip install --system --no-cache` in Dockerfile is not reproducible~~ — ✅ Resolved (pre-Phase 2 cleanup step 6, combined with #4)

~~The Dockerfile uses `uv pip install` with version ranges. Two builds a month apart may install different versions. No `uv.lock` file committed.~~

Resolved as part of the multi-stage Dockerfile rewrite. `uv.lock` is committed; `uv sync --frozen` is used in the Dockerfile for reproducible builds.

---

## Minor / Low Priority

- ~~**`health.py` version is hardcoded** to `"0.1.0"`.~~ — ✅ Resolved: Now uses `importlib.metadata.version("scrapeflow-api")` with `"dev"` fallback.

- **`alembic.ini` `sqlalchemy.url`** likely has the localhost default which is wrong inside Docker. The `env.py` overrides it from `settings.database_url`, so it works — but the `.ini` value is a footgun for anyone running `alembic` directly outside the container. *(Still open — advisory)*

- ~~**No `.dockerignore`**~~ — ✅ Resolved: `api/.dockerignore` added, excludes `.venv/`, `__pycache__/`, `.pytest_cache/`, `.env.*`, `dist/`.

- ~~**`assert` statements for null-checks in production code** (`redis.py`, `minio.py`, `nats.py`).~~ — ✅ Resolved: Module-level globals eliminated entirely; all clients read from `request.app.state`. No asserts remain.

- **No `__all__` exports in `app/models/__init__.py`** — the `import app.models` in `migrations/env.py` relies on `__init__.py` importing the submodules. If someone adds a new model file and forgets to add it to `__init__.py`, Alembic won't detect it. *(Still open — advisory)*

- ~~**`import time` inside `_current_window()` in `rate_limit.py`**~~ — ✅ Resolved: `import time` now at module top.

- ~~**No `Retry-After` header on 429 responses**~~ — ✅ Resolved: 429 response now includes `Retry-After: <seconds>` header calculated from current window position.

- **Go worker uses standard `log` package** — No structured logging, no job_id correlation across log lines. `log/slog` (Go 1.21 stdlib) or `zerolog` would give structured output. *(Deferred per TODO.md — not blocking Phase 2)*

- **Rate limit tests mutate global `settings` object** — `settings.rate_limit_requests = 5` directly mutates the shared singleton. Works because tests run sequentially, but would cause race conditions with `-parallel`. *(Still open — minor)*

---

## Summary Table

| # | Issue | Severity | Effort | Status |
|---|-------|----------|--------|--------|
| 1 | No migration check at startup | Critical | Low | ✅ Resolved — `lifespan` runs `alembic upgrade head` |
| 2 | Worker cancellation protocol undefined | Critical | Medium | ✅ Resolved — ADR-001 §7 |
| 4 | Tests in production Docker image | Critical | Low | ✅ Resolved — multi-stage Dockerfile + `uv.lock` (also resolves #15) |
| 5 | CORS wildcard + credentials | Important | Low | ✅ Resolved — `settings.allowed_origins`, dynamic `allow_credentials` |
| 6 | Infrastructure singletons vs `app.state` | Important | Medium | ✅ Resolved — all clients on `app.state`, deps read via `Request` |
| 7 | No pagination on list endpoints | Important | Low | ✅ Resolved — PROGRESS.md 6d |
| 8 | `last_used_at` never updated | Important | Low | ✅ Resolved — `update(ApiKey).values(last_used_at=...)` in `verify_api_key` |
| 9 | Add `--reload` for `develop.watch` | Important | Low | ✅ Resolved — `--reload` in docker-compose `command:` |
| 10 | Worker contract undocumented | Design | Low | ✅ Resolved — ADR-001 |
| 10a | NATS stream never created in Docker Compose | Critical | Low | ✅ Resolved — PROGRESS.md 8a |
| 10b | API result consumer not yet tracked/planned | Critical | Medium | ✅ Resolved — PROGRESS.md 6g |
| 10c | MaxDeliver advisory — superseded by #19 | Design | Medium | Superseded |
| 10d | NATS stream retention diverges from ADR | Design | Low | ✅ Resolved — `--retention work` in docker-compose |
| 10e | MaxDeliver exhaustion leaves jobs stuck | Design | Medium | ⏳ Deferred to Phase 2 |
| 11 | Postgres ENUM migration pain | Design | Low | ⏳ Process advisory — use VARCHAR+CHECK for new Phase 2 fields |
| 12 | Health endpoint is shallow | Design | Low | ✅ Resolved — `GET /health/ready` added |
| 13 | structlog barely used | Design | Low | ✅ Resolved — logging in jobs, users, auth routes |
| 14 | No request correlation ID | Design | Medium | ✅ Resolved — `CorrelationIdMiddleware` |
| 15 | Non-reproducible Docker builds | Minor | Low | ✅ Resolved — combined with #4 |
| 16 | SSRF — no private IP blocklist on job URLs | **Critical** | Medium | ✅ Resolved — `_validate_no_ssrf()` with DNS resolution |
| 17 | Rate limiter INCR/EXPIRE race condition | **Critical** | Low | ✅ Resolved — Lua script (atomic INCR+EXPIRE) |
| 18 | Worker `NATS_MAX_DELIVER` config unused | Important | Low | ✅ Resolved — `Run(ctx, maxDeliver int)` |
| 19 | `publishResult` failure silently loses jobs | **Critical** | Low | ✅ Resolved — returns error, `NakWithDelay(30s)` |
| 20 | `result_consumer.py` uses `logging` not `structlog` | Important | Low | ✅ Resolved — `import structlog` |
| 21 | No response size limit in Go fetcher | Important | Low | ✅ Resolved — `io.LimitReader(resp.Body, maxBodySize)` 10MB |
| 22 | Worker push subscription has unbounded concurrency | Design | Medium | ⏳ Deferred to Phase 2 — pull consumer with fixed pool |
| 23 | Worker graceful shutdown ignores in-flight fetches | Design | Low | ✅ Resolved — `ctx` threaded into `handleMessage` → `processJob` → `Fetch` |
