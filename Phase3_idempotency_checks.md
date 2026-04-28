# Phase 3 — Idempotency Audit: Message Consumers

> **Date:** 2026-04-28
> **Scope:** All production message consumer code — `api/app/core/result_consumer.py`, `api/app/core/webhooks.py`, `api/app/core/quota.py`, `coordinator/coordinator/result_handler.py`, `coordinator/coordinator/dispatcher.py`
> **Trigger:** NATS JetStream guarantees at-least-once delivery. A crash between `db.commit()` and `msg.ack()` causes redelivery. Every state mutation in a consumer must be safe to run twice.

---

## Background

The canonical idempotency failure pattern in this codebase is:

```
fetch entity → mutate DB → commit → [crash] → NATS redelivers → fetch entity → mutate DB again
```

The coordinator's `result_handler.py` already implements the correct defence at lines 130–139 — an explicit terminal-status guard that turns the second execution into a no-op before any mutation fires. The API-side `result_consumer.py` was written without equivalent guards and is the primary source of risk.

---

## Findings (ordered by severity)

---

### Finding 1 — Redelivered scrape "completed" misidentified as LLM completion

**Severity:** Critical
**Patterns:** status overwrite without terminal check + blind counter increment + external side effect
**File:** `api/app/core/result_consumer.py`

#### What happens

When a job has LLM processing enabled:

1. `_handle_scrape_completed` sets `run.status = "processing"`, increments storage quota, publishes to the LLM NATS subject → commits → acks.
2. NATS redelivers the original scrape `"completed"` message.
3. The `elif worker_status == "completed" and run.status == "processing":` branch matches → calls `_handle_llm_completed`.
4. The code treats the redelivered scrape result as if the LLM had finished.

#### Second-execution harm

- Storage quota incremented a second time via `_try_increment_storage`
- `run.result_path` overwritten with the raw scrape path, discarding the LLM result that arrives later
- A `job.completed` webhook fires immediately to the user's endpoint
- When the real LLM result arrives, `_handle_llm_completed` runs a third time → second `job.completed` webhook

#### Fix

Add a `source: "scrape" | "llm"` discriminator to the `ResultMessage` schema so the two completed messages are structurally distinct. Gate `_handle_llm_completed` on `source == "llm"` only.

---

### Finding 2 — Terminal run flipped back to `failed` on redelivery

**Severity:** Critical
**Pattern:** status overwrite without terminal check
**File:** `api/app/core/result_consumer.py` — `else` arm of `_handle_job_result`

#### What happens

```python
else:
    run.status = "failed"
    run.error = error
    run.completed_at = datetime.now(UTC)
    ...
    create_webhook_delivery(... event="job.failed" ...)
```

A `"failed"` result message is redelivered after the run is already in a terminal state. No guard prevents the else arm from executing again.

#### Second-execution harm

- A successfully-completed run is written back to `status = "failed"` in Postgres
- A duplicate `job.failed` webhook is queued and delivered — irreversible on the receiver's system

#### Fix

At the top of `_handle_job_result`, early-return on any terminal state. The `cancelled` check already exists; extend it:

```python
if run.status in ("completed", "failed", "cancelled"):
    return
```

---

### Finding 3 — Batch counters double-incremented on redelivery

**Severity:** Critical
**Patterns:** blind counter increment + status overwrite + external side effect
**File:** `api/app/core/result_consumer.py` — `_handle_batch_result` else arm

#### What happens

```python
# No terminal guard present
item.status = "failed"
run.status = "failed"
UPDATE batches SET failed = failed + 1 WHERE id = :id RETURNING completed, failed
```

Two redelivery paths hit this:

1. A `"failed"` result for a batch item is redelivered directly.
2. A redelivered `"completed"` message, when `run.status` is already `"completed"`, falls through all elif branches (none match a terminal run) and lands in the else arm — wrongly incrementing `batches.failed`.

#### Second-execution harm

- `batches.failed` (or `batches.completed`) exceeds `batches.total`
- When `completed + failed == total` triggers, a duplicate `batch.completed` webhook fires
- UI shows impossible completion percentages

#### Fix

Same early-return guard at the top of `_handle_batch_result`:

```python
if run.status in ("completed", "failed", "cancelled"):
    return
```

---

### Finding 4 — `worker_status="running"` can overwrite a terminal run

**Severity:** High
**Pattern:** status overwrite without terminal check
**File:** `api/app/core/result_consumer.py` — `_handle_job_result` running branch

#### What happens

```python
if worker_status == "running":
    run.status = "running"
    run.started_at = started_at
    run.nats_stream_seq = nats_seq
```

NATS redelivers an old `"running"` message after the job has already reached `"completed"`. The running branch has no terminal guard.

#### Second-execution harm

- `run.status` reverts from `"completed"` to `"running"` in Postgres
- `run.nats_stream_seq` is overwritten — the advisory subscriber (`api/app/core/advisory.py`) may later match this seq and mark the finished run as stalled, then fail it
- UI shows a completed job as still in-progress

#### Fix

The coordinator's `result_handler.py` (line 131) already implements the model pattern. Mirror it here:

```python
if worker_status == "running" and run.status not in ("completed", "failed", "cancelled"):
    ...
```

---

### Finding 5 — Storage quota increments are non-idempotent

**Severity:** High (compounds with Findings 1–3)
**Pattern:** blind counter increment
**File:** `api/app/core/quota.py` — `increment_storage_bytes`

#### What happens

```sql
INSERT INTO user_quotas ... ON CONFLICT (user_id) DO UPDATE
SET storage_bytes_used = user_quotas.storage_bytes_used + :size
```

The `ON CONFLICT` deduplicates the upsert row, but **not the increment itself**. Every redelivery that reaches this call path adds `:size` to the user's quota again regardless of whether the MinIO object was actually written twice.

#### Second-execution harm

Users are falsely reported as exceeding their storage quota. Subsequent jobs are rejected at `check_storage_quota` even though no additional MinIO bytes were written.

#### Fix

**Primary fix:** the upstream terminal guards (Findings 1–3) make this path unreachable on redelivery.

**Belt-and-suspenders:** add a `run.storage_accounted_at` timestamp column set atomically with the first increment; skip the increment if already set.

---

### Finding 6 — Webhook delivery rows inserted without idempotency key

**Severity:** High
**Patterns:** insert without conflict handling + external side effect
**Files:** `api/app/core/webhooks.py` — `create_webhook_delivery` and `create_batch_webhook_delivery`

#### What happens

```python
db.add(WebhookDelivery(...))   # unconditional — no unique constraint on the table
```

The `webhook_deliveries` table has no `UNIQUE (run_id, event)` constraint. Every redelivery that reaches a `create_webhook_delivery` call site inserts a fresh `pending` row. The delivery loop POSTs all pending rows independently.

#### Second-execution harm

Users receive N copies of the same `job.completed` / `job.failed` / `batch.completed` webhook. For non-idempotent receivers (payment triggers, ticketing systems, alerting pipelines), this causes real downstream failures that cannot be rolled back.

#### Fix (two layers)

**Layer 1 — database constraint:**
```sql
CREATE UNIQUE INDEX idx_webhook_deliveries_dedup
  ON webhook_deliveries (run_id, event)
  WHERE status != 'exhausted';
```

**Layer 2 — insert guard:**
Change the ORM add to `INSERT ... ON CONFLICT DO NOTHING`.

The terminal guards from Findings 1–3 cover the consumer redelivery path. The index is defence-in-depth for any future code path that calls `create_webhook_delivery` without going through the consumer.

---

### Finding 7 — `check_completion` in the coordinator doesn't guard against already-terminal crawl

**Severity:** Low (latent / structural risk)
**Pattern:** status overwrite without terminal check
**File:** `coordinator/coordinator/result_handler.py` — `check_completion`

#### What happens

```python
def check_completion(...):
    if active_count == 0:
        crawl.status = "completed"   # no check: was it already completed?
        return True
```

**Current mitigation:** `_process_crawl_result` returns early (lines 138–139) before calling `check_completion`, so the redelivery path is currently blocked. This is a structural latent risk — any future caller that invokes `check_completion` on an already-terminal crawl will re-fire the crawl webhook.

#### Fix

```python
if active_count == 0 and crawl.status not in _TERMINAL_QUEUE_STATUSES:
    crawl.status = "completed"
    return True
return False
```

---

## What passes the audit

| Location | Why it's safe |
|---|---|
| `coordinator/result_handler.py:130–139` | Explicit terminal-status guard before any mutation — the model pattern to replicate everywhere |
| `api/app/core/advisory.py:27–43` | Filters on `status.in_(["pending","running","processing"])` — cannot flip a terminal run |
| `api/app/core/webhook_loop.py:78–90` | Re-fetches and re-checks `status == "pending"` before delivering — guards the SKIP LOCKED race |
| Go HTTP worker / Playwright worker / LLM worker | No DB writes; MinIO path is deterministic per run; result publish is safe to repeat |
| `coordinator/dispatcher.py` — `_enqueue_url` | Uses `ON CONFLICT DO NOTHING`; only increments `total_queued` when `rowcount > 0` |

---

## Recommended fix order

| Priority | Finding | Change |
|---|---|---|
| 1 | Findings 2, 3, 4, 5 | Add a single terminal-status early-return at the top of `_handle_job_result` and `_handle_batch_result`. One change neutralises four findings. |
| 2 | Finding 1 | Add `source: "scrape" \| "llm"` discriminator to `ResultMessage`; gate `_handle_llm_completed` on `source == "llm"`. Terminal guard alone cannot fully close this. |
| 3 | Finding 6 | Add `UNIQUE (run_id, event)` partial index on `webhook_deliveries` + `ON CONFLICT DO NOTHING` in both insert helpers. |
| 4 | Finding 7 | Harden `check_completion` with terminal-status check. Single-replica deployment makes this low urgency. |

---

## The root cause pattern

The coordinator and the API result consumer were written independently. The coordinator got the terminal-status guard; the API consumer did not. In distributed systems, this asymmetry is common — the fix in one component teaches you the pattern, but the other component accumulates the same class of bug in isolation.

The structural fix is a shared guard utility (or at minimum a lint rule) enforced at every consumer entry point, so the correct pattern cannot be omitted silently in future consumers.
