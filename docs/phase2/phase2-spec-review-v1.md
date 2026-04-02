# Phase 2 Engineering Spec — Architect Review

**Reviewer:** Senior Software Architect  
**Spec reviewed:** `phase2-engineering-spec.md` (dated 2026-04-01)  
**Review date:** 2026-04-01  
**Verdict:** **Do not implement as written.** Four issues (P0) will cause startup failure or data corruption and must be resolved before any code is written. Eight additional issues (P1) are silent logic bugs that will produce wrong behavior in production without crashing. Remaining items are spec gaps or hardening concerns.

---

## Summary Table

| # | Severity | Section | Issue |
|---|----------|---------|-------|
| 1 | **P0** | §6.4 | `asyncio.TaskGroup` before `yield` — FastAPI never starts |
| 2 | **P0** | §5.3 | Webhook secret generated *after* NATS publish — race condition |
| 3 | **P0** | §5.3, §7 | Cancellation enforcement completely unspecified for Phase 2 |
| 4 | **P0** | §6.3 | `nats_stream_seq` column referenced but missing from all migrations |
| 5 | **P1** | §7 | Diff computed on raw HTML for LLM jobs — should happen post-extraction |
| 6 | **P1** | §4.1 | Go worker pull consumer pattern causes AckWait timeouts under load |
| 7 | **P1** | §6.1 | Scheduler dispatch not atomic — NATS publish + DB update can split-brain |
| 8 | **P1** | §7 | Result consumer has no mechanism to distinguish scrape vs LLM results |
| 9 | **P1** | §6.3 | MaxDeliver advisory ignores `processing` status — LLM jobs never marked failed |
| 10 | **P1** | §4.3 | LLM worker has no timeout — hung LLM call permanently blocks a worker slot |
| 11 | **P1** | §7 | Two simultaneous webhook deliveries per change-detected run — double POST |
| 12 | **P1** | §3.1 | `docker compose down -v` for NATS migration destroys Postgres + MinIO volumes |
| 13 | **P2** | §8.2 | `scripts/cleanup_old_runs.py` implementation entirely undefined |
| 14 | **P2** | §2.3, §7 | `job_runs.result_path` stores which path — `latest/` or `history/`? Diff depends on answer |
| 15 | **P2** | §5 | `DELETE /jobs/{id}` (user cancel) not updated for Phase 2 schema |
| 16 | **P2** | §5.5 | PATCH /jobs scope underspecified — can `url`, `schedule_cron`, `llm_config` change? |
| 17 | **P2** | §4.3 | `call_anthropic()` implementation not defined |
| 18 | **P2** | §4.3 | No content truncation strategy — large pages will fail LLM extraction silently |
| 19 | **P3** | §6.2 | SSRF check only at job creation — webhook delivery at send time is unchecked |
| 20 | **P3** | §2.3 | Redundant B-tree + BRIN index on the same column |
| 21 | **P3** | §2.3 | `job_runs.status` has no CHECK constraint |
| 22 | **P3** | §4.2 | Playwright `wait_strategy` valid values not enumerated or validated |
| 23 | **P2** | §2 | Migration workflow inverted — spec writes raw SQL but project uses SQLAlchemy autogenerate |

---

## P0 — Will Break Startup or Corrupt Data

### Issue 1: `asyncio.TaskGroup` before `yield` — FastAPI Never Starts

**Location:** §6.4 Lifespan Integration

**The problem:** The spec proposes this lifespan structure:

```python
async with asyncio.TaskGroup() as tg:
    tg.create_task(scheduler_loop(...))
    tg.create_task(webhook_delivery_loop(...))
    tg.create_task(start_result_consumer(...))
    tg.create_task(maxdeliver_advisory_subscriber(...))
```

`asyncio.TaskGroup` is a structured concurrency primitive — the `async with` block **does not exit until all tasks complete**. Since every task is an infinite `while True` loop, the block never exits, the `yield` is never reached, and FastAPI never begins accepting requests. The API starts and immediately hangs.

**Fix:** Replace with individual `asyncio.create_task()` calls (the pattern the existing `start_result_consumer` already uses), then cancel them on shutdown:

```python
# Startup
scheduler_task = asyncio.create_task(scheduler_loop(get_db, app.state.nats_js))
webhook_task = asyncio.create_task(webhook_delivery_loop(get_db, httpx_client, fernet))
result_task = await start_result_consumer(app.state.nats_js)
maxdeliver_task = asyncio.create_task(maxdeliver_advisory_subscriber(...))

yield  # FastAPI serves here

# Shutdown — cancel in reverse start order
for task in [maxdeliver_task, scheduler_task, webhook_task]:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
result_task.cancel()
await asyncio.gather(result_task, return_exceptions=True)
```

Additionally: each background loop's `while True` body must catch all exceptions internally and log + continue. Without this, a transient DB connection error in `scheduler_loop` kills that task permanently. TaskGroup would at least surface this (by crashing the app); bare `create_task` silently swallows it. Add:

```python
async def scheduler_loop(db_factory, js):
    while True:
        try:
            await asyncio.sleep(60)
            async with db_factory() as db:
                ...
        except Exception:
            logger.exception("Scheduler loop error — will retry next cycle")
```

---

### Issue 2: Webhook Secret Race Condition — Generated After NATS Publish

**Location:** §5.3 New creation flow, steps 3–4

**The problem:** The spec defines this ordering:

```
1. Create jobs row
2. Create job_runs row (status=pending)
3. Publish to NATS  ← job is now in-flight
4. Generate webhook_secret, encrypt, store on jobs row  ← secret not yet in DB
5. Return response
```

Between steps 3 and 4, the worker can complete the job, the result consumer fires, and the webhook delivery loop attempts to read `job.webhook_secret` to sign the payload — but the secret hasn't been committed yet. The delivery fails, increments `attempts`, and burns one of the five backoff slots before the secret even exists.

This is a TOCTOU (time-of-check to time-of-use) race, and it's worse with fast HTTP jobs on a local or low-latency network.

**Fix:** Generate and store the webhook secret on the `jobs` row **before** publishing to NATS:

```
1. If webhook_url: generate secret, encrypt → store in memory
2. Create jobs row WITH webhook_secret already set
3. Create job_runs row (status=pending)
4. Publish to NATS
5. Return response including the plaintext secret (shown once)
```

No step 4 write is needed; the secret is part of the initial `INSERT`.

---

### Issue 3: Cancellation Enforcement Completely Unspecified

**Location:** §5.3 (job creation), §5.5 (new endpoints), §7 (result consumer)

**The problem:** Phase 1 cancellation works by setting `jobs.status = 'cancelled'`. The result consumer then checks:

```python
if job.status == JobStatus.cancelled:
    await msg.ack()
    return
```

Migration 2.4 removes `jobs.status`. After this migration runs, the result consumer references a non-existent column and crashes at runtime. More fundamentally, the spec never answers three questions:

1. **What happens to `DELETE /jobs/{id}`** (the user-facing cancel endpoint in Phase 1)? It's not mentioned in §5 at all.
2. **Where does cancellation state live in Phase 2?** On `job_runs.status`? On a new `jobs.cancelled_at` column?
3. **How does the result consumer identify a cancelled run** given messages now carry `run_id`?

**Fix — prescriptive resolution:**

Keep `DELETE /jobs/{id}` as the cancel endpoint but update its behavior: set `job_runs.status = 'cancelled'` on the latest run for this job (only if in a non-terminal state). The result consumer already receives `run_id` in Phase 2 messages — use it directly:

```python
# In result consumer
run = await db.get(JobRun, run_id)
if run is None:
    await msg.ack(); return

if run.status == JobRunStatus.cancelled:
    await msg.ack(); return  # ADR-001 cancellation enforcement, now via run_id
```

This requires adding `cancelled` to the `job_runs` status values (currently `pending, running, processing, completed, failed` — `cancelled` is absent from the Phase 2 schema).

---

### Issue 4: `nats_stream_seq` Referenced But Missing From All Migrations

**Location:** §6.3 MaxDeliver Advisory Subscriber

**The problem:** Section 6.3 states:

> **Schema addition required:** Add `nats_stream_seq BIGINT NULL` to `job_runs`.

This column is required for the MaxDeliver advisory to identify which job run failed. But the column appears in zero migrations — not in 2.3 (which creates `job_runs`) and in no subsequent migration. An engineer implementing §6.3 will have to add a migration that isn't in the spec, which violates the spec's own rule: "Run in the order listed."

**Fix:** Add as migration 2.8:

```sql
ALTER TABLE job_runs ADD COLUMN nats_stream_seq BIGINT NULL;
CREATE INDEX idx_job_runs_nats_stream_seq ON job_runs (nats_stream_seq)
    WHERE nats_stream_seq IS NOT NULL;
```

And update the complete schema summary in §2 to include it. Specify explicitly who sets this value (the result consumer, on first receipt of a `running` status message from any worker) and when (before committing the `running` state update).

---

## P1 — Silent Logic Bugs

### Issue 5: Diff Computed on Raw HTML for LLM Jobs

**Location:** §7 Result Consumer, first `status: "completed"` handler

**The problem:** The result consumer runs the diff computation immediately when a scrape worker completes:

```python
# 2. Compute diff (if this is not the first run)
prev_run = await get_previous_completed_run(db, job_id)
if prev_run:
    diff = await compute_diff(job, minio_path, prev_run.result_path)
    await db.execute(UPDATE job_runs SET diff_detected=...)

# 3. Check if LLM processing needed
if job.llm_config:
    await dispatch_to_llm(...)
    return  # wait for LLM completion
```

For an LLM job, this computes a text diff on raw HTML and stores it on `job_runs`, then dispatches the same raw HTML to the LLM worker. When the LLM worker completes and the result consumer processes the LLM result, it writes a new `result_path` (structured JSON), **but the diff computed in step 2 was against raw HTML from the previous run, not structured JSON**.

The concepts doc (§8) is explicit that LLM jobs should use field-by-field JSON diff because the LLM extraction implicitly removes noise. The current spec flow defeats this entirely.

**Fix:** In the first `status: "completed"` handler, skip diff computation for LLM jobs:

```python
if job.llm_config:
    await dispatch_to_llm(...)
    return  # diff happens after LLM completion, not here
```

In the LLM result handler (`status: "completed"` from LLM worker):

```python
# Get previous completed LLM run (status='completed' with llm result_path)
prev_run = await get_previous_completed_run(db, job_id)
if prev_run:
    diff = await compute_json_diff(minio_path, prev_run.result_path)
    await db.execute(UPDATE job_runs SET diff_detected=..., diff_summary=...)
```

---

### Issue 6: Go Worker Pull Consumer — AckWait Timeout Under Load

**Location:** §4.1 Go HTTP Worker Changes

**The problem:** The proposed pull consumer pattern:

```go
msgs, err := sub.Fetch(cfg.WorkerPoolSize, nats.MaxWait(5*time.Second))
for _, msg := range msgs {
    sem <- struct{}{}   // ← blocks here waiting for capacity
    go func(m *nats.Msg) {
        defer func() { <-sem }()
        w.handleMessage(ctx, m)
    }(msg)
}
```

`sub.Fetch(WorkerPoolSize, ...)` fetches up to N messages from NATS. NATS starts the AckWait timer the moment a message is delivered. The semaphore is then acquired in-process — if 8 messages are fetched but only 2 slots are free, messages 3–8 sit in a goroutine's stack waiting for `sem <-`. If `handleMessage` takes longer than AckWait ÷ 6, messages 3–8 time out before being processed, NATS redelivers them, and you get duplicate processing.

**Fix:** Only fetch as many messages as there are available slots:

```go
for {
    available := cap(sem) - len(sem)
    if available == 0 {
        time.Sleep(100 * time.Millisecond)
        continue
    }
    msgs, err := sub.Fetch(available, nats.MaxWait(5*time.Second))
    for _, msg := range msgs {
        sem <- struct{}{}
        go func(m *nats.Msg) {
            defer func() { <-sem }()
            w.handleMessage(ctx, m)
        }(msg)
    }
}
```

This guarantees every fetched message has a semaphore slot waiting for it.

---

### Issue 7: Scheduler Loop Not Atomic — NATS + DB Can Split-Brain

**Location:** §6.1 Scheduler Loop

**The problem:** The loop's pseudocode:

```python
for job in due:
    run = await create_job_run(db, job.id)   # writes DB
    await dispatch_to_nats(js, job, run.id)  # writes NATS
    await db.execute(UPDATE jobs SET next_run_at=...)
```

If `dispatch_to_nats` succeeds for job A but fails for job B, and then `db.commit()` (implicit at end of `async with db_factory()`) rolls back, then:
- NATS has a message for job A's run (dispatched, now in-flight)
- DB has no `job_runs` row for job A (rolled back)
- The result consumer will receive job A's result and find `run_id` not in DB → undefined behavior

Conversely, if the DB commit succeeds but the process dies before NATS publish for some jobs, those jobs' `next_run_at` is updated but no NATS message was sent — the run is lost silently until the next cycle creates a new run.

**Fix:** Update `next_run_at` and commit **before** publishing to NATS, using the same "write to DB first" principle from ADR-001:

```python
for job in due:
    run = await create_job_run(db, job.id, status='pending')
    next_run = croniter(job.schedule_cron, datetime.now(UTC)).get_next(datetime)
    await db.execute(UPDATE jobs SET next_run_at=next_run, last_run_at=NOW() WHERE id=job.id)
await db.commit()  # commit all runs and next_run_at updates first

# Then publish — if this fails, next cycle will re-dispatch
# (next_run_at was already advanced, so no double-dispatch)
for run in created_runs:
    await dispatch_to_nats(js, run)
```

If the NATS publish fails, the job_run sits as `pending` forever. Add a recovery path: jobs where `job_runs.status = 'pending'` AND `created_at < NOW() - 10 minutes` are re-dispatched on the next scheduler cycle (stale pending run detection).

---

### Issue 8: Result Consumer Cannot Distinguish Scrape vs LLM Results

**Location:** §7 Result Consumer

**The problem:** Both the scrape workers and the LLM worker publish results to `scrapeflow.jobs.result` with identical message schemas. The spec says:

> On `status: "completed"` from the LLM worker: Same flow as above but skip the LLM routing check.

But the result consumer receives identical messages from both worker types. There is no `worker_type` field in the result message schema (§3.3). The consumer has no way to know whether to apply the LLM dispatch logic or the post-LLM webhook logic.

**Fix:** The simplest mechanism is already available — check `job_run.status` at the time the result is received:

- If `job_run.status == 'pending'` or `'running'` when a `completed` result arrives → this is from a scrape worker → apply LLM dispatch if `llm_config` is set
- If `job_run.status == 'processing'` when a `completed` result arrives → this is from the LLM worker → skip LLM dispatch, create webhook

Make this explicit in the spec. Add a required comment in the result consumer that states this invariant.

---

### Issue 9: MaxDeliver Advisory Misses `processing` Status

**Location:** §6.3 MaxDeliver Advisory Subscriber

**The problem:** The advisory handler comment says:

```python
# UPDATE job_runs SET status='failed', error='Max NATS redeliveries exceeded'
# WHERE nats_stream_seq = advisory['stream_seq'] AND status IN ('pending','running')
```

But LLM jobs transition through: `pending → running → processing → completed/failed`. A NATS message for the LLM subject (`scrapeflow.jobs.llm`) can exhaust redeliveries while the job_run is in `processing` state. The WHERE clause misses `processing`, and the LLM job will sit in `processing` forever with no failure recorded.

**Fix:** Add `'processing'` to the status filter:

```sql
WHERE nats_stream_seq = :seq AND status IN ('pending', 'running', 'processing')
```

---

### Issue 10: LLM Worker Has No Timeout — Worker Slot Can Hang Permanently

**Location:** §4.3 Python LLM Worker

**The problem:** The LLM call in the spec has no timeout:

```python
response = await client.chat.completions.create(
    model=model,
    messages=[...],
    response_format={...}
)
```

OpenAI and Anthropic APIs can hang for minutes under overload or network issues. Since the LLM worker uses a pull consumer with `PLAYWRIGHT_MAX_WORKERS` (default 3) parallel slots, a single hung request holds one of three slots indefinitely. With three concurrent large-page requests to a slow vLLM instance, the worker silently processes zero jobs.

**Fix:** Configure timeouts at the client level:

```python
# openai_compatible
client = AsyncOpenAI(
    api_key=api_key,
    base_url=base_url or None,
    timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
)
```

Add `LLM_REQUEST_TIMEOUT_SECONDS` to the environment variables table (§9), default `120`.

---

### Issue 11: Double Webhook POST Per Change-Detected Run

**Location:** §7 Result Consumer, `diff_detected=true` handling

**The problem:** When a run detects a change, the spec creates two webhook deliveries:

1. `event="job.completed"` — always, for every completed run
2. `event="job.change_detected"` — additionally, when diff is detected

For a recurring job that changes on every run, every completion fires two POSTs to the consumer's endpoint. The `job.completed` payload doesn't include `diff_summary` (not specified in §3.3 or elsewhere), so the consumer can't even use it for change information — they have to wait for the `job.change_detected` event anyway.

This is inconsistent with standard webhook design (Stripe, GitHub both send one event per logical occurrence) and will confuse integrators.

**Fix — two options, spec must pick one:**

**Option A (preferred):** Remove the separate `job.change_detected` event. Promote `diff_detected` and `diff_summary` into the `job.completed` payload. Consumers inspect `diff_detected` to decide whether to act.

**Option B:** Fire only `job.change_detected` (not `job.completed`) when a diff is detected. Consumers configure which event(s) they care about.

Either way, the webhook delivery payload schema (currently absent from the spec) must be defined explicitly.

---

### Issue 12: NATS Stream Migration Destroys All Docker Compose Data

**Location:** §3.1 NATS Stream Change

**The problem:** The migration procedure is:

> `docker compose down -v` is required to recreate the NATS volume with the new stream config.

`-v` destroys **all named volumes** — including Postgres and MinIO. In a development environment with test data, this wipes everything. In a production-like staging environment, this is a data loss event.

**Fix:** Use the NATS CLI to update the stream in-place without destroying data:

```bash
nats stream edit SCRAPEFLOW --subjects "scrapeflow.jobs.>" --server nats:4222
```

If the NATS version doesn't support `stream edit` for subject changes, the stream can be deleted and recreated while leaving other Docker volumes intact:

```bash
docker compose exec nats-init \
  nats stream rm SCRAPEFLOW --force --server nats:4222

docker compose exec nats-init \
  nats stream add SCRAPEFLOW \
    --subjects "scrapeflow.jobs.>" \
    --retention work \
    --max-deliver 3 \
    --storage file \
    --replicas 1 \
    --server nats:4222
```

Update the NATS init service's entrypoint to handle the case where the stream already exists (use `stream add --force` or an idempotent `stream create-or-update` pattern). Update §8.1 with this procedure. Remove all mention of `docker compose down -v`.

---

## P2 — Spec Gaps (Engineer Will Have to Guess)

### Issue 13: `scripts/cleanup_old_runs.py` Entirely Undefined

**Location:** §8.2 k8s CronJob

The spec references this script but never defines it. This is not a minor omission — the script touches both Postgres (delete rows) and MinIO (delete objects), and the failure modes matter:

- What happens if the MinIO delete fails but the DB row is deleted? The DB loses the reference; the object orphans in MinIO forever.
- Is the script idempotent if re-run?
- Does it clean up `webhook_deliveries` rows too?

**Required additions to spec:**

1. Full script pseudocode showing the query and deletion order
2. Atomicity strategy: delete MinIO objects first (if they fail, retry; DB row preserved), then delete DB rows
3. Cleanup scope: `job_runs` older than retention window AND their corresponding `webhook_deliveries` rows AND their `history/{job_id}/*` MinIO objects. `latest/` objects are never deleted.

---

### Issue 14: `job_runs.result_path` Stores Which Path?

**Location:** §2.3, §7, concepts §11

The spec says workers write to two MinIO paths per run:

```
latest/{job_id}.{ext}
history/{job_id}/{timestamp}.{ext}
```

But the result message (§3.3) has only one `minio_path` field. The result consumer writes this to `job_runs.result_path`. The diff algorithm "reads the two most recent `history/` entries" — but if `result_path` stores the `latest/` path, the diff algorithm cannot retrieve previous runs by path (there's only one `latest/` per job). If it stores the `history/` path, the API's `result_path` in responses needs to be the history path for downloads.

**Resolution required:** Specify that workers publish the `history/` path in the result message. `job_runs.result_path` stores the `history/` path. The diff algorithm reads `job_runs.result_path` for the current and previous run directly. The API may optionally derive the `latest/` path for convenience responses.

Update the result message schema in §3.3 to make this explicit.

---

### Issue 15: `DELETE /jobs/{id}` (User Cancel) Unspecified for Phase 2

**Location:** §5

Phase 1 `DELETE /jobs/{id}` sets `jobs.status = 'cancelled'`. Migration 2.4 removes `jobs.status`. The spec's §5 modifies `GET /jobs`, adds `PATCH /jobs/{id}`, and adds `POST /jobs/{id}/webhook-secret/rotate` — but says nothing about the existing `DELETE /jobs/{id}`. It will compile but fail at runtime when it tries to set `job.status`.

**Required:** Add a §5.x entry specifying that `DELETE /jobs/{id}` is updated to: query the latest `job_runs` row for this job; if status is not terminal (`completed`, `failed`, `cancelled`), set `job_runs.status = 'cancelled'`. Add `cancelled` to the `job_runs` status value set and to migration 2.3's schema.

---

### Issue 16: `PATCH /jobs/{id}` Scope Underspecified

**Location:** §5.5

The spec says PATCH updates `schedule_status` and `webhook_url`. Unanswered:

- Can `schedule_cron` be changed on an existing scheduled job? If yes, what happens to the current `next_run_at`?
- Can `url` be changed? (Changing the URL invalidates all history diffs.)
- Can `llm_config` be updated? (Changing `llm_key_id` while a run is in `processing` state is a race.)
- Can `engine` be changed from `http` to `playwright` after creation?

The spec must either enumerate allowed fields explicitly or state "only `schedule_status` and `webhook_url` are mutable after creation."

---

### Issue 17: `call_anthropic()` Implementation Not Defined

**Location:** §4.3 Python LLM Worker

The spec defines `call_openai_compatible()` in full but `call_anthropic()` is a bare reference with no body. The Anthropic Python SDK uses a different call signature — no `response_format`, uses `tools` for structured output. This is not a one-liner adaptation of the OpenAI path.

**Required:** Define the full `call_anthropic()` signature, the tool-use approach for structured output, and the response parsing. Anthropic structured output via tool_use:

```python
response = await client.messages.create(
    model=model,
    max_tokens=4096,
    tools=[{"name": "extract", "input_schema": output_schema}],
    tool_choice={"type": "tool", "name": "extract"},
    messages=[{"role": "user", "content": f"Extract:\n\n{content}"}]
)
return response.content[0].input  # dict matching output_schema
```

---

### Issue 18: No Content Truncation Strategy for LLM Context Window

**Location:** §4.3 Python LLM Worker

The LLM worker passes `content` (full rendered HTML or Markdown) verbatim. A single JS-heavy SPA page can be 500KB+ of HTML, exceeding 128K token context windows. The OpenAI API returns HTTP 400. The Anthropic API returns HTTP 400. The worker catches this as `except Exception`, publishes `status=failed`, and the user sees "context_length_exceeded" in the error column — with no guidance on how to fix it.

**Required:** Add `LLM_MAX_CONTENT_CHARS` env var (default `200000`, approximately 50K tokens) with explicit truncation before the LLM call:

```python
if len(content) > settings.llm_max_content_chars:
    content = content[:settings.llm_max_content_chars]
    logger.warning("Content truncated for LLM", job_id=job_id, original_len=len(content))
```

Document this limit in the API response for `POST /users/llm-keys` or as a new `GET /config` endpoint.

---

## P3 — Production Hardening

### Issue 19: SSRF Check Only at Job Creation — Webhook Delivery Unchecked at Send Time

**Location:** §6.2 Webhook Delivery Loop

The spec correctly validates `webhook_url` for SSRF at `POST /jobs` time (§5.3). But DNS rebinding attacks change the IP that a hostname resolves to after the initial check. At delivery time, `attempt_delivery()` POSTs to `delivery.webhook_url` without re-validating.

**Fix:** Re-resolve the hostname before every delivery attempt and reject private/loopback IPs. This should reuse the extracted `_validate_no_ssrf()` utility function that §5.1 already plans to move to `api/app/core/security.py`.

---

### Issue 20: Redundant Indexes — B-tree + BRIN on `job_runs.created_at`

**Location:** §2.3

Migration 2.3 creates:

```sql
CREATE INDEX idx_job_runs_created_at ON job_runs (created_at);          -- B-tree
CREATE INDEX idx_job_runs_created_at_brin ON job_runs USING BRIN (created_at);  -- BRIN
```

B-tree indexes support all query patterns a BRIN index supports, with better precision. BRIN is only worthwhile at billions of rows where space savings are critical. At this scale, a B-tree plus a BRIN on the same column wastes write overhead with zero read benefit.

**Fix:** Drop `idx_job_runs_created_at_brin`. Keep only the B-tree. Revisit if the table grows beyond 100M rows.

---

### Issue 21: `job_runs.status` Has No CHECK Constraint

**Location:** §2.3

`job_run.status` is `VARCHAR(20) NOT NULL` with no constraint. The existing `jobs.status` uses a Postgres ENUM type (`jobstatus`) enforced at the DB level. `job_runs` is the highest-volume table in the Phase 2 schema and the most critical for operational correctness. A bug that writes `job_runs.status = 'compleated'` would be silently accepted.

**Fix — two options:**

**Option A:** Reuse/extend the `jobstatus` enum (requires migration 2.6 to also add values used only in `job_runs`).

**Option B (simpler):** Add a CHECK constraint in migration 2.3:

```sql
ALTER TABLE job_runs ADD CONSTRAINT job_runs_status_check
    CHECK (status IN ('pending', 'running', 'processing', 'completed', 'failed', 'cancelled'));
```

Note: `cancelled` must be added here per the fix for Issue 15.

---

### Issue 22: Playwright `wait_strategy` Values Not Enumerated

**Location:** §4.2 Python Playwright Worker, §2.2 `playwright_options` JSONB

`wait_strategy` is stored as a string in JSONB and used directly in:

```python
await page.wait_for_load_state(job.playwright_options.wait_strategy)
```

Playwright accepts `'load'`, `'domcontentloaded'`, `'networkidle'`. Passing any other string raises a Python exception inside the worker, which bubbles to the generic `except Exception` handler and marks the job failed with a Playwright API error. Users get no feedback at job creation time.

**Fix:** Define `wait_strategy` as an enum in the `PlaywrightOptions` Pydantic model on the API side so validation happens at `POST /jobs` time:

```python
class WaitStrategy(str, enum.Enum):
    load = "load"
    domcontentloaded = "domcontentloaded"
    networkidle = "networkidle"

class PlaywrightOptions(BaseModel):
    wait_strategy: WaitStrategy = WaitStrategy.load
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    block_images: bool = False
```

---

### Issue 23: Migration Workflow Inverted — Spec Writes Raw SQL, Project Uses Autogenerate

**Location:** §2 (all migrations)

**The problem:** The spec presents every migration as raw SQL statements, implying an engineer would write `op.execute("ALTER TABLE ...")` by hand. But this project's existing workflow — and the existing migration at `api/migrations/versions/8a673d38fe23_*.py` — uses `alembic revision --autogenerate`: define the SQLAlchemy model first, run the autogenerate command, and Alembic produces the `op.*` Python DSL from the model diff. Writing raw SQL migrations manually is extra work that also bypasses Alembic's autogenerated `downgrade()` functions.

**Fix:** Reframe §2 to describe the correct workflow:

1. Update/create SQLAlchemy models in `api/app/models/` to reflect the target schema
2. Run `alembic revision --autogenerate -m "phase 2 schema"` — Alembic diffs the models against the live DB and generates the `op.*` migration code
3. Review the generated file and manually append the two things autogenerate cannot produce (see below)
4. Run `alembic upgrade head`

The raw SQL in §2 is useful as a **reference** for what the schema should look like — keep it, but label it as "intended schema" not "migration code."

**The two manual additions autogenerate cannot handle:**

**1. The data migration in §2.3** — copying existing job outcomes into `job_runs` is data manipulation, not DDL; Alembic has no `op.*` primitive for it. Must be appended manually:

```python
# Append inside upgrade() after op.create_table('job_runs', ...)
op.execute("""
    INSERT INTO job_runs (id, job_id, status, result_path, error, completed_at, created_at)
    SELECT gen_random_uuid(), id, status, result_path, error, updated_at, created_at
    FROM jobs WHERE status != 'pending'
""")
```

**2. `ALTER TYPE jobstatus ADD VALUE 'processing'` in §2.6** — Alembic autogenerate does not detect ENUM value additions. This must be its own separate migration file with `transaction = False` set at the module level:

```python
# Required — ALTER TYPE ADD VALUE cannot run inside a transaction
transaction = False

def upgrade() -> None:
    op.execute("ALTER TYPE jobstatus ADD VALUE 'processing' AFTER 'running'")

def downgrade() -> None:
    # Postgres has no ALTER TYPE DROP VALUE — downgrade is a no-op or full type rebuild
    pass
```

**Practical result:** Phase 2 needs **two `alembic revision` runs**, not seven separate files. One autogenerated revision for all structural changes (new tables, new columns, dropped columns, indexes), with the data migration appended manually. One hand-written revision for the ENUM value addition. This matches how Phase 1 was built.

---

## Pre-Implementation Checklist

Before any Phase 2 code is written, the spec needs the following resolved:

- [ ] Rewrite §6.4 to use `asyncio.create_task()` with explicit shutdown (Issue 1)
- [ ] Fix §5.3 creation flow to generate webhook_secret before NATS publish (Issue 2)
- [ ] Add §5.x for updated `DELETE /jobs/{id}` + add `cancelled` to `job_runs` statuses (Issue 3, 15)
- [ ] Add migration 2.8 for `nats_stream_seq` (Issue 4)
- [ ] Fix §7 result consumer to skip diff for LLM jobs (Issue 5)
- [ ] Fix §4.1 pull consumer fetch pattern (Issue 6)
- [ ] Fix §6.1 scheduler to commit before NATS publish (Issue 7)
- [ ] Add result consumer `job_run.status` check to distinguish scrape vs LLM (Issue 8)
- [ ] Fix §6.3 advisory to include `processing` in status filter (Issue 9)
- [ ] Add `LLM_REQUEST_TIMEOUT_SECONDS` to §4.3 and §9 (Issue 10)
- [ ] Resolve double-webhook UX in §7 — pick Option A or B (Issue 11)
- [ ] Replace `docker compose down -v` with `nats stream edit` procedure in §3.1 (Issue 12)
- [ ] Define `scripts/cleanup_old_runs.py` in full (Issue 13)
- [ ] Clarify `result_path` stores `history/` path, update §3.3 result message (Issue 14)
- [ ] Clarify PATCH /jobs mutable fields (Issue 16)
- [ ] Define `call_anthropic()` implementation (Issue 17)
- [ ] Add `LLM_MAX_CONTENT_CHARS` truncation to §4.3 and §9 (Issue 18)
- [ ] Reframe §2 migrations as SQLAlchemy model changes + autogenerate workflow; label raw SQL as reference schema only (Issue 23)
