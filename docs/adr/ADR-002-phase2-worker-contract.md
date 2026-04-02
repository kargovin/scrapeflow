# ADR-002: Phase 2 Worker Contract

**Status:** Accepted
**Date:** 2026-04-02
**Deciders:** @karthik
**Supersedes:** ADR-001 (subjects and message schemas only — ack timing, retry policy, and cancellation principles are unchanged)

---

## Context

ADR-001 defined the Phase 1 worker contract: a single Go HTTP scraper worker consuming from `scrapeflow.jobs.run`, writing to MinIO, and publishing results to `scrapeflow.jobs.result`. Phase 1 is complete.

Phase 2 adds two new workers — a Python Playwright worker and a Python LLM worker — and introduces recurring jobs (run history). This requires changes to:

1. **NATS stream subjects** — each worker type needs its own subject; the Phase 1 single subject is not adequate for routing
2. **Message schemas** — every message must carry a `run_id` (new `job_runs` table from Phase 2 DB migration); scrape workers must include `nats_stream_seq` on running messages for MaxDeliver advisory handling; MinIO paths shift to a `history/` convention for per-run immutability
3. **Worker consumption model** — push subscriptions (Phase 1) are replaced with pull consumers and a bounded worker pool to cap concurrency

ADR-001 principles are preserved: fat messages (workers are DB-ignorant), ack-after-MinIO-write, NATS-managed retries, cancellation enforced by the API result consumer.

---

## Decisions

### 1. NATS Stream Subject Change

**Before (Phase 1):**
```
Stream:   SCRAPEFLOW
Subjects: scrapeflow.jobs.run, scrapeflow.jobs.result
```

**After (Phase 2):**
```
Stream:   SCRAPEFLOW
Subjects: scrapeflow.jobs.>
```

The `>` wildcard matches all subjects with one or more tokens after `scrapeflow.jobs.`. This covers all current subjects and any future ones (e.g. `scrapeflow.jobs.run.v2`) without stream reconfiguration.

**Migration (dev):** `docker compose down -v && docker compose up -d` recreates all volumes including the NATS stream. The `nats-init` command is updated to idempotent create-or-edit:
```bash
nats stream info SCRAPEFLOW --server nats:4222 \
  && nats stream edit SCRAPEFLOW --subjects 'scrapeflow.jobs.>' --server nats:4222 \
  || nats stream add SCRAPEFLOW \
       --subjects 'scrapeflow.jobs.>' \
       --retention work --max-deliver 3 \
       --storage file --replicas 1 \
       --server nats:4222
```

**Migration (production — before staging exists):** Use `nats stream edit` for in-place subject update, or delete and recreate the stream (stream-level operation does not touch other volumes).

---

### 2. Updated Subjects

| Subject | Publisher | Consumer | Purpose |
|---------|-----------|----------|---------|
| `scrapeflow.jobs.run.http` | API | Go HTTP worker | HTTP scrape jobs |
| `scrapeflow.jobs.run.playwright` | API | Python Playwright worker | JS-rendered scrape jobs |
| `scrapeflow.jobs.llm` | API result consumer | Python LLM worker | LLM structured extraction |
| `scrapeflow.jobs.result` | All workers | API result consumer | Job outcomes (unchanged subject) |

`constants.py` changes:
```python
# Replace NATS_JOBS_RUN_SUBJECT with:
NATS_JOBS_RUN_HTTP_SUBJECT       = "scrapeflow.jobs.run.http"
NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT = "scrapeflow.jobs.run.playwright"
NATS_JOBS_LLM_SUBJECT            = "scrapeflow.jobs.llm"
NATS_JOBS_RESULT_SUBJECT         = "scrapeflow.jobs.result"   # unchanged
```

**Why separate subjects per worker type:** Workers subscribe to a specific subject — routing logic stays out of the workers entirely. A Go HTTP worker that received a Playwright job would silently produce wrong output (no JS rendering) with no error. Subject-based routing makes invalid dispatch a NATS-level delivery failure (no consumer for the subject), not a silent correctness bug.

**Why `scrapeflow.jobs.llm` is dispatched by the result consumer (not the API):** LLM extraction is conditional and sequential — it only runs after a scrape worker completes and uploads the raw content to MinIO. The result consumer is the only component that knows both that a scrape completed successfully AND that the job has `llm_config` set. Having the API dispatch LLM jobs directly would require the API to subscribe to result events, duplicating result consumer logic.

---

### 3. Updated Message Schemas

#### `scrapeflow.jobs.run.http` / `scrapeflow.jobs.run.playwright` — Job dispatch (API → worker)

```json
{
  "job_id":  "uuid-v4",
  "run_id":  "uuid-v4",
  "url":     "https://example.com",
  "output_format": "html | markdown | json",
  "playwright_options": {
    "wait_strategy":   "load | domcontentloaded | networkidle",
    "timeout_seconds": 60,
    "block_images":    false
  }
}
```

`playwright_options` is present only in Playwright dispatch messages. The Go HTTP worker ignores unknown fields.
`run_id` is new — workers include it in all result messages so the API result consumer can target the exact `job_runs` row without a DB lookup by `job_id`.

#### `scrapeflow.jobs.llm` — LLM dispatch (API result consumer → LLM worker)

```json
{
  "job_id":          "uuid-v4",
  "run_id":          "uuid-v4",
  "raw_minio_path":  "scrapeflow-results/history/uuid/1743516000.html",
  "provider":        "anthropic | openai_compatible",
  "encrypted_api_key": "<fernet-ciphertext>",
  "base_url":        "https://vllm.example.com/v1",
  "model":           "Qwen/Qwen2.5-72b-Instruct",
  "output_schema":   { }
}
```

`encrypted_api_key` is the Fernet ciphertext from `user_llm_keys.encrypted_api_key`. The LLM worker decrypts it with `LLM_KEY_ENCRYPTION_KEY` at call time — the plaintext key is never stored or logged and goes out of scope immediately after the LLM call.

#### `scrapeflow.jobs.result` — Job outcome (all workers → API result consumer)

```json
{
  "job_id":          "uuid-v4",
  "run_id":          "uuid-v4",
  "status":          "running | completed | failed",
  "minio_path":      "scrapeflow-results/history/uuid/1743516000.json",
  "nats_stream_seq": 42,
  "error":           "optional, present only when status=failed"
}
```

**`run_id`** — new field. Required on all messages. The result consumer uses it to update the exact `job_runs` row without querying by `job_id`.

**`nats_stream_seq`** — present **only on `status: "running"` messages**. The worker reads this from `msg.Metadata().Sequence.Stream` before publishing. The result consumer stores it on `job_runs.nats_stream_seq`. The MaxDeliver advisory subscriber uses this value to identify and fail a stalled run — NATS advisory messages contain only `stream_seq`, no `job_id` or `run_id`.

**`minio_path`** — stores the `history/` path (see §4 below). Present only on `status: "completed"` messages.

**`error`** — present only on `status: "failed"` messages.

---

### 4. MinIO Path Convention

**Before (Phase 1):**
```
{bucket}/{job_id}.{ext}
e.g. scrapeflow-results/550e8400-e29b-41d4-a716-446655440000.html
```

**After (Phase 2):**
```
latest/{job_id}.{ext}       — always overwritten; reflects current state
history/{job_id}/{unix_timestamp}.{ext}   — append-only; one object per run
```

Workers write to **both paths** on every run. The `history/` path is published in the result message as `minio_path` and stored in `job_runs.result_path` — it is the immutable, per-run result. The `latest/` path is always derivable as `latest/{job_id}.{ext}` and does not need to be stored.

**Why two paths:** Change detection (diff algorithm) needs to retrieve two consecutive completed runs by their `result_path`. Overwriting a single path on every run would leave no prior result to diff against. The `history/` path makes each run independently addressable. The `latest/` path is a convenience — webhook payloads and human inspection always have a stable URL to the most recent result.

---

### 5. Pull Consumer Replacing Push Subscription

Phase 1 used NATS push subscriptions, which dispatch messages to goroutines without bound. Under load, this creates unbounded goroutine growth — a single slow HTTP target can stall all available goroutines.

Phase 2 workers use **pull consumers with a semaphore worker pool**:

```go
sub, err := js.PullSubscribe(subject, durableName, nats.MaxDeliver(cfg.NATSMaxDeliver))
sem := make(chan struct{}, cfg.WorkerPoolSize)  // default: runtime.NumCPU()

for {
    available := cap(sem) - len(sem)
    if available == 0 {
        time.Sleep(100 * time.Millisecond)
        continue
    }
    // Fetch only as many as there are free slots.
    // Fetching more than capacity causes messages to wait in-process while
    // NATS's AckWait timer runs — leading to spurious redelivery.
    msgs, err := sub.Fetch(available, nats.MaxWait(5*time.Second))
    if err != nil {
        continue
    }
    for _, msg := range msgs {
        sem <- struct{}{}
        go func(m *nats.Msg) {
            defer func() { <-sem }()
            w.handleMessage(ctx, m)
        }(msg)
    }
}
```

**Why fetch only `available` slots:** Fetching more messages than available worker slots causes messages to sit in-process while NATS's `AckWait` timer runs. If `AckWait` expires before a slot frees up, NATS redelivers the message — causing a duplicate scrape. Fetching exactly `available` eliminates this class of spurious redelivery.

The Python Playwright and LLM workers use the same pull-consumer pattern via `nats-py`'s `js.pull_subscribe()`.

---

### 6. Unchanged from ADR-001

The following principles are unchanged:

| Principle | Still holds |
|-----------|-------------|
| Ack-after-MinIO-write | Worker acks only after a successful `history/` write |
| Fat messages | Workers are DB-ignorant; all needed data is in the dispatch message |
| NATS-managed retries | `MaxDeliver` controls retry count; no application-level retry loop |
| Cancellation via result consumer | API sets `job_runs.status = 'cancelled'`; result consumer discards results for cancelled runs |
| Worker never touches Postgres | Go HTTP worker: NATS + MinIO only. Python workers: NATS + MinIO only |
| Stream creation outside API/worker | `nats-init` service owns stream lifecycle |

---

## Consequences

- The Go HTTP worker requires updates: new subject, `run_id` in messages, dual MinIO writes, pull consumer, `nats_stream_seq` on running messages. These are contained within the worker — no API changes needed beyond the NATS constants update.
- Adding a new worker type in the future requires: (1) choosing a new `scrapeflow.jobs.run.*` subject, (2) updating `constants.py`, (3) updating `POST /jobs` to route to the new subject. No stream reconfiguration needed — the `>` wildcard handles it.
- The LLM dispatch path adds a second hop through NATS (API → result consumer → LLM subject → LLM worker → result consumer). This is intentional — the result consumer is the only component with full context at completion time.
- Changing `minio_path` to `history/` convention requires updating all existing Phase 1 paths stored in `jobs.result_path`. Migration 2.3 handles this by copying existing rows into `job_runs` using the old path format — old paths remain valid MinIO objects. The new convention applies only to Phase 2 runs.
