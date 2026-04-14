# ADR-003: Job/Run Data Model Split

**Status:** Accepted
**Date:** 2026-04-09
**Deciders:** @karthik

---

## Context

Phase 1 stored `status`, `result_path`, and `error` directly on the `jobs` table. Every job had exactly one execution — one row, one state. `GET /jobs` was a plain `SELECT * FROM jobs` with no joins required.

Phase 2 introduces `schedule_cron` — a single job definition can fire N times on a schedule. A scheduled job that runs daily for a month produces 30 executions. With run-state on `jobs`, each new execution overwrites the previous one: there is no history, no diff baseline, and no way to inspect individual runs. The change-detection feature (Phase 2 core) requires comparing the result of the current run against the previous completed run — which is impossible if only one result is ever stored.

The solution is to separate the **job definition** (what to scrape, how often, what to do with the result) from the **job execution** (what happened when it ran). Migration 2.4 formalizes this by dropping `jobs.status`, `jobs.result_path`, and `jobs.error` — the three columns that belong to execution, not definition.

**Migration 2.4 is irreversible.** Once these columns are dropped, there is no downgrade path without restoring from backup.

---

## Decisions

### 1. Table Roles After the Split

**`jobs` — job definition template**

One row per job. Holds everything needed to dispatch and configure an execution:
- Identity and target: `id`, `user_id`, `url`, `output_format`, `engine`
- Schedule config: `schedule_cron`, `schedule_status`, `next_run_at`, `last_run_at`
- Webhook config: `webhook_url`, `webhook_secret` (Fernet-encrypted)
- Processing config: `llm_config` (JSONB), `playwright_options` (JSONB)
- Timestamps: `created_at`, `updated_at`

`jobs` rows are never deleted by users in Phase 2 — they are the permanent definition of what to scrape. Hard delete is deferred to Phase 3.

**`job_runs` — one row per execution**

One row per time the job fires. Holds everything about what happened during that execution:

```sql
CREATE TABLE job_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL,
    CONSTRAINT job_runs_status_check CHECK (
        status IN ('pending', 'running', 'processing', 'completed', 'failed', 'cancelled')
    ),
    result_path     TEXT NULL,          -- always the history/ MinIO path
    diff_detected   BOOLEAN NULL,
    diff_summary    JSONB NULL,
    error           TEXT NULL,
    started_at      TIMESTAMPTZ NULL,
    completed_at    TIMESTAMPTZ NULL,
    nats_stream_seq BIGINT NULL,        -- set on status='running' messages only (see §3)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_job_runs_job_id          ON job_runs (job_id);
CREATE INDEX idx_job_runs_status          ON job_runs (status);
CREATE INDEX idx_job_runs_created_at      ON job_runs (created_at);
CREATE INDEX idx_job_runs_nats_stream_seq ON job_runs (nats_stream_seq)
    WHERE nats_stream_seq IS NOT NULL;
```

`result_path` always stores the `history/{job_id}/{unix_timestamp}.{ext}` path — the immutable, per-run MinIO object. This is the path the diff algorithm uses to retrieve two consecutive runs for comparison. See ADR-002 §4 for the full MinIO path convention.

`nats_stream_seq` is populated by the result consumer when it receives a `status: "running"` message from a scrape worker. The MaxDeliver advisory subscriber (Step 22) uses this value to identify and fail a stalled run — NATS advisory messages contain only `stream_seq`, no `job_id` or `run_id`.

---

### 2. Reading Current Status — LATERAL JOIN

After Migration 2.4, `jobs` has no `status` column. To return the current status of a job in `GET /jobs` and `GET /jobs/{id}`, the API JOINs to the latest `job_runs` row using a LATERAL JOIN:

```sql
SELECT j.*, jr.id AS run_id, jr.status, jr.result_path,
       jr.diff_detected, jr.error, jr.completed_at
FROM jobs j
LEFT JOIN LATERAL (
    SELECT * FROM job_runs
    WHERE job_id = j.id
    ORDER BY created_at DESC
    LIMIT 1
) jr ON true
WHERE j.user_id = :user_id
```

`LATERAL` allows the subquery to reference the outer row (`j.id`) — standard SQL `JOIN` cannot do this per-row. The `LIMIT 1` with `ORDER BY created_at DESC` retrieves the latest run. If no runs exist yet (job was just created), all `jr.*` fields are `NULL`.

This is why `idx_job_runs_job_id` exists — the LATERAL subquery filters by `job_id` on every outer row.

---

### 3. Result Consumer Uses `run_id`, Not `job_id`

All result messages include `run_id` (defined in ADR-002 §3). The result consumer looks up the `job_runs` row directly:

```python
run = await db.get(JobRun, run_id)
```

No `job_id` query, no join. The `run_id` is the primary key — O(1) lookup.

**Cancellation guard:** The result consumer checks `run.status == 'cancelled'` at the top of every message handler before doing any work. This is the enforcement point — workers are unaware of cancellations.

---

### 4. `DELETE /jobs/{id}` Semantics

`DELETE /jobs/{id}` does **not** delete the `jobs` template row. It:

1. Finds the latest `job_runs` row where `status IN ('pending', 'running', 'processing')`
2. Sets that row's `status = 'cancelled'`
3. If the job has `schedule_cron IS NOT NULL`: also sets `jobs.schedule_status = 'paused'` and `jobs.next_run_at = NULL`

Step 3 is required because cancelling the active run without pausing the schedule would cause the scheduler (Step 20) to fire a new `job_runs` row at the next cron tick. The existing `schedule_status` field (values: `'active'`, `'paused'`) is the correct mechanism — no new column needed.

If no non-terminal run exists, the handler returns `{"message": "Job has no active run to cancel"}`.

User-facing hard delete (removing the `jobs` row and all `job_runs` via CASCADE) is deferred to Phase 3. Admin hard delete is available via `DELETE /admin/jobs/{id}`.

---

### 5. Migration 2.4 — Irreversible

> **Spec reference:** [`docs/phase2/phase2-engineering-spec-v3.md` §2.4](../phase2/phase2-engineering-spec-v3.md)

```python
def upgrade():
    op.drop_column("jobs", "status")
    op.drop_column("jobs", "result_path")
    op.drop_column("jobs", "error")

def downgrade():
    pass  # no downgrade — backup restore required
```

**Pre-check before running:**
```bash
docker compose exec db psql -U scrapeflow -c "SELECT COUNT(*) FROM job_runs;"
# Must be > 0 if existing data exists
```

This migration must not run until Steps 10 and 11 are complete and merged — those steps update all code that previously read `jobs.status`, `jobs.result_path`, and `jobs.error`.

---

## Alternatives Considered

### Hybrid — keep `status` on `jobs` as a denormalized cache, add `job_runs` for history

The appeal: `GET /jobs` reads `jobs.status` directly — no LATERAL JOIN needed.

Rejected because the write side becomes the problem:

- Every status transition requires updating two rows (`job_runs.status` AND `jobs.status`) in the same transaction. The result consumer (Step 15) is already the most complex component — adding mandatory dual-table updates on every transition increases the failure surface.

- For recurring jobs, `jobs.status` becomes semantically ambiguous. When a scheduled job has `jobs.status = 'completed'` and a new `job_runs` row at `status = 'pending'`, which is "current"? Any rule (e.g. "always reflect the latest run") requires the scheduler and result consumer to find and update `jobs.status` on every transition — effectively doing the LATERAL JOIN on every write instead of every read.

- If the result consumer updates `job_runs` but crashes before updating `jobs`, the tables diverge silently. No constraint violation. The mismatch is only detectable by cross-querying both tables.

**Trade-off summary:** hybrid = fast reads, risky writes. Full split = LATERAL JOIN on reads (bounded cost, correct), single-table writes (safe). Correctness outweighs read simplicity.

---

## Phase 1 Data Migration Note

Migration 2.3 copies existing Phase 1 `jobs` rows into `job_runs`:

```sql
INSERT INTO job_runs (id, job_id, status, result_path, error, completed_at, created_at)
SELECT gen_random_uuid(), id, status, result_path, error, updated_at, created_at
FROM jobs WHERE status != 'pending'
```

These migrated rows carry Phase 1 MinIO paths (`{job_id}.ext`), not the Phase 2 `history/{job_id}/{timestamp}.ext` format. The diff algorithm will treat them as "no prior result" — the first Phase 2 run of any job always baselines from scratch. This is intentional and harmless: Phase 1 was local development only; Phase 2 is the first production deployment.

---

## Consequences

- `GET /jobs` and `GET /jobs/{id}` require a LATERAL JOIN to surface current run state. This is a bounded performance cost covered by `idx_job_runs_job_id`.
- Adding new per-run fields (future) requires schema changes to `job_runs` only — `jobs` is stable.
- The result consumer targets `job_runs.id` (run_id) directly — O(1) lookup, no join.
- New worker types (future) naturally produce `job_runs` rows with no `jobs` schema changes.
- `cleanup_old_runs.py` (Step 25) deletes old `job_runs` rows and their MinIO `history/` objects. `jobs` template rows and `latest/` MinIO objects are never deleted by the cleanup script.
- Admin hard delete (`DELETE /admin/jobs/{id}`) cascades to all `job_runs` via the FK `ON DELETE CASCADE`.
