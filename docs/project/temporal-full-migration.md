# ScrapeFlow — Complete Temporal migration (change inventory)

> **Consolidated Phase 4 view: [`phase4-backlog.md`](./phase4-backlog.md)** (item **WF**).

> Companion to `workflows-scoping.md`. That doc scoped a *new* Workflows layer coexisting
> with the NATS path and compared engines. **This doc assumes the decision is made — Temporal —
> and inventories every change if we go *all the way*: all orchestration on Temporal, the
> hand-rolled loops retired, NATS removed.** It describes the **end state** and the deltas to
> reach it. It is a design doc, not a commitment; it feeds a future engine ADR.

**Status:** Draft — for discussion
**Date:** 2026-07-14
**Author:** @karthik
**Decision:** Temporal (chosen over DBOS/Restate for portfolio value + first-class Python **and** Go SDKs)
**Related:** `docs/project/workflows-scoping.md`, `docs/project/open-questions.md` (Q8), ADR-005, ADR-006

---

## 1. What "complete Temporal" means

Two adoption depths were defined in the scoping doc. This document describes the **deep** end
state (integration option **b**):

- Every orchestration flow — regular jobs, batches, crawls, scheduling, webhooks — runs as a
  **Temporal workflow**.
- The scrape/Playwright/LLM workers become **Temporal activity workers** (they poll a Temporal
  task queue, not a NATS subject).
- **NATS JetStream is retired.** Task distribution and result transport are Temporal's job.
- The hand-rolled durable-execution code — `result_consumer.py`, `scheduler.py`,
  `webhook_loop.py`, `advisory.py`, and the `coordinator/` service — is **deleted.**
- Postgres stays as the **metadata / read-model** store; Temporal owns **execution state**.

We still get there via the **strangler-fig** sequence in §9 (run both, migrate flow by flow,
delete NATS last) — never a big-bang cutover. §1's picture is the destination, not step one.

**Key correction to keep in mind throughout:** Temporal Server does **not** run our logic. It
is infrastructure (durable history, timers, retries, task routing). Our workflow/activity
*code* runs in **our own worker pods** that connect to it. So logic moves *out of the API pod*
into dedicated worker pods — not "into the engine."

---

## 2. Target architecture

```
                    ┌──────────────────────────────────────────┐
   SPA  ── HTTPS ──►│  API pod(s)  — THIN, horizontally scalable │
                    │  auth · CRUD · start/signal/query workflows │
                    └───────────────┬───────────────┬────────────┘
                                    │ start/signal  │ read model + WS
                                    ▼               ▼
                    ┌───────────────────────┐   ┌──────────────────────┐
                    │  Temporal Server       │   │  Postgres (app meta)  │
                    │  frontend/history/     │   │  users, jobs (tmpl),  │
                    │  matching + its OWN    │   │  read-model mirror,   │
                    │  Postgres + Web UI     │   │  Redis (rate limit)   │
                    └───────┬───────┬────────┘   └──────────────────────┘
                task queues │       │ task queues
                            ▼       ▼
        ┌───────────────────────┐  ┌──────────────────────────────────┐
        │ Workflow-worker pod(s) │  │ Activity workers (the scrapers)   │
        │ JobWorkflow, Batch-    │  │ Go http-worker, Playwright, LLM — │
        │ Workflow, CrawlWork-   │  │ same scraping code, now polling a │
        │ flow, MonitorWorkflow  │  │ Temporal task queue not NATS      │
        └───────────────────────┘  └──────────────────────────────────┘
                            │
                            ▼
                    ┌──────────────┐
                    │    MinIO      │  (unchanged — result storage)
                    └──────────────┘
```

Two Postgres roles that must not be conflated: the **existing app-metadata DB** and a **new
Temporal-persistence DB** (Temporal's own event-history store).

---

## 3. Component-by-component change inventory

| Component | Today | Under complete Temporal | Verdict |
|---|---|---|---|
| **API pod background loops** (`api/app/main.py` lifespan) | Runs `start_result_consumer`, `scheduler_loop`, `webhook_delivery_loop`, `maxdeliver_advisory_subscriber` in-process | All four removed; API only starts/signals/queries workflows | **Transform (shrink)** |
| **`result_consumer.py`** | scrape→LLM→diff→webhook + batch state machine; idempotency guards; Q8 overloaded status | Becomes the body of `JobWorkflow` / `BatchWorkflow`; guards gone (engine = exactly-once); Q8 dissolved | **Delete** |
| **`scheduler.py`** | 60s poll of due cron `Job`s + `_recover_stale_pending` | Temporal **Schedules** (native cron), one per recurring job/crawl; stale recovery is intrinsic | **Delete** (also closes the scheduled-crawl gap) |
| **`webhook_loop.py` / `webhooks.py`** | 15s poll, `BACKOFF_SECONDS` schedule, SSRF revalidation, `WebhookDelivery` rows | A `deliver_webhook` **activity** with a Temporal RetryPolicy; SSRF check stays *inside* the activity | **Delete loop; keep SSRF logic** |
| **`advisory.py`** | NATS MaxDeliver → mark run failed (dead-letter) | Retry exhaustion is a workflow failure branch | **Delete** |
| **`coordinator/` service** | BFS via `crawl_queue`, `dispatch_loop`, `result_handler_loop`, `reenqueue_stalled` | `CrawlWorkflow` — frontier in workflow state or child-workflow-per-page; stall recovery intrinsic | **Delete service** |
| **Scrape/Playwright/LLM workers** | Thin NATS consumers | **Temporal activity workers** — same scraping code (Patchright, formatter, robots, MinIO upload), new entry point polling a task queue | **Transform (rewire transport)** |
| **NATS JetStream** | Job dispatch + result transport + stream/consumer infra | Removed; Temporal task queues replace it (also erases the ack_wait/Q6 class of bug) | **Delete** |
| **App Postgres** | Source of truth for run state (`job_runs.status`, `crawl_queue`, counters) | Metadata + **read-model mirror**; execution state authority moves to Temporal | **Keep (reshape)** |
| **Temporal Server + its Postgres + Web UI** | — | New infra: history, timers, retries, task routing, visibility UI | **Add** |
| **MinIO** | Result storage | Unchanged; activities read/write it | **Keep** |
| **Redis** | Rate limiting + quota | Unchanged (stays in the API) | **Keep** |
| **JobNotifier + `pg_notify` → WebSocket** | Fires on every `run.status` transition for the SPA | Kept via a status-mirror activity (see §6) | **Keep (re-source)** |
| **Frontend SPA** | Reads status via WS + REST | Unchanged if the mirror + WS pattern is preserved; new pipeline/monitor UI is later work | **Keep** |

---

## 4. Logic relocation map

Where today's code *goes*, so nothing is "lost," only moved:

| Today | New home under Temporal |
|---|---|
| `_handle_scrape_completed` / `_handle_llm_completed` decision logic | Sequential body of `JobWorkflow` (scrape activity → if `llm_config` → llm activity → diff activity → webhook activity) |
| `_handle_batch_result` counter aggregation + completion detection | `BatchWorkflow` fans out one child `JobWorkflow` per URL; fan-in is `await all children` |
| `reenqueue_stalled` / stalled-page recovery | Activity heartbeat + start-to-close timeout (automatic) |
| `_recover_stale_pending` | Activity RetryPolicy (automatic) |
| `BACKOFF_SECONDS` webhook schedule | RetryPolicy on the `deliver_webhook` activity |
| MaxDeliver advisory dead-letter | Workflow failure handling after retries exhausted |
| `crawl_queue` BFS frontier + dedup | Workflow state (visited set + frontier) in `CrawlWorkflow` |
| `scheduler.py` croniter dispatch | Temporal Schedule attached to a recurring workflow |
| Idempotency guards (`if run.status in terminal: return`) everywhere | Deleted — exactly-once activity results make them unnecessary |
| Content dedup (`xxhash`), `diff.py` | A `diff`/`dedup` **activity** — pure logic reused verbatim |
| SSRF revalidation (`validate_no_ssrf_core`) | Called inside outbound activities (webhook + future sinks) |

---

## 5. Data model changes (app Postgres)

Postgres stops being the orchestration source of truth and becomes a **read model** the SPA and
REST API query. Table-by-table:

- **`job_runs`** — kept, but `status` and timing columns become a **mirror** of Temporal
  workflow state (written by a status-mirror activity), not the authority. The `nats_stream_seq`
  column is dropped. Q8's overloaded status is moot — Temporal holds the real state machine.
- **`crawl_queue`, `crawl_pages`** — `crawl_queue` **retired** (frontier now lives in workflow
  state). `crawl_pages` optionally kept as a per-page result mirror for the UI.
- **`webhook_deliveries`** — optional. Temporal tracks attempts/history; keep the table only if
  we want an app-queryable audit trail independent of Temporal's retention.
- **`jobs`, `batches`, `crawls`** — kept as user-facing template/record rows; `schedule_cron`
  now drives a Temporal Schedule instead of `scheduler.py`.
- **`users`, `api_keys`, `llm_keys`, `job_secrets`, `user_quota`** — unchanged (auth, secrets,
  quota are not orchestration).

The **workflow ID** becomes the correlation key between a user-facing record and its Temporal
execution (e.g. `job-run-{run_id}`), replacing today's NATS-seq / run-id correlation.

---

## 6. Real-time status to the SPA

Today, each transition in `result_consumer.py` writes `run.status` and calls `pg_notify`, which
`JobNotifier` fans out over WebSocket. To keep the SPA **unchanged**, preserve that contract:
a small **status-mirror activity** (called at each workflow stage) writes the mirror row and
`pg_notify`s exactly as before. The SPA and its WebSocket path don't know Temporal exists.

Alternative (later): stream Temporal workflow events directly, or expose Temporal Web UI for
operators. Decision deferred to the ADR — the mirror approach is the zero-frontend-change path.

---

## 7. Infra & deployment changes (k3s / FluxCD)

New manifests in the infra repo (`govindappa-k8s-config`):

- **Temporal Server** — via the official Helm chart or `auto-setup` image; needs a dedicated
  **Postgres database** (its persistence store) and, for advanced search, optional
  Elasticsearch/OpenSearch (can start with standard visibility, no ES).
- **Temporal Web UI** — an ingress (e.g. `temporal.scrapeflow.govindappa.com`), admin-gated.
- **Workflow-worker Deployment** — new; hosts the workflow + activity definitions (Python SDK).
- **Activity-worker changes** — the three scraper Deployments swap their entry points from NATS
  consumers to Temporal task-queue workers (Go SDK for http-worker; Python SDK for the others).
  Their Dockerfiles/`entrypoint.sh` change; the scraping code doesn't.
- **API Deployment** — the `strategy: Recreate` + single-replica constraint is **removed.** The
  handoff notes the API result consumer "uses a push consumer… limits to one replica and
  requires `Recreate` strategy" (`scrapeflow-session-handoff.md:255`); once orchestration leaves
  the API, it becomes stateless and horizontally scalable with a rolling strategy.
- **NATS** — its StatefulSet/stream-init manifests are **removed** at the end of the migration.

**Operational win called out explicitly:** the API's inability to scale today is a direct
consequence of in-process background loops. Moving them to workflow workers is what unblocks
horizontal scaling — a concrete architectural payoff, not just relocation.

---

## 8. New cross-cutting concerns

- **Determinism.** Workflow code must be deterministic — no direct I/O, `datetime.now()`, or
  `random()` inside a workflow; those belong in activities (or use `workflow.now()`). One-time
  discipline, mostly around not sneaking side effects into workflow bodies.
- **Versioning.** Changing a workflow while runs are in-flight requires Temporal's versioning
  (patched APIs / Worker Versioning). New release discipline vs today's "redeploy and it
  reconnects."
- **Multi-tenancy.** Enforce tenant isolation via **Temporal namespaces** (per tier) and/or
  `user_id`-scoped workflow IDs. Maps to today's "cross-tenant = 404" invariant.
- **Testing.** Temporal ships a **time-skipping test framework** — long sleeps and monitors are
  testable in milliseconds. Activities remain plain functions (existing pytest style). The 243
  API tests mostly stay (CRUD); orchestration tests migrate from consumer tests to workflow tests.
- **Observability.** Temporal Web UI gives a per-workflow timeline/history — replaces
  `grep status=` log-spelunking. A real debugging upgrade, especially for the Q8-class of bug.
- **Backups.** The Temporal Postgres becomes critical state that must be backed up (it *is* the
  in-flight work). New operational responsibility on the homelab.

---

## 9. Migration sequence (strangler-fig — how we actually get there)

Never big-bang. Each step ships independently; both systems run until a flow is fully moved:

1. **Stand up Temporal** alongside everything (server + its DB + Web UI + one workflow-worker
   pod). Prove a "hello workflow." NATS untouched.
2. **Regular jobs onto `JobWorkflow`.** Activities call the *existing* NATS workers at first
   (integration option a) — reproduce today's scrape→LLM→diff→webhook as a workflow. New jobs
   route to Temporal; existing job history stays readable via the mirror. `result_consumer.py`
   still serves in-flight legacy jobs.
3. **Cut workers over to activity workers** (option b) — swap their transport from NATS to
   Temporal task queues. Now the scrape/LLM path touches no NATS.
4. **Batches → `BatchWorkflow`; crawls → `CrawlWorkflow`.** Retire the `coordinator/` service
   and `crawl_queue`.
5. **Scheduling → Temporal Schedules; webhooks → activity.** Delete `scheduler.py`,
   `webhook_loop.py`, `advisory.py`.
6. **Delete `result_consumer.py`** once no flow routes through it, and **remove NATS** entirely.
7. **Remove the API single-replica/Recreate constraint;** enable horizontal scaling + rolling
   deploys.

Each of these is a reversible increment — if a step misbehaves, that flow falls back to the
NATS path until fixed.

---

## 10. Risks specific to full adoption

- **Operational weight on a single-node k3s homelab.** Temporal Server (multiple services + its
  own Postgres + optional ES) is the heaviest dependency we'd run. Mitigation: standard
  visibility (no ES) initially; accept single-node (no Temporal HA) for a homelab.
- **Temporal DB is now critical in-flight state** — needs backup/restore discipline it didn't
  before.
- **Determinism bugs** are a new failure class (non-deterministic workflow code breaks replay).
  Mitigation: strict "side effects only in activities" review rule + the test framework.
- **The big-bang temptation.** The end state is clean, but jumping there directly is the risk.
  The §9 sequence exists specifically to avoid it.
- **Two languages, two SDKs** (Go + Python) — small duplication in activity-worker setup.

---

## 11. Open decisions for the engine ADR

1. **Frontier model for crawls** — visited-set-in-workflow-state vs child-workflow-per-page
   (history-size vs simplicity trade-off; `continue-as-new` for large crawls).
2. **Keep `webhook_deliveries` / `crawl_pages` as mirrors, or drop them** and read from Temporal?
3. **Namespace-per-tier vs single namespace** for multi-tenancy.
4. **Status to SPA** — mirror-activity + `pg_notify` (zero frontend change) vs streaming Temporal
   events.
5. **How long to keep the NATS fallback** before deleting it (step 6 gating criteria).
6. **Temporal retention / archival** settings for completed workflows (history growth).

---

## 12. What explicitly does NOT change

- The **scraping muscle** — Patchright/headed-Chrome stealth (ADR-008), the Go fetcher, the LLM
  call logic, formatters, robots handling, MinIO dual-write. Only the transport wrapper changes.
- **MinIO** result storage and its `latest/` + `history/` convention.
- **Auth** (Clerk JWT), **Redis** rate limiting, **secrets** encryption (Fernet), and the bulk of
  **REST CRUD** endpoints.
- The **cross-tenant = 404** invariant (now enforced additionally via workflow-ID scoping).
- Most of the **243 API tests** (CRUD-level).
