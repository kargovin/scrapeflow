# ScrapeFlow Phase 4 — Temporal Implementation Backlog

> **Persona:** Tech Lead. This is the ordered, engineer-ready task list for the migration
> ADR-009 §16 names. It sequences; it does not decide. Where a task needed a call the ADR did
> not make, the call is marked **TL call** with its reasoning and is reversible; where a task
> exposes a gap in the design, it is marked **⚠️ raise to Architect** and the task is blocked on
> that answer, not on a guess.
>
> **Scope source of truth:** `phase4-backlog.md` (§2 the migration · §3 **do NOT fix** · §4 survives).
> **Decisions:** ADR-009 (Accepted), ADR-011 (Accepted), ADR-010 (Draft — *not* implementable yet).
> **Inventory + shapes:** `temporal-full-migration.md`. **Product spec:** PRD-016.
> **Last updated:** 2026-09-21 · **Tracking:** the status table below is the tracker.

---

## How to use this

- **Groups are ADR-009 §16's named steps, in order.** Names, not numbers — the sequence has
  been reordered once already and the references into it silently broke (16a). Task IDs are
  `<group-letter>.<n>` and stay stable if a task is inserted.
- **One task per session** is the intended grain. Each task states what it depends on; do not
  start one whose dependencies are not ✅.
- **Every `main` fast-forward is a release** (owner's standing rule). Tasks marked 🚀 are the
  release points; everything between two 🚀 marks ships together. Per-flow releases are wanted
  (owner, 2026-09-11) — attribution is the reason.
- **The drain gate** (zero unprocessed, zero outstanding acks, `nats consumer info --json`) fires
  before **every flow cutover** and before **every v1 deletion** — 16b. It is a runbook step
  inside the tasks that need it, not a separate task.
- **Never fix §3.** If a task tempts you toward a bug in `phase4-backlog.md` §3, stop.
- **Group A is built local-first (owner, 2026-09-21):** each engine piece lands in
  `docker/docker-compose.yml` and is verified there before its infra-repo manifest is written.
  So A.7 is not one task at the end — its compose services arrive alongside A.1–A.6, and a
  task's status shows both halves (local · k8s) until both are ✅.

---

## Status

| # | Task | Status |
|---|---|---|
| **A** | **Engine up** | |
| A.1 | Temporal persistence: second Postgres StatefulSet, two databases | ✅ 2026-09-21 (local + k8s; infra `de903a2`, verified in prod) |
| A.2 | Temporal server Deployment (auto-setup image, standard visibility) | ⬜ |
| A.3 | Namespace registration init Job, retention 30 d | ⬜ |
| A.4 | Temporal Web UI — ClusterIP only, no ingress | ⬜ |
| A.5 | Workflow-worker scaffold in `api/` + `HelloWorkflow` | ⬜ |
| A.6 | Workflow-worker Deployment in the infra repo | ⬜ |
| A.7 | Local dev: compose services for Temporal + workflow worker | 🟡 `temporal-postgres` ✅ 2026-09-21 · `temporal`, `temporal-ui`, `workflow-worker` ⬜ |
| A.8 | 🚀 Engine-up release + prove `HelloWorkflow` in prod; capacity + backup check | ⬜ |
| **B** | **Worker port** (Go → LLM → Playwright) | |
| B.1 | Activity contracts: input/output types + `contracts/` arm | ⬜ |
| B.2 | Go http-worker: `Scrape` activity entry point + mode flag | ⬜ |
| B.3 | Go second Deployment (Temporal-bound) | ⬜ |
| B.4 | `ScrapeProbeWorkflow` + the §9 pre-gate on the Go activity | ⬜ |
| B.5 | 🚀 Go port release | ⬜ |
| B.6 | LLM worker: `LLMExtract` activity (cold start, classifier, heartbeat) | ⬜ |
| B.7 | LLM second Deployment + 🚀 release | ⬜ |
| B.8 | Playwright worker: `PlaywrightScrape` activity (bot wall raises, container contract) | ⬜ |
| B.9 | Playwright second Deployment + 🚀 release; pre-gate on both engines | ⬜ |
| **C** | **Pipeline lane** (layer A — PRD-016, R6 gate) | |
| C.1 | Schema: `pipelines`, `pipeline_versions`, `pipeline_runs`, `pipeline_run_blocks` | ⬜ |
| C.2 | Widen both quota views + the ledger CHECK for the pipeline lane | ⬜ |
| C.3 | Block catalog, per-type config schemas, save-time validator | ⬜ |
| C.4 | Pipelines CRUD API + versioning + delete semantics + admin routes | ⬜ |
| C.5 | Run trigger: admission (three meters + headroom buffer), row insert, workflow start | ⬜ |
| C.6 | Workflow-worker DB activities: mirror, accounting, cancel-check | ⬜ |
| C.7 | `PipelineWorkflow` body | ⬜ |
| C.8 | Clean + Validate activities | ⬜ |
| C.9 | Webhook activity: wire contract, SSRF, four-timeout ladder, `waiting` | ⬜ |
| C.10 | Cancellation: API cancel + workflow cancel + boundary re-check | ⬜ |
| C.11 | Run/result read API + `pipeline_status` notify channel + WS route | ⬜ |
| C.12 | Frontend: Pipelines pages + WS reconnect (11b, both lanes) | ⬜ |
| C.13 | Worker Versioning: decide + enable server-side (⚠️ Architect) | ⬜ |
| C.14 | 🚀 Pipeline-lane release + **R6 acceptance gate** | ⬜ |
| **D** | **Job cutover** | |
| D.1 | Lane marker on `job_runs` (mechanism 4) + v1 dispatcher filters | ⬜ |
| D.2 | `ChangeDetection` activity: content-hash dedup + `diff.py` port (**R5 obligation**) | ⬜ |
| D.3 | `JobWorkflow` + job-lane webhook row activity | ⬜ |
| D.4 | Single dispatch switch in `dispatch.py` (`create_job` + scheduler) + cancel path | ⬜ |
| D.5 | 🚀 Job cutover release: drain gate → flip → verify; rollback runbook | ⬜ |
| **E** | **Batch and crawl cutover** | |
| E.0 | Gate: ADR-010 Accepted; task-queue shape decision (per-host limiter / lanes) | ⬜ |
| E.1 | `BatchWorkflow` (fan-out child `JobWorkflow`, absolute-total notify) | ⬜ |
| E.2 | 🚀 Batch cutover release: drain gate → flip → verify | ⬜ |
| E.3 | Crawl frontier: table shape + admission activity (SSRF, eTLD+1, dedup index) | ⬜ |
| E.4 | Crawl fetch-side activities: robots + sitemap on `httpx`, link extraction port | ⬜ |
| E.5 | `CrawlWorkflow`: BFS via activities, continue-as-new, per-page ledger + ceiling | ⬜ |
| E.6 | Crawl compensating gate (no v1 reference exists) | ⬜ |
| E.7 | 🚀 Crawl cutover release; `coordinator/` deletion | ⬜ |
| **F** | **Schedule and webhook cutover** | |
| F.0 | Gate: Schedule overlap policy decided (⚠️ Architect) | ⬜ |
| F.1 | Temporal Schedule per recurring job + quota parking (ADR-010 §1) | ⬜ |
| F.2 | `schedule_status` interlock guard + `active_recurring_jobs` scoping | ⬜ |
| F.3 | Per-job migration runbook (pause v1 → verify → create Schedule) | ⬜ |
| F.4 | Job-lane webhook delivery as an activity; `webhook_deliveries` fate decided | ⬜ |
| F.5 | Three webhook meters scoped to the job lane (15e) | ⬜ |
| F.6 | 🚀 Release; delete `scheduler.py`, `webhook_loop.py`, `advisory.py` (drain gate) | ⬜ |
| **G** | **Consumer deletion** | |
| G.1 | 🚀 Delete `result_consumer.py` (drain gate at deletion) | ⬜ |
| **H** | **NATS removal** | |
| H.1 | Delete NATS-bound worker Deployments (obligation 3) | ⬜ |
| H.2 | 🚀 Remove NATS: stream init, StatefulSet, client deps, `nats_stream_seq`, contracts NATS arm | ⬜ |
| **I** | **API thinning** | |
| I.1 | Alembic-on-startup made multi-replica safe | ⬜ |
| I.2 | 🚀 `replicas: 2`, `RollingUpdate`; JobNotifier per replica verified | ⬜ |
| **Docs track** (parallel; not on the critical path until named) | | |
| X.1 | Write PRD-019 — before PRD-018; may be written during C | ⬜ |
| X.2 | Promote ADR-010 — needed by E.0 | ⬜ |
| X.3 | Schedule overlap policy → ADR (BUFFER_ONE recommended) — needed by F.0 | ⬜ |

---

## Dependency groups and hard constraints

1. **A before everything.** Nothing connects to a server that is not there. A.1–A.7 are infra-repo
   and scaffold work with **no app release needed** until A.8.
2. **B before C.** The activity workers *are* the pipeline lane's executors (§9 reversal — no
   bridge). Go first (simplest, `NATS_MAX_DELIVER=3` makes the v1 comparison clean), then LLM
   (carries the two §10 Group-B ports), then Playwright (the container contract).
3. **C before D.** The pipeline lane adds a lane; the job cutover *moves* one. Everything the job
   cutover needs from the workflow worker (mirror, accounting, cancel-check, webhook activity) is
   built and proven in C on a lane with no double-execution risk.
4. **D before E, E before F.** `BatchWorkflow` fans out `JobWorkflow`; Schedules wrap `JobWorkflow`.
   Crawls go last inside E (13a — rewrite with no v1 reference).
5. **The lane marker (D.1) is built at the job cutover, not earlier.** Built in B it is inert and
   untestable (16a). Built after D.5 it is a window in which `_recover_stale_pending` re-dispatches
   v2 runs to NATS (§7 mechanism 4).
6. **ADR-010 must be Accepted before E.0.** A Draft is not a decision. The overlap policy (X.3)
   must exist before F.1 creates the first Schedule.
7. **Two documents the owner already committed to are parallel to C**: PRD-019 (X.1) is written
   during A's build and built after A ships; it is not on this backlog's critical path.

---

## Group A — Engine up

> Infra repo `govindappa-k8s-config/clusters/k3s-server/scrapeflow/` unless stated. NATS untouched.
> Cites: ADR-009 §2 (2a–2d), `temporal-full-migration.md` §7.

#### A.1 — Temporal persistence: a second Postgres *instance*

**Why:** §2a — a second database on the app instance buys no isolation; the schema is owned by
`temporal-sql-tool`, not Alembic; the restore posture differs (it is in-flight work).
**What:** clone `infrastructure/postgres.yaml` → `infrastructure/temporal-postgres.yaml`: own
StatefulSet, own PVC (10Gi), own Secret. **Two databases inside it: `temporal` and
`temporal_visibility`** — provisioning one is §2a's "predictable way to lose an hour".
**Verify:** `psql -l` from a pod shows both. Both empty (auto-setup fills them in A.2).
**Depends on:** —
**Local half ✅ 2026-09-21:** `docker/docker-compose.yml` → `temporal-postgres` (`postgres:16-alpine`,
own `temporal_postgres_data` volume, host port **5434** — 5433 was taken by another project's
container on the owner's machine) + `docker/temporal-postgres/init.sql`, bind-mounted into
`/docker-entrypoint-initdb.d/`. `POSTGRES_DB` makes `temporal`; the script makes
`temporal_visibility`. Verified: `\l` lists both, 0 tables in each. ⚠️ Two hook facts the k8s half
inherits: the initdb hook runs **only against an empty `PGDATA`** — adding the script after
Postgres has once started does nothing (reset = drop the volume/PVC); and a bind-mount of a file
that does not yet exist creates a *directory* at that path, so write the script before the first
start. **k8s half ✅ 2026-09-21 — infra repo `de903a2`, applied by Flux in ~35 s, pod ready in 21 s,
Verify line run from the pod: both databases owned by `temporal`, 0 tables each; the boot log shows
`POSTGRES_DB`'s `CREATE DATABASE` and then the hook running `01-visibility-db.sql`.** Files:
`clusters/k3s-server/scrapeflow/infrastructure/temporal-postgres.yaml` (ConfigMap with the same
script + StatefulSet `scrapeflow-temporal-postgresql` + ClusterIP Service), its kustomization
entry, and a README section for the `scrapeflow-temporal-db-credentials` Secret (user/password
only). `POSTGRES_DB` is a *name the server is configured with* (A.2's `DBNAME`/`VISIBILITY_DBNAME`),
so it is hardcoded, not a Secret key. Carries the app manifest's `PGDATA=…/pgdata` (a fresh PVC's
`lost+found` makes `initdb` refuse the mount root — compose never hit this) and `postgres:16` to
match the sibling. `kubectl apply --dry-run=server` passes; node memory limits at 54% before it.
Order that worked: owner created the Secret → push → Flux applied → Verify from the pod.
⚠️ The PVC (`data-scrapeflow-temporal-postgresql-0`) outlives the StatefulSet — a wrong first boot
is reset by deleting it explicitly.

#### A.2 — Temporal server

**TL call:** the `temporalio/auto-setup` image as one Deployment, `DB=postgres12`, standard
visibility, **no** Elasticsearch. Reasoning: it runs `temporal-sql-tool setup/update-schema` on
every start, which is exactly the "server bump needs a schema step nothing in our deploy performs"
trap §2a warns about, handled for free. Cost: a single process for all four services (frontend /
history / matching / worker) — no HA, which §1 already accepts at homelab scale. The Helm chart is
the reversal path if it ever matters.
**What:** `infrastructure/temporal.yaml`: Deployment + ClusterIP Service on 7233; env from A.1's
Secret; a `dynamicconfig` ConfigMap mounted (empty now — C.13 writes into it); resources sized
against §2d (**limits, not requests, are the constraint** — do not overcommit further; see A.8).
**Verify:** `tctl`/`temporal` CLI from a pod: `temporal operator cluster health` → SERVING.
**Depends on:** A.1

#### A.3 — Namespace registration init Job

**What:** clone `app/nats-init-job.yaml` → `app/temporal-init-job.yaml`: register namespace
`scrapeflow` with **retention 30 d** (§2c — an operator dial, changeable later, no correctness
role). Idempotent (namespace-exists is not an error).
**Verify:** `temporal operator namespace describe scrapeflow` shows the retention.
**Depends on:** A.2

#### A.4 — Temporal Web UI

**What:** `temporalio/ui` Deployment + **ClusterIP only**. **No Ingress, no DNS record, no cert**
(§2b — it is a write-capable control plane with no auth of its own; single namespace means every
tenant's runs share one listing). Access: `kubectl -n scrapeflow port-forward svc/temporal-ui 8080`.
Document the port-forward line in the infra README.
**Depends on:** A.2

#### A.5 — Workflow-worker scaffold + `HelloWorkflow`

**TL call — the workflow worker is a second entrypoint of the api image, not a new service.**
It is "the direct successor to `result_consumer.py`'s role" (§8d) and every piece of logic it hosts
— models, `ledger.py`, `quota.py`, `security.py`'s SSRF check, `webhooks.py`'s wire contract,
`diff.py` — lives in `api/app/`. A separate service would either duplicate that (ADR-011 §6's
deliberate-duplicate pattern is for a transport being deleted, not for domain logic that stays) or
install `api/` as a path dependency across build contexts. The cleanup CronJob already runs from
the api image with a different command; this is the same pattern. ⚠️ **Cost, stated:** until
Group I, a workflow-worker-only change rebuilds the api image and triggers an API `Recreate`
rollout (the ~80 s window). Accepted — fix-forward is the standing posture, and extracting a
`workflow-worker/` service later is a directory move, not a redesign.
**What:**
- `temporalio` added to `api/pyproject.toml` (the API needs the client SDK regardless — it starts,
  signals and queries workflows from C.5 on). `uv lock`; diff the lock (the Dependabot-sweep lesson).
- `api/app/workflows/` package: `client.py` (connect from `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`),
  `queues.py` (task-queue name constants — **in `constants.py`-style code, not settings**: queue
  names are contract, like NATS subjects), `hello.py` (`HelloWorkflow` + one activity),
  `worker_main.py` (registers workflows + workflow-worker-queue activities, runs the worker,
  handles SIGTERM).
- `api/app/config.py`: the two settings above.
- Tests: `temporalio.testing.WorkflowEnvironment` time-skipping; one test runs `HelloWorkflow`.
  **This establishes the workflow test pattern every later group reuses.**
**Verify:** `docker compose exec api uv run pytest tests/test_workflows_hello.py`.
**Depends on:** — (can be built before A.1–A.4 land; needs A.7 to run locally)

#### A.6 — Workflow-worker Deployment

**What:** `app/workflow-worker.yaml`: api image, `command: [python, -m, app.workflows.worker_main]`,
**app-DB + MinIO credentials** (this is the *only* pod on the v2 side with DB access — §8d), Temporal
env, `replicas: 1`, `RollingUpdate` (stateless polling — safe). Add an `ImagePolicy`/automation entry
mirroring `api`'s so it follows the same tag.
**Verify:** pod logs `worker started` naming the queue; Web UI (port-forward) lists the poller.
**Depends on:** A.2, A.5

#### A.7 — Local dev

**What:** `docker/docker-compose.yml`: `temporal-postgres`, `temporal` (auto-setup), `temporal-ui`
(host port for the browser — fine locally), `workflow-worker` (api image, the A.6 command, `api/`
mounted like the api service so it hot-reloads). Temporal env on the `api` service.
**Verify:** `HelloWorkflow` started via a one-line script shows in the local UI.
**Depends on:** A.5
**Progress:** `temporal-postgres` ✅ 2026-09-21 (see A.1). Next compose service is `temporal`
(A.2's auto-setup image) — it must set `DBNAME=temporal` and `VISIBILITY_DBNAME=temporal_visibility`
explicitly (they are the image defaults, but the wiring should be readable from the file) and
`depends_on: temporal-postgres: condition: service_healthy`.

#### A.8 — 🚀 Engine-up release + proof

**What:** ff `main` (the api image rebuilds for A.5; nothing else changes behaviour — no NATS
message, no schema, no route). Flux applies A.1–A.6. Then:
1. Start `HelloWorkflow` from inside the workflow-worker pod; watch it complete in the port-forwarded UI.
2. **Capacity:** `kubectl describe node` — requests and **limit overcommit** before/after. Record
   both in the handoff. §2d: a headed render + a history burst = CFS throttling on the history
   service that *looks like a workflow bug*. Write that sentence next to the numbers.
3. **Backups:** the Temporal PG is now in-flight work (§10 risks). ⚠️ **Owner item:** name where
   its backup lives, or record that it does not yet. Not blocking; must not be silent.
**Depends on:** A.1–A.7

---

## Group B — Worker port

> Additive. Nothing routes to the Temporal-bound deployments until C.14. NATS deployments untouched.
> Cites: ADR-009 §9 (sequence, costs, pre-gate), §10 (Group B ports, the three non-retryable
> places, Rider 1 + 2), ADR-008, `CLAUDE.md` Key decisions (container start, classifiers).

#### B.1 — Activity contracts + the `contracts/` arm

**Why:** P6's lesson — three consumer-side schemas and zero producer-side ones *was* BUG-005. The
activity boundary is a new wire; it gets a producer-side definition on day one.
**What:**
- `api/app/workflows/activities/contracts.py`: `ScrapeInput`, `ScrapeOutput`, `LLMInput`,
  `LLMOutput` (Pydantic; dataclass-compatible for the SDK). **Field set = today's `ScrapeMessage`
  / `LLMMessage` minus transport fields** (`schema_version`, `run_id`, `nats_*`). `artifact_id`
  stays the lane-neutral key (ADR-011) — for pipelines it will be `pipeline_run_id/block_id`.
  Outputs carry `result_path`, `bytes` (the worker knows the size; the accountant does not need
  MinIO), `content_hash`, `warnings`, `screenshot_paths`, and the error string on a terminal failure.
- The three workers get the consumer-side twins (Go struct, two Pydantic models).
- `contracts/` gains an arm: API-side input → each worker's real activity-input parser, Go via
  committed fixtures (**not optional** — the `null`→`""` silent variant is Python→Go only).
**Verify:** the contract test command in the handoff's *Commands*, extended.
**Depends on:** A.5

#### B.2 — Go http-worker: `Scrape` activity + mode flag

**What:**
- `go.temporal.io/sdk` in `go.mod` (`go.sum` — Go is already reproducible).
- `internal/activity/scrape.go`: `Scrape(ctx, ScrapeInput) (ScrapeOutput, error)` wrapping the
  existing `processJob` (§9: "already has the shape Temporal wants"). Untouched: `fetcher`,
  `formatter`, `robots`, MinIO upload.
- **Classifier stays; Temporal retries.** Wrap: catch → `classifyMinIO` inside the existing
  `*uploadError` scoping → terminal ⇒ `temporal.NewNonRetryableApplicationError(...)`; transient ⇒
  return the error (retryable). **No in-activity retry loop.** Fail-closed default preserved.
- `activity.RecordHeartbeat` around the fetch (cheap; establishes the pattern).
- `WORKER_MODE=nats|temporal` selects the entry point in `cmd/worker/main.go`. NATS path unchanged.
- Tests: `testsuite.TestActivityEnvironment` — one success, one transient (retryable error type),
  one terminal (non-retryable), one dead-target-site (terminal — §10's Go divergence).
**Depends on:** B.1

#### B.3 — Go second Deployment

**What:** `app/http-worker-temporal.yaml` — same image, `WORKER_MODE=temporal`, Temporal env, **no
NATS env, no DB env** (light-worker rule survives). `ImagePolicy` shared with the NATS one.
**Depends on:** B.2

#### B.4 — `ScrapeProbeWorkflow` + the §9 pre-gate

**Why:** 16d — the R6 gate runs on a lane with **no fallback**, which makes §9's pre-gate a
requirement: separate "the adapter is wrong" from "the model is wrong" *before* the model exists.
**What:** `api/app/workflows/probe.py`: a dev/operator-only workflow that runs one `Scrape` activity
on the scraper queue with a given `ScrapeInput` and returns the output. A script under
`api/scripts/` starts it and prints the `result_path`. **Gate:** same URL through v1 (`POST /jobs`,
`engine=http`) and through the probe; diff the two MinIO objects (byte-equal expected on `http` —
no LLM, no nondeterminism). Record the result in the handoff.
**Depends on:** B.3 deployed (A.8-style: ff `main` for B.2 first — B.5)

#### B.5 — 🚀 Go port release

**What:** ff `main`; `rollout status` on **both** http-worker Deployments; NATS consumer
`go-worker` unchanged (`nats consumer info --json`). Then B.4's gate against prod.
**Depends on:** B.2, B.3

#### B.6 — LLM worker: `LLMExtract` activity

**What:**
- `temporalio` in `llm-worker/pyproject.toml`. ⚠️ **`llm-worker/` has no lockfile (BUG-013).**
  Add one here — a new dependency on the reproducibility-blind list is how the `httpx`/`httpx2`
  crash-loop happened. Owner's call whether to pin the `anthropic`/`openai` majors at the same time.
- `worker/activity.py`: `llm_extract(LLMInput) -> LLMOutput` wrapping `call_llm` + MinIO upload.
  **Ports intact (§10 Group B):** `ensure_ready()` before the call; `llm_max_retries=0` stays pinned
  on both SDK clients (R4 — one visible retry layer).
- **Rider 2:** `activity.heartbeat()` every ≤30 s through warm-up *and* the call. The workflow
  (C.7) sets `heartbeat_timeout`; both halves are required or it fails every cold start / hangs.
- **Rider 1:** the activity's docstring states the start-to-close requirement: **≥ warm-up +
  request = ≈360 s against production** (`LLM_REQUEST_TIMEOUT_SECONDS=180` in the infra repo),
  not the 240 s the repo defaults imply. C.7 reads it from there.
- Classifier: `errors.classify()` unchanged → terminal ⇒ `ApplicationError(..., non_retryable=True)`;
  transient ⇒ raise (Temporal retries). Delete nothing from `errors.py`.
- `WORKER_MODE` flag; NATS path untouched.
- Tests: `ActivityEnvironment`; cold-start path (mocked `/models` slow then awake) heartbeats;
  429 → retryable; 401 → non-retryable; unknown → non-retryable.
**Depends on:** B.1

#### B.7 — LLM second Deployment + 🚀 release

**What:** `app/llm-worker-temporal.yaml` (same env as the NATS one minus NATS; keep
`LLM_REQUEST_TIMEOUT_SECONDS=180`). Release; `rollout status` on both.
**Depends on:** B.6

#### B.8 — Playwright worker: `PlaywrightScrape` activity

**What:**
- `temporalio` in `playwright-worker/pyproject.toml` — **add a lockfile** (same BUG-013 note).
- `worker/activity.py`: wraps the existing `worker.py` scrape (Patchright, stealth, actions,
  `blocking.py`, `formatter`, screenshots, MinIO). Untouched internals.
- **§10's third non-retryable place:** `detect_block()` *returns* today and the worker publishes
  `failed`. In the activity a block **raises `ApplicationError(non_retryable=True)`** with
  `error="blocked:<vendor>"`, and so does a robots.txt disallow. Otherwise Temporal re-renders the
  same wall from the same IP three times.
- Heartbeat through the render (replaces `msg.in_progress()`); storage classifier as in B.6.
- **⚠️ Container contract, preserved exactly:** `entrypoint.sh` unchanged in shape — Xvfb → wait
  for the socket → `exec python -m <module>`. `WORKER_MODE` selects the module *inside* the
  `exec`, nothing else changes; **never `xvfb-run`**, `PYTHONUNBUFFERED=1` stays, `/tmp/.X11-unix`
  pre-created. A test asserts the entrypoint still ends in `exec python`.
- Tests: `ActivityEnvironment`; a wall fixture (the Myntra 481 B body) → non-retryable; a normal
  page → output; a MinIO 5xx → retryable.
**Depends on:** B.1

#### B.9 — Playwright second Deployment + 🚀 release + pre-gate on both engines

**What:** `app/playwright-worker-temporal.yaml` (dshm volume, resources, Xvfb env — copy the NATS
one exactly). Release; `rollout status` on both. Then B.4's probe with `engine=playwright`
against a real page: v1 vs probe outputs compared on structure (headed Chrome is not byte-stable).
**Depends on:** B.8, B.4

---

## Group C — Pipeline lane (layer A)

> The largest group and the first one with product surface. Cites: PRD-016 R1–R6; ADR-009 §3–§8
> (tables, blocks, references, pinning, metering, ledger, wall), §11 (mirror, reconnect), §15
> (webhook), 16d (no fallback). **R6 is the exit gate.**

#### C.1 — Schema

**What:** one Alembic revision (the pause-then-`run` procedure in the handoff's *Commands* —
strip the `idx_webhook_deliveries_dedup` false positive; name every FK):
- `pipelines` (`id`, `user_id` FK CASCADE, `name`, `deleted_at` nullable, timestamps;
  `UNIQUE (user_id, name)` — the soft-deleted row **keeps** the name, 409 on reuse — §6).
- `pipeline_versions` (`id`, `pipeline_id`, `version`, `definition JSONB`, `created_at`;
  immutable rows; `UNIQUE (pipeline_id, version)`).
- `pipeline_runs` (`id`, `pipeline_id`, `pipeline_version_id` — the pin, §6 — `user_id`,
  `status CHECK IN ('running','completed','failed','cancelled')`, `cancel_requested_at` nullable
  (R3: "cancellation in progress" is visible), `inputs JSONB`, `workflow_id`, `result_path`,
  timestamps). ⚠️ `running` is the only in-flight value; do not add stage states — that is Q8.
- `pipeline_run_blocks` (`id`, `pipeline_run_id`, `block_id` (the definition's stable ID — §4),
  `position`, `type`, `status CHECK IN ('pending','running','completed','failed','skipped','waiting')`
  — **all six from day one, §4 + 15a** — `input_ref`, `output_ref`, `error`, `started_at`,
  `finished_at`, `collected_at` nullable (8c: *collected* renders as collected, never as 404)).
**Verify:** `alembic check` reports only the standing false positive; models + views round-trip.
**Depends on:** A.5 (models live in `api/app/models/`)

#### C.2 — Widen both quota views + the ledger CHECK

**Why:** without an arm, pipeline runs consume none of the three meters **by construction** — P7's
bug on a new lane (16e). Without the FK, the accounting activity's first insert fails loudly in dev
(§8d — that is the designed failure).
**What:** same revision as C.1 or the next one — **drop both views, recreate both** (the
`CLAUDE.md` *Run-counting views* rule; hand-written SQL; `quota_views.py` `Table`s updated):
- `quota_run_units` + arm: one row per `pipeline_runs` row (one run = one unit, §8).
- `quota_active_submissions` + arm: a `pipeline_runs` row **with at least one block in `running`**
  (15a — `waiting` holds no slot; v1 arms unchanged, R5).
- `storage_objects`: add `pipeline_run_block_id` FK (CASCADE), widen the CHECK to
  `num_nonnulls(job_run_id, crawl_page_id, pipeline_run_block_id) = 1`. `ledger.record_object`
  gains the kwarg; `release_pipeline_run_objects` added (enumerate rows; 503 rule).
- Tests: the P7 pattern — one test per arm, mutation-checked against the old view; a ledger test
  for the new FK.
**Depends on:** C.1

#### C.3 — Block catalog + validator

**What:** `api/app/pipelines/` (shared by API and workflow worker):
- Five types, each with a Pydantic config schema, `consumes`/`produces` reference types,
  `kind ∈ {content, effect}` (§5), `bindable_fields` (Scrape: `url` only — §4), a declared
  time budget with a default (R4).
- `validate(definition)` at save time, errors naming block + reason (R1): unknown type; invalid
  config; **exactly one starting block and it is a Scrape — written as its own rule, per §8** (do
  not let it fall out of two other rules); single chain in both senses (block *n* consumes *n−1*);
  run-input references declared; ≤1 Webhook (message names layer C); limits (`max_blocks_per_pipeline`,
  `max_pipelines_per_user` settings); **budgets compose** — a run ceiling shorter than the sum of
  block budgets fails at save.
- Historical-shape obligation (§4): a version's `config_schema_version` per block type; adding a
  required field needs a default for saved definitions. Write the rule in the module docstring.
- Tests: one per validation rule, positive + negative; the R6 recipe validates.
**Depends on:** C.1

#### C.4 — Pipelines CRUD + versioning + delete + admin

**What:** `routers/pipelines.py`: `POST/GET/PATCH/DELETE /pipelines[/{id}]`, `GET /pipelines/{id}/versions`.
- Update = new `pipeline_versions` row; **block IDs carry through** — an ID present in the update
  is the same logical step, absent means deleted, a block without one gets a new ID (§4). The
  client sends IDs; the server never regenerates one it was given.
- Delete: no runs → hard delete; runs → soft delete, name held, 409 on reuse (§6).
- Cross-tenant 404; `/admin/pipelines*` list + read (R5).
- Tests: CRUD, 404, 409, version increments, ID stability, soft-delete branch.
**Depends on:** C.3

#### C.5 — Run trigger

**What:** `POST /pipelines/{id}/runs` (body: run inputs):
1. Ownership check (the **only** tenant boundary — §12).
2. Admission against all three meters: the two views (C.2) and storage **with the headroom
   buffer** (§8d — refuse to *start* near the wall; new setting `storage_headroom_bytes`, an
   operator dial; the response names which meter refused).
3. One transaction: `pipeline_runs` (`running`) + N `pipeline_run_blocks` (`pending`), pinned
   `pipeline_version_id`.
4. Start `PipelineWorkflow` with id `pipeline-run-{id}`, **`WorkflowIdReusePolicy.REJECT_DUPLICATE`**
   (§7 mechanism 2 — the default is `ALLOW_DUPLICATE`), **the definition as the input argument**
   (§6 — the body never loads it), task queue = workflow-worker queue.
5. Commit **before** start (the DB-row-is-the-recovery-path rule); if start fails, mark `failed`
   with a named error. ⚠️ Temporal down → the run fails loudly at trigger; it does not queue.
- Tests: admission per meter; buffer; REJECT_DUPLICATE pinned (assert on the start call);
  definition passed as argument.
**Depends on:** C.2, C.4, A.5

#### C.6 — Workflow-worker DB activities

**What:** `api/app/workflows/activities/db.py`, registered on the workflow-worker queue only (§8d —
routing enforces the no-DB rule on scraper pods):
- `mirror_block_status(run_id, block_id, status, refs, error)` and `mirror_run_status(run_id, status)`:
  write the row **and** `pg_notify('pipeline_status', json)` in one transaction. **Precedence rule
  (11a):** read `pipeline_runs.status` first; if `cancelled`, do not overwrite, re-notify, return
  `cancelled` so the workflow stops. Payload: identifiers + status only, absolute state (11d);
  never error text. **A failed mirror fails the run (11c)** — no swallow.
- `record_storage(run_id, block_id, object_key, bytes, user_id)`: `ledger.record_object(pipeline_run_block_id=…)`.
  Idempotent by `UNIQUE (object_key)` (a retried activity is a no-op).
- `check_cancelled(run_id) -> bool` (15d fallback).
- `on_storage_wall(run_id, block_id)`: the run **completes and is charged** (§8d); the response
  names block + quota + bytes (R3 "legible") — a distinct error code, not `storage_accounting_failed`.
- Tests: precedence rule mutation-checked (a mirror after cancel must not flip it); idempotent
  ledger insert; notify payload shape.
**Depends on:** C.1, C.2

#### C.7 — `PipelineWorkflow`

**What:** `api/app/workflows/pipeline.py`. Deterministic body (no I/O, no `datetime.now()` — §6;
a review rule, add it to the PR checklist):
- For each block in order: mirror `running` → execute the block's activity on **its** queue
  (Scrape → the engine's scraper queue; LLM → LLM queue; Clean/Validate → C.8's home; Webhook →
  workflow-worker queue) with `start_to_close` from the declared budget (**LLM ≥ 360 s in prod**,
  B.6), `heartbeat_timeout` set where the activity heartbeats, `RetryPolicy` per block type
  (non-retryable errors are raised by the activity, not listed here — §10) → `record_storage` for
  content blocks → mirror `completed` with refs.
- References only: an activity returns `result_path`, never bytes (§5). Effect blocks pass their
  input ref through.
- Terminal failure: mirror `failed` (block + run), remaining blocks `skipped`; stop.
- Cancellation (R3 + 15d): at every block boundary `check_cancelled`; on `True` mirror the run
  `cancelled`, remaining `skipped`, **completed blocks' outputs stay** (R3). Also handle
  `asyncio.CancelledError` from a workflow cancel (C.10) at the same boundaries.
- Result: mirror `pipeline_runs.result_path` = last **content** block's output (§5).
- Tests (time-skipping env, activities mocked): R6 recipe happy path; block failure → skipped
  tail; cancel at boundary; storage wall → completed + named error; the budget composition.
**Depends on:** C.6, B.1

#### C.8 — Clean + Validate activities

**TL call — home: the LLM worker's Temporal deployment, on a `content` task queue.** Both read an
object from MinIO and (Clean) write one; neither renders a page nor needs a database. The LLM image
already has the MinIO client and content handling; the workflow worker stays orchestration + DB;
scraper pods stay hostile-page renderers. Reversible — activities move by re-registering.
**What:**
- `Clean`: boilerplate strip over the scraped object → new object `pipelines/{run}/{block}.md`.
  ⚠️ **Algorithm is a product/design gap** — PRD-016 R2 says "strips boilerplate (nav, ads,
  scripts)"; `competitor-research.md` §B has a scored-ladder proposal. **Minimum viable:** run the
  existing markdown formatter's tag stripping with an expanded denylist; record the choice as a
  known R6-irrelevant simplification (R6's recipe has no Clean block).
- `Validate`: declarative rules only (schema / type / presence / comparison to a constant — R2);
  failing rule ⇒ `ApplicationError(non_retryable=True)` naming the rule; passes its input ref through.
- Tests: rule matrix; Clean on a fixture page; both idempotent on retry (deterministic keys — §5).
**Depends on:** B.6 (image + queue plumbing), C.7

#### C.9 — Webhook activity

**What:** `api/app/workflows/activities/webhook.py` on the workflow-worker queue (the SSRF check
and wire contract live in `api/app/core/` already — port, do not rewrite):
- **Wire contract byte-identical** (§10 Group A): HMAC-SHA256 over raw bytes,
  `X-ScrapeFlow-Signature: sha256=<hex>`, header always sent (`b""` secret), success = `< 300`,
  **10 s POST timeout**. Add the test the repo never had: a fixture receiver asserting header
  name + hex + threshold.
- Payload: today's fields; **the two R6 divergences decided here and recorded in PRD-016 R6**:
  `job_id` **omitted** and `pipeline_id` + `pipeline_run_id` added (TL call — null would be a lie
  about shape); `diff_detected: false`, `diff_summary: null` constants until Monitors.
- **SSRF re-validation on every attempt** ⇒ failure is `non_retryable` immediately (§10 place 2).
- **The four-timeout ladder (15c), set where it says:** POST 10 s (code) · `start_to_close` ~20 s
  and `schedule_to_close` ≥ 2.6 h + `maximum_attempts 5` + `RetryPolicy(initial 30 s, ×10,
  ceiling 7200 s)` (C.7's call site) · the run budget > that (C.3's validator).
- **`waiting` (15a) under a `RetryPolicy` — TL reading of the ADR:** the workflow is blocked
  inside one `execute_activity` across retries, so only the activity can flip the state. On
  attempt start: `check_cancelled` (15d's boundary re-check — raise `non_retryable` if cancelled)
  then mirror `running`; on a retryable failure: mirror `waiting`, then raise. `activity.info().attempt`
  is the counter. ⚠️ **Raise to Architect if this reads as rebuilding the loop** — it is not: no
  sleeps, no second counter, Temporal owns the schedule (15b).
- Tests: header/threshold; SSRF → non-retryable, no attempt; state flips per attempt.
**Depends on:** C.6, C.7

#### C.10 — Cancellation end to end

**What:** `POST /pipelines/{id}/runs/{run_id}/cancel`:
1. Write `cancel_requested_at` + notify (instant UI — 11a; the row is **not** yet `cancelled`,
   R3: never report cancelled before it stopped).
2. **Best-effort `handle.cancel()`** to the workflow (15d primary; Temporal down ⇒ fallback only).
3. Response says what is still running and the upper bound (the block's declared budget — R3),
   and whether an LLM block has already billed.
- The workflow (C.7) turns the cancel into `cancelled` at the next boundary; the mirror carries the
  precedence rule so a late activity result cannot flip it back.
- Tests: cancel mid-block → `cancel_requested_at` set, status still `running`; boundary →
  `cancelled` + tail `skipped`; Temporal-unreachable path still cancels via fallback.
**Depends on:** C.6, C.7, C.9

#### C.11 — Read API + notify channel + WS

**What:** `GET /pipelines/{id}/runs`, `GET …/runs/{run_id}` (per-block status/timing/refs; a
collected output renders as `collected` — 8c), `GET …/runs/{run_id}/result` (ownership-checked
**before** resolving the bare path — §5/§12), admin twins. `JobNotifier`: third listener on
`pipeline_status`, subscriber map, `subscribe_pipeline_run`; WS route mirroring `subscribe_job`
(300 s timeout kept, 4029 kept — 11b). ⚠️ **BUG-009 (`JobNotifier` never reconnects) becomes more
load-bearing here** — a second channel and multi-hour runs. Backlog §4 defers it; **recommend the
owner pull it in at this task** (detect drop, re-register every channel on backoff, log loudly).
**Depends on:** C.1, C.6

#### C.12 — Frontend

**What:** Pipelines list / editor (JSON-first is acceptable for layer A; Monaco exists) / run
detail with per-block status and the cancel-in-progress state (R3). **11b reconnect on both
lanes:** `JobDetail.tsx` and the new page reconnect on any close without a terminal message, with
backoff, honouring 4029. `tsc -b && vite build` is the only verification; click-through after
release. ⚠️ BUG-018 (token caching, tabled) touches the same fetch path — owner's call whether it
rides here.
**Depends on:** C.11

#### C.13 — Worker Versioning (⚠️ Architect decision + infra task)

**Why:** the deferral table's trigger arrives **in layer A**: a Webhook block parks a run ≈2.6 h,
and a rolling deploy inside that window cannot be drained. Worker Versioning is GA and the stated
default; `patched()` is the alternative. Needs **server-side enablement** (A.2's `dynamicconfig`).
**What:** the decision (Architect), the dynamicconfig entry, the worker's deployment-version
stamp in `worker_main.py` and the three activity workers, and a written deploy rule in the handoff.
**Depends on:** A.2, C.7 — **must be in place before C.14's release** or the first mid-run deploy
breaks a parked run.

#### C.14 — 🚀 Pipeline-lane release + R6 gate

**What:** ff `main` (api image: schema + routes + worker; LLM image: C.8). Migrations run on
startup under `Recreate`. Then **R6, judged as PRD-016 writes it**: the `scrape → LLM → webhook`
recipe as a pipeline against the same URL and schema as a v1 job; same blocks in order with
expected outcomes; same schema populated; artifact retrievable; webhook payload same shape **with
the two recorded exceptions**; the four divergences read as known exclusions. Measure and record
the repeat-run cost delta (one LLM call + one stored artifact — R6). **Record the verdict in
PRD-016 and the handoff.** No block outside R2's catalog is designed until this passes.
**Rollback:** there is none — switch the feature off (16d). Say so in the release notes.
**Depends on:** C.1–C.13

---

## Group D — Job cutover

> The first *moved* lane: double-execution risk is real from here. Cites: ADR-009 §7 (all four
> mechanisms), 16b (drain gate at cutover), 16d, §10 (homeless pair), §11a.

#### D.1 — Lane marker (mechanism 4) + v1 dispatcher filters

**What:** migration: `job_runs.lane VARCHAR CHECK IN ('v1','v2') NOT NULL DEFAULT 'v1'` (adding a
column does **not** require dropping the views — only `DROP COLUMN`/`ALTER TYPE` does). Written
**in the insert transaction** by every `JobRun(...)` constructor (`routers/jobs.py`,
`routers/batch.py`, `core/scheduler.py`). Filters: `_recover_stale_pending` selects `lane='v1'`
only; `advisory.py` already matches on `nats_stream_seq` (safe by accident — leave it, it goes in
F.6). **Optional belt-and-braces (16b residual):** `result_consumer._handle_result` refuses a
result for a `lane='v2'` row — cheap, recommended.
**Tests:** recovery skips a v2 row (mutation-checked against the unfiltered query).
**Depends on:** C.14 (built *at* the cutover — constraint 5)

#### D.2 — `ChangeDetection` activity (⚠️ TL finding — raise to Architect)

**Why:** ADR-009 §10 parks content-hash dedup + `diff.py` as "relocated, not re-homed, wait for
Monitors" — **that is the pipeline lane's answer.** On the **job lane**, R5 forbids user-visible
change, and today's job path does dedup (skip LLM, delete the new object, repoint `result_path`)
and the reporting diff (`diff_detected`/`diff_summary` in the payload). `JobWorkflow` replaces
`result_consumer.py` for jobs, so it must carry both or the job cutover regresses jobs. The ADR
does not say this; it is implied by R5 and by the sequence.
**What:** `api/app/workflows/activities/change_detection.py` on the workflow-worker queue:
`_compute_content_hash` + the dedup branch + `diff.py` calls, ported **with the hazard named in
the docstring** (cross-run object sharing breaks per-run collection — 8c; and the shared object
must be charged once: on a match, release the new object's ledger row rather than leave two).
Regular job path only (not batch, not crawl — today's rule). Fail-open on hash error preserved.
**Tests:** match → LLM skipped, object deleted, ledger released, `result_path` repointed; no match
→ diff populated.
**Depends on:** C.6

#### D.3 — `JobWorkflow` + job-lane webhook row

**What:** `api/app/workflows/job.py`, id `job-run-{run_id}`, `REJECT_DUPLICATE`, input = the
`ScrapeInput` built by `dispatch.py`'s existing builders (byte-equality tests keep holding):
Scrape → `record_storage` → `ChangeDetection` → (LLM → `record_storage`) → mirror `completed` on
`job_runs` via the **existing** `job_status` channel (positional payload — do not widen it, §11)
→ **`create_webhook_delivery` row** through a workflow-worker activity, so `webhook_loop.py`
delivers exactly as today (fire-and-forget; an undelivered webhook never fails a job — R6's third
divergence is a *pipeline* property). `job.failed` on failure. Storage wall: today's job-lane
behaviour (`result_consumer.py` deletes the result and fails the run) — **keep it on the job lane**
(R5); §8d's finish-and-charge is the pipeline rule.
**Tests:** happy path; LLM-less job; failure → `job.failed` row; cancelled-before-result discards
(precedence rule).
**Depends on:** D.1, D.2, C.6

#### D.4 — One dispatch switch + cancel path

**What:** `dispatch.py` gains `dispatch_run(job, run, credentials)` that either publishes to NATS
(`lane='v1'`) or starts `JobWorkflow` (`lane='v2'`) on a setting `JOB_LANE=v1|v2`. **Both**
`create_job` and `_dispatch_due_jobs` call it (so scheduled runs and on-demand runs land on the
same lane per the switch; `_recover_stale_pending` keeps re-publishing — v1 rows only). Cancel
routes (`jobs.py`, `admin.py`) add the best-effort workflow cancel for `lane='v2'` rows. MCP
unchanged (it calls the API).
**Tests:** switch → correct lane, marker set in the same transaction; cancel signals only v2.
**Depends on:** D.3

#### D.5 — 🚀 Job cutover release

**Runbook:** ff `main` with `JOB_LANE=v1` (code lands dormant) → verify rollouts → **drain gate**
(job flow: no `pending`/`running` v1 job runs; `go-worker`, `python-playwright-worker`,
`python-llm-worker`, `api-result-consumer` at zero/zero via `--json`) → flip `JOB_LANE=v2` in the
infra repo (a ConfigMap change, not a rebuild) → submit one http and one playwright job → watch
both in the UI and the SPA. **Rollback:** flip to `v1`; in-flight v2 runs finish on v2 (they are
marked); no drain needed in that direction.
**Depends on:** D.1–D.4

---

## Group E — Batch and crawl cutover

> Cites: ADR-009 §13 (13a–13d), §8d (per-page ceiling), ADR-010 §2, BUG-010 part 1,
> `competitor-research.md` §C.

#### E.0 — Gate

1. **ADR-010 Accepted** (X.2). It is Draft; a Draft is not implementable.
2. **⚠️ Owner decision:** `CLAUDE.md` records two crw mechanisms "owed before the batch-and-crawl
   cutover shapes the task queues" — a **per-eTLD+1 host limiter** and **interactive/batch reserved
   lanes** — while the owner deferred the crw work "until after the Temporal pipeline". Both
   statements are on record. Decide here whether E.1/E.5 build against a single scraper queue per
   engine (today's shape; a 100-URL batch queues every single job behind it) or two lanes. This is
   the one-way-ish door; everything else in §C is activity-port input.
**Depends on:** D.5

#### E.1 — `BatchWorkflow`

**What:** fan out one child `JobWorkflow` per item (`job-run-{run_id}` ids, `REJECT_DUPLICATE`,
lane marker on each row), fan-in = await all; `batch_status` notify with **absolute totals** (11d —
the existing JSON channel); batch webhook row at completion via the D.3 activity; one concurrency
slot per batch (already the view's rule). `dispatch.py`'s batch builder feeds the child inputs.
**Tests:** N children; partial failure totals; cancel fans out.
**Depends on:** D.3, E.0

#### E.2 — 🚀 Batch cutover release

**Runbook:** as D.5 with `BATCH_LANE`; drain gate on the batch flow; one 3-URL batch verified.
**Depends on:** E.1

#### E.3 — Crawl frontier: table shape + admission activity

**What:** decide `crawl_queue`'s shape at build time (13b — one table for queue + seen-set, or two;
**the dedup mechanism is the index `UNIQUE (crawl_id, url)` + `ON CONFLICT DO NOTHING`**, keep it).
`admit_urls(crawl_id, urls)` activity on the workflow-worker queue — the **one** place seed, extracted
links and sitemap entries converge (13d): `validate_no_ssrf` per URL (**BUG-010 part 1**; rejected
⇒ skipped, crawl continues, never retried); **eTLD+1 scope** for sitemap entries (ADR-010 §2 —
pinned offline public-suffix list, **with a lockfile**); include/exclude filters; insert with the
index. `crawl_pages` row per admitted URL (P7's unit; 13c).
**Depends on:** E.0

#### E.4 — Crawl fetch-side activities

**TL call — home: the Playwright worker's Temporal deployment** (Python, `httpx` already there in
`robots.py`, no DB; target-facing fetches stay on scraper pods — the aiohttp CVE was a response
parser, so "not a browser" is not "not hostile"). **`sitemap.py` ported to `httpx` — a change, not
a copy (13d; BUG-006 is why).** `link_extractor.py` ported intact. Both "port and then actually
run" (13a — neither has ever executed).
**Tests:** sitemap parse fixtures incl. a sitemap index; robots fetch; the SSRF-bait fixture from
13d's chain passes through E.3's admission and is skipped.
**Depends on:** B.8, E.3

#### E.5 — `CrawlWorkflow`

**What:** BFS through activities only (never reads Postgres in the body — §6): pop batch →
`JobWorkflow`-shaped scrape per page (or the scrape activity directly — TL call: **direct
activity**, a crawl page is not a job and must not create `job_runs` rows) → `record_storage(crawl_page_id=…)`
(**the per-page insert P7 deferred**) → **per-page storage ceiling check** (§8d — the buffer does
not cover crawls) → extract links / sitemap on the seed → `admit_urls` → **`continue-as-new`** every
N pages (13b — mandatory) → completion when the frontier is empty or `max_pages` reached → crawl
webhook via **`create_webhook_delivery`** (not the coordinator's bypass — §3 row) → `crawls.status`
terminal. `crawl_status` notify if the SPA needs it (product call — 13c).
**Tests:** time-skipping env; frontier exhaustion; `max_pages` cap; continue-as-new boundary;
ceiling hit mid-crawl completes and charges.
**Depends on:** E.3, E.4

#### E.6 — Crawl compensating gate

**Why:** 13a — no v1 crawl ever completed, so §9's diff-against-v1 pre-gate **cannot exist**.
**What:** a controlled fixture site (a small static site in `contracts/fixtures/crawl-site/` served
from a pod or compose service) with a known link graph, a sitemap, a robots.txt, an SSRF-bait
sitemap entry, a cross-domain sitemap entry and an out-of-scope subdomain. Expected page set is
written down; the crawl must produce exactly it, charge exactly its bytes, skip the bait.
**Depends on:** E.5

#### E.7 — 🚀 Crawl cutover release + `coordinator/` deletion

**Runbook:** `POST /crawls` routes to `CrawlWorkflow` (no drain gate needed — the crawl flow has
never completed on v1; **cancel any `running` v1 crawl rows first**, `audit_crawl_quota.py` lists
them — none in prod as of 2026-09-19). Delete `coordinator/` (service, image, CI job, compose,
infra Deployment, `coordinator/messages.py`'s deliberate duplicate). BUG-008 and BUG-012 dissolve
here — close them as dissolved, not fixed.
**Depends on:** E.6

---

## Group F — Schedule and webhook cutover

> Cites: ADR-009 §7 mechanism 3, 16c, §15 (15e), ADR-010 §1 + rider, `phase4-backlog.md` gotcha 6.

#### F.0 — Gate: overlap policy (⚠️ Architect)

`BUFFER_ONE` recommended (gotcha 6). Not decided. Without it a parked run under an hourly
schedule stacks one workflow per tick. Written as an ADR (X.3) before F.1 creates a Schedule.
**Depends on:** X.3

#### F.1 — Temporal Schedule per recurring job + quota parking

**What:** `ScheduledJobWorkflow` (or `JobWorkflow` with a first step): **fires unconditionally**;
first step consults the views and **parks per meter** (ADR-010 §1: concurrency + monthly park on a
durable timer and re-check, storage fails now naming the remedy). Schedule created/paused/deleted
by the API on `schedule_cron` / `schedule_status` writes for **v2-owned** schedules; overlap policy
from F.0. `next_run_at` for v2 comes from the Schedule (`describe`), not the row.
**Tests:** time-skipping: parked run releases when the meter clears; storage breach fails.
**Depends on:** D.3, F.0

#### F.2 — `schedule_status` interlock guard + meter scoping

**What (16c, both recorded obligations of this step):** (1) a job whose schedule is on v2 must
not be re-armed on v1 by `PATCH {"schedule_status": "active"}` — a `schedule_lane` column (or the
D.1 marker's sibling on `jobs`) that the v1 scheduler filters on and the PATCH handler respects;
(2) `active_recurring_jobs` renamed/scoped to *v1 recurring jobs* or made lane-aware — **naming,
not features**. `UsageStats.tsx` label updated.
**Depends on:** F.1

#### F.3 — Per-job migration runbook

**Order (mechanism 3, counter-intuitive):** pause in v1 → confirm no v1 dispatch in flight →
create the Schedule. **Rollback is the mirror:** pause the Schedule → confirm no v2 execution →
set `schedule_status` back. Script it (`api/scripts/migrate_schedule.py`, dry-run default). No
scheduled jobs exist in prod today — the script is still the artefact.
**Depends on:** F.2

#### F.4 — Job-lane delivery as an activity; `webhook_deliveries` fate

**What:** `JobWorkflow`/`BatchWorkflow`/`CrawlWorkflow` call the C.9 activity after completion
(job-lane semantics: delivery failure never fails the run; `job.failed` still fires on failure).
**⚠️ Owner decision (deferral table):** does `webhook_deliveries` survive as a v1-only audit
mirror (the activity writes the row for the admin endpoints) or retire with the loop? Decide here.
**Depends on:** C.9, D.3

#### F.5 — Three webhook meters (15e)

`webhook_deliveries_pending` / `_exhausted` / `_success_rate_7d` renamed or scoped to the job lane;
`UsageStats.tsx` updated. The two admin endpoints stay job-lane-only by design.
**Depends on:** F.4

#### F.6 — 🚀 Release; delete three loops

**Drain gate at deletion** for each: `scheduler.py` (no `active` v1 schedules; no v1 `pending`
rows for `_recover_stale_pending` to find), `webhook_loop.py` (no `pending` deliveries),
`advisory.py` (no NATS publishes remain — true after E.7). Remove them from `main.py`'s lifespan.
**Depends on:** F.1–F.5

---

## Group G — Consumer deletion

#### G.1 — 🚀 Delete `result_consumer.py`

**Drain gate at deletion:** `api-result-consumer` zero/zero; no `lane='v1'` rows non-terminal.
D.2 already rescued the homeless pair; verify nothing else in the file is unported (the `quota.py`
wall branch is job-lane behaviour D.3 kept). `JobNotifier` stays (BUG-009 survives).
**Depends on:** F.6

---

## Group H — NATS removal

#### H.1 — Delete the NATS-bound worker Deployments (obligation 3)

After G.1: delete `http-worker.yaml`, `playwright-worker.yaml`, `llm-worker.yaml` (the NATS-bound
ones); the Temporal-bound ones become the only ones. `WORKER_MODE` flag and the NATS code paths
removed from all three workers.
**Depends on:** G.1

#### H.2 — 🚀 Remove NATS

`nats-init-job.yaml`, `infrastructure/nats.yaml`, `nats-py`/`nats.go` deps, `messages.py`'s
`to_nats_bytes`, `constants.py` subjects, the `contracts/` NATS arm (the activity arm stays),
compose services, `nats_stream_seq` dropped (the views do not read it — no drop/recreate).
**Depends on:** H.1

---

## Group I — API thinning

#### I.1 — Alembic-on-startup made multi-replica safe

**⚠️ Not in any ADR; found sequencing this group.** `main.py` runs `alembic upgrade head` on every
startup. Two replicas starting together race the migration. Options: a Postgres advisory lock
around the upgrade (smallest change), or an init container / Job that runs it once. **TL call:
advisory lock** — keeps the auto-run contract every handoff relies on.
**Depends on:** H.2

#### I.2 — 🚀 `replicas: 2`, `RollingUpdate`

`api.yaml`: drop `Recreate`; `JobNotifier` per replica is correct by construction (`pg_notify`
fans out to every listener); `ws_max_connections_per_user` is per-process — note it. The
migration's stated payoff; record the before/after in the handoff.
**Depends on:** I.1

---

## Docs track

| # | What | Needed by |
|---|---|---|
| X.1 | **PRD-019** — conditional execution (four obligations on its `phase4-backlog.md` §2 row) | before PRD-018; not on this critical path |
| X.2 | **Promote ADR-010** (owner review) | E.0 |
| X.3 | **Schedule overlap policy** ADR — `BUFFER_ONE` recommended | F.0 |

---

## Non-negotiables — do not revise during implementation

| Decision | What it means for the engineer |
|---|---|
| Scraper workers never touch Postgres | A scrape/LLM/Playwright activity with a DB call is a stop. Accounting is C.6, on the workflow-worker queue |
| The classifier decides; Temporal retries | Terminal ⇒ non-retryable `ApplicationError`; transient ⇒ raise. No `non_retryable_error_types` lists, no in-activity retry loops, `llm_max_retries=0` stays |
| References, never content, in workflow history | An activity returns a `result_path`; the 2 MiB limit is not raised in config |
| The definition is a workflow argument | The body never loads it from Postgres |
| `REJECT_DUPLICATE` on every workflow start | `pipeline-run-{id}`, `job-run-{id}` |
| Mirror precedence | A mirror write never moves a run out of a terminal state it did not set |
| Drain gate before every cutover and deletion | `nats consumer info --json`, zero/zero |
| Playwright container start contract | Xvfb → wait → `exec python` as pid 1. Never `xvfb-run` |
| Named steps | Every cross-reference says *job cutover*, never *step 4* |
| §3 is do-not-fix | Including `_recover_stale_pending`'s log spam and BUG-012 — the lane filter in D.1 is a different obligation |

---

## Open items this backlog surfaced (for the Architect / owner)

1. **D.2** — the job cutover must port dedup + diff or R5 breaks; ADR-009 §10 frames them as
   waiting for Monitors, which is the pipeline lane's answer only.
2. **C.9** — `waiting` under a `RetryPolicy` can only be written by the activity itself; confirm
   that reading of 15a/15b/15d.
3. **C.13** — Worker Versioning vs `patched()`: due before C.14, not after.
4. **E.0** — task-queue shape (per-host limiter, reserved lanes) vs the crw deferral.
5. **F.4** — `webhook_deliveries` as a v1-only audit mirror or retired.
6. **I.1** — Alembic-on-startup under two replicas; no ADR covers it.
7. **C.11** — BUG-009 is deferred past Phase 4 in `phase4-backlog.md` §4 but is made load-bearing
   by the pipeline lane; recommend pulling it in.
8. **A.8** — where the Temporal Postgres backup lives.
