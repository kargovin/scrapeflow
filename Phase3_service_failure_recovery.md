# Phase 3 — Service Failure & Recovery Audit

> **Date:** 2026-04-28
> **Scope:** End-to-end recovery behaviour when any service dies mid-operation —
> coordinator, API background loops, workers, scheduler, webhook loop.
> **Companion:** `Phase3_idempotency_checks.md` covers NATS redelivery idempotency.
> This document covers the broader question: if a service is killed mid-job,
> mid-crawl, or mid-batch, does the system recover to a consistent state?

Status legend: `[ ]` open · `[x]` fixed · `[~]` partially mitigated

---

## Background

The failure modes differ by service type:

| Service type | Recovery mechanism |
|---|---|
| Workers (Go, Playwright, LLM) | Stateless — NATS JetStream redelivers the scrape message to another worker after AckWait |
| API background loops (result_consumer, scheduler, webhook_loop) | DB-polled or NATS-pulled — restart re-reads pending work from Postgres |
| Coordinator | Stateful per-crawl — BFS state is persisted in Postgres, but in-flight FK references can break if the process dies |

The canonical failure window is the same in all services:

```
db.commit()   ← state change is durable
[crash]
msg.ack()     ← never reached → NATS redelivers
```

The companion document covers what happens when that redelivery is processed by
application code without an idempotency guard. This document covers what happens
at the infrastructure level — what each service's restart looks like and whether
data is left in an inconsistent state regardless of redelivery.

---

## Part 1 — Coordinator

### [x] Finding C1 — `reenqueue_stalled` creates orphaned CrawlPage rows

**Severity:** High
**File:** `coordinator/coordinator/dispatcher.py` — `reenqueue_stalled`
**Fixed:** commit `3afe49d` (prod review #51)

#### The invariant

Each `CrawlQueueItem` is linked to exactly one `CrawlPage` via `crawl_page_id` FK.
The `CrawlPage` is created when the item is dispatched; the FK is set atomically
in the same transaction. This 1:1 is load-bearing — `result_handler.py` updates
`CrawlQueueItem` by looking up `crawl_page_id`, and `check_completion` counts
non-terminal `CrawlQueueItem` rows to decide when the crawl is done.

#### What breaks

`reenqueue_stalled` resets stale `"dispatched"` items:

```python
.values(status="pending", dispatched_at=None, crawl_page_id=None)
```

It nulls `crawl_page_id` but **leaves the linked `CrawlPage` row intact**.
The dispatcher re-picks the now-`pending` item and creates a second `CrawlPage`
(page2) for the same URL. page1 is now an orphan — no FK points to it.

When the original worker delivers its result for `page1.id`:

```
update(CrawlQueueItem) where crawl_page_id == page1.id → 0 rows
page1.status = "completed"          # page updated
crawl.total_completed += 1          # counter incremented
links extracted from page1 HTML     # new queue items added
```

Later, the re-dispatched worker completes for `page2.id`:

```
page2.status = "completed"
crawl.total_completed += 1          # double-counted
links extracted again               # safe: ON CONFLICT DO NOTHING on crawl_queue
```

Note: `crawl_pages` has no `UNIQUE (crawl_id, url)` constraint, so Postgres
silently allows two rows for the same URL in the same crawl.

#### Fix

In `reenqueue_stalled`: SELECT the `crawl_page_id` values about to be nulled →
DELETE those `CrawlPage` rows first → then run the existing UPDATE. The old
worker result now hits `db.get(CrawlPage, page1.id) == None` → early return
(already handled by the existing None check in `_process_crawl_result`).

---

### [x] Finding C2 — Commit-before-ack causes double-counted crawl counters

**Severity:** Medium
**File:** `coordinator/coordinator/result_handler.py` — `_process_crawl_result`
**Fixed:** commit `e1bb016` (prod review #52)

#### What breaks

The coordinator's result loop:

```python
await _process_crawl_result(db, minio, data)  # db.commit() inside
await db.commit()
await msg.ack()    ← crash here
```

NATS redelivers the terminal message. `_process_crawl_result` runs again.
No guard → `crawl.total_completed` (or `total_failed`) incremented a second time,
corrupting the counter that surfaces in `GET /crawls/{id}`.

#### Fix

Early-return guard after the `"running"` branch:

```python
if page.status in ("completed", "failed"):
    return
```

---

### [x] Finding C3 — `"running"` message overwrites terminal status under multi-replica

**Severity:** Medium (High under multi-replica deployment)
**File:** `coordinator/coordinator/result_handler.py` — `_process_crawl_result`
**Fixed:** commit `ed931b5` (prod review #52)

#### What breaks

With multiple coordinator replicas sharing the same durable NATS consumer:

1. Instance A processes `"running"` → commits (`page.status = "running"`) → crashes (no ack)
2. Instance B fetches `"completed"` (NATS pull consumers can advance past unacked messages) → `page.status = "completed"` → acks
3. NATS redelivers `"running"` to any instance → original code: `page.status = "running"` — overwrites `"completed"`

The page is stuck in `"running"`. `check_completion` counts it as non-terminal and the
crawl can never complete.

Also triggered by a `"running"` message reaching `MaxDeliver` — NATS gives up
redelivering it and advances the sequence, allowing `"completed"` to be delivered
and processed first.

#### Fix

Skip the status write if the page is already terminal:

```python
if worker_status == "running":
    if page.status not in ("completed", "failed"):
        page.status = "running"
    return
```

---

### [~] Finding C4 — Publish-before-commit race in dispatcher

**Severity:** Low (currently safe)
**File:** `coordinator/coordinator/dispatcher.py` — `_dispatch_batch`

#### What happens

```python
db.flush()          # CrawlPage created in transaction (page1.id allocated)
js.publish(...)     # NATS message sent with page1.id in payload
db.commit()         # crash here → transaction rolls back, page1 never persists
```

The worker receives the message, scrapes the URL, publishes a result for `page1.id`.
The result handler: `db.get(CrawlPage, page1.id) → None` → early return.
The `CrawlQueueItem` (still `"pending"` after rollback) is re-dispatched normally.

**Currently safe** — the existing None check in `_process_crawl_result` absorbs this.
No data corruption; one extra worker scrape is wasted.

---

### [ ] Finding C5 — `check_completion` has no guard against already-terminal crawl

**Severity:** Low (latent / structural)
**File:** `coordinator/coordinator/result_handler.py` — `check_completion`

#### What happens

```python
if active_count == 0:
    crawl.status = "completed"   # no check: was it already "completed"?
    return True
```

Current mitigation: `_process_crawl_result` returns early before calling
`check_completion` when the page is already terminal (Finding C2 fix). The
latent risk is any future caller that invokes `check_completion` on an already-
completed crawl would fire `enqueue_crawl_webhook` a second time, inserting a
duplicate `WebhookDelivery` row.

#### Fix

```python
if active_count == 0 and crawl.status not in ("completed", "cancelled"):
    crawl.status = "completed"
    crawl.completed_at = datetime.now(UTC)
    return True
return False
```

Also see: companion document Finding 7 (same root cause, same fix wording).

---

## Part 2 — Workers (Go / Playwright / LLM)

Workers are stateless — no in-memory per-job state survives a crash. Recovery
is entirely NATS-driven.

| Crash point | DB state left behind | Recovery path |
|------------|---------------------|---------------|
| Before publishing `"running"` | `job_runs.status = "pending"` | NATS redelivers scrape message; scheduler `_recover_stale_pending` resets stale pending runs as fallback |
| After `"running"` published, before `"completed"` | `job_runs.status = "running"` | NATS redelivers scrape message; new worker processes and publishes `"completed"` |
| After MinIO write, before NATS result publish | MinIO object written; run still `"running"` | NATS redelivers; new worker overwrites same MinIO path (path is deterministic per `run_id`) — safe |

**Edge case:** Worker A publishes `"running"`; the API result consumer crashes before
writing it to the DB; `job_runs.status` stays `"pending"`. The scheduler treats it
as stale and re-dispatches. Now two workers are active for the same `run_id`. When
both publish `"completed"`, the second delivery hits an already-terminal run — caught
by the terminal guard recommended in the companion document (Finding 2/4). Without
that guard, the second result corrupts the run.

---

## Part 3 — API Background Loops

### `result_consumer_loop`

The same commit-before-ack window as the coordinator. The API result consumer
is more complex (LLM routing, batch routing, dedup, quota) with more mutation
sites — meaning more places where the second execution causes harm.

Covered in full by the companion document (`Phase3_idempotency_checks.md`
Findings 1–4). The fix pattern is identical to what we implemented for the
coordinator: terminal-status early-return at the top of every result handler.

### `scheduler_loop`

Polls the DB for due jobs on every tick — no in-memory state to lose.

The only failure window is:

```python
await js.publish(scrape_message)   # NATS message sent
run.status = "pending"
await db.commit()                  # crash here
```

On crash before commit: the run row is never created. The worker receives the
NATS message, publishes a result for a `run_id` that does not exist in `job_runs`.
The result consumer calls `db.get(JobRun, run_id)` — if this returns `None`, it
should return early. **Worth verifying this None check exists** — a missing guard
here would cause an `AttributeError` on every such redelivery.

On crash after commit: `job_runs.status = "pending"`, scrape message already
published. Normal delivery path — no issue.

### `webhook_delivery_loop`

Polls `webhook_deliveries` for rows with `next_attempt_at <= now`. On restart:
re-polls, picks up pending rows. The only failure window is:

```
POST <webhook_url>             # HTTP request sent — irreversible
UPDATE webhook_deliveries ...  # crash here
```

On restart: the row is still `"pending"` → re-attempted → webhook fires twice.
This is acceptable by HTTP webhook convention (receivers should be idempotent).
The companion document Finding 6 addresses the distinct risk of duplicate
`WebhookDelivery` rows being inserted in the first place.

---

## Recovery Matrix

| Service | Fails mid-operation | DB state after crash | Recovers correctly? |
|---------|-------------------|---------------------|-------------------|
| Go/Playwright/LLM worker | Before result published | `job_runs.status = "running"` or `"pending"` | Yes — NATS redelivers |
| Coordinator | Mid-dispatch (before commit) | `CrawlQueueItem = "pending"` | Yes — publish-before-commit is safe (C4) |
| Coordinator | Mid-dispatch (after commit) | `CrawlQueueItem = "dispatched"`, `CrawlPage` exists | Yes — reenqueue_stalled + delete (C1 fix) |
| Coordinator | After result commit, before ack | `CrawlPage = "completed"`, unacked message | Yes — idempotency guard (C2 fix) |
| Coordinator (multi-replica) | Out-of-order delivery | `CrawlPage` could revert to `"running"` | Yes — running-branch guard (C3 fix) |
| API result_consumer | After DB commit, before ack | Run terminal, unacked message | No — see companion doc Findings 1–4 |
| API scheduler | After js.publish, before commit | Orphaned NATS message | Probably — depends on None check in result_consumer |
| API webhook_loop | After HTTP send, before row update | Row still `"pending"` | Acceptable — webhook fires twice |

---

## Fix Order

| Priority | Finding | File | Status |
|----------|---------|------|--------|
| 1 | Companion doc Findings 2, 3, 4 — terminal-status guard | `result_consumer.py` | `[ ]` open |
| 2 | Companion doc Finding 1 — scrape vs LLM source discriminator | `ResultMessage` schema + workers | `[ ]` open |
| 3 | Companion doc Finding 6 — webhook delivery dedup index | new migration | `[ ]` open |
| 4 | C5 — `check_completion` terminal-status guard | `result_handler.py` | `[ ]` open |
| 5 | Verify `result_consumer.py` returns early on `None` `job_run` (scheduler crash path) | `result_consumer.py` | `[ ]` needs audit |
| — | C1 — orphaned CrawlPage on reenqueue | `dispatcher.py` | `[x]` fixed #51 |
| — | C2 — commit-before-ack in coordinator | `result_handler.py` | `[x]` fixed #52 |
| — | C3 — running-branch overwrite multi-replica | `result_handler.py` | `[x]` fixed #52 |
