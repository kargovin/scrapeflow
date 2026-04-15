# ADR-005: Site Crawl BFS Coordinator

**Status:** Accepted
**Date:** 2026-04-15
**Deciders:** @karthik

---

## Context

PRD-007 introduces site crawl: given a seed URL, ScrapeFlow discovers and scrapes all reachable pages within the same domain up to configurable `max_depth` and `max_pages` limits. This is fundamentally different from the existing single-URL job model.

The existing orchestration pattern — API dispatches one NATS message, one worker executes, result consumer updates DB — handles one job → one execution cleanly. Site crawl requires a BFS loop: each completed page potentially enqueues N new pages, up to `max_depth` levels deep. Someone must own this loop.

Three options were evaluated:

| Option | Description |
|--------|-------------|
| **A. API background task** | The API result consumer maintains a per-crawl queue; on each page result it enqueues discovered links and dispatches next-level messages |
| **B. Dedicated coordinator process** | A new Python process owns the BFS queue per crawl, subscribes to crawl result messages, dispatches next pages |
| **C. Workers self-enqueue** | On completing a page, the worker publishes discovered links back to NATS |

Option C was eliminated immediately: it breaks the core ADR-001 invariant that workers are topology-ignorant. Workers do not know about crawl depth, `max_pages` limits, or visited-URL deduplication. Enforcing those constraints from inside a worker would give workers business logic they must never own.

The real decision was A vs B.

**Why Option A was rejected:**

Option A was initially preferred at homelab scale (API restarts are infrequent; losing a BFS queue is an acceptable failure). That constraint has been removed — Phase 3 targets a multi-tenant deployment where API rolling updates happen regularly. An API deploy would interrupt all in-progress crawls for all users simultaneously. Recovering would require persisting the entire BFS frontier to Postgres on every enqueue — at which point Option A has become Option B with worse separation of concerns and no independent lifecycle.

---

## Decisions

### 1. Dedicated Python coordinator process (Option B)

A new service at `coordinator/` in the monorepo owns all BFS coordination. It is a standalone Python process, separate from the API.

**Why Python (not Go):**
The coordinator needs SQLAlchemy models (to read crawl config, write crawl page results), a Postgres connection, a Redis client, and a NATS client. All three client libraries are already established in the Python API codebase. A Go coordinator would require duplicating all three integrations. Shared Python infrastructure is the lower-complexity choice.

**Coordinator responsibilities:**
1. On startup, subscribe to `scrapeflow.jobs.result` and filter for messages where `crawl_context` is non-null
2. On receiving a completed crawl page result:
   - Extract discovered links from the result
   - Filter links: same-origin, include/exclude path rules, not already visited
   - Check `max_depth` and `max_pages` limits
   - Insert new URLs into `crawl_queue` with status `pending`
   - Dispatch NATS messages for newly enqueued URLs (up to available worker capacity)
3. Maintain a dispatch loop: poll `crawl_queue` for `pending` rows and dispatch to workers
4. Detect crawl completion: when all `crawl_queue` rows for a `crawl_id` are terminal and no pending items remain, update `crawls.status = 'completed'` and fire the webhook

**The coordinator does NOT:**
- Handle regular (non-crawl) job results — these pass through the existing API result consumer unchanged
- Write to the `jobs` or `job_runs` tables for crawl pages — crawl pages use the `crawl_pages` table (see §2 below)
- Expose any HTTP endpoints

### 2. Postgres `crawl_queue` table for BFS queue

The BFS frontier is persisted in Postgres, not Redis.

**Why Postgres over Redis:**
- The coordinator already has a Postgres connection; no additional infrastructure dependency
- A Redis outage would silently drop the entire frontier for all active crawls simultaneously — a worse failure mode than slower Postgres writes
- Postgres survives coordinator restart, API restart, and full node reboot — any component can restart without losing crawl state
- The "table churn" concern (rows are short-lived) is mitigated by a partial index on `(crawl_id, status)` where `status = 'pending'`

**`crawl_queue` schema:**

```sql
CREATE TABLE crawl_queue (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    crawl_id      UUID NOT NULL REFERENCES crawls(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    depth         INT NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',
                  -- pending → dispatched → completed | failed | skipped
    crawl_page_id UUID NULL REFERENCES crawl_pages(id),
                  -- set when dispatched; links queue entry to its result row
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ NULL,
    completed_at  TIMESTAMPTZ NULL
);

CREATE INDEX idx_crawl_queue_pending
    ON crawl_queue (crawl_id, created_at)
    WHERE status = 'pending';

CREATE UNIQUE INDEX idx_crawl_queue_url
    ON crawl_queue (crawl_id, url);
    -- prevents duplicate URL enqueueing within a crawl
```

### 3. New tables: `crawls` and `crawl_pages`

Crawl jobs use their own tables, separate from `jobs`/`job_runs`. This keeps the existing job model clean.

**`crawls` table:**

```sql
CREATE TABLE crawls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    seed_url        TEXT NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'queued',
                    -- queued → running → completed | failed | cancelled | quota_exceeded
    max_depth       INT NOT NULL DEFAULT 3,
    max_pages       INT NOT NULL DEFAULT 100,
    include_paths   TEXT[] NULL,
    exclude_paths   TEXT[] NULL,
    ignore_sitemap  BOOLEAN NOT NULL DEFAULT FALSE,
    output_format   VARCHAR(20) NOT NULL DEFAULT 'markdown',
    engine          VARCHAR(20) NOT NULL DEFAULT 'http',
    webhook_url     TEXT NULL,
    respect_robots  BOOLEAN NOT NULL DEFAULT TRUE,
    schedule_cron   TEXT NULL,
    total_queued    INT NOT NULL DEFAULT 0,
    total_completed INT NOT NULL DEFAULT 0,
    total_failed    INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ NULL
);
```

**`crawl_pages` table:**

```sql
CREATE TABLE crawl_pages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    crawl_id    UUID NOT NULL REFERENCES crawls(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    depth       INT NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',
                -- pending → running → completed | failed
    result_path TEXT NULL,   -- MinIO history/ path
    error       TEXT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL
);

CREATE INDEX idx_crawl_pages_crawl_id ON crawl_pages (crawl_id, status);
```

### 4. NATS subjects for crawl pages

Crawl page dispatches reuse existing worker subjects. The `crawl_context` sub-object in the fat message (ADR-004) identifies the message as a crawl page — workers process it identically to a regular job, but the coordinator (not the API result consumer) handles the result.

The API result consumer routes based on `crawl_context` presence:
- `crawl_context` is null → existing result consumer path
- `crawl_context` is non-null → pass to coordinator result handler (via a shared NATS subscription filter or an internal routing check)

No new NATS subjects are required.

---

## Consequences

**Positive:**
- Independent lifecycle: coordinator can restart without interrupting the API or workers
- API rolling deploys do not abort in-progress crawls
- BFS queue is durable: survives any single component failure
- Workers are unchanged — they process crawl pages identically to regular jobs

**Negative:**
- New monorepo service (`coordinator/`) with its own Dockerfile, k8s Deployment, and CI build step
- Coordinator must be deployed alongside the API — a missing coordinator means crawl results are processed but the BFS loop never advances (crawls stall, not fail)
- The `crawl_queue` table generates high write/delete churn for large crawls; the partial index on `status = 'pending'` is essential for query performance
