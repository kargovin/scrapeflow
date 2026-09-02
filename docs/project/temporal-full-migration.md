# ScrapeFlow — Complete Temporal migration (change inventory)

> **Consolidated Phase 4 view: [`phase4-backlog.md`](./phase4-backlog.md)** (item **WF**).

> **What this document is, now that the engine ADR exists.**
> [**ADR-009**](../adr/ADR-009-workflow-engine-temporal.md) is **Accepted** (2026-09-08) and holds
> the *decisions*: the engine, the block model, metering, tenancy, the v1/v2 coexistence contract.
> This document holds the *inventory and the shapes* — component-by-component, what is deleted,
> transformed, kept or added, and the diagrams ADR-009 §16 deliberately points here for rather
> than drawing itself. **Where the two disagree, ADR-009 wins**; that is a standing rule, not a
> warning about staleness.

> **Status:** redrawn **2026-09-08** against the Accepted ADR, in one pass — pending owner review.
> The five 🔴 divergence markers this file used to carry are gone because the divergences are gone,
> not because they were waived. What the redraw changed, so a note written before it can be
> checked: the sequence in §9 is **named, not numbered**, and begins **after** the pre-migration
> queue; the NATS bridge is **rejected** everywhere, so §9a's peak diagram no longer shows one and
> the stacked-retry hazard it illustrated is withdrawn; `crawl_queue` **stays** and the coordinator
> is a **rewrite**; the Web UI is **not exposed**; tenancy is a **single namespace** with no
> identity in the workflow ID; and content dedup + `diff.py` are **relocated but not yet
> re-homed**. Prior contents of §11 ("open decisions for the engine ADR") were answered by the ADR
> and are replaced by pointers to the answers.

**Date:** 2026-07-14 · redrawn 2026-09-08
**Author:** @karthik
**Decision:** Temporal — taken in [ADR-009 §1](../adr/ADR-009-workflow-engine-temporal.md#1-the-engine-is-temporal)
(chosen over DBOS/Restate for portfolio value + first-class Python **and** Go SDKs)
**Related:** `docs/project/workflows-scoping.md`, `docs/project/open-questions.md` (Q8), ADR-005, ADR-006

---

## 1. What "complete Temporal" means

Two adoption depths were defined in the scoping doc. This document describes the **deep** end
state (integration option **b**):

- Every orchestration flow — regular jobs, batches, crawls, scheduling, webhooks — runs as a
  **Temporal workflow**.
- The scrape/Playwright/LLM workers become **Temporal activity workers** (they poll a Temporal
  task queue, not a NATS subject) — **in the first increment, not eventually**. ADR-009 §9
  rejected the NATS bridge that would have deferred this.
- **NATS JetStream is retired.** Task distribution and result transport are Temporal's job.
- The hand-rolled durable-execution code — `result_consumer.py`, `scheduler.py`,
  `webhook_loop.py`, `advisory.py`, and the `coordinator/` service — is **deleted.**
- Postgres stays as the **metadata / read-model** store; Temporal owns **execution state**.

We still get there via the **strangler-fig** sequence in §9 (run both, migrate flow by flow,
delete NATS last) — never a big-bang cutover. §1's picture is the destination, not the first step.

**Key correction to keep in mind throughout:** Temporal Server does **not** run our logic. It
is infrastructure (durable history, timers, retries, task routing). Our workflow/activity
*code* runs in **our own worker pods** that connect to it. So logic moves *out of the API pod*
into dedicated worker pods — not "into the engine."

**The second correction, which the word "worker" makes easy to miss.** After the migration there
are **two kinds of worker** and they have opposite database postures (ADR-009 §8d):

- the **scraper workers** (Go http, Playwright, LLM) — they run hostile pages through a real
  browser and hold **no database credentials**, on either lane. This is ADR-001's light-worker
  rule, and it **survives the entire migration**;
- the **workflow worker** — a new pod holding the orchestration logic that leaves the API, the
  direct successor to `result_consumer.py`'s role. It has database access by definition, because
  orchestration *is* database work.

The boundary is enforced by **task-queue routing**, not convention: an activity is registered
against a queue and the server only dispatches to pods listening on it, so a scraper pod is never
offered accounting or mirror work.

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
                    │  matching + its OWN    │   │  pipeline_runs, read- │
                    │  Postgres INSTANCE     │   │  model mirror, ledger │
                    │  + Web UI (NOT exposed)│   │  Redis (rate limit)   │
                    └───────┬───────┬────────┘   └──────────────────────┘
                task queues │       │ task queues
                            ▼       ▼
        ┌───────────────────────┐  ┌──────────────────────────────────┐
        │ Workflow-worker pod(s) │  │ Activity workers (the scrapers)   │
        │ JobWorkflow, Batch-    │  │ Go http-worker, Playwright, LLM — │
        │ Workflow, CrawlWork-   │  │ same scraping code, now polling a │
        │ flow, PipelineWorkflow │  │ Temporal task queue not NATS      │
        │ + mirror & accounting  │  │ ⚠ NO database credentials         │
        │ activities (has DB)    │  │                                   │
        └───────────────────────┘  └──────────────────────────────────┘
                            │                        │
                            ▼                        ▼
                    ┌──────────────┐         ┌──────────────┐
                    │ Postgres     │         │    MinIO      │
                    │ (app meta)   │         │  (results)    │
                    └──────────────┘         └──────────────┘
```

Three things the picture encodes that are decisions, not drawing choices:

- **Two Postgres roles that must not be conflated** — the existing **app-metadata DB** and a
  **separate Temporal-persistence instance** (ADR-009 §2a: a second StatefulSet, not a second
  database on the existing one; the instance boundary is the one that holds under a history-write
  burst, and the two schemas have different owners — Alembic vs `temporal-sql-tool`).
- **The Web UI is not ingress-exposed** (§2b) — `kubectl port-forward` only. It can terminate,
  cancel, signal and reset workflows, so exposing it publishes a write-capable control plane over
  production orchestration, and OSS Temporal UI ships with no authentication of its own.
- **Only the workflow worker touches Postgres.** The scraper pods reach MinIO and nothing else.

---

## 3. Component-by-component change inventory

| Component | Today | Under complete Temporal | Verdict |
|---|---|---|---|
| **API pod background loops** (`api/app/main.py` lifespan) | Runs `start_result_consumer`, `scheduler_loop`, `webhook_delivery_loop`, `maxdeliver_advisory_subscriber` in-process | All four removed; API only starts/signals/queries workflows | **Transform (shrink)** |
| **`result_consumer.py`** | scrape→LLM→diff→webhook + batch state machine; idempotency guards; Q8 overloaded status | Becomes the body of `JobWorkflow` / `BatchWorkflow`; guards gone (engine = exactly-once). ⚠️ Q8 dissolves only **where the engine owns the transitions** — §6's two writers stay | **Delete** |
| **`scheduler.py`** | 60s poll of due cron `Job`s + `_recover_stale_pending` | Temporal **Schedules** (native cron), one per recurring job/crawl; stale recovery is intrinsic. ⚠️ One behaviour has **no Schedule equivalent** — see §11 | **Delete** (also closes the scheduled-crawl gap) |
| **`webhook_loop.py` / `webhooks.py`** | 15s poll, `BACKOFF_SECONDS` schedule, SSRF revalidation, `WebhookDelivery` rows | A `deliver_webhook` **activity** with a Temporal RetryPolicy; SSRF check and the wire contract stay *inside* the activity. No `webhook_deliveries` row on the v2 lane (ADR-009 §15) | **Delete loop; keep SSRF + wire contract** |
| **`advisory.py`** | NATS MaxDeliver → mark run failed (dead-letter) | Retry exhaustion is a workflow failure branch | **Delete** |
| **`coordinator/` service** | BFS via `crawl_queue`, `dispatch_loop`, `result_handler_loop`, `reenqueue_stalled` | `CrawlWorkflow`, with the frontier **still in `crawl_queue`**, reached through activities. ⚠️ **A rewrite, not a port** — only the dispatch half has ever executed (BUG-008), so there is no working reference to migrate (ADR-009 §13a) | **Delete service; rewrite the logic** |
| **Scrape/Playwright/LLM workers** | Thin NATS consumers | **Temporal activity workers** — same scraping code (Patchright, formatter, robots, MinIO upload), new entry point polling a task queue. During coexistence each runs as **two deployments of one image**: one NATS-bound, one Temporal-bound. Go http-worker ports first | **Transform (rewire transport)** |
| **NATS JetStream** | Job dispatch + result transport + stream/consumer infra | Removed; Temporal task queues replace it (also erases the `ack_wait`/Q6 class of bug) | **Delete** |
| **App Postgres** | Source of truth for run state (`job_runs.status`, `crawl_queue`, counters) | Metadata + **read-model mirror**; execution state authority moves to Temporal. **Gains** `pipeline_runs` / `pipeline_run_blocks`, the run-counting **view**, and the shared storage **ledger** — the last two land *before* the migration starts (§9) | **Keep (reshape)** |
| **Temporal Server + its own Postgres instance + Web UI** | — | New infra: history, timers, retries, task routing. Namespace retention **30 days**. Web UI **not exposed** | **Add** |
| **Workflow-worker pod** | — | New: workflow definitions plus the DB-holding activities (status mirror, storage accounting). The successor to `result_consumer.py`'s role | **Add** |
| **MinIO** | Result storage | Unchanged; activities read/write it. v2 artifact paths are keyed on **run and block**, not on a job (ADR-009 §5) | **Keep** |
| **Redis** | Rate limiting + quota | Unchanged (stays in the API) | **Keep** |
| **JobNotifier + `pg_notify` → WebSocket** | Fires on every `run.status` transition for the SPA | Kept via a status-mirror activity (see §6). Pipelines get their **own** JSON channel, not `job_status` | **Keep (re-source)** |
| **Frontend SPA** | Reads status via WS + REST | Job path unchanged. The **pipeline lane still needs** a channel, listener, subscriber map, WS route and a page — "zero frontend change" covers jobs only | **Keep + extend** |

---

## 4. Logic relocation map

Where today's code *goes*, so nothing is "lost," only moved. ADR-009 §10 is the authority; this is
its inventory view.

| Today | New home under Temporal |
|---|---|
| `_handle_scrape_completed` / `_handle_llm_completed` decision logic | Sequential body of `JobWorkflow` (scrape activity → if `llm_config` → llm activity → webhook activity) |
| `_handle_batch_result` counter aggregation + completion detection | `BatchWorkflow` fans out one child `JobWorkflow` per URL; fan-in is `await all children` |
| `reenqueue_stalled` / stalled-page recovery | Activity heartbeat + start-to-close timeout (automatic) |
| `_recover_stale_pending` | Activity RetryPolicy (automatic) |
| `BACKOFF_SECONDS` webhook schedule | RetryPolicy on the `deliver_webhook` activity. ⚠️ It reaches the same **horizon** (≈2.6 h) but cannot reproduce the interval list — attempts 4–5 drift ~20 min. A named R6 divergence, not a bug |
| MaxDeliver advisory dead-letter | Workflow failure handling after retries exhausted |
| `crawl_queue` BFS frontier + dedup | **Stays in Postgres**, reached through activities. The dedup mechanism to preserve is the **index** (`idx_crawl_queue_url UNIQUE (crawl_id, url)` + `on_conflict_do_nothing`), not an in-memory set. `continue-as-new` is required in either candidate design |
| `scheduler.py` croniter dispatch | Temporal Schedule attached to a recurring workflow |
| Idempotency guards (`if run.status in terminal: return`) everywhere | Deleted — exactly-once activity results make them unnecessary. **Except** the cancellation guard, which is load-bearing and stays (§6) |
| **LLM cold-start handling** — `ensure_ready()` probe against `/models`, warm-up budget | Into the LLM activity, intact. ⚠️ Its start-to-close timeout must exceed **warm-up + request** — **≈360 s against production**, not the 240 s the repo defaults imply |
| **Transient/terminal classifier** (`errors.py` ×2, `errors.go`) | Ports untouched into each activity. **The classifier decides; Temporal retries** — the activity catches, calls `classify()`, and re-raises terminal verdicts as a **non-retryable application error**. Not a `RetryPolicy` error-type list. Fail-closed default (unknown → terminal) preserved |
| **The heartbeat obligation** (`ensure_ready`'s caller contract) | The *mechanism* dies with `ack_wait` and `in_progress()`; the **duty** re-homes onto `activity.heartbeat()` + `heartbeat_timeout`. Both ways of forgetting it fail: heartbeat_timeout with no heartbeat fails every cold start; no timeout and a dead worker's LLM job hangs for the full start-to-close |
| **Bot-wall detection** (`blocking.py`) | Stays in the Playwright scrape activity, semantics unchanged. Note it **returns** a verdict today rather than raising — the classifier has never seen a block, so making it terminal is a decision the port has to take explicitly |
| SSRF revalidation (`validate_no_ssrf_core`) | Called inside outbound activities (webhook + future sinks). ⚠️ **Terminal and immediate**, not a retry-ladder participant — today it marks the delivery exhausted without incrementing attempts, and a naive port turns "instantly dead" into "dead in ≈2.6 h", re-resolving a hostname an attacker is rebinding |
| **The webhook wire contract** — HMAC-SHA256 over raw bytes, `X-ScrapeFlow-Signature: sha256=<hex>`, success = `status_code < 300`, 10 s per-attempt timeout, header always sent even with no secret | Into the webhook activity **byte-identical**. The most externally visible thing in the migration, with no failing test in this repo to catch a change |
| Content dedup (`xxhash`), `diff.py` | ⚠️ **Relocated, and not yet re-homed.** Both halves of change detection went to **Monitors (layer B)**, which is unwritten — so they must **survive `result_consumer.py`'s deletion and wait**. Two things to keep straight: the dedup branch is **not pure logic** (on a hash match it deletes the new `history/` object and repoints `result_path` at the previous run's object — the cross-run object sharing that breaks per-run collection), and the two are **not equally at risk** — `diff.py` is its own module and survives its caller intact, while the content-hash lives *inside* the deleted file (`result_consumer.py:49-56` plus the branch at `:375-392`). Only the second can be lost by accident |

---

## 5. Data model changes (app Postgres)

Postgres stops being the orchestration source of truth and becomes a **read model** the SPA and
REST API query. Table-by-table:

- **`job_runs`** — kept, but `status` and timing columns become a **mirror** of Temporal
  workflow state (written by a status-mirror activity), not the authority. `nats_stream_seq` is
  dropped. Gains a **lane marker** at the job cutover (ADR-009 §7, mechanism 4) — written in the
  same transaction as the row insert.
- **`pipeline_runs` + `pipeline_run_blocks`** — **new.** Pipeline runs are never `job_runs` rows:
  `job_runs` carries job-shaped columns and a two-way exclusive-or constraint that would become
  three-way, and R3's per-block status and timing needs a child table regardless.
- **The run-counting view** — **new, and pre-migration (P7).** Quota counting stops naming a table.
  The view is the single definition of *"a run this user started"* and carries **four lanes** —
  job `job_runs`, batch `job_runs`, **`crawl_pages`**, `pipeline_runs`. `monthly_runs` counts rows;
  `concurrent_jobs` counts distinct **submissions**. Without it a new lane is invisible to every
  meter by construction, which is exactly what already happened to crawls.
- **The shared storage ledger** — **new, and pre-migration (P8).** One per-object row, **shared
  across lanes and lane-blind by construction** (the meter reads `user_id` and `bytes` only). It is
  BUG-007's fix vehicle and the table P7 needs, which is why it is sequenced between them. Written
  by an accounting **activity on the workflow-worker queue** — never by a scraper.
- **`crawl_queue`** — **kept.** The frontier and visited set stay in Postgres: at the advertised
  10,000-page ceiling the history budget is ≈5 events per page against a cheapest-possible cost of
  3, and a visited set carried as workflow state is ≈800 KB against a 2 MiB hard limit. What is
  still open is the table's *shape*, not its location.
- **`crawl_pages`** — **required, not a UI convenience.** It is P7's per-page metering unit, the
  ledger's producer link, and the artifact's own name (`dispatcher.py:120` puts `crawl_page_id`
  into the message's `job_id` field).
- **`webhook_deliveries`** — **job-lane-only by design.** There is no v2 row; workflow history is
  the attempt record. Whether the table outlives the job lane is decided at the step that retires
  that lane, not now.
- **`jobs`, `batches`, `crawls`** — kept as user-facing template/record rows; `schedule_cron`
  now drives a Temporal Schedule instead of `scheduler.py`.
- **`users`, `api_keys`, `llm_keys`, `job_secrets`, `user_quota`** — unchanged (auth, secrets,
  quota are not orchestration).

The **workflow ID** is the correlation key between a user-facing record and its Temporal execution:
`pipeline-run-{pipeline_run_id}`, `job-run-{run_id}`. ⚠️ **It carries no user identity** — Temporal
treats the ID as an opaque string and never parses it, so an embedded `user_id` would protect
nothing that the API's ownership check does not already protect (ADR-009 §12).

---

## 6. Real-time status to the SPA

Today, each transition in `result_consumer.py` writes `run.status` and calls `pg_notify`, which
`JobNotifier` fans out over WebSocket. To keep the SPA **unchanged on the job path**, preserve that
contract: a small **status-mirror activity** (called at each workflow stage) writes the mirror row
and `pg_notify`s exactly as before. Streaming Temporal events to the browser is rejected — it puts
the engine on the request path for a pure UI concern and couples the SPA to engine retention.

Four properties of that contract are load-bearing, and three of them are easy to lose in a port:

- **Two writers are kept, and the precedence rule is the point.** `result_consumer.py` was never
  the only writer — `quota.py`, `routers/jobs.py` and `routers/admin.py` also write and notify,
  the last two **inside the request**, for cancellation. That stays: routing cancellation through
  the workflow would make Cancel *look broken*, since a block is never aborted mid-execution, so a
  run cancelled four minutes into an LLM block would not grey out for four minutes. ⚠️ **The rule
  the v2 mirror activity must carry: a write from the engine side never moves a run out of a
  terminal state it did not itself set.** Forgetting it fails silently — the user watches a run
  cancel, then watches it flip back to `completed`.
- **A failed mirror write fails the run.** The mirror is not best-effort.
- **Payloads carry identifiers and status only, and absolute state — never deltas.**
- **The socket reconnects.** `JobNotifier` does not today (**BUG-009**), and that is not dissolved
  by Temporal.

**Pipelines get their own notify channel with a JSON payload.** They do not reuse `job_status`,
whose payload is a positional `job_id:run_id:status` string — `job_notifier.py:51` unpacks exactly
three fields and a fourth is caught, logged as malformed, and **dropped**, so widening it fails
silently. `batch_status` already demonstrates the JSON pattern to follow.

The Temporal Web UI is an operator tool, is not exposed (§2/§7), and nothing user-facing may depend
on it being reachable.

---

## 7. Infra & deployment changes (k3s / FluxCD)

New manifests in the infra repo (`govindappa-k8s-config`):

- **Temporal Server** — via the official Helm chart or `auto-setup` image; needs its **own
  Postgres StatefulSet** (not a database on the app instance). ⚠️ Temporal wants **two databases
  inside** that instance — `temporal` and `temporal_visibility` — even on standard visibility;
  provisioning one is the predictable way to lose an hour. Elasticsearch/OpenSearch is optional and
  deliberately not adopted.
- **A namespace-registration init job**, analogous to today's `nats-init-job.yaml`. This is where
  **retention is set: 30 days** — an operator dial with no correctness role, and changeable after
  creation.
- **Temporal Web UI** — deployed, **no ingress, no DNS record, no cert**. Access by
  `kubectl port-forward`. This is reversible (an ingress is purely additive) and is revisited
  post-Phase 4; the two candidates are Traefik `basicAuth` (the mlflow pattern — cheap, one shared
  credential, no attribution) and `forwardAuth` against an API admin endpoint (real attribution,
  but must be built and couples UI availability to the API).
- **Workflow-worker Deployment** — new; hosts the workflow definitions plus the DB-holding
  activities (status mirror, storage accounting). Horizontally scalable — stateless task-queue
  polling — so the API's bottleneck is removed rather than moved.
- **Activity-worker changes** — the three scraper Deployments gain a Temporal entry point. During
  coexistence each image runs as **two deployments**: one bound to NATS (v1), one to a Temporal
  task queue (v2). Their Dockerfiles/`entrypoint.sh` change; the scraping code does not.
  ⚠️ **The Playwright container start contract is the riskiest single item in the port** — Xvfb
  started, socket waited for, then `exec python` as pid 1; **never `xvfb-run` as the entrypoint**,
  or a dead worker looks healthy to k8s forever. Preserve it exactly.
- **API Deployment** — the `strategy: Recreate` + single-replica constraint is **removed**, but
  **only at the last step**. It is a consequence of the in-process loops, and the loops do not all
  leave until the schedule and webhook cutover.
- **NATS** — its StatefulSet/stream-init manifests are **removed** at the end of the migration.

**Capacity.** The node (8 CPU / 32 GiB) sits at **28% CPU requests, 11% memory**. Temporal Server +
its Postgres + Web UI + a workflow worker is roughly **+1.5–2 CPU and +2–3 GiB in requests** —
landing near 50% CPU requests. ⚠️ **That figure already is the coexistence peak**, not steady state:
the 28% baseline includes NATS, the coordinator and all three workers, so it describes the cluster
from the **worker port** through the flow cutovers, and every later step only removes things.
**Requests are not the constraint — limits are.** The node is at **162% limit overcommit** and the
Playwright worker alone is 500m request / 2000m limit. A simultaneous headed render and a Temporal
history burst means CFS throttling on the history service, surfacing as workflow task timeouts and
retries. **That looks exactly like a workflow bug and is not one.**

---

## 8. New cross-cutting concerns

- **Determinism.** Workflow code must be deterministic — no direct I/O, `datetime.now()`, or
  `random()` inside a workflow; those belong in activities (or use `workflow.now()`). One-time
  discipline, mostly around not sneaking side effects into workflow bodies.
- **Versioning our workflow code.** Distinct from pinning *user* pipeline definitions, which is
  decided (the definition travels as a workflow input argument). **Worker Versioning is GA and
  Temporal's stated default**; it needs **server-side enablement** on a self-hosted deployment, so
  it is an infra task rather than a code flag. ⚠️ **The trigger arrives in layer A, not with
  Monitors:** short runs can be drained before a deploy, but a Webhook block parks a run for up to
  ≈2.6 h, and a rolling deploy inside that window is the first case that cannot be drained.
- **Multi-tenancy — one namespace, and the API is the only boundary.** Namespace-per-user is
  rejected (provisioning becomes part of signup; configuration drifts). Namespace-per-tier is left
  open for noisy-neighbour isolation but buys nothing at current scale. Task queues are **shared**
  across tenants. ⚠️ **Nothing structural backs the boundary up at the engine**: cross-tenant 404
  holds because the API checks ownership of the row before making any engine call. Any future path
  that reaches Temporal without loading and ownership-checking the app row is a tenant-isolation
  bug with no second line of defence.
- **Testing.** Temporal ships a **time-skipping test framework** — long sleeps and monitors are
  testable in milliseconds. Activities remain plain functions (existing pytest style). The 249 API
  tests mostly stay (CRUD); orchestration tests migrate from consumer tests to workflow tests.
- **Observability.** Temporal gives a per-workflow timeline/history — a real debugging upgrade over
  `grep status=`, especially for the Q8 class of bug. ⚠️ Reached by `port-forward`, not a URL.
- **Backups.** The Temporal Postgres becomes critical state that must be backed up (it *is* the
  in-flight work). New operational responsibility on the homelab.

---

## 9. Migration sequence (strangler-fig — how we actually get there)

Never big-bang. Each step ships independently; both systems run until a flow is fully moved.

**Entry condition — the sequence does not start until the pre-migration queue is empty:**
**P6 → P8 → P7 + BUG-007.** P8 (the shared storage ledger) is a hard dependency — the v2 charging
activity has no table without it — and without P7's counting view a pipeline run consumes **none**
of the three meters, reproducing P7's own bug on a brand-new lane. `phase4-backlog.md` §1 is the
single source of truth for its contents.

⚠️ **The steps are named, not numbered, and every cross-reference names a step.** This list was
reordered once already, and the references into it silently pointed somewhere else afterwards.
Names do not renumber.

| step | what it does |
|---|---|
| **Engine up** | Temporal server, its own Postgres instance, the namespace init job, one workflow-worker pod. Prove a "hello workflow." NATS untouched. |
| **Worker port** | The three scrapers gain Temporal activity entry points, **Go http-worker first**. Each image now runs as two deployments — NATS-bound and Temporal-bound. |
| **Pipeline lane** | Pipelines run end-to-end on v2. **The R6 acceptance gate is run here.** |
| **Job cutover** | Jobs move onto `JobWorkflow`. The `job_runs` lane marker is built **here** — it is inert and untestable earlier. |
| **Batch and crawl cutover** | Batches onto `BatchWorkflow`; crawls onto `CrawlWorkflow` — a rewrite, kept last. |
| **Schedule and webhook cutover** | Temporal Schedules replace `scheduler.py`; delivery becomes an activity. `webhook_loop.py` and `advisory.py` go. |
| **Consumer deletion** | `result_consumer.py` is deleted once no flow routes through it. |
| **NATS removal** | The stream, the consumers and the client dependencies go. |
| **API thinning** | The single-replica / `Recreate` constraint is lifted; horizontal scaling and rolling deploys. |

The **worker port** comes before the **job cutover** because there is no bridge to carry pipelines
in the meantime — the activity workers *are* the first increment's executors.

**Cutover obligations.** (1) A unit of work executes on **exactly one lane**. (2) A recurring job
moved to a Temporal Schedule is **paused in v1 first**, in that order, with the pause verified
before the Schedule is created. ⚠️ The flag that does this, `schedule_status`, is **user-facing and
user-writable**, and one `PATCH /jobs/{id}` re-arms both lanes — obligation 1 gets structural
treatment, obligation 2 rests on a switch the user owns. (3) NATS workers stay alive until v1 is
drained, as the second deployment of the same image; removing them happens **after** the flow
migration, not during it.

**The drain gate fires at two points, not one:**

- **at every flow cutover**, before routing that flow to v2 — because an already-published, unacked
  NATS message is still deliverable to a v1 worker no matter what the routing switch says;
- **at deletion**, before a v1 component is removed.

Both read the same three numbers: the flow is drained, and its NATS consumers report **zero
unprocessed messages and zero outstanding acks**. ⚠️ Verify with **`nats consumer info --json`** —
the table output omits `Max Deliver` when it is `-1`, so it cannot distinguish a capped consumer
from an uncapped one. **None of the four one-lane mechanisms reaches the unacked-message window**
(the workers hold no DB access and cannot read a lane marker), so draining before flipping is the
only thing that closes it.

**Reversibility is a property of *migrated* flows, not of the plan.** A flow that was cut over falls
back to its v1 path. A flow with no v1 implementation — which is **every pipeline**, by the routing
rule — falls back to being **switched off**. Both are acceptable; they are not the same promise.

### 9a. The two shapes worth seeing

§2 draws the **end state**. It is the shape we operate for the longest, but it is not the shape
that carries the risk. Two others matter: where we start, and the **peak** — from the worker port
through the flow cutovers, when both orchestrators run at once. Everything after that is purely
subtractive and needs no picture.

**Today (v1) — five hand-rolled orchestrators, execution state in Postgres.**

```
   SPA ──HTTPS──►┌────────────────────────────────────────────────┐
                 │  API pod      replicas: 1 · strategy: Recreate │
                 │  auth · CRUD · quotas                          │
                 │  ┌──────────────────────────────────────────┐  │
                 │  │ 4 in-process loops — the reason for      │  │
                 │  │ replicas: 1                              │  │
                 │  │  result_consumer  ·  scheduler           │  │
                 │  │  webhook_loop     ·  advisory            │  │
                 │  └──────────────────────────────────────────┘  │
                 └──────────┬───────────────────────┬─────────────┘
                            │                       │
   ┌────────────────────┐   │                       │
   │ coordinator pod    │   │                       │
   │ BFS crawl frontier ├───┤                       │
   │ (5th orchestrator) │   │                       │
   └─────────┬──────────┘   │                       │
             │              ▼                       ▼
             │   ┌─────────────────────────┐   ┌──────────────────────┐
             └──►│ NATS JetStream          │   │ Postgres (app)       │
                 │ stream SCRAPEFLOW       │   │ job_runs.status IS   │
                 │ --retention work        │   │   execution state    │
                 │  jobs.run.http          │   │ crawl_queue (BFS)    │
                 │  jobs.run.playwright    │   │ webhook_deliveries   │
                 │  jobs.llm               │   └──────────────────────┘
                 │  jobs.result            │   ┌──────────────────────┐
                 │  $JS…MAX_DELIVERIES     │   │ Redis (rate limit)   │
                 └───┬────────┬────────┬───┘   └──────────────────────┘
                     ▼        ▼        ▼
              ┌──────────┐ ┌────────┐ ┌────────┐
              │ Go http- │ │ play-  │ │ llm-   │
              │ worker   │ │ wright │ │ worker │
              └────┬─────┘ └───┬────┘ └───┬────┘
                   └───────────┴──────────┘
                               ▼
                    ┌──────────────────────┐
                    │ MinIO (results)      │
                    └──────────────────────┘
```

Two properties to hold onto, because both **invert** in the end state: orchestration state lives in
**app Postgres** (which is what made Q8 possible — one overloaded status column), and NATS is
`--retention work`, so orchestration messages are **deleted on ack** — free and self-cleaning.
Temporal's history is neither.

**The peak — after the worker port, through the flow cutovers. Two orchestrators, two lanes, one
image per scraper running twice.**

```
                          ┌──────────────────────────────────────────┐
       SPA ──HTTPS───────►│  API pod     replicas: 1 · Recreate      │
                          │  auth · CRUD · quotas                    │
                          │  4 in-process loops — still all running  │
                          └──┬─────────────────────────────┬─────────┘
             v2: pipelines,  │                             │  v1: everything not
             then each flow  │                             │  yet cut over
             as it cuts over │                             │
            ┌───────────────▼──────────────┐    ┌──────────▼──────────────┐
            │ Temporal Server              │    │ NATS JetStream          │
            │  Postgres #2 (own instance)  │    │  --retention work       │
            │  Web UI — port-forward only  │    │  (unchanged)            │
            └───────────────┬──────────────┘    └──────────┬──────────────┘
        workflow queue      │      scraper queues          │  subjects
            ┌───────────────▼──────────────┐               │
            │ workflow-worker pod          │               │
            │  PipelineWorkflow, then      │               │
            │  JobWorkflow, Batch, Crawl   │               │
            │  + mirror & accounting acts  │               │
            │  ⚠ the ONLY pod with app-DB  │               │
            │    credentials on this side  │               │
            └───────────────┬──────────────┘               │
                            │                              │
      ┌─────────────────────▼─────────────┐  ┌─────────────▼──────────────────┐
      │ scrapers — Temporal-bound         │  │ scrapers — NATS-bound          │
      │ go-http · playwright · llm        │  │ go-http · playwright · llm     │
      │ deployment #2 of ONE image        │  │ deployment #1 of ONE image     │
      │ no DB credentials                 │  │ no DB credentials              │
      └─────────────────┬─────────────────┘  └─────────────┬──────────────────┘
                        └──────────────┬───────────────────┘
                                       ▼
                          ┌──────────────────────────┐
                          │ MinIO (results) — shared │
                          └──────────────────────────┘

   app Postgres + Redis are reached by the API and the workflow worker only.
   still running, not drawn: coordinator pod (v1 crawls) · Redis
```

**What the picture is for.** The two lanes are **disjoint all the way down to the pods**. There is
no bridge: a v2 activity does not publish into NATS, so **Temporal's `RetryPolicy` is the only
retry layer on v2 work**. That is the point of ADR-009 §9's rejection of option (a) — a bridge
would have stacked JetStream redelivery underneath the workflow's own retries on the same unit of
work, which is the Q5/Q6/Q7 failure mode reintroduced by the migration itself, and on the LLM path
each duplicate is billed to the **user's own** API key.

Four further facts the diagram makes visible:

- **The API does not get thinner here.** It keeps `replicas: 1` and all four loops until the
  schedule and webhook cutover. The horizontal-scaling payoff lands at **API thinning**, the last
  step — the shrink is the *last* thing to arrive, not the first.
- **The scrapers are the one component with no v1/v2 split in the *code*.** The split is in the
  **deployment**: one image, two Deployments, two transports. That is what makes obligation 3
  ("workers stay alive until v1 is drained") a manifest change rather than a code branch.
- **This is the capacity worst case.** Nothing is removed until the cutovers finish; everything in
  the "today" diagram is still running underneath. §2d sizes the cluster against this moment
  deliberately, not against steady state.
- **The pipeline lane has no fallback.** Every other lane in this picture can be routed back to
  v1. Pipelines cannot — there is no v1 pipeline — so the R6 gate is run on a lane whose only
  rollback is switching the feature off.

---

## 10. Risks specific to full adoption

- **Operational weight on a single-node k3s homelab.** Temporal Server (multiple services + its
  own Postgres) is the heaviest dependency we'd run. Mitigation: standard visibility (no ES);
  accept single-node (no Temporal HA) for a homelab.
- **Temporal DB is now critical in-flight state** — needs backup/restore discipline it didn't
  before. Losing it loses *running work*, not just history.
- **Determinism bugs** are a new failure class (non-deterministic workflow code breaks replay).
  Mitigation: strict "side effects only in activities" review rule + the test framework.
- **The first increment has no v1 fallback.** The pipeline lane is new, so "a misbehaving flow
  falls back to v1" — true for jobs, batches and crawls — is false exactly where the acceptance
  gate runs. This is an argument for §9's standalone pre-gate (run the Scrape activity alone and
  diff it against a v1 run of the same URL) being a requirement rather than a nicety.
- **Crawls migrate with no reference implementation at all.** The pre-gate that covers every other
  lane *cannot exist* for crawls: there is no v1 crawl result to diff against, because no crawl has
  ever got past dispatching its seed page. The compensating gate has to be built, not borrowed.
- **The big-bang temptation.** The end state is clean, but jumping there directly is the risk.
  The §9 sequence exists specifically to avoid it.
- **Two languages, two SDKs** (Go + Python) — and the real price is not the SDK duplication. It is
  the **Playwright container start contract** (Xvfb → `exec python` as pid 1), which is the
  riskiest single item in the port because getting it wrong produces a healthy-looking dead pod.

---

## 11. What ADR-009 settled, and what is still open

The list this section used to hold was a set of questions for the engine ADR. The ADR answered
them; what follows is where the answers live, and the items genuinely still open.

**Answered:**

| Question | Answer | Where |
|---|---|---|
| Frontier model for crawls | Frontier and visited set **stay in Postgres**; `continue-as-new` is required either way; the visited set cannot ride in workflow state | §13b |
| Keep `webhook_deliveries` / `crawl_pages`? | `webhook_deliveries` is **job-lane-only**, no v2 row; **`crawl_pages` is required** on the v2 lane | §15, §13c |
| Namespace-per-tier vs single namespace | **Single namespace**; the API's ownership check is the only tenant boundary | §12 |
| Status to the SPA | **Mirror activity + `pg_notify`**; two writers kept; pipelines get their own JSON channel | §11 |
| How long to keep the NATS fallback | Per flow, gated by the **drain gate** at cutover *and* deletion | §16 |
| Temporal retention / archival | **30 days**; archival stays off | §2c |

**Still open** — the ones that bear on this inventory. ADR-009's **"Deliberately not decided
here"** table is the complete list; each item below is dated to a step rather than left vague:

- **The crawl frontier table's *shape*** — one table for both the queue and the seen-set, or two.
  Decide at the batch and crawl cutover.
- **Whether sitemap entries are origin-restricted like extracted links.** Extracted links are
  restricted to the seed's origin; sitemap entries are taken verbatim from the target's robots.txt
  and are not. SSRF checking at frontier admission is decided and covers the security half; what
  remains is a quota and attribution question, complicated by sitemaps legitimately crossing
  subdomains. ⚠️ **Needed before the crawl step is built.**
- **What a quota-blocked scheduled run does.** Today the scheduler declines to create the run and
  does *not* advance `next_run_at`, so the poll retries until a slot frees — a waiting room,
  nothing dropped. **No Temporal Schedule overlap policy reproduces it**: every policy reacts to a
  previous execution still running, not to the account's meters, which a Schedule cannot read.
- **The headroom buffer's size**, and **per-page ceiling checks for crawls** — the buffer only
  holds while the most a single admitted run can add is smaller than it, which is false for crawls.
- **The retention window for intermediate block outputs** — the number only.
- **Worker Versioning vs `patched`** for our own workflow code — decide at the first deploy that
  must survive an in-flight run, which arrives in layer A.

---

## 12. What explicitly does NOT change

- The **scraping muscle** — Patchright/headed-Chrome stealth (ADR-008), the Go fetcher, the LLM
  call logic, formatters, robots handling. Only the transport wrapper changes.
- **MinIO** result storage, and its `latest/` + `history/` convention **for the v1 lane**. The v2
  lane keys artifacts on run and block instead — the one live exception to ADR-002 §4.
- **The light-worker rule** — scraper workers reach NATS/Temporal and MinIO, never a database.
  This survives the whole migration and is what makes several other decisions work.
- **Auth** (Clerk JWT), **Redis** rate limiting, **secrets** encryption (Fernet), and the bulk of
  **REST CRUD** endpoints.
- The **cross-tenant = 404** invariant — enforced by the API's ownership check, which after §12 is
  the *only* thing enforcing it.
- Most of the **249 API tests** (CRUD-level).
