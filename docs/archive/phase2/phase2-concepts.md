# ScrapeFlow Phase 2 — Concepts & Rationale

> **Purpose:** Explains the *why* behind every major Phase 2 design decision.
> Use this document when onboarding engineers, revisiting decisions, or asking
> "why didn't we just do X instead?"
>
> Companion to: `phase2-engineering-spec.md`

---

## 1. Webhooks — Why Push Beats Pull

### The Problem: Polling Is Wasteful

When a scrape job finishes, something needs to act on the result — an ML pipeline fetches the data, a monitoring system logs the outcome, a downstream service triggers the next step. The naive approach is **polling**: your system keeps asking ScrapeFlow "is it done yet?" every few seconds.

```
Your system → GET /jobs/123 → { "status": "running" }   (wait 5s)
Your system → GET /jobs/123 → { "status": "running" }   (wait 5s)
Your system → GET /jobs/123 → { "status": "running" }   (wait 5s)
Your system → GET /jobs/123 → { "status": "completed" } ← finally
```

Every request that returns "running" is wasted. At scale — 100 users, each with 10 active jobs, polling every 5 seconds — that's 1,200 wasted API requests per minute before a single useful result is processed.

### The Solution: Webhooks Flip the Direction

A webhook is a configured HTTP endpoint on *your* system that *ScrapeFlow* calls when something happens. You register the URL once. ScrapeFlow does the work and knocks on your door when it's done.

```
Job completes
      │
      ▼
ScrapeFlow → POST https://your-pipeline.com/notify
             { "event": "job.completed", "job_id": "...", "result_path": "..." }
      │
      ▼
Your system processes immediately — no polling, no waiting
```

This is called an **event-driven** or **push** architecture. The producer (ScrapeFlow) pushes notifications to consumers (your systems) when events occur.

### Why Three Event Types

ScrapeFlow fires three events: `job.completed`, `job.failed`, `job.change_detected`.

We fire all three on every delivery. The receiving system decides which events to handle and ignores the rest. This is the standard webhook pattern (used by Stripe, GitHub, Shopify) — the sender doesn't try to filter for each consumer's preferences. One configuration, one delivery path, consumer handles routing.

`job.change_detected` is the most important for ScrapeFlow's ML pipeline use case — it means "data you were watching just changed, here's what changed." Your pipeline only needs to do work when this fires, not on every completed run.

---

## 2. HMAC Signing — Authenticating Webhook Payloads

### The Problem: Anyone Can POST to Your Endpoint

When ScrapeFlow sends a webhook to `https://your-pipeline.com/notify`, your server receives an HTTP POST. The problem: HTTP doesn't prove who sent a request. Anyone can POST to your endpoint — including attackers trying to inject fake "job.completed" events to trick your pipeline into processing fraudulent data.

### What HMAC Is

HMAC (Hash-based Message Authentication Code) lets two parties verify a message is authentic using a **shared secret** — a random string only they both know.

**The process:**

1. When you set `webhook_url` on a job, ScrapeFlow generates a random secret (`wh_sec_x7kP9...`) and shows it to you once. You store it in your system.

2. When ScrapeFlow sends a webhook, it signs the payload:
   ```python
   signature = hmac.new(secret, payload_bytes, sha256).hexdigest()
   # Adds header: X-ScrapeFlow-Signature: sha256=a3f9c2b1...
   ```

3. Your endpoint verifies:
   ```python
   expected = hmac.new(your_secret, request.body, sha256).hexdigest()
   if hmac.compare_digest(received_sig, f"sha256={expected}"):
       # authentic — process it
   else:
       return 401  # reject
   ```

An attacker can send a fake payload, but without the secret they can't produce the correct signature. Your endpoint rejects anything with a missing or wrong signature.

### Why `hmac.compare_digest` Instead of `==`

Regular string comparison (`==`) short-circuits — it stops the moment it finds a differing character. An attacker can measure how long comparisons take and deduce the correct signature one character at a time — a **timing attack**. `hmac.compare_digest` always takes the same amount of time regardless of where the strings differ, making this class of attack impossible.

### Why Per-Job, Not Per-User

Each job's webhook URL has its own secret. If one integration is compromised (the secret leaks), you rotate that one job's secret without affecting any other job. Per-user secrets would mean one leak exposes all your webhook integrations simultaneously.

---

## 3. Two-Stage LLM Pipeline — Why Go Scrapes and Python Extracts

### The Constraint: Go Has No Good LLM Ecosystem

The existing HTTP worker is Go. Go is excellent for HTTP clients — concurrent, fast, small memory footprint, great stdlib. But the LLM ecosystem is Python-first. The `openai` and `anthropic` SDKs are Python SDKs. Structured output handling (`response_format: json_schema`), retry logic, streaming — all battle-tested in Python, thin or non-existent in Go.

Adding LLM calls to the Go worker would mean writing raw HTTP calls to OpenAI's API, manually parsing responses, handling rate limits, and maintaining code that will drift from the Python SDK as the API evolves.

### The Solution: Separate Concerns by Language Strength

The Go worker is excellent at scraping — HTTP fetching, HTML parsing, MinIO storage. Keep it doing that for every job.

A separate Python LLM worker picks up where the Go worker left off — it reads the scraped HTML from MinIO, calls the LLM API, writes structured JSON back to MinIO.

```
ALL jobs:
  Go worker: fetch URL → store raw HTML → publish "completed"

LLM jobs only (conditional second stage):
  API result consumer: sees llm_config set → routes to LLM subject
  Python LLM worker: reads raw HTML → calls LLM → stores structured JSON → publish "completed"
```

### The Compounding Benefit for Change Detection

This design choice has a second-order benefit. Non-LLM jobs produce raw HTML — detecting meaningful changes requires stripping noise (ads, timestamps, rotating content). LLM jobs produce structured JSON matching the user's exact schema. Change detection on structured JSON is trivial:

```
Run 1: { "price": 49.99, "in_stock": true }
Run 2: { "price": 39.99, "in_stock": false }
Diff:  { "price": "49.99 → 39.99", "in_stock": "true → false" }
```

The LLM extraction step implicitly removes noise — if the user's schema doesn't include "timestamp" or "ad content," those fields never appear in the output and never trigger false change alerts. The two-stage design wasn't built for change detection, but makes it dramatically better as a side effect.

---

## 4. Pull Consumer — Why Playwright Can't Use a Push Subscription

### The Go Worker Uses Push (and That's Fine)

The Go HTTP worker uses a NATS push subscription — NATS pushes messages to the worker as fast as they arrive. This works because Go goroutines are cheap: each costs ~4KB of stack. Handling 50 concurrent HTTP requests uses ~200KB total. The worker can absorb bursts.

### Playwright Jobs Are 1000× More Expensive

Each Chromium browser page uses 150–300MB RAM. With a push subscription, NATS could push 10 Playwright jobs simultaneously before the worker has finished processing the first three. The worker would either:
- Start 10 browser pages (1.5–3GB RAM — the pod crashes), or
- Queue jobs internally (NATS thinks they're in-flight, AckWait expires, redelivers, chaos)

### Pull Consumer: Worker Controls Its Own Rate

With a pull consumer, the worker only fetches a message when it has capacity. A semaphore with `PLAYWRIGHT_MAX_WORKERS=3` means at most 3 browser pages are ever open simultaneously:

```
Worker has 1 free slot → pull 1 message → open 1 page → process → close page → free slot → pull next
```

NATS never pushes more work than the worker can handle. Memory usage stays bounded. This also solves the "unbounded concurrency" issue deferred from Phase 1 — pull consumers are the right model for any job with significant per-unit resource cost.

---

## 5. DB Polling Scheduler — Why Not APScheduler or k8s CronJob

### The Problem We're Solving

Users can create recurring jobs with cron expressions. Something needs to look at those expressions and dispatch jobs to NATS at the right time.

### Why Not APScheduler

APScheduler is a Python library that registers jobs in memory and wakes them up on schedule. It works perfectly for a single process. The problem: ScrapeFlow runs on k3s and can have multiple API replicas.

With two API instances both running APScheduler, both instances register the same cron jobs and both fire at the same time — the same scrape job dispatches twice, creates two `job_runs` rows, writes two results to MinIO. A rolling deploy produces a brief window where three instances run simultaneously. Every deployment causes triple-dispatch.

APScheduler has a Postgres-backed job store that can coordinate between instances, but it requires careful configuration and has its own failure modes when the lock expires mid-schedule.

### Why Not k8s CronJob

A k8s CronJob spins up a container on a schedule. It's clean infrastructure separation — the scheduler is entirely outside the application. But it doesn't eliminate the need for the same polling logic (query due jobs, dispatch to NATS). You're writing the same code, just in a separate container that takes 2–10 seconds to start up every minute.

k8s CronJobs make sense when the scheduled work has no parent service — nightly database cleanup, weekly report generation. When the work is tightly coupled to the API (it dispatches NATS messages using the same client code), keeping it in the API is simpler and cheaper.

### Why DB Polling with `SELECT FOR UPDATE SKIP LOCKED`

The polling loop runs every 60 seconds and queries:

```sql
SELECT * FROM jobs
WHERE schedule_cron IS NOT NULL
  AND schedule_status = 'active'
  AND next_run_at <= NOW()
FOR UPDATE SKIP LOCKED
```

`FOR UPDATE SKIP LOCKED` is a Postgres primitive built exactly for this pattern. `FOR UPDATE` places a row-level lock on each selected row. `SKIP LOCKED` means: if another connection already locked a row, skip it entirely (don't wait).

With two API instances polling simultaneously:
- Instance 1 locks job A, dispatches it, updates `next_run_at`, releases lock
- Instance 2 sees job A is locked, skips it, gets 0 rows, does nothing

Job A is dispatched exactly once. No distributed coordination needed beyond a single SQL clause. The database you already have handles the distributed locking for free.

---

## 6. `jobs` vs `job_runs` — Why Separate Tables

### The Original Model's Limitation

In Phase 1, `jobs` has `status`, `result_path`, and `error` directly on the row. One job = one execution. This works because every job was one-shot.

Phase 2 introduces recurring jobs — one job definition can produce hundreds of executions over its lifetime. If we kept execution state on the `jobs` row, we'd lose all history. The row would just reflect the latest run, with no way to query "what were the results of the last 10 runs?" or "when did this job start failing?"

### The Split

`jobs` becomes a pure **template** — it stores the definition (URL, engine, schedule, options). It doesn't change after creation except for scheduling housekeeping.

`job_runs` stores every **execution** — one row per run, with status, result, diff, timestamps. A one-shot job has exactly one `job_runs` row. A recurring job accumulates rows over time.

The API joins the two tables to return current status:

```sql
SELECT j.*, jr.status, jr.result_path, jr.diff_detected
FROM jobs j
LEFT JOIN LATERAL (
    SELECT * FROM job_runs WHERE job_id = j.id ORDER BY created_at DESC LIMIT 1
) jr ON true
```

`LATERAL` is a Postgres feature that lets the subquery reference the outer query's row. It efficiently fetches only the most recent `job_run` for each job without a full table scan.

### Why One-Shot Jobs Also Get a `job_runs` Row

Consistency. If one-shot jobs used the `jobs` table for status and recurring jobs used `job_runs`, every query would need to branch on job type. By creating a `job_runs` row for every execution (including one-shot), the result consumer, change detection, webhook delivery, and admin stats all work against a single table regardless of job type.

---

## 7. Fernet Encryption — Protecting Secrets at Rest and in Transit

### What Secrets We Store

Phase 2 introduces two new categories of sensitive data:
- **LLM API keys** — user's credentials for OpenAI, Anthropic, vLLM, etc.
- **Webhook secrets** — HMAC signing keys for webhook delivery

Both are stored in Postgres. Both appear in NATS messages in transit.

### Why Not Bcrypt / SHA-256 (Like API Keys)

ScrapeFlow API keys (`sf_...`) are hashed with SHA-256 before storage because they only need to be *verified* — you hash the incoming key and compare. You never need to recover the original value.

LLM API keys and webhook secrets need to be *used* — to call the LLM API, you need the plaintext key. To compute an HMAC signature, you need the plaintext secret. One-way hashing is irreversible, so it can't work here.

### Fernet: Reversible Symmetric Encryption

Fernet (from Python's `cryptography` package) is AES-128-CBC + HMAC-SHA256 with automatic IV generation. It produces encrypted ciphertext that can be decrypted back to plaintext given the same key.

```python
from cryptography.fernet import Fernet

key = Fernet.generate_key()        # 32-byte key, base64-encoded
fernet = Fernet(key)

ciphertext = fernet.encrypt(b"sk-openai-key...")
plaintext  = fernet.decrypt(ciphertext)   # → b"sk-openai-key..."
```

The encryption key lives in `LLM_KEY_ENCRYPTION_KEY` — a single env var shared between the API (encrypts on write) and the LLM worker (decrypts before use). It never touches the database.

### Why the Same Ciphertext Travels Through NATS

When the API dispatches an LLM job, it reads the already-encrypted ciphertext from `user_llm_keys.encrypted_api_key` and puts it directly in the NATS message — no decryption and re-encryption step. The LLM worker decrypts it at use time.

This means the plaintext LLM API key never exists in the API process's memory during dispatch. The API touches only ciphertext. The key is decrypted exactly once, in the LLM worker, immediately before the LLM call — then garbage collected.

### The Security Boundary

If an attacker has access to both the Postgres database AND the `LLM_KEY_ENCRYPTION_KEY` env var, they can decrypt all stored keys. But at that point, they have full system access anyway. The encryption protects against partial compromise — someone who dumps the database gets useless ciphertext, not usable API keys.

---

## 8. Normalised Text Diff vs JSON Diff — Different Strategies Per Job Type

### The Web Is Full of Noise

A naive approach to change detection: hash the raw HTML and compare. If the hash changes, something changed. The problem: nearly every page load produces a different hash even when nothing meaningful changed.

```html
<footer>Last updated: 2 hours ago</footer>          ← changes every hour
<div id="ad">Shop Nike Running Shoes</div>           ← changes with each ad rotation
<script>window._ts = 1743516000;</script>             ← changes every second
```

A full-page hash treats these as real changes. Your ML pipeline would trigger on ad rotations and timestamps — massive false positive rate.

### For Non-LLM Jobs: Normalised Text Diff

Strip HTML tags, collapse whitespace, then run `difflib.unified_diff` between the cleaned text of two consecutive runs. This catches genuine content changes (new paragraphs, changed prices in text form, updated data) while being less sensitive to markup changes and embedded scripts.

It's not perfect — "2 hours ago" → "3 hours ago" still counts as a change. But it's dramatically less noisy than raw HTML comparison and requires no knowledge of the page's structure.

### For LLM Jobs: Field-by-Field JSON Diff

When `llm_config` is set, the LLM worker extracts a structured JSON object matching the user's schema from every run. The user defined their schema — which fields they care about. The diff is a simple deep-equals comparison:

```python
for field in current_result:
    if current_result[field] != previous_result[field]:
        diff[field] = {"from": previous_result[field], "to": current_result[field]}
```

The ad rotation, the timestamp, the footer — the LLM discarded all of it when extracting the schema. If the user's schema doesn't include "ad_content," that field never appears in the output and can never trigger a false change alert. The extraction step is an implicit noise filter.

The diff output is also directly usable downstream — `{"price": {"from": 49.99, "to": 39.99}}` can go straight into a webhook payload, an ML pipeline, a Slack notification, without any further parsing.

---

## 9. NATS Subject Routing — Why Separate Subjects Per Worker Type

### The Alternative: One Subject, Worker Decides

We could keep `scrapeflow.jobs.run` as a single subject and include `"engine": "playwright"` in the message. Workers would consume all messages and skip ones they don't handle.

The problem: NATS WorkQueue retention deletes a message when it's acknowledged. If the Playwright worker picks up an HTTP job and acks it (to skip it), the HTTP worker never sees it. If both workers pull from the same subject and don't ack the ones they skip, the unacked messages redeliver to both workers repeatedly — a livelock.

### The Solution: Separate Subjects, Clean Routing

```
scrapeflow.jobs.run.http        → only HTTP workers subscribe
scrapeflow.jobs.run.playwright  → only Playwright workers subscribe
```

Each worker type consumes exactly the messages meant for it. No routing logic in the worker code — the subscription subject *is* the filter. Adding a new engine type in Phase 3 means adding a new subject and a new worker, with zero changes to existing workers.

The NATS stream uses `scrapeflow.jobs.>` (matches one or more tokens) to cover all current and future `run.*` subjects without needing to list each one explicitly. Future workers register new subjects automatically.

---

## 10. LLM Key Storage — Why Per-User Stored Keys Beat Per-Request Keys

### The Per-Request Alternative

The simplest design: user passes their LLM API key in every `POST /jobs` request. API encrypts it, puts it in the NATS message, never writes it to Postgres.

For a one-shot job, this works. But Phase 2 adds recurring/scheduled jobs. A job created today with `schedule_cron: "0 */6 * * *"` will run automatically every 6 hours. Nobody is there to pass the API key on each run — there's no request to attach it to.

### Per-User Storage Solves This

Users register their LLM credentials once via `POST /users/llm-keys`. Jobs reference the key by ID (`llm_key_id` inside `llm_config` JSONB). At dispatch time (including scheduler-triggered dispatches), the API resolves the key by ID and passes the ciphertext in the NATS message.

The user experience matches other production API platforms (OpenAI, Anthropic's own console) — register a credential once, reference it in configurations.

### The Hard Delete Decision

When a user deletes an LLM key, it's gone immediately — no soft-delete, no revoked flag. Jobs that reference the deleted key fail at dispatch with `error: "LLM key not found"`. The user chose to delete it — that's the expected consequence.

We chose hard delete over soft-delete to avoid accumulating sensitive data. Deleted credentials should be gone. The tradeoff is that recurring jobs break silently until the user creates a new key and updates their jobs. The admin panel makes this visible — failed jobs surface in operational stats.

---

## 11. MinIO `latest/` + `history/` Path Convention — Enabling Change Detection

### The Problem: Comparison Requires Two Versions

Change detection requires comparing the current run's result against the previous run's result. The worker writes one result per run. Where do previous results live?

If the worker overwrites the same path every run (`{job_id}.html`), only the latest result exists — you can't compute a diff because the previous version is gone.

If the worker writes a new path every run (`{job_id}/{timestamp}.html`), you accumulate results forever and must query to find the two most recent runs to compare.

### The Two-Path Convention

Workers write to **two paths** on every run:

```
scrapeflow-results/latest/{job_id}.{ext}          ← always overwritten
scrapeflow-results/history/{job_id}/{timestamp}.{ext}  ← append-only
```

`latest/` always contains the most recent result — fast, predictable, no querying needed. This is what webhook payloads reference in `result_path`.

`history/` accumulates all results over time, each timestamped. The diff algorithm reads the two most recent `history/` entries for a job: the current run and the run before it.

The 90-day retention cleanup runs nightly: it deletes `job_runs` rows older than the retention window from Postgres AND deletes the corresponding `history/` objects from MinIO. `latest/` objects are never deleted by the cleanup — they always reflect the current state of the scraped URL.

---

## 12. `SELECT FOR UPDATE SKIP LOCKED` — Multi-Instance Job Safety

This Postgres feature appears in two places in Phase 2: the scheduler loop and the webhook delivery loop. It's worth understanding in depth because it replaces what would otherwise require a distributed lock, a message queue, or a coordination service.

### The Problem: Two Instances, One Queue

Both the scheduler loop and the webhook delivery loop are background tasks running inside the API process. In production on k3s, you may run two or more API replicas. Both replicas run both loops simultaneously. Without coordination:

- Scheduler loop: both instances query due jobs, both dispatch the same job twice
- Webhook loop: both instances query pending deliveries, both POST to the same webhook URL

The naive fix is a distributed lock (Redis SETNX, ZooKeeper, etc.) — acquire a lock, do the work, release. But this adds infrastructure complexity and has its own failure modes (what if the process dies while holding the lock?).

### `FOR UPDATE SKIP LOCKED` — Built-In Solution

When a Postgres transaction selects rows `FOR UPDATE`, it acquires a row-level lock on each selected row. Other transactions trying to lock the same rows will wait (block) by default.

`SKIP LOCKED` changes the behavior: instead of blocking, the second transaction silently skips any rows that are already locked and returns only the rows it could lock immediately.

```
Instance 1:
  BEGIN;
  SELECT * FROM jobs WHERE next_run_at <= NOW() FOR UPDATE SKIP LOCKED;
  -- Locks job A, job B
  -- Dispatches A and B
  -- Updates next_run_at
  COMMIT;  -- releases locks

Instance 2 (running simultaneously):
  BEGIN;
  SELECT * FROM jobs WHERE next_run_at <= NOW() FOR UPDATE SKIP LOCKED;
  -- Tries to lock A → already locked → SKIPPED
  -- Tries to lock B → already locked → SKIPPED
  -- Gets 0 rows → does nothing
  COMMIT;
```

Job A and B are dispatched exactly once. No Redis, no ZooKeeper, no application-level retry loops. The database provides the coordination primitive for free, using infrastructure you already have.

The lock is held only for the duration of the transaction — typically milliseconds. Even if an instance crashes mid-transaction, Postgres automatically releases the lock when the connection drops.
