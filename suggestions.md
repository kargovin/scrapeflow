# ScrapeFlow — Architect's Review

> Reviewed by: architect analysis pass
> Codebase state: Phase 1 MVP in progress (steps 1–5 complete)
> Date: 2026-03-25

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

---

## Overall Assessment

The foundation is solid. The technology choices are appropriate for the problem, the code is clean and readable, and the separation of concerns is sensible for a project at this stage. The patterns established early (async SQLAlchemy, structlog, dependency injection, Clerk for auth) are production-grade choices that will scale without requiring re-architecture.

The main gaps are not quality issues — they are the expected incompleteness of an MVP in flight. The notes below are ranked by impact and flag the things that will bite hardest if not addressed before the project grows.

---

## Critical — Address Before Phase 2

### 1. No migration safety net at startup

The API starts and immediately serves traffic with no guarantee that migrations have been applied. In Docker Compose this is handled by `depends_on: condition: service_healthy` for the DB, but Postgres being *ready* is not the same as the schema being *current*.

A single scrape job written to a table that doesn't yet have a column (e.g., `error` column added in a future migration) will corrupt data silently or throw a 500 with no clear cause.

**Recommendation:** Add a startup check in `lifespan` that runs `alembic upgrade head` (or at minimum checks `alembic current` == `alembic head`) before the app begins accepting requests. For k8s, this becomes an init container.

---

### 2. ~~Worker cancellation protocol undefined~~ — ✅ Resolved by ADR-001

~~The job model has a `cancelled` status and a NATS queue. When a job is `pending` or `running`, cancelling it means the worker may still be processing it — setting `status = cancelled` in Postgres alone does not stop the worker. The worker needs to either check job status before writing results (poll-before-write), or receive a cancellation signal via NATS. Without this, a cancelled job can still have its result written to MinIO and its status flipped back to `completed` by the worker, silently overwriting the cancellation.~~

ADR-001 §7 defines the cancellation protocol: `DELETE /jobs/{id}` sets `status = cancelled` in Postgres only. The API result consumer on `scrapeflow.jobs.result` discards the result if `status = cancelled`. The worker wastes one scrape; correctness is preserved. Decision is documented and explicit.

---

### 4. Tests ship inside the production Docker image

`COPY tests/ tests/` in the Dockerfile puts all test code, fixtures, and test-only dependencies (pytest, httpx) into the production image. This increases image size and attack surface, and `.[dev]` installs dev dependencies in production.

**Recommendation:** Use a multi-stage build. Stage 1 installs only production deps and copies only `app/`, `migrations/`, `alembic.ini`. Stage 2 builds on Stage 1, adds test deps and `tests/`. The production target is Stage 1.

```dockerfile
FROM python:3.12-slim AS base
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml .
RUN uv pip install --system --no-cache .
COPY app/ app/
COPY migrations/ migrations/
COPY alembic.ini .

FROM base AS test
RUN uv pip install --system --no-cache .[dev]
COPY tests/ tests/

FROM base AS production
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## Important — Address Before Production

### 5. CORS is wide open with credentials enabled

```python
allow_origins=["*"],
allow_credentials=True,
```

This combination is rejected by browsers (they won't send cookies/credentials to a wildcard origin), and it's explicitly wrong for production. The comment in the code acknowledges this, which is good — but it needs to actually be fixed before the k8s deployment.

**Recommendation:** Add `ALLOWED_ORIGINS` to `Settings` now, defaulting to `["*"]` for local dev. The TODO comment is a debt item that will be forgotten.

---

### 6. Global mutable singletons for all infrastructure clients

`redis.py`, `minio.py`, and `nats.py` all use module-level globals (`_pool`, `_client`, `_nc`). This works fine for a single-process deployment but makes testing fragile — tests share the same singleton, so test isolation depends on test ordering and cleanup.

The current test suite works because `conftest.py` initializes once per session. But this means you cannot run individual test files in isolation without the session fixture, and you cannot easily inject a mock/test client.

**Recommendation:** Store infrastructure clients on `app.state` in the FastAPI lifespan. FastAPI's `request.app.state` is the idiomatic place for this. Dependencies can then read from `request.app.state` instead of module globals. This makes testing trivially easy — just set `app.state.redis = FakeRedis()` in the test fixture.

```python
# In lifespan:
app.state.redis_pool = create_pool()
app.state.minio = await minio.create_client()

# In dependency:
def get_redis(request: Request) -> redis.Redis:
    return redis.Redis(connection_pool=request.app.state.redis_pool)
```

---

### 7. ~~No pagination on `GET /jobs`~~ — ✅ Resolved, tracked in PROGRESS.md 6d

~~When the jobs router is built, listing all jobs for a user with `SELECT * FROM jobs WHERE user_id = ?` will return unbounded results. A user with 10,000 jobs will cause a large memory allocation on every list request.~~

~~**Recommendation:** Add `limit` and `offset` (or cursor-based) pagination to `GET /jobs` from day one. It is significantly harder to add pagination to an API that clients are already consuming than to include it upfront. Default `limit=50`, max `limit=200`.~~

---

### 8. API key `last_used_at` is never updated

The `ApiKey` model has `last_used_at` but `verify_api_key()` never sets it. This field will always be `NULL`.

**Recommendation:** Update `last_used_at` in `verify_api_key()` after a successful lookup. Be aware this is a write on every authenticated request — use a fire-and-forget update (don't block the request) or accept the minor overhead. For MVP, a simple `await db.execute(update(ApiKey)...)` after auth is fine.

---

### 9. Add `--reload` to complete the `develop.watch` setup

`docker-compose.yml` already has a `develop.watch` block with `action: sync` on `app/` — this syncs file changes into the running container without a full image rebuild. However, uvicorn is not started with `--reload`, so synced files are never picked up until the container restarts.

Adding `--reload` completes the loop: file saved → synced into container → uvicorn reloads. This is especially useful when using an AI coding tool (Claude Code) that makes rapid successive edits — each change is picked up automatically with no rebuild. A full rebuild is only triggered when `pyproject.toml` changes (handled by `action: rebuild`).

**Recommendation:** Add `--reload` to the uvicorn command for development, either via a compose `command:` override on the `api` service or an entrypoint script that switches on `APP_ENV`.

---

## Design Observations — Worth Discussing

### 10. ~~Worker contract undocumented~~ — ✅ Resolved by ADR-001

~~The Go worker is a placeholder, but the contract between API and worker is not documented anywhere. Key questions that will shape both sides: what is the NATS subject/stream name and message schema? Who creates the JetStream stream? How does the worker report progress? Does the worker write directly to Postgres, or does it publish a result event that the API consumes? The API and worker are separate processes written in different languages — the contract is the interface, and it needs to be explicit.~~

ADR-001 covers stream lifecycle, subjects, message schemas, worker responsibilities, ack timing, retry policy, cancellation, and MinIO path convention. Well-structured and explicit.

---

### 10a. ~~NATS stream is never created in Docker Compose (gap from ADR-001)~~ — ✅ Resolved, tracked in PROGRESS.md 8a

~~ADR-001 §1 correctly delegates stream creation to infra — neither the API nor the worker creates it. But the Docker Compose has no service or init step that creates the `SCRAPEFLOW` stream. Until this exists, the API will hard-crash at startup with a missing stream error (which is the right behavior per the ADR, but means the stack is currently non-functional for job dispatch).~~

~~**Recommendation:** Add a one-shot init container to `docker-compose.yml` using the `nats` CLI (available in the `natsio/nats-box` image) that creates the stream on startup. For k3s, this becomes a k8s Job in the `scrapeflow` namespace that runs before the API deployment.~~

**Docker Compose has no init containers — that is a Kubernetes concept.** The correct Docker Compose equivalent is a one-shot service with `restart: "no"` that exits after doing its work, and a `condition: service_completed_successfully` dependency on it. Available since Docker Compose v2.

```yaml
nats-init:
  image: natsio/nats-box:latest
  depends_on:
    nats:
      condition: service_healthy
  restart: "no"
  command: >
    nats stream add SCRAPEFLOW
      --server nats://nats:4222
      --subjects "scrapeflow.jobs.run,scrapeflow.jobs.result"
      --retention work
      --max-deliver 3
      --defaults

api:
  depends_on:
    nats-init:
      condition: service_completed_successfully
    # ... other deps
```

The API won't start until `nats-init` exits with code 0 — guaranteeing the stream exists. For k3s, this becomes a k8s `Job` resource that runs before the API `Deployment`.

---

### 10b. ~~The API result consumer doesn't exist yet~~ — ✅ Resolved, tracked in PROGRESS.md 6g

~~ADR-001 §4 assigns "update job status in Postgres" and "enforce cancellation" to the API result consumer — a subscriber to `scrapeflow.jobs.result`. This is a significant piece of backend logic that has no implementation yet and isn't tracked in PROGRESS.md.~~

~~This consumer needs to run as a long-lived background task inside the API process (started in `lifespan`), subscribe to `scrapeflow.jobs.result` via JetStream push or pull consumer, and handle: `completed` → update `status`, `result_path`; `failed` → update `status`, `error`; `cancelled` → discard result.~~

~~**Recommendation:** Add step 8b to PROGRESS.md: "API result consumer — subscribe to `scrapeflow.jobs.result`, update job status in Postgres." It should be implemented alongside the worker (step 8), not after, since neither is useful without the other.~~

---

### 10c. MaxDeliver advisory detection is non-trivial

ADR-001 §6 says "The API result consumer is responsible for detecting [MaxDeliver exhaustion] via the `MaxDeliver` advisory." This is correct but easy to underestimate. NATS publishes exhaustion advisories to `$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SCRAPEFLOW.*` — the API needs a separate subscription to this subject, parse the advisory payload, extract the `job_id` from the original message metadata, and mark the job as `failed`.

**Recommendation:** Add this as an explicit implementation note in PROGRESS.md step 8 so it doesn't get missed. The alternative (simpler) approach: set a NATS `MaxDeliver` and also set `AckWait` with a `NoAck` policy to a dead-letter subject, then subscribe to the dead-letter subject instead of the advisory.

---

### 11. SQLAlchemy Enum types will cause migration pain

Both `JobStatus` and `OutputFormat` are stored as native Postgres `ENUM` types (as shown in the migration). Adding a new status (e.g., `paused`, `retrying`) requires an `ALTER TYPE ... ADD VALUE` migration. In Postgres, this cannot be done inside a transaction, which means Alembic's default transactional migration behavior will fail.

**Recommendation:** Consider storing enums as `VARCHAR` with a check constraint instead of native Postgres ENUMs. It trades some DB-level enforcement for significantly easier migrations. Alternatively, be aware of this limitation and plan migrations for enum changes carefully.

---

### 12. Health endpoint doesn't check dependencies

`GET /health` returns `{"status": "ok"}` unconditionally. If Postgres is down, Redis is OOM, or NATS is disconnected, the health endpoint still returns 200. Kubernetes liveness/readiness probes will think the pod is healthy when it is not.

**Recommendation:** Add a `/health/ready` endpoint that checks each dependency (DB query, Redis ping, NATS connection state). The existing `/health` can stay as a liveness probe (process is alive). `/health/ready` becomes the readiness probe (process can serve traffic).

---

### 13. `structlog` is imported but barely used

`structlog` is in the dependency list and used in `main.py` for startup logs. None of the other modules use it — auth, models, and routers use no logging at all. This means request-level events (auth failures, job creation, errors) are invisible in production.

**Recommendation:** Add structured logging at the key decision points: auth failures (with reason), job creation (with user_id, job_id), and any exception paths. This is especially important for a multi-tenant system where debugging issues per-user requires correlated logs.

---

### 14. No request ID / correlation ID

In a multi-tenant async system, debugging requires correlating logs across a request's lifecycle. Currently there is no trace/request ID attached to logs.

**Recommendation:** Add a middleware that generates a `request_id` (UUID) per request, adds it to `request.state`, and uses `structlog.contextvars.bind_contextvars(request_id=...)` to attach it to all logs within that request. This is a 20-line middleware and pays dividends immediately in production debugging.

---

### 15. `uv pip install --system --no-cache` in Dockerfile is not reproducible

The Dockerfile uses `uv pip install` with version ranges from `pyproject.toml` (e.g., `fastapi>=0.115.0`). This means two builds a month apart may install different versions. There is no `uv.lock` file committed.

**Recommendation:** Run `uv lock` to generate a `uv.lock` file, commit it, and use `uv sync --frozen` in the Dockerfile. This gives reproducible builds and prevents surprise upgrades breaking the container image.

---

## Minor / Low Priority

- **`health.py` version is hardcoded** to `"0.1.0"`. It will drift from `pyproject.toml`. Read it from the package metadata at startup and store it on `app.state`.

- **`alembic.ini` `sqlalchemy.url`** likely has the localhost default which is wrong inside Docker. The `env.py` overrides it from `settings.database_url`, so it works — but the `.ini` value is a footgun for anyone running `alembic` directly outside the container.

- **No `.dockerignore`** — the Docker build context includes `.venv/`, test outputs, and `__pycache__`. This slows builds and bloats the build context sent to the daemon. Add a `.dockerignore` at the `api/` level.

- **`assert` statements for null-checks in production code** (`redis.py`, `minio.py`, `nats.py`). Python can be run with `-O` (optimize) which strips asserts. Use `if _pool is None: raise RuntimeError(...)` instead.

- **No `__all__` exports in `app/models/__init__.py`** — the `import app.models` in `migrations/env.py` relies on `__init__.py` importing the submodules. If someone adds a new model file and forgets to add it to `__init__.py`, Alembic won't detect it. Add a comment warning about this.

---

## Summary Table

| # | Issue | Severity | Effort | Status |
|---|-------|----------|--------|--------|
| 1 | No migration check at startup | Critical | Low | Open |
| 2 | Worker cancellation protocol undefined | Critical | Medium | ✅ Resolved — ADR-001 §7 |
| 4 | Tests in production Docker image | Critical | Low | Open |
| 5 | CORS wildcard + credentials | Important | Low | Open |
| 6 | Infrastructure singletons vs `app.state` | Important | Medium | Open |
| 7 | No pagination on list endpoints | Important | Low | ✅ Resolved — PROGRESS.md 6d |
| 8 | `last_used_at` never updated | Important | Low | Open |
| 9 | Add `--reload` for `develop.watch` | Important | Low | Open |
| 10 | Worker contract undocumented | Design | Low | ✅ Resolved — ADR-001 |
| 10a | NATS stream never created in Docker Compose | Critical | Low | ✅ Resolved — PROGRESS.md 8a |
| 10b | API result consumer not yet tracked/planned | Critical | Medium | ✅ Resolved — PROGRESS.md 6g |
| 10c | MaxDeliver advisory detection is non-trivial | Design | Medium | Open |
| 11 | Postgres ENUM migration pain | Design | Low | Open |
| 12 | Health endpoint is shallow | Design | Low | Open |
| 13 | structlog barely used | Design | Low | Open |
| 14 | No request correlation ID | Design | Medium | Open |
| 15 | Non-reproducible Docker builds | Minor | Low | Open |
