# ADR-006: Batch Scraping Data Model

**Status:** Accepted
**Date:** 2026-04-15
**Deciders:** @karthik

---

## Context

PRD-006 introduces batch scraping: a user submits a list of URLs in a single API call and receives a single `batch_id` to poll for aggregate progress. Each URL is scraped independently using the existing worker infrastructure.

The existing data model has a hard FK dependency: `job_runs.job_id REFERENCES jobs(id) NOT NULL`. To reuse the existing result consumer and worker machinery (which both operate on `job_runs` rows), every `job_runs` row must have a parent.

Two approaches were considered:

**Option A — Synthetic `jobs` rows per batch item:**
For each URL in a batch, create a "headless" `jobs` row (not user-visible, no template semantics) and a `job_runs` row. Workers and the result consumer are unchanged.

Problem: ADR-003 defines `jobs` rows as *templates* — reusable definitions that produce multiple runs over time. A batch item is the opposite: a one-shot URL with no scheduling, no PATCH lifecycle, no template semantics. Forcing batch items into `jobs` pollutes the template table and requires a discriminator column (`source = 'batch'`) on every existing `GET /jobs` query to filter them out.

**Option B — Nullable `job_id` on `job_runs`:**
Allow `job_runs.job_id` to be nullable. Add `batch_item_id FK → batch_items` as an alternative parent. A run belongs to either a job or a batch item, never both, enforced by a check constraint.

Option B was chosen. It keeps the `jobs` table clean (no synthetic rows, no discriminator), and the mutual exclusion check constraint enforces correctness at the DB level without application-layer guards.

---

## Decisions

### 1. New tables: `batches` and `batch_items`

**`batches` table:**

```sql
CREATE TABLE batches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    status          VARCHAR(20) NOT NULL DEFAULT 'queued',
                    -- queued → running → completed | partial_failure | cancelled
    output_format   VARCHAR(20) NOT NULL DEFAULT 'markdown',
    engine          VARCHAR(20) NOT NULL DEFAULT 'http',
    webhook_url     TEXT NULL,
    respect_robots  BOOLEAN NOT NULL DEFAULT FALSE,
    total           INT NOT NULL DEFAULT 0,
    completed       INT NOT NULL DEFAULT 0,
    failed          INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ NULL
);
```

**`batch_items` table:**

```sql
CREATE TABLE batch_items (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id    UUID NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',
                -- pending → running → completed | failed | cancelled
    result_path TEXT NULL,
    error       TEXT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL
);

CREATE INDEX idx_batch_items_batch_id ON batch_items (batch_id, status);
```

### 2. `job_runs.job_id` becomes nullable; add `batch_item_id`

**Migration on `job_runs`:**

```sql
ALTER TABLE job_runs
    ALTER COLUMN job_id DROP NOT NULL,
    ADD COLUMN batch_item_id UUID NULL REFERENCES batch_items(id);

-- Exactly one of job_id or batch_item_id must be non-null
ALTER TABLE job_runs
    ADD CONSTRAINT chk_job_runs_single_parent CHECK (
        (job_id IS NOT NULL) != (batch_item_id IS NOT NULL)
    );

CREATE INDEX idx_job_runs_batch_item_id
    ON job_runs (batch_item_id)
    WHERE batch_item_id IS NOT NULL;
```

**Why a check constraint rather than application logic:**
The mutual exclusion rule — a run belongs to exactly one parent — is an invariant that must hold regardless of which code path creates the row. A DB check constraint enforces this at insert/update time for all paths: ORM, raw SQL, admin scripts, future migrations.

### 3. Result consumer routing

The result consumer gains a routing branch based on which FK is set on the `job_runs` row:

```
result consumer receives completed/failed result
  → load job_runs row
  → if job_runs.job_id IS NOT NULL:
      → existing path (diff, LLM dispatch, job webhook)
  → if job_runs.batch_item_id IS NOT NULL:
      → update batch_items row (status, result_path, error)
      → increment batches.completed or batches.failed counter (atomic UPDATE)
      → if batches.completed + batches.failed == batches.total:
          → set batches.status = 'completed' or 'partial_failure'
          → fire batch.completed webhook
```

Cancellation follows the same pattern: `DELETE /batch/{id}` sets all `pending`/`running` `batch_items` rows to `cancelled`, and the result consumer discards results for cancelled items (same mechanism as regular job cancellation).

### 4. Workers are unchanged

Workers receive individual NATS messages with the standard fat message schema (ADR-004). The message for a batch item looks identical to a regular job message — no `batch_id` or `batch_item_id` in the NATS message itself. The result consumer handles batch aggregation after the worker completes.

The fat message carries `job_id = null` and a `run_id` for batch runs. Workers use only `run_id` for their result message, so this is transparent to them.

**Note:** `batch_item_id` is not added to the fat message. Workers don't need it — only the result consumer needs it, and the result consumer looks it up via `job_runs.batch_item_id` after receiving the `run_id`.

---

## Consequences

**Positive:**
- `GET /jobs` is unchanged — it queries `job_runs WHERE job_id IS NOT NULL`, which naturally excludes batch runs
- No synthetic `jobs` rows; the `jobs` table retains its template-only semantics (ADR-003 invariant preserved)
- Workers require zero changes — batch items are dispatched as standard NATS messages
- The check constraint enforces parent exclusivity at the DB level

**Negative:**
- `job_runs.job_id NOT NULL` constraint is dropped — existing queries that assume non-null `job_id` must be audited. The partial index `WHERE batch_item_id IS NOT NULL` ensures batch-item run lookups remain fast.
- The result consumer gains a routing branch — more logic in an already-complex consumer. This branch must be covered by integration tests.
- Any `JOIN jobs ON job_runs.job_id = jobs.id` in existing queries must be changed to `LEFT JOIN` or filtered with `WHERE job_runs.job_id IS NOT NULL` to avoid dropping batch runs from aggregate queries (e.g. admin stats).
