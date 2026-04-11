You are an Engineer working on ScrapeFlow, a self-hosted multi-tenant web scraping platform (Apify clone). Read @CLAUDE.md for the full architecture.

We are building Phase 2 iteratively. Read @docs/phase2/phase2-engineering-spec-v3.md for the spec, @docs/project/PHASE2_BACKLOG.md for the task breakdown, and @docs/project/PROGRESS.md for current status.

## Completed this session

**Step 17** (commit TBD): LLM key management routes

### What was built

**Schema refactor** (`api/app/schemas/`)
- Created `api/app/schemas/users.py` — extracted `UserResponse`, `ApiKeyCreate`, `ApiKeyResponse`, `ApiKeyCreatedResponse` from the router into their own schema file (matching the pattern already used by `schemas/jobs.py`)
- Added `api/app/schemas/__init__.py` for consistency with other packages
- Added to `schemas/users.py`: `Providers` enum, `LLMKeyCreate`, `LLMKeyResponse`, `LLMKeyCreatedResponse`

**`POST /users/llm-keys`** (`api/app/routers/users.py`)
- SSRF-validate `base_url` if provided
- Fernet-encrypt `api_key` → stored as `encrypted_api_key` in `user_llm_keys`
- Returns masked key: `api_key[:4] + "*****"` (e.g. `sk-a*****`)
- 201 response

**`GET /users/llm-keys`** (`api/app/routers/users.py`)
- List all keys for current user, ordered newest first
- Response: `list[LLMKeyResponse]` — no `api_key` field at all

**`DELETE /users/llm-keys/{id}`** (`api/app/routers/users.py`)
- Ownership check → 404 for cross-tenant or missing
- Hard delete via `db.execute(delete(...))` + `db.commit()`
- Returns `LLMKeyResponse` (the deleted object)

**Bug fix in `POST /jobs`** (`api/app/routers/jobs.py:106`)
- Added ownership check for `llm_key_id`: `user_llm_key.user_id != user.id` → 404
- Previously only checked `is None`

### Tests added (`api/tests/test_llm_keys.py`)
12 new tests (100 total, all passing):
- `test_create_llm_key` — 201, masked api_key
- `test_create_llm_key_with_base_url` — base_url stored and returned
- `test_create_llm_key_short_api_key` — < 8 chars → 422
- `test_create_llm_key_ssrf_base_url` — private address → 400
- `test_create_llm_key_unauthenticated` — 401
- `test_list_llm_keys` — list with no api_key field
- `test_list_llm_keys_empty` — []
- `test_list_llm_keys_isolation` — cross-tenant isolation
- `test_delete_llm_key` — 200, gone from DB
- `test_delete_llm_key_other_user` — 404
- `test_delete_llm_key_not_found` — 404
- `test_create_job_llm_key_other_user` — POST /jobs with another user's llm_key_id → 404

### Key facts

**Test command:** Run from `./docker` folder:
```bash
cd /home/karthik/Documents/Claude/scrapeflow/docker
docker compose exec api uv run pytest tests/ -v
```

**Alembic auto-migration is disabled in dev:**
`api/app/main.py` — the `alembic upgrade head` call in the lifespan is commented out (lines 31–37). Apply manually:
```bash
docker compose exec api uv run alembic upgrade head
```

## Current state

- All 100 tests passing
- On branch `develop`
- Steps 1–17 complete

## Next step

**Step 18**: Python Playwright worker (new service)

From `docs/project/PHASE2_BACKLOG.md` Step 18:

**Files:**
- New: `playwright-worker/` directory — Python service
  - Subscribes to `scrapeflow.jobs.run.playwright`
  - Uses Playwright (async) to render JS-heavy pages
  - Writes result to MinIO (same dual-write convention: `latest/` + `history/`)
  - Publishes result to `scrapeflow.jobs.result` (same schema as Go worker)
  - Config from env vars (NATS URL, MinIO endpoint/credentials)

**Key rules:**
- Same NATS message schema as Go worker (`job_id`, `run_id`, `url`, `output_format`, `playwright_options`)
- `playwright_options`: `wait_strategy` (load/domcontentloaded/networkidle), `timeout_seconds`, `block_images`
- Must ack NATS only after successful MinIO write (same durability guarantee as Go worker)
- Dockerfile: Python base image with Playwright + browser install

**Spec ref:** §4.2

**Verify:** Docker Compose build + smoke test via `POST /jobs` with `"engine": "playwright"`
