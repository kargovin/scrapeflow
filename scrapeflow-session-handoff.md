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

- Full architecture: `@CLAUDE.md`
- Phase 2 spec: `@docs/phase2/phase2-engineering-spec-v3.md`
- Task breakdown: `@docs/project/PHASE2_BACKLOG.md`
- Progress tracker: `@docs/project/PROGRESS.md`

**Test command** (run from `./docker`):
```bash
docker compose exec api uv run pytest tests/ -v
```

**Apply migrations manually** (auto-run is disabled in dev):
```bash
docker compose exec api uv run alembic upgrade head
```

---

## Current state

- Branch: `develop`
- Steps 1–23 complete
- **130 API tests passing** (`docker compose exec api uv run pytest tests/ -v`)
- **28 playwright-worker tests passing** (`docker compose exec playwright-worker python -m pytest tests/ -v`)
- **27 llm-worker tests passing** (`docker compose exec llm-worker python -m pytest tests/ -v`)

---

## What was built in Steps 1–17

Steps 1–16: Phase 2 API — jobs schema, engine routing, LLM key management, webhook secret rotation, job run endpoints, scheduler fields, result consumer updates, etc.

**Step 17** — LLM key management routes (`api/app/routers/users.py`, `api/app/schemas/users.py`):
- `POST /users/llm-keys` — SSRF-validate base_url, Fernet-encrypt key, return masked key
- `GET /users/llm-keys` — list keys, no api_key field in response
- `DELETE /users/llm-keys/{id}` — ownership-checked hard delete
- Bug fix: `POST /jobs` now 404s on cross-tenant `llm_key_id`
- 12 new tests in `api/tests/test_llm_keys.py` (100 total)

---

## What was built in Step 18

**Step 18** — Python Playwright worker (`playwright-worker/`):

A new Docker service that subscribes to `scrapeflow.jobs.run.playwright` and renders JS-heavy pages with headless Chromium.

### Files created

| File | Purpose |
|------|---------|
| `playwright-worker/Dockerfile` | Base: `mcr.microsoft.com/playwright/python:v1.44.0-jammy`; pip install |
| `playwright-worker/pyproject.toml` | Pinned `playwright==1.44.0` (must match image version), nats-py, miniopy-async, markdownify, beautifulsoup4, pydantic-settings |
| `playwright-worker/worker/config.py` | Pydantic-settings env config (NATS/MinIO/concurrency) |
| `playwright-worker/worker/models.py` | `JobMessage`, `PlaywrightOptions`, `ResultMessage` Pydantic models |
| `playwright-worker/worker/formatter.py` | HTML → html/md/json (mirrors Go worker's `formatter.go`) |
| `playwright-worker/worker/storage.py` | Async MinIO dual-write: `latest/{job_id}.ext` + `history/{job_id}/{ts}.ext` |
| `playwright-worker/worker/main.py` | Startup, browser launch, pull consumer, semaphore-gated worker loop |

### Tests added (`playwright-worker/tests/` — 28 tests)

| File | Coverage |
|------|---------|
| `tests/test_formatter.py` | 6 tests — all `format_output` branches, script stripping, missing title |
| `tests/test_models.py` | 5 tests — `to_nats_bytes()` exclude_none contract, `PlaywrightOptions` defaults |
| `tests/test_storage.py` | 5 tests — dual-write call count, key structure, return path, Content-Type per ext |
| `tests/test_main.py` | 10 tests — malformed message, running/completed/failed publish order, ack on both paths, context.close in finally, block_images routing |

All tests use `AsyncMock` — no live NATS, MinIO, or browser. `upload` is patched via `worker.main.upload` (the name in the module under test, not `worker.storage.upload`).

### Files modified

| File | Change |
|------|--------|
| `docker/docker-compose.yml` | Added `playwright-worker` service with env vars |
| `playwright-worker/Dockerfile` | Added `COPY tests/ ./tests/` so tests bake into the image |
| `.pre-commit-config.yaml` | Ruff lint/format `files` pattern extended to `^(api\|playwright-worker)/` |

### Key implementation facts

- **Durable name**: `python-playwright-worker` (distinct from Go worker's `go-worker`)
- **Version pin**: `playwright==1.44.0` in pyproject.toml — must stay pinned to match the Docker image. Using `>=1.44.0` installs the latest (1.58.0) which looks for browser binaries at a different path and crashes.
- **nats-py callbacks** must be `async def` coroutine functions, not lambdas — `nats.errors.InvalidCallbackTypeError` otherwise
- **nats-py stream verify**: use `await js.stream_info(STREAM_NAME)` (not `find_stream`)
- **Concurrency**: `asyncio.Semaphore(playwright_max_workers)` gates fetches — only fetches as many messages as there are free slots to avoid spurious AckWait expiry
- **Ack on failure**: mirrors the Go worker — result event already told the API it failed, re-delivery won't help a down site

### Smoke test result

Published a test job directly to NATS, worker rendered `https://example.com` and dual-wrote:
- `latest/ec15820a-....html` (528 bytes)
- `history/ec15820a-.../1775983415.html` (528 bytes)

---

## What was built in Step 19

**Step 19** — Python LLM worker (`llm-worker/`):

A new Docker service that subscribes to `scrapeflow.jobs.llm`, fetches raw scrape content from MinIO, calls Anthropic or OpenAI with the user's own API key, and publishes structured JSON results.

### Files created

| File | Purpose |
|------|---------|
| `llm-worker/Dockerfile` | Base: `python:3.12-slim`; pip install; bakes in `worker/` and `tests/` |
| `llm-worker/pyproject.toml` | nats-py, miniopy-async, cryptography, anthropic, openai, pydantic-settings, structlog, pytest, pytest-asyncio |
| `llm-worker/worker/config.py` | Pydantic-settings; `env_file` anchored to repo-root `.env` via `Path(__file__).parent.parent.parent`; Fernet key validated at startup; defaults: `llm_max_workers=3`, `llm_request_timeout_seconds=60`, `llm_max_content_chars=50_000` |
| `llm-worker/worker/models.py` | `JobMessage` (job_id, run_id, raw_minio_path, provider, encrypted_api_key, base_url, model, output_schema); `ResultMessage` (same schema as other workers) |
| `llm-worker/worker/llm.py` | `_decrypt_key` (Fernet); `_call_anthropic` (tool-use forced output); `_call_openai_compatible` (json_schema response_format); `call_llm` public entry point (decrypt → truncate → dispatch) |
| `llm-worker/worker/storage.py` | Dual-write MinIO, always JSON — no `ext` param unlike playwright-worker |
| `llm-worker/worker/worker.py` | Per-job lifecycle: fetch content → call LLM → upload → publish result |
| `llm-worker/worker/main.py` | Startup, NATS/MinIO setup, pull consumer loop |

### Tests added (`llm-worker/tests/` — 27 tests)

| File | Coverage |
|------|---------|
| `tests/test_models.py` | 6 tests — exclude_none contract, running/completed/failed shapes, JobMessage parsing, optional base_url |
| `tests/test_storage.py` | 4 tests — dual-write call count, key structure, return path, Content-Type always application/json |
| `tests/test_llm.py` | 9 tests — decrypt success + wrong-key error, routing to anthropic/openai, content truncation, tool-use return shape, JSON parse, base_url passthrough |
| `tests/test_worker.py` | 8 tests — malformed ack+skip, running first with seq, completed with path, ack on success, failed on error, ack on failure, fetch_content path, call_llm args |

### Files modified

| File | Change |
|------|--------|
| `docker/docker-compose.yml` | `llm-worker` service confirmed (was scaffolded in Step 18 bootstrap) |
| `.env.example` | Added `LLM_REQUEST_TIMEOUT_SECONDS`, `LLM_MAX_CONTENT_CHARS`, `LLM_MAX_WORKERS` |
| `.pre-commit-config.yaml` | Ruff pattern extended to `^(api\|playwright-worker\|llm-worker)/` |

### Key implementation facts

- **Logic split**: `worker.py` owns per-job lifecycle; `main.py` owns startup/loop — different from playwright-worker which puts all logic in `main.py`. This makes `worker.py` independently testable.
- **Provider routing**: explicit `provider` field (`"anthropic"` | `"openai_compatible"`), not model-name prefix — handles custom vLLM endpoints with arbitrary model names
- **Anthropic structured output**: tool-use pattern (`tools=[{"name": "extract", "input_schema": schema}]` + `tool_choice`); returns `response.content[0].input` which is already a dict (no `json.loads`)
- **OpenAI structured output**: `response_format={"type": "json_schema", ...}`; returns `json.loads(response.choices[0].message.content)`
- **Clients constructed per-job**: api_key decrypted at call time, goes out of scope immediately after the call completes
- **httpx.Timeout**: applied at transport layer covering connect + read + write + pool; `read` timeout = `llm_request_timeout_seconds`
- **conftest.py env bootstrap**: `os.environ.setdefault("LLM_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())` at module level — must run before `worker.config` is imported and `Settings()` fires

---

## What was built in Steps 20–21

**Step 20** — Scheduler loop (`api/app/core/scheduler.py`):
- `scheduler_loop(db_factory, js)` — polls every 60s (sleep at top, no startup trigger)
- `_dispatch_due_jobs` — `SELECT ... FOR UPDATE SKIP LOCKED` for active cron jobs with `next_run_at <= now()`; commits per-job before NATS publish; advances `next_run_at` using `croniter(job.schedule_cron, job.next_run_at).get_next()` (base = stored value, not `now()`, to prevent drift)
- `_recover_stale_pending` — re-publishes `job_runs` with `status='pending'` and `created_at < now() - 10m`; no DB write (reuses existing run)
- Wired into `api/app/main.py` lifespan via `asyncio.create_task()`
- 4 new tests in `api/tests/test_scheduler.py`

**Step 21** — Webhook delivery loop (`api/app/core/webhook_loop.py`):
- `webhook_delivery_loop(db_factory, http_client, fernet)` — polls every 15s (sleep at top)
- `_attempt_delivery` — re-fetches delivery by ID in its own session (avoids holding connection across HTTP calls); HMAC-SHA256 over payload bytes; `X-ScrapeFlow-Signature: sha256=...`; 10s timeout
- `_apply_backoff` — `BACKOFF_SECONDS = [0, 30, 300, 1800, 7200]`; `min(attempts, len-1)` index cap; marks `exhausted` after `settings.webhook_max_attempts` (default 5)
- Shared `httpx.AsyncClient` + `Fernet` instance created in lifespan and passed to the task
- `webhook_max_attempts: int = 5` added to `api/app/settings.py`
- 5 new tests in `api/tests/test_webhook_delivery.py`

### Key implementation facts

- **Croniter base**: use `job.next_run_at` (not `now()`) as the croniter start time — prevents schedule drift by poll-cycle jitter
- **Per-job commit in scheduler**: committing inside the `for job in jobs` loop releases the `FOR UPDATE` lock per row; if NATS publish then fails, stale-pending recovery re-publishes it next cycle
- **Webhook session strategy**: query session closes before HTTP calls begin (up to 50 × 10s calls); each `_attempt_delivery` opens a fresh session and re-checks `status == "pending"` as a race guard
- **Test isolation**: stale-pending recovery tests scope assertions to fixture `run_id` rather than `call_count`, since session-scoped DB accumulates pending runs from other tests

---

## What was built in Step 22

**Step 22** — MaxDeliver advisory subscriber (`api/app/core/advisory.py`):

When NATS exhausts `MaxDeliver` retries for a message, it publishes an advisory containing only the `stream_seq` of the exhausted message. This subscriber bridges NATS's world back to the DB: it maps `stream_seq → job_run` (via `job_runs.nats_stream_seq`) and marks the run `failed`.

### Files created/modified

| File | Change |
|------|--------|
| `api/app/core/advisory.py` | NEW — `_handle_advisory` (testable inner fn) + `maxdeliver_advisory_subscriber` (subscriber loop) |
| `api/app/constants.py` | Added `NATS_ADVISORY_MAX_DELIVER_SUBJECT = "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SCRAPEFLOW.*"` |
| `api/app/main.py` | Wired `advisory_task` into lifespan startup and shutdown gather |
| `api/tests/test_advisory.py` | 3 tests: happy path, no matching run, malformed JSON |

### Key implementation facts

- **Core NATS, not JetStream**: advisories live on the NATS broker's internal system — `nats_client.subscribe()` is used, not `js.subscribe()`. `app.state.nats_client` is the correct handle.
- **`_handle_advisory` is top-level**: extracted as a named function (not a closure) so tests can call it directly without setting up the subscriber — mirrors `_handle_result` in `result_consumer.py`.
- **Status filter includes `processing`**: LLM jobs can exhaust retries on `scrapeflow.jobs.llm` while still in `processing` state — must be included alongside `pending` and `running`.
- **`await asyncio.Future()` hold-open pattern**: same as `result_consumer.py` — the subscriber is push-based (callback fires on message arrival), so the task just needs to stay alive without burning CPU. `CancelledError` breaks the future → `sub.unsubscribe()` → re-raise.

---

## What was built in Step 23

**Step 23** — Admin panel API routes (`api/app/routers/admin.py`):

8 routes under `/admin/*`, all gated on `Depends(get_current_admin_user)` (403 for non-admins).

### Files created/modified

| File | Change |
|------|--------|
| `api/app/schemas/admin.py` | NEW — `AdminUserResponse`, `AdminUserDetailResponse`, `AdminWebhookDeliveryResponse` |
| `api/app/routers/admin.py` | NEW — 8 routes + `_admin_jobs_with_latest_run_stmt` helper |
| `api/app/main.py` | Added `admin` import + `include_router(admin.router)` |
| `api/tests/test_admin.py` | NEW — 18 tests, 3 local fixtures |

### Routes implemented

| Method | Path | Behaviour |
|--------|------|-----------|
| `GET` | `/admin/users` | Paginated list; optional `email` partial filter (ilike) |
| `GET` | `/admin/users/{id}` | User detail + `job_counts: dict[str, int]` (sparse GROUP BY) |
| `DELETE` | `/admin/users/{id}` | Hard delete, 204; self-delete guard (400) |
| `GET` | `/admin/jobs` | All jobs across all users; optional `user_id`/`status`/`engine` filters |
| `GET` | `/admin/jobs/{id}` | Any job regardless of ownership |
| `DELETE` | `/admin/jobs/{id}` | Cancel active runs (default) or `?hard_delete=true` |
| `GET` | `/admin/webhooks/deliveries` | Paginated; optional `status` exact filter |
| `POST` | `/admin/webhooks/deliveries/{id}/retry` | Reset `attempts=0`, `status='pending'`, `next_attempt_at=now()` |

### Key implementation facts

- **`_admin_jobs_with_latest_run_stmt`**: adapts `_jobs_with_latest_run_stmt` from `jobs.py` — same LATERAL join, but `user_id` is optional and adds `status`/`engine` filters. Lives in `admin.py` (not refactored into shared util) to avoid risking the existing 112 tests.
- **LATERAL join is INNER**: jobs with zero runs are excluded — consistent with user-facing `GET /jobs`.
- **`job_counts` is a sparse dict**: only statuses with ≥1 run appear. Callers should use `.get("pending", 0)`.
- **Self-delete guard**: `DELETE /admin/users/{id}` returns 400 if `user_id == admin.id` (lockout prevention).
- **`model_validate` on list returns**: `admin_list_users` and `admin_list_webhook_deliveries` use explicit `model_validate` in list comprehensions (not raw `.all()`) to satisfy Pylance's list invariance check.
- **18 tests**: 403 batch (all 8 routes in one test), happy path per route including cross-tenant assertions and DB verification after mutations.

---

## Next step

**Step 24**: Admin stats endpoint

From `docs/project/PHASE2_BACKLOG.md` Step 24:

**Files to implement:**
- EDIT `api/app/routers/admin.py` — add `GET /admin/stats` and `GET /admin/stats/users/{id}`

**`GET /admin/stats` response shape (§5.9):**
```json
{
  "operational": {
    "jobs_running": 12,
    "jobs_pending": 3,
    "jobs_by_engine": { "http": 9, "playwright": 3 },
    "webhook_deliveries_pending": 7,
    "webhook_deliveries_exhausted": 14,
    "active_recurring_jobs": 22,
    "next_scheduled_run_at": "2026-04-01T18:05:00Z"
  },
  "historical": {
    "jobs_today": 148,
    "jobs_this_week": 891,
    "jobs_this_month": 3204,
    "jobs_by_status_7d": { "completed": 820, "failed": 45, "cancelled": 26 },
    "jobs_by_engine_7d": { "http": 760, "playwright": 131 },
    "top_users_by_jobs": [
      { "user_id": "uuid", "email": "a@b.com", "job_count": 312 }
    ],
    "minio_storage_bytes": 4509715660,
    "webhook_delivery_success_rate_7d": 0.94
  }
}
```

**Key rules:**
- `minio_storage_bytes`: MinIO `bucket_size` API call, cached in Redis with 5-minute TTL (`scrapeflow:cache:minio_storage`)
- All historical stats query `job_runs.created_at`, not `jobs.created_at`
- `GET /admin/stats/users/{id}` — per-user breakdown of the same shape

**Spec ref:** §5.9
