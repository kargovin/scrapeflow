# ScrapeFlow Phase 2 — Engineering Specification

> **Status:** Approved for implementation
> **Date:** 2026-04-01
> **Audience:** Software architects and engineers
> **Prerequisites:** Phase 1 complete (all 18 cleanup items done), ADR-001 accepted

---

## 1. Context & Pre-Conditions

Phase 1 delivered: authenticated job CRUD, Go HTTP scraper worker, NATS JetStream pipeline, MinIO storage, Redis rate limiting, Clerk auth (JWT + API keys). The codebase is clean — all pre-Phase-2 cleanup items are complete.

Phase 2 adds five features on top of this foundation:

| Feature | Summary |
|---------|---------|
| Playwright worker | Opt-in headless browser rendering for JS-heavy/SPA sites |
| LLM processing | BYOK structured data extraction using user-provided API keys |
| Change detection | Recurring/scheduled jobs with diff-based change notification |
| Webhook delivery | Push notifications on job events with exponential backoff retry |
| Admin panel API | Cross-tenant management, usage stats, operational visibility |

**Before any Phase 2 code is written:**
- Write ADR-002 (this document's §3) and get it accepted
- Apply the NATS stream migration (§3.1) to all environments
- Run the Alembic migrations in order (§2)

---

## 2. Database Schema Changes

All changes are Alembic migrations. Run in the order listed. Never combine multiple migrations into one — each step is independently rollbackable.

### 2.1 Migration: Add `is_admin` to `users`

```sql
ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT false;
```

No data migration needed. All existing users default to non-admin.

---

### 2.2 Migration: Restructure `jobs` — add Phase 2 fields

```sql
-- New columns on jobs
ALTER TABLE jobs ADD COLUMN engine VARCHAR(20) NOT NULL DEFAULT 'http';
ALTER TABLE jobs ADD CONSTRAINT jobs_engine_check CHECK (engine IN ('http', 'playwright'));

ALTER TABLE jobs ADD COLUMN schedule_cron VARCHAR(100) NULL;
ALTER TABLE jobs ADD COLUMN schedule_status VARCHAR(20) NULL;
ALTER TABLE jobs ADD CONSTRAINT jobs_schedule_status_check
    CHECK (schedule_status IN ('active', 'paused'));

ALTER TABLE jobs ADD COLUMN next_run_at TIMESTAMPTZ NULL;
ALTER TABLE jobs ADD COLUMN last_run_at TIMESTAMPTZ NULL;

ALTER TABLE jobs ADD COLUMN webhook_url TEXT NULL;
ALTER TABLE jobs ADD COLUMN webhook_secret TEXT NULL;     -- Fernet-encrypted

ALTER TABLE jobs ADD COLUMN llm_config JSONB NULL;
ALTER TABLE jobs ADD COLUMN playwright_options JSONB NULL;

-- Index for scheduler polling loop
CREATE INDEX idx_jobs_next_run_at
    ON jobs (next_run_at)
    WHERE schedule_cron IS NOT NULL AND schedule_status = 'active';
```

**Note on `status`, `result_path`, `error`:** These columns are removed in migration 2.4 after `job_runs` is populated. Do not remove them in this migration.

---

### 2.3 Migration: Create `job_runs` table

```sql
CREATE TABLE job_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL,
    result_path     TEXT NULL,
    diff_detected   BOOLEAN NULL,
    diff_summary    JSONB NULL,
    error           TEXT NULL,
    started_at      TIMESTAMPTZ NULL,
    completed_at    TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_job_runs_job_id ON job_runs (job_id);
CREATE INDEX idx_job_runs_status ON job_runs (status);
CREATE INDEX idx_job_runs_created_at ON job_runs (created_at);

-- For retention cleanup (nightly CronJob)
CREATE INDEX idx_job_runs_created_at_brin ON job_runs USING BRIN (created_at);
```

**Data migration — copy existing job outcomes into job_runs:**
```sql
INSERT INTO job_runs (id, job_id, status, result_path, error, completed_at, created_at)
SELECT
    gen_random_uuid(),
    id,
    status,
    result_path,
    error,
    updated_at,
    created_at
FROM jobs
WHERE status != 'pending';
```

---

### 2.4 Migration: Remove run-state columns from `jobs`

After verifying job_runs is populated correctly:

```sql
ALTER TABLE jobs DROP COLUMN status;
ALTER TABLE jobs DROP COLUMN result_path;
ALTER TABLE jobs DROP COLUMN error;
```

**Warning:** This migration is irreversible without restoring from backup. Verify job_runs data before running.

---

### 2.5 Migration: Create `user_llm_keys` table

```sql
CREATE TABLE user_llm_keys (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name              VARCHAR(100) NOT NULL,
    provider          VARCHAR(20) NOT NULL,
    encrypted_api_key TEXT NOT NULL,               -- Fernet-encrypted
    base_url          TEXT NULL,                   -- for openai_compatible providers
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_user_llm_keys_user_id ON user_llm_keys (user_id);
```

---

### 2.6 Migration: Add `processing` status (ALTER TYPE)

**Critical:** This migration cannot run inside a transaction. Use `connection.execute()` directly, not within `op.execute()` inside a transaction block.

```sql
-- Must run outside a transaction:
ALTER TYPE jobstatus ADD VALUE 'processing' AFTER 'running';
```

In `env.py`, set `transaction_per_migration = False` for this migration only, or run it manually before the Alembic run.

---

### 2.7 Migration: Create `webhook_deliveries` table

```sql
CREATE TABLE webhook_deliveries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    run_id          UUID NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
    webhook_url     TEXT NOT NULL,                 -- snapshot at event time
    payload         JSONB NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT NULL,
    delivered_at    TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_webhook_deliveries_status_next
    ON webhook_deliveries (next_attempt_at)
    WHERE status = 'pending';

CREATE INDEX idx_webhook_deliveries_job_id ON webhook_deliveries (job_id);
```

---

### Complete Schema After Phase 2

```
users               — id, clerk_id, email, is_admin, created_at
api_keys            — id, user_id, key_hash, name, revoked, last_used_at, created_at
user_llm_keys       — id, user_id, name, provider, encrypted_api_key, base_url, created_at
jobs                — id, user_id, url, engine, output_format,
                      schedule_cron, schedule_status, next_run_at, last_run_at,
                      webhook_url, webhook_secret, llm_config, playwright_options,
                      created_at, updated_at
job_runs            — id, job_id, status, result_path, diff_detected, diff_summary,
                      error, started_at, completed_at, created_at
webhook_deliveries  — id, job_id, run_id, webhook_url, payload, status, attempts,
                      next_attempt_at, last_error, delivered_at, created_at
```

---

## 3. ADR-002: Phase 2 Worker Contract

**Status:** Accepted
**Supersedes:** ADR-001 (subjects and message schemas only — ack timing, retry policy, cancellation principles unchanged)

### 3.1 NATS Stream Change

**Before (Phase 1):**
```
Stream: SCRAPEFLOW
Subjects: scrapeflow.jobs.run, scrapeflow.jobs.result
Retention: WorkQueuePolicy
```

**After (Phase 2):**
```
Stream: SCRAPEFLOW
Subjects: scrapeflow.jobs.>          ← wildcard replaces explicit list
Retention: WorkQueuePolicy
```

The `>` wildcard matches all subjects with one or more tokens after `scrapeflow.jobs.`. This covers all current and future subjects without stream reconfiguration.

**Migration:** `docker compose down -v` is required to recreate the NATS volume with the new stream config. Update `docker/docker-compose.yml` `nats-init` command accordingly.

---

### 3.2 Updated Subjects

| Subject | Publisher | Consumer | Purpose |
|---------|-----------|----------|---------|
| `scrapeflow.jobs.run.http` | API | Go HTTP worker | HTTP scrape jobs |
| `scrapeflow.jobs.run.playwright` | API | Python Playwright worker | JS-rendered scrape jobs |
| `scrapeflow.jobs.llm` | API result consumer | Python LLM worker | LLM extraction (conditional) |
| `scrapeflow.jobs.result` | All workers | API result consumer | Job outcomes (unchanged) |

**`constants.py` changes:**
```python
# Replace NATS_JOBS_RUN_SUBJECT with:
NATS_JOBS_RUN_HTTP_SUBJECT       = "scrapeflow.jobs.run.http"
NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT = "scrapeflow.jobs.run.playwright"
NATS_JOBS_LLM_SUBJECT            = "scrapeflow.jobs.llm"
NATS_JOBS_RESULT_SUBJECT         = "scrapeflow.jobs.result"   # unchanged
```

---

### 3.3 Updated Message Schemas

**Job dispatch — HTTP / Playwright (API → worker):**
```json
{
  "job_id": "uuid",
  "run_id": "uuid",
  "url": "https://example.com",
  "output_format": "html|markdown|json",
  "playwright_options": {
    "wait_strategy": "load",
    "timeout_seconds": 60,
    "block_images": false
  }
}
```
`playwright_options` is present only in Playwright messages. HTTP worker ignores any extra fields.
`run_id` is new — result consumer uses it to target the exact `job_runs` row.

**LLM dispatch (API result consumer → LLM worker):**
```json
{
  "job_id": "uuid",
  "run_id": "uuid",
  "raw_minio_path": "scrapeflow-results/latest/uuid.html",
  "provider": "openai_compatible|anthropic",
  "encrypted_api_key": "<fernet-ciphertext>",
  "base_url": "https://vllm.example.com/v1",
  "model": "Qwen/Qwen2.5-72b-Instruct",
  "output_schema": { ... }
}
```
`encrypted_api_key` is the Fernet ciphertext from `user_llm_keys.encrypted_api_key`. LLM worker decrypts with `LLM_KEY_ENCRYPTION_KEY`.

**Job result (all workers → API):**
```json
{
  "job_id": "uuid",
  "run_id": "uuid",
  "status": "running|completed|failed",
  "minio_path": "scrapeflow-results/latest/uuid.json",
  "error": "optional, only when status=failed"
}
```
`run_id` is new — required for result consumer to update the correct row.

---

## 4. New Services

### 4.1 Go HTTP Worker — Changes Only

The Go HTTP worker requires minimal changes for Phase 2:

- **Subscription subject:** `scrapeflow.jobs.run` → `scrapeflow.jobs.run.http`
- **Message schema:** Accept `run_id` field, include in all published results
- **MinIO path:** Write to both `latest/{job_id}.{ext}` AND `history/{job_id}/{timestamp}.{ext}`
- **Structured logging:** Replace `log.Printf` with `slog.Info/Error` (key-value pairs)
- **Pull consumer:** Switch from push subscription to pull consumer with configurable worker pool

**Pull consumer pattern (Go):**
```go
sub, err := js.PullSubscribe(
    NATS_JOBS_RUN_HTTP_SUBJECT,
    "go-http-worker",
    nats.MaxDeliver(cfg.NATSMaxDeliver),
)

// Worker pool — semaphore pattern
sem := make(chan struct{}, cfg.WorkerPoolSize)  // default: runtime.NumCPU()

for {
    msgs, err := sub.Fetch(cfg.WorkerPoolSize, nats.MaxWait(5*time.Second))
    for _, msg := range msgs {
        sem <- struct{}{}
        go func(m *nats.Msg) {
            defer func() { <-sem }()
            w.handleMessage(ctx, m)
        }(msg)
    }
}
```

---

### 4.2 Python Playwright Worker — New Service

**Location:** `playwright-worker/`
**Base image:** `mcr.microsoft.com/playwright/python:v1.44.0-jammy`

**Startup sequence:**
1. Load config from env vars
2. Connect to NATS, verify SCRAPEFLOW stream exists
3. Connect to MinIO, verify bucket exists
4. Launch Chromium browser (`playwright.chromium.launch(headless=True)`)
5. Create pull consumer on `scrapeflow.jobs.run.playwright`
6. Run worker loop (concurrency: `PLAYWRIGHT_MAX_WORKERS`, default 3)

**Per-job lifecycle:**
```python
async def process_job(msg, job):
    # 1. Publish running status
    await publish_result({"job_id": job.job_id, "run_id": job.run_id, "status": "running"})

    # 2. Create isolated browser context
    context = await browser.new_context()
    page = await context.new_page()

    try:
        # 3. Optional resource blocking
        if job.playwright_options.block_images:
            await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}",
                           lambda r: r.abort())

        # 4. Navigate
        await page.goto(job.url, timeout=job.playwright_options.timeout_seconds * 1000)
        await page.wait_for_load_state(job.playwright_options.wait_strategy)

        # 5. Extract rendered HTML
        html = await page.content()
        final_url = page.url

        # 6. Format + upload (same as Go worker)
        result = format(html, job.output_format, final_url)
        minio_path = await upload(job.job_id, result)

        # 7. Publish completed
        await publish_result({
            "job_id": job.job_id, "run_id": job.run_id,
            "status": "completed", "minio_path": minio_path
        })
        await msg.ack()

    except Exception as e:
        await publish_result({
            "job_id": job.job_id, "run_id": job.run_id,
            "status": "failed", "error": str(e)
        })
        await msg.ack()

    finally:
        await context.close()   # always discard session state
```

---

### 4.3 Python LLM Worker — New Service

**Location:** `llm-worker/`
**Base image:** `python:3.12-slim`
**Dependencies:** `openai`, `anthropic`, `nats-py`, `miniopy-async`, `cryptography`, `structlog`

**Startup sequence:**
1. Load config (requires `LLM_KEY_ENCRYPTION_KEY`)
2. Connect to NATS, MinIO
3. Pull subscribe to `scrapeflow.jobs.llm` (durable: `python-llm-worker`)

**Per-job lifecycle:**
```python
async def process_job(msg, job):
    # 1. Decrypt the LLM API key
    fernet = Fernet(settings.llm_key_encryption_key)
    api_key = fernet.decrypt(job.encrypted_api_key.encode()).decode()

    # 2. Fetch raw content from MinIO
    raw_bytes = await minio.get_object(BUCKET, job.raw_minio_path)
    content = raw_bytes.decode("utf-8", errors="replace")

    # 3. Call LLM
    if job.provider == "anthropic":
        result = await call_anthropic(api_key, content, job.model, job.output_schema)
    else:
        result = await call_openai_compatible(
            api_key, job.base_url, content, job.model, job.output_schema
        )

    # 4. Upload structured result to MinIO
    minio_path = await upload_json(job.job_id, result)

    # 5. Publish final completed result
    await publish_result({
        "job_id": job.job_id, "run_id": job.run_id,
        "status": "completed", "minio_path": minio_path
    })
    await msg.ack()
    # api_key goes out of scope here — not persisted anywhere
```

**LLM call — openai_compatible (covers OpenAI, vLLM, Groq, Together, Ollama):**
```python
client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
response = await client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": f"Extract data from:\n\n{content}"}],
    response_format={
        "type": "json_schema",
        "json_schema": {"name": "extraction", "schema": output_schema}
    }
)
return json.loads(response.choices[0].message.content)
```

---

## 5. API Changes

### 5.1 Pre-Phase-2 Refactor

Before adding new routes, move `_validate_no_ssrf()` from `api/app/routers/jobs.py` to `api/app/core/security.py`. It is reused by:
- `POST /jobs` (job URL)
- `POST /jobs` (webhook_url)
- `POST /users/llm-keys` (base_url)

---

### 5.2 New Dependency: `get_current_admin_user`

```python
# api/app/auth/dependencies.py
async def get_current_admin_user(
    user: User = Depends(get_current_user),
) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
```

---

### 5.3 Modified: `POST /jobs`

**New request fields:**
```python
class JobCreate(BaseModel):
    url: HttpUrl
    output_format: OutputFormat = OutputFormat.html
    engine: Engine = Engine.http                   # new
    playwright_options: PlaywrightOptions | None = None  # new
    schedule_cron: str | None = None               # new
    webhook_url: HttpUrl | None = None             # new
    llm_config: LLMJobConfig | None = None         # new
    # llm_config = {llm_key_id, model, output_schema}
```

**New validations at creation:**
1. SSRF check on `url` (existing)
2. SSRF check on `webhook_url` (if provided)
3. SSRF check on LLM key's `base_url` (resolved from `llm_key_id`)
4. Validate `schedule_cron` with `croniter`
5. Enforce `SCHEDULE_MIN_INTERVAL_MINUTES` — return 422 if too frequent
6. Validate `llm_key_id` exists and belongs to user — return 404 if not found

**New creation flow:**
```
1. Create jobs row
2. Create job_runs row (status=pending)
3. Publish to NATS with {job_id, run_id, url, output_format, engine, playwright_options}
4. If webhook_url set: generate webhook_secret, encrypt, store on jobs row
5. Return JobResponse with job_id + run_id + webhook_secret (shown once if generated)
```

---

### 5.4 Modified: `GET /jobs` and `GET /jobs/{id}`

Both endpoints JOIN with the latest `job_runs` row to return current status:

```sql
SELECT j.*, jr.status, jr.result_path, jr.diff_detected, jr.error, jr.completed_at
FROM jobs j
LEFT JOIN LATERAL (
    SELECT * FROM job_runs
    WHERE job_id = j.id
    ORDER BY created_at DESC
    LIMIT 1
) jr ON true
WHERE j.user_id = :user_id
```

---

### 5.5 New Endpoints — Jobs

```
GET  /jobs/{id}/runs            — paginated run history for this job (user-scoped)
PATCH /jobs/{id}                — update schedule_status ('active'|'paused'), webhook_url
POST /jobs/{id}/webhook-secret/rotate  — regenerate webhook secret (returns new secret once)
```

---

### 5.6 New Endpoints — LLM Keys

```
POST   /users/llm-keys
  Body: { name, provider, api_key, base_url? }
  - SSRF check on base_url
  - Fernet-encrypt api_key
  - Returns: { id, name, provider, api_key: "sk-***" }  ← masked

GET    /users/llm-keys
  Returns list: [ { id, name, provider, base_url, created_at } ]  ← no key

DELETE /users/llm-keys/{id}
  Hard delete — referencing jobs fail at dispatch with error: "LLM key not found"
```

---

### 5.7 New Endpoints — Admin

All require `Depends(get_current_admin_user)`.

```
GET    /admin/users                         paginated, filterable
GET    /admin/users/{id}                    detail + usage stats
DELETE /admin/users/{id}                    hard delete + CASCADE

GET    /admin/jobs                          all jobs, filterable by user_id/status/engine
GET    /admin/jobs/{id}                     any job regardless of ownership
DELETE /admin/jobs/{id}                     force cancel/delete

GET    /admin/stats                         operational + historical (see §5.8)
GET    /admin/stats/users/{id}              per-user breakdown

GET    /admin/webhooks/deliveries           filterable by status
POST   /admin/webhooks/deliveries/{id}/retry  reset attempts=0, status=pending
```

---

### 5.8 `GET /admin/stats` Response Shape

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

`minio_storage_bytes`: MinIO API call, cache in Redis with 5-minute TTL (`scrapeflow:cache:minio_storage`).

All historical stats query `job_runs.created_at`, not `jobs.created_at`.

---

## 6. Background Tasks

Three new background tasks, all started in the FastAPI lifespan context manager after existing client startup.

### 6.1 Scheduler Loop

```python
async def scheduler_loop(db_factory, js):
    while True:
        await asyncio.sleep(60)
        async with db_factory() as db:
            # FOR UPDATE SKIP LOCKED prevents double-dispatch in multi-instance deploy
            due = await db.execute(
                SELECT jobs
                WHERE schedule_cron IS NOT NULL
                  AND schedule_status = 'active'
                  AND next_run_at <= NOW()
                FOR UPDATE SKIP LOCKED
            )
            for job in due:
                run = await create_job_run(db, job.id)
                await dispatch_to_nats(js, job, run.id)
                next_run = croniter(job.schedule_cron, datetime.now(UTC)).get_next(datetime)
                await db.execute(
                    UPDATE jobs SET next_run_at = next_run, last_run_at = NOW()
                    WHERE id = job.id
                )
```

---

### 6.2 Webhook Delivery Loop

```python
BACKOFF_SECONDS = [0, 30, 300, 1800, 7200]  # attempts 1–5

async def webhook_delivery_loop(db_factory, http_client, fernet):
    while True:
        await asyncio.sleep(15)
        async with db_factory() as db:
            pending = await db.execute(
                SELECT webhook_deliveries
                WHERE status = 'pending' AND next_attempt_at <= NOW()
                FOR UPDATE SKIP LOCKED
                LIMIT 50
            )
            for delivery in pending:
                await attempt_delivery(db, http_client, fernet, delivery)

async def attempt_delivery(db, http_client, fernet, delivery):
    job = await db.get(Job, delivery.job_id)
    secret = fernet.decrypt(job.webhook_secret.encode()).decode()

    payload_bytes = json.dumps(delivery.payload).encode()
    sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()

    try:
        resp = await http_client.post(
            delivery.webhook_url,
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-ScrapeFlow-Signature": f"sha256={sig}",
            },
            timeout=10.0,
        )
        if resp.status_code < 300:
            await db.execute(
                UPDATE webhook_deliveries
                SET status='delivered', delivered_at=NOW()
                WHERE id = delivery.id
            )
            return
        error = f"HTTP {resp.status_code}"
    except Exception as e:
        error = str(e)

    attempts = delivery.attempts + 1
    if attempts >= settings.webhook_max_attempts:
        new_status = "exhausted"
        next_attempt = None
    else:
        new_status = "pending"
        next_attempt = datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[attempts])

    await db.execute(
        UPDATE webhook_deliveries
        SET attempts=attempts, status=new_status,
            next_attempt_at=next_attempt, last_error=error
        WHERE id = delivery.id
    )
```

---

### 6.3 MaxDeliver Advisory Subscriber

When NATS exhausts redeliveries for a message, it publishes to `$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SCRAPEFLOW.*`.

```python
async def maxdeliver_advisory_subscriber(nats_client, db_factory):
    await nats_client.subscribe(
        "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SCRAPEFLOW.*",
        cb=handle_maxdeliver_advisory,
    )

async def handle_maxdeliver_advisory(msg):
    advisory = json.loads(msg.data)
    # advisory contains: stream_seq (the original message sequence number)
    # We need to correlate stream_seq → job_id/run_id
    # The original message payload is NOT in the advisory — only metadata
    # Strategy: store stream_seq on job_runs at dispatch time
    # Then: UPDATE job_runs SET status='failed', error='Max NATS redeliveries exceeded'
    #       WHERE nats_stream_seq = advisory['stream_seq'] AND status IN ('pending','running')
```

**Schema addition required:** Add `nats_stream_seq BIGINT NULL` to `job_runs`. Populate it from the `nats.Msg.Metadata().Sequence.Stream` value when the worker first receives the message and publishes the `running` event. The result consumer stores it.

---

### 6.4 Lifespan Integration

```python
# api/app/main.py — lifespan additions
async with asyncio.TaskGroup() as tg:
    tg.create_task(scheduler_loop(get_db, app.state.nats_js))
    tg.create_task(webhook_delivery_loop(get_db, httpx_client, fernet))
    tg.create_task(start_result_consumer(app.state.nats_js))
    tg.create_task(maxdeliver_advisory_subscriber(app.state.nats_client, get_db))
```

---

## 7. Result Consumer Changes

The result consumer (`api/app/core/result_consumer.py`) gains two new responsibilities:

**On `status: "completed"` from a scrape worker:**
```python
# 1. Update job_run
await db.execute(UPDATE job_runs SET status='completed', result_path=minio_path,
                  completed_at=NOW() WHERE id = run_id)

# 2. Compute diff (if this is not the first run)
prev_run = await get_previous_completed_run(db, job_id)
if prev_run:
    diff = await compute_diff(job, minio_path, prev_run.result_path)
    await db.execute(UPDATE job_runs SET diff_detected=diff.detected,
                      diff_summary=diff.summary WHERE id = run_id)

# 3. Check if LLM processing needed
job = await db.get(Job, job_id)
if job.llm_config:
    await db.execute(UPDATE job_runs SET status='processing' WHERE id = run_id)
    llm_key = await db.get(UserLLMKey, job.llm_config['llm_key_id'])
    if llm_key is None:
        await db.execute(UPDATE job_runs SET status='failed',
                          error='LLM key not found or deleted' WHERE id = run_id)
    else:
        await js.publish(NATS_JOBS_LLM_SUBJECT, build_llm_message(job, run_id, llm_key, minio_path))
        return  # do NOT create webhook delivery yet — wait for LLM completion

# 4. Create webhook delivery if applicable
if job.webhook_url:
    await create_webhook_delivery(db, job, run_id, event="job.completed", minio_path=minio_path)
```

**On `status: "completed"` from the LLM worker:**
Same flow as above but skip the LLM routing check. Create webhook delivery with `event="job.completed"`.

**On `diff_detected=true`:**
Create a second webhook delivery with `event="job.change_detected"` including `diff_summary`.

---

## 8. Infrastructure Changes

### 8.1 Docker Compose

New services to add:

```yaml
playwright-worker:
  build:
    context: playwright-worker/
  depends_on:
    nats-init:
      condition: service_completed_successfully
    minio:
      condition: service_healthy
  environment:
    NATS_URL: nats://nats:4222
    MINIO_ENDPOINT: minio:9000
    MINIO_ACCESS_KEY: minioadmin
    MINIO_SECRET_KEY: minioadmin
    MINIO_BUCKET: scrapeflow-results
    PLAYWRIGHT_MAX_WORKERS: "3"
    PLAYWRIGHT_DEFAULT_TIMEOUT_SECONDS: "60"
  restart: unless-stopped

llm-worker:
  build:
    context: llm-worker/
  depends_on:
    nats-init:
      condition: service_completed_successfully
    minio:
      condition: service_healthy
  environment:
    NATS_URL: nats://nats:4222
    MINIO_ENDPOINT: minio:9000
    MINIO_ACCESS_KEY: minioadmin
    MINIO_SECRET_KEY: minioadmin
    MINIO_BUCKET: scrapeflow-results
    LLM_KEY_ENCRYPTION_KEY: "${LLM_KEY_ENCRYPTION_KEY}"
  restart: unless-stopped
```

**NATS init command update** (stream subject change):
```yaml
nats-init:
  command: >
    nats stream add SCRAPEFLOW
      --subjects "scrapeflow.jobs.>"
      --retention work
      --max-deliver 3
      --storage file
      --replicas 1
      --server nats:4222
```

**Go worker subject update:**
```yaml
worker:
  environment:
    NATS_JOBS_RUN_SUBJECT: "scrapeflow.jobs.run.http"  # renamed
```

---

### 8.2 k3s — New Deployments

Two new Deployments in `namespace: scrapeflow`:

- `playwright-worker` — 1 replica (Chromium memory: request 512Mi, limit 1.5Gi)
- `llm-worker` — 1 replica (standard Python: request 128Mi, limit 512Mi)

New nightly CronJob for run history cleanup:
```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: job-runs-cleanup
  namespace: scrapeflow
spec:
  schedule: "0 2 * * *"   # 2am daily
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: cleanup
            image: scrapeflow-api:latest
            command: ["python", "scripts/cleanup_old_runs.py"]
            env:
            - name: SCHEDULE_RUN_RETENTION_DAYS
              value: "90"
```

---

## 9. Environment Variables Reference

| Variable | Service | Default | Description |
|----------|---------|---------|-------------|
| `LLM_KEY_ENCRYPTION_KEY` | API, LLM worker | required | Fernet key for LLM API keys + webhook secrets |
| `SCHEDULE_MIN_INTERVAL_MINUTES` | API | `5` | Minimum cron interval (0 = disabled) |
| `SCHEDULE_RUN_RETENTION_DAYS` | cleanup CronJob | `90` | Days to keep job_runs history |
| `WEBHOOK_MAX_ATTEMPTS` | API | `5` | Max webhook delivery attempts before exhausted |
| `PLAYWRIGHT_MAX_WORKERS` | Playwright worker | `3` | Concurrent browser pages |
| `PLAYWRIGHT_DEFAULT_TIMEOUT_SECONDS` | Playwright worker | `60` | Default page load timeout |

**Generating `LLM_KEY_ENCRYPTION_KEY`:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 10. Testing Requirements

### 10.1 General Rules
- All integration tests run via `docker compose exec api python -m pytest`
- Real Postgres, Redis, NATS, MinIO — no mocking infrastructure
- Mock: Clerk (already done), external LLM APIs, external webhook endpoints

### 10.2 Required Test Coverage

**Migrations:**
- Run full migration suite on empty DB, verify all tables and constraints
- Run data migration (2.3) against a seeded DB with existing jobs, verify job_runs populated correctly

**LLM Keys:**
- Create key → verify stored encrypted (raw key not visible in DB)
- List keys → verify api_key masked
- Delete key → verify hard deleted
- Create job referencing deleted key → verify 404 at job creation

**Playwright Worker:**
- Mock Chromium: `playwright.chromium.launch()` with a mock browser
- Verify `networkidle` and `load` wait strategies both dispatch correctly
- Verify context is closed after every job (success and failure)

**Scheduler:**
- Seed a job with `next_run_at = NOW() - 1 minute`, run one loop iteration
- Verify `job_runs` row created, NATS message published, `next_run_at` updated
- Seed two API instances (two scheduler loops), verify job dispatched exactly once

**Webhook Delivery:**
- Mock httpx: return 2xx → verify `status=delivered`
- Mock httpx: return 500 → verify `attempts++`, `next_attempt_at` uses backoff
- After 5 failures → verify `status=exhausted`
- Verify HMAC header present and correct on every delivery attempt
- Admin retry endpoint → verify `status=pending`, `attempts=0`

**Admin Panel:**
- Every `/admin/*` route: assert 403 for non-admin user
- Every `/admin/*` route: assert 200 for admin user with correct data
- Stats endpoint: seed known data, verify counts match

**Change Detection:**
- First run: verify `diff_detected=NULL`
- Second run, same content: verify `diff_detected=false`
- Second run, changed content: verify `diff_detected=true`, `diff_summary` populated
- LLM job: verify field-by-field JSON diff in `diff_summary`

**MaxDeliver Advisory:**
- Simulate NATS advisory message, verify `job_runs.status` set to `failed`
