You are an Engineer working on ScrapeFlow, a self-hosted multi-tenant web scraping platform (Apify clone). Read @CLAUDE.md for the full architecture.

We are building Phase 2 iteratively. Read @docs/phase2/phase2-engineering-spec-v3.md for the spec, @docs/project/PHASE2_BACKLOG.md for the task breakdown, and @docs/project/PROGRESS.md for current status.

## Completed this session

**Step 16** (commit TBD): New job routes

### What was built

**`GET /jobs/{id}/runs`** (`api/app/routers/jobs.py`)
- Ownership check → 404 for cross-tenant
- `select(JobRun)` with `.limit()/.offset()` — returns `list[JobResponse]`
- Returns `[]` for jobs with no runs

**`PATCH /jobs/{id}`** (`api/app/routers/jobs.py`)
- `model_dump(exclude_unset=True)` loop with `setattr` for simple fields
- Special cases: `schedule_cron` → validate + recalculate `next_run_at`; `webhook_url=None` → clear `webhook_secret`; `webhook_url` set → SSRF-validate + generate new Fernet secret; `llm_config` → 409 if active run is `processing`
- Returns `JobResponse` (includes `webhook_secret` when a new one is generated)

**`POST /jobs/{id}/webhook-secret/rotate`** (`api/app/routers/jobs.py`)
- Ownership check + webhook_url guard (422 if job has no webhook)
- Generates new Fernet secret, updates DB, returns `RotateWebhookSecretResponse`

### Schema changes (`api/app/schemas/jobs.py`)
- Added `_MutableJobFields` base class (shared mutable fields between `JobCreate` and `JobPatch`)
- `JobCreate` now inherits from `_MutableJobFields`
- `JobPatch` inherits from `_MutableJobFields`, adds `schedule_status` + `ConfigDict(extra="forbid")` (immutable fields rejected with 422)
- `webhook_secret: str | None = None` moved up to `JobResponse` (was only on `JobCreateResponse`)
- `JobCreateResponse` removed (now just `JobResponse`)
- Added `RotateWebhookSecretResponse(webhook_secret: str)`
- Fixed `uri_to_str` validator to handle `None`: `return str(v) if v is not None else None`
- Renamed `playwight_options` typo → `playwright_options` throughout

### Tests added
14 new tests covering all three endpoints (40 total, all passing).

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

- All 40 tests passing
- On branch `develop`
- Steps 1–16 complete

## Next step

**Step 17**: LLM key management routes

From `docs/project/PHASE2_BACKLOG.md` Step 17:

**Files:**
- Edit: `api/app/routers/users.py` — add three new handlers:
  - `POST /users/llm-keys` — Fernet-encrypt `api_key`, store, return masked key (`sk-***`)
  - `GET /users/llm-keys` — list keys for current user (no key in response)
  - `DELETE /users/llm-keys/{id}` — hard delete (ownership check)

**Key rules:**
- SSRF-validate `base_url` if provided (same pattern as webhook_url in jobs)
- Mask returned key: show only first 4 chars + `***` (e.g. `sk-t***`)
- Hard delete — referencing jobs will fail at dispatch with "LLM key not found"

**Spec ref:** §5.7

**Verify:** `pytest tests/ -v`
