# ADR-001: Worker Job Contract

**Status:** Accepted
**Date:** 2026-03-25
**Deciders:** @karthik

---

## Context

ScrapeFlow's API (Python/FastAPI) and scrape worker (Go) are separate processes that communicate via NATS JetStream. Before implementing either side, the contract between them must be explicit — they are written in different languages and the message format is the interface.

This ADR defines the NATS subjects, message schemas, lifecycle, and behavioral contracts for job dispatch and result reporting.

---

## Decisions

### 1. Stream Lifecycle

The JetStream stream is created **outside the API and worker** — via an init container (Docker Compose) or a k8s Job (k3s). Neither the API nor the worker creates the stream.

**At startup, the API asserts the stream exists and fails fast if it does not.** This is consistent with how all other infrastructure clients (Redis, MinIO, NATS connection) are handled — hard crash with a clear message rather than a silent degraded state.

```
Stream name:    SCRAPEFLOW
Subjects:       scrapeflow.jobs.run, scrapeflow.jobs.result
Retention:      WorkQueuePolicy (messages deleted after ack)
Max deliver:    Configurable via NATS_MAX_DELIVER (default: 3)
```

---

### 2. Subjects

| Subject | Publisher | Consumer | Purpose |
|---|---|---|---|
| `scrapeflow.jobs.run` | API | Worker | Dispatch a new scrape job |
| `scrapeflow.jobs.result` | Worker | API | Report job outcome |

---

### 3. Message Schemas

#### `scrapeflow.jobs.run` — Job dispatch (API → Worker)

```json
{
  "job_id": "uuid-v4",
  "url": "https://example.com",
  "output_format": "html" | "markdown" | "json"
}
```

**Why fat message (URL + options included):** The worker has no database access. All information needed to execute the scrape must be in the message. This keeps the worker DB-ignorant and dependent only on NATS and MinIO.

#### `scrapeflow.jobs.result` — Job progress and outcome (Worker → API)

The worker publishes to this subject **twice** per job — once when it starts, once when it finishes.

**Progress event (published when worker begins scraping):**
```json
{
  "job_id": "uuid-v4",
  "status": "running"
}
```

**Outcome event (published after result is written to MinIO or on failure):**
```json
{
  "job_id": "uuid-v4",
  "status": "completed" | "failed",
  "minio_path": "scrapeflow-results/{job_id}.html",
  "error": "optional error message, present only when status=failed"
}
```

`minio_path` is present only when `status=completed`. `error` is present only when `status=failed`. Neither field is present in the `running` progress event.

**Why reuse the same subject:** A separate `scrapeflow.jobs.progress` subject would require the API to maintain two subscriptions and two durable consumers. The result subject already handles all worker→API communication; `status` as a discriminator is sufficient.

---

### 4. Worker Responsibilities

The worker is intentionally **light** — it owns scraping and storage only. All business logic lives in the API.

| Responsibility | Owner |
|---|---|
| Fetch URL and produce output | Worker |
| Write raw result to MinIO | Worker |
| Publish result event | Worker |
| Update job status in Postgres | API (result consumer) |
| Enforce cancellation | API (result consumer) |
| Retry logic | NATS JetStream (via `MaxDeliver`) |

**Worker dependencies: NATS + MinIO only. No database access.**

---

### 5. Acknowledgment Timing

The worker **acknowledges the NATS message after successfully writing the result to MinIO**, not before scraping begins.

```
Worker receives message
        │
        ▼
Worker fetches URL
        │
        ▼
Worker writes result to MinIO
        │
        ▼
Worker publishes to scrapeflow.jobs.result
        │
        ▼
Worker acks NATS message   ← ack happens here
```

**Why ack-after:** If the worker crashes before writing to MinIO, the message is not acknowledged and NATS redelivers it. Acking before scraping would silently lose the job on a mid-scrape crash.

**Implication:** A worker crash causes a redelivery and a duplicate scrape attempt. This is acceptable — scraping a URL twice is idempotent.

---

### 6. Retry Policy

NATS JetStream handles retries automatically via `MaxDeliver`. After `NATS_MAX_DELIVER` unacknowledged deliveries (default: 3), NATS stops redelivering. The API result consumer is responsible for detecting this via the `MaxDeliver` advisory and marking the job as `failed`.

No application-level retry loop is needed in the worker.

---

### 7. Cancellation

`DELETE /jobs/{id}` sets `status = cancelled` in Postgres only. No cancellation signal is sent to NATS or the worker.

If a job is cancelled while the worker is already processing it:

```
Worker scrapes URL → writes to MinIO → publishes to scrapeflow.jobs.result
        │
        ▼
API result consumer receives result
        │
        ▼
API checks job status in DB → status = cancelled → discards result, does not update DB
```

The worker wastes one scrape. Correctness is preserved — the job remains `cancelled`. The API result consumer is the single enforcement point for cancellation.

---

### 8. MinIO Path Convention

```
{bucket}/{job_id}.{extension}

Examples:
  scrapeflow-results/550e8400-e29b-41d4-a716-446655440000.html
  scrapeflow-results/550e8400-e29b-41d4-a716-446655440000.md
  scrapeflow-results/550e8400-e29b-41d4-a716-446655440000.json
```

The worker constructs the path from `job_id` and `output_format`. The path is included in the result event so the API can store it in `jobs.result_path` without knowing the convention.

---

## Consequences

- The worker is simple to implement and test in isolation — it only needs a NATS message and MinIO credentials.
- The API owns all state transitions and business logic. The worker never touches Postgres.
- Adding a new output format or job option requires updating the job message schema and bumping this ADR.
- Cancellation during an in-flight scrape wastes one network request. This is acceptable for MVP; a future improvement is a NATS cancellation subject the worker subscribes to.
