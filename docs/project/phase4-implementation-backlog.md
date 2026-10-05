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
> **Last updated:** 2026-10-05 (C.3 ✅ — block catalog + save-time validator; C.2 ✅; C.1 ✅) · **Tracking:** the status table below is the tracker.

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
| A.2 | Temporal server Deployment (`temporalio/server` + schema init container, standard visibility) | ✅ 2026-09-22 (local + k8s; infra `e8f32e1`, verified in prod) — TL call revised the same day: `auto-setup` is deprecated |
| A.3 | Namespace registration init Job, retention 30 d | ✅ 2026-09-25 (local + k8s; infra `60b0aee`, verified in prod) |
| A.4 | Temporal Web UI — ClusterIP only, no ingress | ✅ 2026-09-25 (local + k8s; infra `cd7b36c`, verified in prod) |
| A.5 | Workflow-worker scaffold in `api/` + `HelloWorkflow` | ✅ 2026-09-29 (local; `HelloWorkflow` completed on the compose server) |
| A.6 | Workflow-worker Deployment in the infra repo | ✅ 2026-09-29 (infra `ddde657`, pushed inside A.8; verified in prod) |
| A.7 | Local dev: compose services for Temporal + workflow worker | ✅ `temporal-postgres` ✅ 2026-09-21 · `temporal-schema` + `temporal` ✅ 2026-09-22 · `temporal-namespace` ✅ 2026-09-25 · `temporal-ui` ✅ 2026-09-25 · `workflow-worker` ✅ 2026-09-29 — **A.7 ✅** |
| A.8 | 🚀 Engine-up release + prove `HelloWorkflow` in prod; capacity + backup check | ✅ 2026-09-29 (`main` → `ce614d8`; `HelloWorkflow` COMPLETED in prod) |
| A.9 | Temporal env on the API Deployment (added at the A.8 review) | ✅ 2026-09-29 (local already via `.env`; infra `7ae6a9b`, verified in prod) — **Group A ✅** |
| **B** | **Worker port** (Go → LLM → Playwright) | |
| B.1 | Activity contracts: input/output types + `contracts/` arm | ✅ 2026-09-29 (local; nothing calls them until B.2) |
| B.2 | Go http-worker: `Scrape` activity entry point + mode flag | ✅ 2026-09-29 (local, compose service included; not released) |
| B.3 | Go second Deployment (Temporal-bound) | ✅ 2026-09-29 (infra `e70fbe9`, deployed with B.5) |
| B.4 | `ScrapeProbeWorkflow` + the §9 pre-gate on the Go activity | ✅ 2026-09-29 — **gate passed in prod** (`example.com`, v1 = v2 = `a6cdea39c93062d1`) |
| B.5 | 🚀 Go port release | ✅ 2026-09-29 (`ef68942`, infra `e70fbe9`) — **Go port live beside NATS** |
| B.6 | LLM worker: `LLMExtract` activity (cold start, classifier, heartbeat) | ✅ 2026-09-30 (local; compose service + `LLMProbeWorkflow` end to end). 🔴 Found an Anthropic-path prod bug on the way — fixed locally, not released |
| B.7 | LLM second Deployment + 🚀 release | ✅ 2026-09-30 (`1717032`, infra `3179f48`) — **LLM port live beside NATS**; BUG-020 closed on the owner's prod probe (401) |
| B.8 | Playwright worker: `Scrape` activity on `scrape-playwright` (bot wall raises, container contract) | ✅ 2026-09-30 (`c405124`, released with B.9 `a11cce6`; compose service end to end). Render pipeline extracted to `worker/scrape.py`, shared with NATS |
| B.9 | Playwright second Deployment + 🚀 release; pre-gate on both engines | ✅ 2026-09-30 released (`a11cce6`, infra `9ebb305`) — **Playwright port live beside NATS; gate passed in prod** (`example.com` → `18f1a13f59dcc2ad` on both) |
| **C** | **Pipeline lane** (layer A — PRD-016, R6 gate) | |
| C.1 | Schema: `pipelines`, `pipeline_versions`, `pipeline_runs`, `pipeline_run_blocks` | ✅ 2026-10-01 (local; `9bdb3a5`, migration 4.4 `cff9ec8fedbe`) — **Group C opens** |
| C.2 | Widen both quota views + the ledger CHECK for the pipeline lane | ✅ 2026-10-01 (local; `b5d0f94`, migration 4.5 `9db1dcda44f7`) |
| C.3 | Block catalog, per-type config schemas, save-time validator | ✅ 2026-10-05 (local; `9fc7198`; built 10-02 while the owner was AFK, walked through 10-05) |
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
**Verify:** `psql -l` from a pod shows both. Both empty (A.2's schema step fills them).
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

**TL call — revised 2026-09-22, before build.** ~~The original call (2026-09-20) was the
`temporalio/auto-setup` image as one Deployment: it runs `temporal-sql-tool setup/update-schema` on
every start, handling §2a's "server bump needs a schema step nothing in our deploy performs" trap
for free.~~ **Reversed:** the owner found the image marked **deprecated on Docker Hub** (*"no
longer maintained and will not receive updates"*); its last tag is ~8 months old — several minors
behind `temporalio/server` (1.31.0 at the time of writing) — and C.13 wants a *recent* server.
Temporal's own reference compose (`temporalio/samples-server` → `compose/docker-compose-postgres.yml`)
now runs `temporalio/server` with the schema step **unbundled into a one-shot
`temporalio/admin-tools` container**. The original reasoning stands; the mechanism moves from
inside the image to inside our manifests.

**Revised call:** `temporalio/server:<pinned>` as one Deployment, `DB=postgres12`, standard SQL
visibility, **no** Elasticsearch. Cost unchanged: a single process for all four services (frontend /
history / matching / worker) — no HA, which §1 already accepts at homelab scale; the Helm chart is
the reversal path. **The schema step is ours now:** an **init container** on the same Deployment
(`temporalio/admin-tools`, same tag) runs a committed script — `temporal-sql-tool --plugin
postgres12 … setup-schema -v 0.0` then `update-schema -d
/etc/temporal/schema/postgresql/v12/{temporal,visibility}/versioned`, once per database. It runs on
**every pod start** and is idempotent (`update-schema` is a no-op when the database is already at
the image's version) — exactly the property auto-setup had. A Job would not re-run on a server
bump, which is why it is an init container and not one.
⚠️ **No `create`.** The reference script creates both databases; ours must not — A.1 owns database
existence (the instance creates databases, Temporal owns the schema).
⚠️ **Two images, one version.** `server` and `admin-tools` are pinned to the **same tag and bumped
together** — the schema `update-schema` applies is baked into admin-tools, and the server expects
the matching version. Third-party images: pinned by hand, **not** under Flux image automation.
`temporalio/ui` (A.4) is a separate version line.
**What:** `infrastructure/temporal.yaml`: the schema script as a ConfigMap (A.1's `init.sql`
pattern); Deployment — init container + server container — and ClusterIP Service on 7233; DB env
from A.1's Secret (`POSTGRES_USER` / `POSTGRES_PWD` for the server, plus `SQL_PASSWORD` for the
init container — `temporal-sql-tool` reads that one), `POSTGRES_SEEDS`, `DB_PORT`, `BIND_ON_IP=0.0.0.0`;
`DBNAME=temporal` and `VISIBILITY_DBNAME=temporal_visibility` set explicitly; a `dynamicconfig`
ConfigMap mounted with `DYNAMIC_CONFIG_FILE_PATH` pointing at it (no keys now — C.13 writes into
it); resources sized against §2d (**limits, not requests, are the constraint** — do not overcommit
further; see A.8).
**Verify:** `temporal operator cluster health` → SERVING (the `temporal` CLI ships in admin-tools);
`\dt` in both A.1 databases is no longer empty; **and a second start is a no-op on the schema
step** — the init container's log reports the current version already equals the target. That
last line is the idempotency check, and the one to re-run after every bump.
**Depends on:** A.1
**Local half:** two compose services. `temporal-schema` — `temporalio/admin-tools`, one-shot,
`depends_on: temporal-postgres: condition: service_healthy`, `entrypoint: [/bin/sh]` running a
bind-mounted `docker/temporal/setup-schema.sh`. `temporal` — `temporalio/server`,
`depends_on: temporal-schema: condition: service_completed_successfully` (compose's analogue of the
init container), the env above, `docker/temporal/dynamicconfig/` mounted, host port 7233, a
`nc -z localhost 7233` healthcheck. Pin both tags through one variable with one default
(`${TEMPORAL_VERSION:-1.31.0}` on both `image:` lines — the A.1 credentials pattern) so a bump is
one edit.
**Local half ✅ 2026-09-22:** `docker/temporal/setup-schema.sh` (no `create`; `SQL_PASSWORD`
required; `DBNAME`/`VISIBILITY_DBNAME` defaulted to the server's names), the comment-only
`docker/temporal/dynamicconfig/development.yaml`, and the `temporal-schema` + `temporal` services
in `docker/docker-compose.yml`. Verified at 1.31.0: `temporal` stamped 0.0 → **1.19** (40 tables),
`temporal_visibility` → **1.14** (3 tables) — the numbers to compare after a bump; the server logged
`Updated dynamic config` for the keys-less file; healthy in < 20 s; `operator cluster health` →
SERVING, run as `docker compose run --rm --no-deps --entrypoint temporal temporal-schema operator
cluster health --address temporal:7233` (the CLI ships in admin-tools, not the server image).
**Idempotency confirmed:** a second `up` re-ran the one-shot — `found zero updates from current
version 1.19` / `1.14`, exit 0 — and `setup-schema -v 0.0` on a stamped database does **not** reset
the version (that needs `--overwrite`), so the script re-runs safely as written, which is what the
k8s init container relies on. ⚠️ Eight `error` lines in the server's first second of boot
(`Not enough hosts to serve the request`, `Queue reader unable to retrieve tasks`) are single-process
start-order noise — history is up before matching joins the ring — and stop by themselves; do not
chase them on k8s either.
**k8s half ✅ 2026-09-22 (infra `e8f32e1`):** `infrastructure/temporal.yaml` — ConfigMap
`scrapeflow-temporal-schema` (the script, a byte-identical copy; the app-repo file is canonical),
ConfigMap `scrapeflow-temporal-dynamicconfig` (`production.yaml`, comment-only — **not** a copy of
local's `development.yaml`: dynamic config is per-environment), Deployment `scrapeflow-temporal`
(`strategy: Recreate` so a bump's schema step never runs beside the old server; init container
`schema` on `admin-tools:1.31.0` + container `server` on `server:1.31.0`; tcpSocket probes on 7233;
requests 100m/256Mi, limits 500m/1Gi), ClusterIP Service `scrapeflow-temporal:7233`. Flux applied
~60 s after the push; the init container migrated prod's empty databases 0.0 → **1.19** (20 updates)
and 0.0 → **1.14** (15 updates) in ~30 s; pod Ready 70 s after creation; `Updated dynamic config`
logged; **zero** `error` lines at boot (the local noise did not occur); `operator cluster health` →
SERVING from a one-off admin-tools pod; `\dt` = 40 + 3 tables, `schema_version.curr_version`
1.19 / 1.14. **Idempotency verified:** `rollout restart` → 20 s → the new pod's `schema` log reads
`found zero updates from current version 1.19` / `1.14`.
⚠️ **Two things the restart check itself taught, found at session close:** (1) **`kubectl rollout
restart` on a Flux-managed Deployment costs *two* rollouts** — Flux strips the `restartedAt`
annotation on its next reconcile, which is another pod-template change and triggers a second
Recreate (~10 min later here). Restart a Flux-managed pod with `kubectl delete pod` instead: no
template change, nothing to revert. (2) **A replacement pod can fail its first ringpop bootstrap**
— it joins the ring from `cluster_membership`, whose rows still carry the *previous* pod's IP with
a heartbeat seconds old, retries ~55 s, exceeds the 30 s max join and exits `fatal … failed to
start ringpop`, exit 1. The kubelet restarts the container and the second attempt joins. Observed
once (of two rollouts), self-healed in ~90 s, healthy since with 0 error lines. **Expected, do not
chase it**; if it ever loops, the stale rows are in `temporal.cluster_membership` (`record_expiry`
is 2 days out, so they are aged out by heartbeat, not by expiry). Node after: CPU limits 168 % → **175 %**,
memory 56 % → 59 % (A.8). Two things decided at build: **`NUM_HISTORY_SHARDS=4` is set explicitly on
both halves** — it is immutable after first start (persisted in `cluster_metadata_info`; verified
`4` in prod and local), so the image default is now a written decision; and the CLI for prod checks
is `kubectl -n scrapeflow run <name> --restart=Never --image=temporalio/admin-tools:1.31.0 --command
-- temporal operator cluster health --address scrapeflow-temporal:7233`, then `kubectl logs` — the
`--rm -i` form loses the output to the pod teardown.

#### A.3 — Namespace registration init Job

**What:** clone `app/nats-init-job.yaml` → `app/temporal-init-job.yaml`: register namespace
`scrapeflow` with **retention 30 d** (§2c — an operator dial, changeable later, no correctness
role). Idempotent (namespace-exists is not an error).
Temporal's reference compose (see A.2) does this the same way — a second one-shot
`temporalio/admin-tools` container running `create-namespace.sh`: wait for `operator cluster
health`, `describe`, `create` if missing — which confirms the Job shape over the server's
`DEFAULT_NAMESPACE` env path. ⚠️ That script sets **no retention**, and its retry branch references
a misspelled variable (`$MAX_ATTdMPTS` under `set -u`) so it fails if ever reached: **write ours, do
not copy it.**
**Verify:** `temporal operator namespace describe scrapeflow` shows the retention.
**Depends on:** A.2
**Local half:** a `temporal-namespace` one-shot compose service on the same image,
`depends_on: temporal: condition: service_healthy`, running a bind-mounted
`docker/temporal/create-namespace.sh`.
**Built 2026-09-25:** `docker/temporal/create-namespace.sh` (canonical) + the `temporal-namespace`
one-shot; infra `app/temporal-init-job.yaml` (ConfigMap with a byte-identical copy + Job
`scrapeflow-temporal-init`, admin-tools at the server's tag), infra `60b0aee`. The script
**converges**, like `nats-init-job.yaml`: missing → `create`, existing → `update --retention`, so
the manifest owns retention and a hand change is reverted on the next run. ⚠️ **The CLI's
`--retention` default is 72h, not 30 d** — ADR-009 §2c's "30 is Temporal's default" does not hold
for `namespace create`; unset would have meant 3 days. ⚠️ No `ttlSecondsAfterFinished`: Flux would
recreate a TTL-deleted Job every reconcile. Re-run = `kubectl delete job`, Flux recreates it.

#### A.4 — Temporal Web UI

**What:** `temporalio/ui` Deployment + **ClusterIP only**. **No Ingress, no DNS record, no cert**
(§2b — it is a write-capable control plane with no auth of its own; single namespace means every
tenant's runs share one listing). Access: `kubectl -n scrapeflow port-forward svc/temporal-ui 8080`.
Document the port-forward line in the infra README.
**Depends on:** A.2
**Done 2026-09-25.** `temporalio/ui:2.54.1` (own version line — `TEMPORAL_UI_VERSION` in compose,
not `TEMPORAL_VERSION`). Local: `temporal-ui` in compose, `http://localhost:8080`. Prod: infra
`infrastructure/temporal-ui.yaml` — Deployment + ClusterIP Service **`scrapeflow-temporal-ui`**
(prefixed like every other object; the line above predates it), `/healthz` probes. Access:
`kubectl -n scrapeflow port-forward svc/scrapeflow-temporal-ui 8081:8080` (8081: the local compose UI holds 8080; any free port works — writes are CSRF-token gated, not origin-gated, verified). `TEMPORAL_DEFAULT_NAMESPACE=scrapeflow`,
`TEMPORAL_CORS_ORIGINS=http://localhost:8080` on both halves. Verified: `/healthz` OK, namespace
`scrapeflow` `REGISTERED` with 30 d retention, server 1.31.0 seen through the UI's API; no Ingress.

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
- ~~`api/app/config.py`~~ `api/app/settings.py` (there is no `config.py`): the two settings above.
- Tests: `temporalio.testing.WorkflowEnvironment` time-skipping; one test runs `HelloWorkflow`.
  **This establishes the workflow test pattern every later group reuses.**
**Verify:** `docker compose exec api uv run pytest tests/test_workflows_hello.py`.
**Depends on:** — (can be built before A.1–A.4 land; needs A.7 to run locally)
**Built 2026-09-29** (`9a365d0`, `831b22f`, `3eea07f`, `a179e7c` + the test/compose commit). Decisions
taken in the build, all owner calls:
- **Queue naming: after the worker *pool*, short, hyphenated, no prefix** — `workflow` now; later
  `scrape-http`, `scrape-playwright`, `llm`, `content`. The Temporal namespace already scopes them.
  Every workflow shares `workflow` (they all run in this pod); a queue per workflow type is wrong.
- **Every activity call states `task_queue=` explicitly** and sets a `RetryPolicy` — Hello uses
  `maximum_attempts=3` (arbitrary). Temporal's default is *unlimited* retries; R4 wants one visible
  layer, so no call may rely on the default.
- **One dataclass input per workflow and per activity** (`HelloWorkflowInput`, `SayHelloInput`) —
  a field can be added without breaking in-flight runs; a positional argument cannot.
- **`graceful_shutdown_timeout=20s`** on the `Worker` (default is **0**: in-flight activities are
  cancelled at once). 20 s sits under k8s's default 30 s `terminationGracePeriodSeconds`.
- `client.connect()` has **no `disconnect()`** — a Temporal `Client` has no close/drain (verified). It
  is **not** in the FastAPI lifespan yet; that is C.5, with a lazy-vs-eager connect decision there.
Found in the build:
- ⚠️ **Bare `python` in the api image is the system interpreter with no packages** — every command
  must be `uv run python …` (the BUG-019 trap). A.6's command below is corrected for it.
- The time-skipping test server is an **81 MB binary downloaded on first use** into the container's
  `/tmp` (first run ~13 s, then ~1 s; re-downloaded when the container is recreated). No CI runs
  pytest, so nothing else is affected. Tests use `WORKFLOW_QUEUE` — each test's server is private.
- The retry test is mutation-checked (`maximum_attempts=5` → `[1, 2, 3, 4, 5] != [1, 2, 3]`), and
  runs 15 s of backoff in 0.2 s — time-skipping confirmed. 283 API tests green (281 → 283).

#### A.6 — Workflow-worker Deployment

**What:** `app/workflow-worker.yaml`: api image, `command: [uv, run, python, -m, app.workflows.worker_main]`
(⚠️ **not** bare `python` — system interpreter, no packages; found in A.5),
**app-DB + MinIO credentials** (this is the *only* pod on the v2 side with DB access — §8d), Temporal
env, `replicas: 1`, `RollingUpdate` (stateless polling — safe). Add an `ImagePolicy`/automation entry
mirroring `api`'s so it follows the same tag.
Leave `terminationGracePeriodSeconds` at ≥ 30 s — the worker drains for up to 20 s (A.5).
**Verify:** pod logs `Workflow worker started … task_queue=workflow`; Web UI (port-forward) lists the poller.
**Depends on:** A.2, A.5
**Built 2026-09-29** (infra `a81b81c`, pushed inside A.8 as **`ddde657`** after a rebase + tag bump): Deployment `scrapeflow-workflow-worker`,
`RollingUpdate`, `terminationGracePeriodSeconds: 30`, a `wait-for-temporal` init container, 50m/128Mi
requests, 250m/256Mi limits (the local worker sits at ~55 MiB). No Service (it only dials out).
- **No new `ImagePolicy`.** The container carries the `scrapeflow-api-policy` setter marker, which is
  how `cleanup-cronjob.yaml` already follows the api tag; the automation's `update.path` covers `app/`.
- **Env is what `app.settings` needs, not only what Hello needs:** both Fernet keys are required at
  import (BUG-019's third trap), plus the Temporal pair, `DATABASE_URL` and MinIO. No NATS, Redis or
  Clerk — later tasks add what their activities use.
- ⚠️ **Push only at A.8, after the new api image exists.** The manifest pins today's tag (`421cbfe`),
  which has neither `temporalio` nor `app/workflows/` — pushed early, the pod crash-loops on import
  until Flux bumps the tag. A.8 order: ff `main` → image built and Flux bumps the tag (rebase the
  infra commit over its automation commits) → push infra.
- **Verified locally on the production-target image** (not the compose `test` target): run as
  `appuser` with only the manifest's env → `Workflow worker started … task_queue=workflow`;
  `HelloWorkflow` (compose worker stopped) → `COMPLETED`; `docker stop` → exit 0 in 0.34 s after
  `stopping`/`stopped`. `uv run` installs the project into `/app/.venv` at each start
  (`Built scrapeflow-api`), as the API's own CMD does — works as non-root.

#### A.7 — Local dev

**What:** `docker/docker-compose.yml`: `temporal-postgres`, `temporal-schema` + `temporal` (A.2),
`temporal-namespace` (A.3), `temporal-ui`
(host port for the browser — fine locally), `workflow-worker` (api image, the A.6 command, `api/app`
mounted — ~~so it hot-reloads~~ **no hot reload; restart it after a code change**). Temporal env on
the `api` service ~~explicitly~~ — it comes from the root `.env` via `env_file`.
**Verify:** `HelloWorkflow` started via a one-line script shows in the local UI.
**Depends on:** A.5
**Progress:** `temporal-postgres` ✅ 2026-09-21 (A.1) · `temporal-schema` + `temporal` ✅ 2026-09-22
(A.2, both halves done) · `temporal-namespace` ✅ 2026-09-25 (A.3, both halves done) · `temporal-ui`
✅ 2026-09-25 (A.4) · `workflow-worker` ✅ 2026-09-29 (A.5). **A.7 complete.** The worker service
sets **`stop_grace_period: 30s`** — Docker's 10 s default would SIGKILL mid-drain. Verified:
`HelloWorkflow` started with the admin-tools CLI (`workflow execute --type HelloWorkflow
--task-queue workflow --input '{"name": …}'`) → `COMPLETED`, `"Hello, Karthik"`, visible in the UI;
`docker compose stop` with **`uv` as PID 1** → exit 0 in 0.38 s, so `uv run` forwards SIGTERM.

#### A.8 — 🚀 Engine-up release + proof

**What:** ff `main` (the api image rebuilds for A.5; nothing else changes behaviour — no NATS
message, no schema, no route). Flux applies A.1–A.6 — ⚠️ **A.6 (infra `a81b81c`) is held back:
push it only after the new api tag is built and Flux has bumped it** (see A.6's *Built* note). Then:
1. Start `HelloWorkflow` and watch it complete in the port-forwarded UI. ⚠️ The api image has **no
   `temporal` CLI** — start it from a one-off `temporalio/admin-tools` pod (A.3's pattern), as A.7 did.
2. **Capacity:** `kubectl describe node` — requests and **limit overcommit** before/after. Record
   both in the handoff. §2d: a headed render + a history burst = CFS throttling on the history
   service that *looks like a workflow bug*. Write that sentence next to the numbers.
3. **Backups:** the Temporal PG is now in-flight work (§10 risks). ⚠️ **Owner item:** name where
   its backup lives, or record that it does not yet. Not blocking; must not be silent.
**Depends on:** A.1–A.7
**Done 2026-09-29.** `main` `421cbfe..ce614d8` (24 commits; `api` the only image built, no Alembic
revision). Flux bumped the api tag (infra `8a5a729`) → API `Recreate` green, `/health` 200. Then A.6:
rebased over the bump, **its own tag hand-set to the new one before pushing** (the rebased file still
pinned `421cbfe`, which would have crash-looped until the next automation pass), pushed as infra
`ddde657`. Worker pod Running, 0 restarts; `Workflow worker started … address=scrapeflow-temporal:7233
… task_queue=workflow` ~21 s after `uv run`'s project build.
1. **Proof:** one-off `temporalio/admin-tools:1.31.0` pod → `task-queue describe` lists workflow + activity
   pollers `25@scrapeflow-workflow-worker-…`; `HelloWorkflow` id `a8-engine-up-proof` →
   **`COMPLETED`, `"Hello, production"`, 140 ms**, 11 history events.
2. **Capacity** (`kubectl describe node`), before → after: CPU requests 2470m (30 %) → **2520m (31 %)**,
   limits 14100m (176 %) → **14350m (179 %)**; memory requests 4284Mi (13 %) → **4412Mi (13 %)**,
   limits 19050Mi (59 %) → **19306Mi (60 %)**. Live: worker 41m / 65Mi, server 26m / 88Mi.
   **§2d: a headed render + a history burst = CFS throttling on the history service that *looks
   like a workflow bug*.** CPU limits are 179 % overcommitted — if a workflow stalls under load,
   check the Temporal server's throttling before the workflow code.
3. **Backups: none exist — for the Temporal Postgres *or* the app Postgres.** Searched the infra repo
   (no `pg_dump`, Velero or backup manifest; the only CronJob is `scrapeflow-cleanup`). **Owner's
   call (2026-09-29): deferred until after the Temporal migration** — the data is mostly junk today.
   Recorded in `phase4-backlog.md` §4.

#### A.9 — Temporal env on the API Deployment

*Added 2026-09-29 by the A.8 review; owner's call to land it in Group A rather than inside C.5.*
**Why:** the API starts, signals and queries workflows from C.5 on, but only the workflow worker's
manifest carried `TEMPORAL_ADDRESS`/`TEMPORAL_NAMESPACE`. The setting defaults to `localhost:7233`,
so the first prod trigger would fail to connect — and C.5 never said to add them.
**What:** `app/api.yaml` — `TEMPORAL_ADDRESS=scrapeflow-temporal:7233`, `TEMPORAL_NAMESPACE=scrapeflow`.
Local: nothing to do — the compose `api` service already reads both from the root `.env` (`.env.example`
carries them). No app code: nothing on the API reads them until C.5.
**Verify:** `kubectl diff` shows only the two env vars; after Flux applies, the API pod's env has them
and `connect()` from inside the pod reaches the server. Pushing restarts the API (`Recreate`, ~80 s).
**Depends on:** A.2
**Built 2026-09-29:** infra `7ae6a9b`. Local verified: `connect()` from the compose api container →
`temporal:7233`, namespace `scrapeflow`, server 1.31.0. Server dry-run clean; `kubectl diff` = the two vars.
**Deployed 2026-09-29:** pushed on the owner's go; Flux applied, API `Recreate` green (new pod Ready
in ~70 s), all five loops started, `/health` 200. `connect()` from inside the prod API pod (via
`/app/.venv/bin/python`) → `scrapeflow-temporal:7233`, namespace `scrapeflow`, server 1.31.0.

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
**Built 2026-09-29.** Decisions taken in the build, owner calls where marked:
- **Pydantic, not dataclasses (owner)** — supersedes A.5's dataclass convention for activity
  contracts. `client.connect()` now passes `pydantic_data_converter`, which validates on decode
  (so `min_length`/`Literal`/`pattern` are checked where the activity *receives* the input) and
  still handles Hello's dataclasses. A `Worker` takes its converter from the client.
- **Inputs drop every NATS-routing field:** `schema_version`, `run_id`, `crawl_context`, and
  **`engine` (owner)** — no worker reads it; the task queue picks the worker, as the subject does
  today, and the workflow reads the engine from the row to choose the queue. `output_format` and
  `provider` tightened to `Literal`s (owner, confirmed after build). Credentials and the LLM key stay **ciphertext** — workflow
  history stores inputs as plain JSON in the Temporal DB and the Web UI.
- **Outputs return success only; a failure is raised** (owner, confirmed after build; B.2's `NonRetryableApplicationError`) —
  the "error string on a terminal failure" above is withdrawn; two routes out of an activity for
  one failure is the Q8 shape. `blocked:<vendor>` becomes the raised error's message.
- **The worker reports size and `content_hash` (owner, confirmed after build)** —
  `StoredObject {path, size}` for the result and every screenshot; `size`, not `bytes` (matches
  `record_object(size=…)`, no builtin shadowing). The accountant needs neither `stat_minio_size`
  nor a download. `content_hash` is required, `^[0-9a-f]{16}$` — Go must format with `%016x`
  (`%x` drops leading zeros, ~1 in 16). `LLMOutput` is `result` only.
- **Changing a contract:** add optional fields only (consumers ignore unknown fields); a breaking
  change is a new activity type, not an edit — no stream to drain, queued tasks keep old payloads.
- **Each service holds its own copy** (`playwright-worker/worker/contracts.py`,
  `llm-worker/worker/contracts.py`, `http-worker/internal/activity/contracts.go`): the contract
  test loads each file standalone, so they depend on pydantic only. Go's `ScrapeInput.Validate()`
  must be the activity's first call — `encoding/json` enforces nothing. Go's slices are
  `omitempty`: a nil slice marshals to `null`, which the API's `list[...] = []` rejects.
- **`contracts/test_activity_contract.py`** (separate from the NATS test, so H deletes that one
  whole): API inputs → the Playwright/LLM parsers; worker outputs → the API's models; field-set
  drift both ways; all through the real converter. **Fixtures run both directions** —
  `scrape_input_*.json` from Python for Go, `go_scrape_output.json` from Go for Python.
  Mutation-checked (dropping `omitempty` fails the Python side). ⚠️ Regenerating the Go fixture
  needs `go test -count=1` — a cached pass skips the write (hit in the build).

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
**Built 2026-09-29.** Decisions taken in the build, owner calls where marked:
- **One binary, env-selected mode.** `main()` branches on `WORKER_MODE` (default `nats`, so the
  existing Deployment needs no edit); no Dockerfile `CMD` change — unlike the api image, whose second
  entrypoint is a different command. `NATS_URL` is required in nats mode only; `TEMPORAL_ADDRESS`
  (default `localhost:7233`) and `TEMPORAL_NAMESPACE` (default `scrapeflow`) are new.
- **Shared pipeline in `internal/scrape`** — fetch → format → upload (`Run`, returns path + size +
  `ContentHash`) and the classifier, moved verbatim from `worker/errors.go` with its tests. Both
  transports import it; neither imports the other, so H deletes `internal/worker` whole. Steps 1–3
  (validate, proxy, robots) stay duplicated (~25 lines) — extracting them would reshape
  `handleMessage`, whose failure branches are interleaved with publish/ack.
- **`internal/activity/scrape.go`:** task queue `scrape-http`, activity name `Scrape` (both
  constants — the Python side must use the identical strings). Error types: `InvalidInput`,
  `ProxyError`, `RobotsDisallowed`, `ScrapeFailed` (all non-retryable) and `StorageTransient`
  (retryable). Messages match the NATS path's. Comments mirror `handleMessage`'s step structure and
  it logs the same `Received job` / `Using proxy for job` lines (owner). Heartbeats at each stage —
  ⚠️ **the SDK sends the first heartbeat at once and throttles the rest**, so only the first is
  observable in a short activity.
- **Activity-only worker** (`DisableWorkflowWorker`) — without it the Go SDK also polls the queue for
  workflow tasks. Pool = `WORKER_POOL_SIZE`; `WorkerStopTimeout` 20 s (A.5 parity).
- ⚠️ **`go.temporal.io/sdk` v1.49 requires Go 1.26** — `go.mod`'s directive moved to 1.26 and the
  Dockerfile builder to `golang:1.26-alpine`. Plus `github.com/cespare/xxhash/v2`;
  `ContentHash` is pinned against Python's `xxhash` values, leading-zero case included.
- **Compose `http-worker-temporal`** added (owner — the local half lands with the code): same build,
  `WORKER_MODE=temporal`, no NATS env, depends on `minio` + `temporal-namespace`. B.3 is now the
  k8s manifest only.
- **Verified:** Go suite green, golangci-lint clean on the new code; a Python workflow on the compose
  server called `Scrape` through the B.1 contracts → valid `ScrapeOutput`, size equal to MinIO's
  stat; a dead site → `ScrapeFailed`, non-retryable; SIGTERM → exit 0 in 0.2 s; NATS mode starts
  and consumes normally on the same image.
- ⚠️ **B.3/B.5 ordering:** the current prod tag's binary has no `WORKER_MODE`, so the Temporal
  manifest pushed before the B.5 image would run NATS mode with no `NATS_URL` and crash-loop —
  A.6's trap. Push the manifest with the new tag.

#### B.3 — Go second Deployment

**What:** `app/http-worker-temporal.yaml` — same image, `WORKER_MODE=temporal`, Temporal env, **no
NATS env, no DB env** (light-worker rule survives). `ImagePolicy` shared with the NATS one.
**Depends on:** B.2

**Built (2026-09-29; rebased and pushed at B.5 as infra `e70fbe9`):**
- `app/http-worker-temporal.yaml` — Deployment `scrapeflow-http-worker-temporal`, container
  `http-worker`, `scrapeflow-http-worker-policy` setter marker (no new `ImagePolicy`). Env: the NATS
  Deployment's minus `NATS_URL`/`NATS_MAX_DELIVER`, plus `WORKER_MODE`/`TEMPORAL_*`;
  `CREDENTIALS_ENCRYPTION_KEY` stays (`config.Load` requires it). `RollingUpdate` (stateless poller,
  as A.6), grace 30 s over the 20 s `WorkerStopTimeout`, `wait-for-temporal` + `wait-for-minio`.
  Resources copied from the NATS Deployment. Kustomization line + infra README section.
- ⚠️ **The file still pins `421cbfe`'s tag**, which has no `WORKER_MODE` → crash-loop. Hand-set it
  to the B.5 image before pushing (A.8's order: ff `main` → Flux tag bump → rebase → set tag → push).
- `WORKER_POOL_SIZE` unset (NATS parity) → `runtime.NumCPU()`, the **node's** count, not the 500m
  limit. Same on the NATS worker today.
- **Verified:** `kubectl apply --dry-run=server` + `kubectl kustomize` clean; the local B.2 image
  run with **only** the manifest's env on the compose network → `Temporal worker started …
  task_queue=scrape-http`, SIGTERM → exit 0.

#### B.4 — `ScrapeProbeWorkflow` + the §9 pre-gate

**Why:** 16d — the R6 gate runs on a lane with **no fallback**, which makes §9's pre-gate a
requirement: separate "the adapter is wrong" from "the model is wrong" *before* the model exists.
**What:** `api/app/workflows/probe.py`: a dev/operator-only workflow that runs one `Scrape` activity
on the scraper queue with a given `ScrapeInput` and returns the output. A script under
`api/scripts/` starts it and prints the `result_path`. **Gate:** same URL through v1 (`POST /jobs`,
`engine=http`) and through the probe; diff the two MinIO objects (byte-equal expected on `http` —
no LLM, no nondeterminism). Record the result in the handoff.
**Depends on:** B.3 deployed (A.8-style: ff `main` for B.2 first — B.5)

**Built (2026-09-29, local):**
- `api/app/workflows/probe.py` — `ScrapeProbeWorkflow(ScrapeInput) -> ScrapeOutput`, one call to
  `SCRAPE_ACTIVITY` on `SCRAPE_HTTP_QUEUE` (both new constants — `contracts.py`, `queues.py`; must
  equal Go's `ScrapeName`/`TaskQueue`). `schedule_to_start` 60 s (not retried — an unpolled queue
  fails instead of hanging), `start_to_close` 90 s, `maximum_attempts=3`. Registered in
  `worker_main.py`.
- `api/scripts/probe_scrape.py <url> [--format]` — random UUID `artifact_id`, prints the output,
  **then deletes the objects it wrote** (owner): no `job_runs` row, so no ledger row; exit 1 if a
  delete fails. Prod: `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api --
  /app/.venv/bin/python -m scripts.probe_scrape <url>`.
- **Gate method:** a static URL (`example.com`) — dynamic pages differ per fetch. Compare the
  probe's `content_hash` with the v1 run's `job_runs.content_hash`; no object download needed.
- **Verified:** two tests (stand-in activity on `scrape-http` only; non-retryable → one attempt),
  mutation-checked — routing to `workflow` fails both. 300 API tests. Compose end-to-end:
  `example.com` → 713 B, `content_hash a6cdea39c93062d1`, object deleted, prefix empty after.
  ⚠️ The compose `workflow-worker` has no hot reload — restart it after changing a workflow.

#### B.5 — 🚀 Go port release

**What:** ff `main`; `rollout status` on **both** http-worker Deployments; NATS consumer
`go-worker` unchanged (`nats consumer info --json`). Then B.4's gate against prod.
**Depends on:** B.2, B.3

**Done (2026-09-29):**
- `main` ff `ce614d8..ef68942`; four images built (api, http-worker, playwright-worker, llm-worker —
  the last two only for B.1's unused contract twins; no Alembic revision). Flux bumped all four;
  `rollout status` green on api, http-worker, llm-worker, playwright-worker, workflow-worker.
- B.3 rebased over the Flux bumps, **tag hand-set** to `main-1790703736-ef68942…`, pushed as infra
  `e70fbe9`; `rollout status` green; `mode=temporal … task_queue=scrape-http`.
- NATS http-worker on the new binary: `mode=nats`, subscribed. `go-worker` before = after: ack_wait
  30 s, max_deliver 3, delivered seq 1126, 0 pending.
- ✅ **A.8's open item closed:** the old workflow-worker pod logged `stopping` → `stopped` on the
  rollout's SIGTERM — the drain is verified in prod.
- **B.4 gate:** `example.com`, `html`, v1 (NATS) vs probe → both `a6cdea39c93062d1` (713 B, equal to
  local). ⚠️ **Choose the gate URL by fetching it twice first:** `govindappa.com` differs on *every*
  fetch (Cloudflare injects `__CF$cv$params={r:<ray>,t:<ts>}`), so v1 ≠ probe there says nothing
  about the paths. Match `output_format` on both sides too.
- The prod probe run needs the owner: `kubectl exec` into the api pod is a remote write (the
  object is created, then deleted) and the auto-mode classifier refuses it.

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

**Owner calls (2026-09-30, before build):**
- **Lockfile + capped majors.** `uv.lock`, Dockerfile installs from it (as `api/`). `anthropic` and
  `openai` capped below their next major (`>=<locked>,<next>`): `errors.py` matches their exception
  class names, so a major that renames one silently turns a transient into TERMINAL. A major bump is
  a deliberate cap raise + a re-check of `errors.py`. Check whether the lockfile brings `llm-worker/`
  into Dependabot's view (BUG-006, `.github/dependabot.yml`).
- **Coarse error types, as Go's.** `ApplicationError.type` ∈ `LLMFailed` (non-retryable),
  `LLMTransient`, `StorageTransient` (retryable); `message` = `describe(exc)`, which keeps the exact
  class name. Every exception goes through `classify()` — the SDK retries any non-`ApplicationError`,
  which would silently undo the fail-closed default. `CancelledError` is not caught.
- **Graceful shutdown covers one full call:** `graceful_shutdown_timeout` ≈ 400 s (not A.5's 20 s),
  so a rollout does not cancel an in-flight call and re-bill the user's key on the retry. Only paid
  when a call is running. B.7 sets `terminationGracePeriodSeconds` above it.
- **Retry numbers for C.7 — today's NATS values, copied, not re-decided:** `LLMExtract` is called
  with `RetryPolicy(initial_interval=5s, backoff_coefficient=2, maximum_interval=60s,
  maximum_attempts=3)`. Temporal's default is unlimited attempts. A heartbeat timeout counts as an
  attempt, as a NATS redelivery does today.
- **Malformed input:** a test sends the activity a bad input to confirm the SDK's behaviour (expected:
  retryable decode failure — 3 instant attempts, no LLM call, then a visible failure). If so, accept
  it; no hand-rolled validation.

**Built 2026-09-30 (local) — `fa43db2` (B.6), `f2738fb` (BUG-020's fix). Not released; B.7 ships both.**
- **Lockfile:** `llm-worker/uv.lock`; the Dockerfile builder runs `uv sync --frozen --no-install-project`
  into `/opt/venv` (`UV_PYTHON_DOWNLOADS=never` — the venv must link the image's interpreter);
  runtime stage and `CMD` unchanged. Resolved: `anthropic` 1.9.0 (`<2`), `openai` 3.22.1 (`<4`),
  `temporalio` 1.33.0 (= `api/`).
- **`worker/activity.py`:** `LLMActivities(minio).llm_extract`, registered as `LLMExtract` (constant
  in `worker/contracts.py`, twin `LLM_EXTRACT_ACTIVITY` in the API's; queue `llm`, twin `LLM_QUEUE`
  in `queues.py` — the contract test asserts both pairs). Error type by stage: fetch/upload →
  `StorageTransient`, llm → `LLMTransient`. `fetch_content` moved to `storage.py` beside `upload`
  so H deletes `worker.py` whole.
- **`worker/temporal_main.py`:** activity-only `Worker` (the Python SDK polls no workflow queue when
  given no workflows — no `DisableWorkflowWorker` equivalent needed), `max_concurrent_activities =
  llm_max_workers`, `graceful_shutdown_timeout = llm_graceful_shutdown_seconds` (400).
  `main.py` branches on `WORKER_MODE`; `llm_heartbeat_seconds` serves both modes (now a float).
- **Decode failure confirmed in the SDK source** (`temporalio/worker/_activity.py`): a plain
  `ApplicationError("Failed decoding arguments")` — retryable. Pinned by a time-skipping test:
  `MAXIMUM_ATTEMPTS_REACHED`, the body never runs. The llm-worker suite now downloads the
  test-server binary on first use (~10 s per fresh container).
- 🔴 **Found in the smoke test — an early SIGTERM was ignored.** PID 1 ignores a signal it has no
  handler for; the imports take ~3.9 s (`anthropic` 1.6, `openai` 0.7, `nats` 0.65) and the loop
  handlers were installed only after connect + `Worker()`. Reproduced: SIGTERM at 1–7 s → exit 137
  after the full stop timeout; in prod that is the ~420 s grace period. Fixed twice over: `main.py`
  installs `sys.exit` on SIGTERM before its imports (nothing is in flight yet), and `temporal_main`
  installs the graceful handler before connecting. Verified at 0.5/1/2/3/4/5/7 s → exit 0. The
  early handler also covers NATS mode's startup — the one change to that path. ⚠️ **The api's
  `worker_main.py` (A.5, in prod) had the same shape — ✅ fixed the same day (owner).** Its window
  was worse and mostly not ours: **`uv run` as PID 1 ignores SIGTERM during its own startup**
  (reproduced: stop at 0.3–1 s → exit 137 after the full timeout; at 2–3 s uv forwards it and the
  importing child dies, exit 143; ≥ 5 s drains). In prod that uv phase is the ~21 s project build.
  Fix: command `/app/.venv/bin/python -m app.workflows.worker_main` (compose + infra `1807870`,
  **unpushed**) + the same two handlers in `worker_main.py`. Verified on compose and on the
  **production-target image as `appuser`**: every early stop → exit 0 in ~0.3 s; `started` at ~3 s
  instead of ~5 s; Hello + the LLM probe run through it. ⚠️ **The API Deployment itself still runs
  `uv run uvicorn …`** (Dockerfile `CMD`) — same uv window; not changed.
- **Tests:** `tests/test_activity.py` (11) — success + size, input passthrough, 429 / 401 / unknown
  / warm-up timeout / MinIO-unreachable on fetch and on upload, cancel passes through as
  `CancelledError`, heartbeats through a 7-probe cold start, bad input. **Mutation-checked:** raw
  re-raise (6 fail), no heartbeat task (1), `except BaseException` (1). 117 llm-worker, 50 contract.
- **Smoke (compose network, `WORKER_MODE=temporal`):** `temporal_worker_started … task_queue=llm`,
  poller listed on `llm` (activity), SIGTERM → `stopping` → `stopped`, exit 0 in ~0.6 s.
- ~~The llm-worker image has no `PYTHONUNBUFFERED`, so the last lines before a SIGKILL are lost.~~
  **Withdrawn 2026-09-30:** structlog's `PrintLogger.msg` prints with `flush=True`, so no structlog
  line is held back; the first smoke run's missing `stopping` line was never *written* (the ignored
  SIGTERM above). `PYTHONUNBUFFERED` would change nothing here.
- ⚠️ **`llm.py` logs through stdlib `logging`, which nothing configures** — INFO is dropped and
  WARNING goes bare to stderr. So *"LLM endpoint warm after cold start"* is never printed (the stub
  run's 20 s cold start left no line), and the truncation warning is unformatted. Pre-existing, both
  modes. ✅ **Fixed the same day (owner: "switch to structlog")** — events `llm_endpoint_warm`
  (`attempts`, `waited_s`, `status`) and `content_truncated`; tests capture both (mutation-checked);
  stub re-run logged `llm_endpoint_warm attempts=7 … waited_s=12.2`. Ships with B.7.
- Exit prints aiohttp `Unclosed connector` noise (miniopy's session is never closed) — same on the
  NATS path; harmless.
- **Step 6 — local end to end.** Compose `llm-worker-temporal` (`WORKER_MODE=temporal`, no NATS env,
  `stop_grace_period: 420s`). **`LLMProbeWorkflow`** (`api/app/workflows/probe.py`, registered on the
  workflow worker): `Scrape` on `scrape-http`, then `LLMExtract` on `llm` against the object it
  wrote — the R6 recipe minus the webhook. LLM call: `schedule_to_start` 60 s, `start_to_close`
  400 s, `heartbeat_timeout` 90 s, the 5 s / ×2 / 60 s / 3 policy. **`scripts/probe_llm.py <url>
  --provider … --model … [--base-url]`**: key from `PROBE_LLM_API_KEY` or a prompt, encrypted with
  `LLM_KEY_ENCRYPTION_KEY`; prints the output and the extraction; deletes **by prefix**
  `history/{artifact_id}/` — a failed LLM step never returns the scrape's path. Prod: `kubectl exec
  -it deploy/scrapeflow-api -c api -- /app/.venv/bin/python -m scripts.probe_llm …` (B.7's check).
  Two workflow tests (routing mutation-checked). **Runs:** a stub OpenAI-compatible endpoint that
  refused connections for 20 s → one `/models` probe after boot, one chat call, `llm.json` beside
  `scrape.md`, both deleted; a fake Anthropic key → one attempt, `LLMFailed`, scrape deleted.
- 🔴 **BUG-020 found by that run — the Anthropic path is broken in production.** `llm.py` passed an
  `httpx.Timeout`; `anthropic` ≥ **1.4.0 (2026-09-04)** uses `httpx2` and raises `TypeError` on it
  (1.3.0 accepted it). Classified TERMINAL, so every `provider=anthropic` job fails without reaching
  Anthropic. **Prod's llm-worker runs `anthropic` 1.9.0** (checked in the pod) — unpinned
  `pip install .` at the 2026-09-19 and 2026-09-29 releases. The current pod's logs (since
  2026-09-29 17:44) show no LLM jobs. `openai` 3.22 also sits on `httpx2` but still accepts *and
  enforces* an `httpx.Timeout` (measured: 2 s budget → `APITimeoutError` at 2.1 s). Missed because
  every test mocks the SDK constructors. **Fix (local):** `_make_timeout(anthropic.Timeout |
  openai.Timeout)` — each SDK's own class — via `_anthropic_client` / `_openai_client`, plus
  real-client tests that assert the timeout and the `max_retries` pin (mutation-checked). Re-run:
  the fake key now reaches Anthropic → 401 `AuthenticationError` → `LLMFailed`, one attempt.
- **Cause chain across the boundary:** `raise err from exc` sends the original exception too, as a
  nested `ApplicationError` whose `type` is its class name. So `ActivityError.cause` = ours
  (`LLMFailed`/…), and *its* `.cause` = e.g. `AuthenticationError`. Read the first one (C.7).

#### B.7 — LLM second Deployment + 🚀 release

**What:** `app/llm-worker-temporal.yaml` (same env as the NATS one minus NATS; keep
`LLM_REQUEST_TIMEOUT_SECONDS=180`). **`terminationGracePeriodSeconds` ≈ 420 s** — above B.6's
≈ 400 s graceful shutdown, or k8s kills the pod at its own deadline first. Release; `rollout status` on both.
**Also push infra `1807870`** (workflow-worker command → venv python; works on the old and new api
image alike, so order does not matter) and verify `kubectl exec … cat /proc/1/cmdline` shows it.
**Carries BUG-020's fix (owner, 2026-09-30 — no hotfix):** the release rebuilds the NATS llm-worker
from B.6's lockfile too. After it: the pod's `anthropic.__version__` = 1.9.0, and `probe_llm.py` with a
fake Anthropic key → `LLMFailed: AuthenticationError … 401`, not `TypeError` (BUG-020 → *After the release*).
**Depends on:** B.6

**Done (2026-09-30):**
- Infra `app/llm-worker-temporal.yaml` — Deployment `scrapeflow-llm-worker-temporal`, `RollingUpdate`,
  `terminationGracePeriodSeconds: 420`, `wait-for-temporal` + `wait-for-minio`, the NATS one's
  resources (50m/128Mi → 500m/512Mi) and LLM env minus `NATS_URL`, plus `WORKER_MODE=temporal` and
  the two `TEMPORAL_*`. Kustomization line + README section. Server dry-run clean.
- Before: 120 llm-worker, 50 contract, 302 API tests green. Prod baseline: `python-llm-worker`
  ack_wait 120 s, max_deliver 3, delivered seq 1005, 0 pending; node CPU limits 185 %.
- `main` ff `ef68942..1717032`; api + llm-worker built, the other three skipped. Flux bumped both
  (infra `ab38bdb`, `6cc4dbf`); `rollout status` green on api, llm-worker, workflow-worker.
- Infra rebased over the bumps — ⚠️ **`1807870` conflicted with the api tag bump** (its `command:`
  line sits under the `image:` line): kept the new tag + the venv command → `bbc101a`. New manifest's
  tag **hand-set** to `main-1790760130-1717032…` → `3179f48`. The classifier refused the infra push;
  the owner pushed `6cc4dbf..3179f48`.
- **Verified:** `scrapeflow-llm-worker-temporal` rolled out, `temporal_worker_started … task_queue=llm
  graceful_shutdown_s=400.0`; `task-queue describe --task-queue llm` (one-off admin-tools pod) lists
  the pod as the activity poller. Workflow worker rolled out, `/proc/1/cmdline` =
  `/app/.venv/bin/python -m app.workflows.worker_main`. NATS llm-worker: `subscribed … ack_wait=120
  max_deliver=3`, consumer before = after. BUG-020's first half: the pod runs `anthropic` 1.9.0 /
  `openai` 3.22.1 with the fixed `llm.py`.
- **Owner's:** the prod `probe_llm.py` run with a fake Anthropic key → `LLMFailed:
  AuthenticationError … 401` (needs `kubectl exec -it`).

#### B.8 — Playwright worker: `Scrape` activity on `scrape-playwright`

**What:**
- **Activity name `Scrape`, task queue `scrape-playwright`** (owner, 2026-09-29 — ~~`PlaywrightScrape`~~).
  Same name and `ScrapeInput`/`ScrapeOutput` as Go; the queue picks the engine, as the NATS subject
  does today (B.1 dropped `engine` for this). Reuse `SCRAPE_ACTIVITY`; add
  `SCRAPE_PLAYWRIGHT_QUEUE`. ⚠️ Each worker polls **only its own** queue — both register `Scrape`,
  so a misconfigured poller steals the other engine's tasks with no error. Longer start-to-close +
  `heartbeat_timeout` than the http call (headed Chrome ~37 s, up to the job's `timeout_seconds`).
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

**Built 2026-09-30 (local) — `c405124`. Not released; B.9 ships it.** Owner wrote the activity's first steps (robots), then "build all".
- **Render pipeline shared, as Go's `internal/scrape` (owner: "mimic go; extract it until nats is
  present").** `worker/scrape.py`: `render()` owns one context end to end (cookies, image blocking,
  CSP, goto, load wait, actions, `detect_block`, format, upload) + `decrypt_credentials()`. A wall
  raises `BlockedPage(detection)` — `worker.py` turns it into its `failed` publish, the activity into
  a non-retryable error. Callers keep robots, the transport and failure handling. H deletes
  `worker.py`; `scrape.py` stays.
- **`worker/activity.py`:** `PlayWrightScrapeActivities(minio, browser).playwright_scrape`, registered
  as `Scrape` (`SCRAPE_ACTIVITY`); queue `SCRAPE_PLAYWRIGHT_QUEUE = "scrape-playwright"`, twin in the
  API's `queues.py`; the contract test asserts both and that the two scrape queues differ. Error
  types: `RobotsDisallowed`, `Blocked` (message `blocked:<vendor>`), `ScrapeFailed` (non-retryable),
  `StorageTransient`. Robots runs before the `try` (nothing to clean up, nothing to classify);
  credentials are decrypted inside it so `InvalidToken` reaches `classify()`. `content_hash` =
  `xxhash.xxh64(bytes).hexdigest()` in the activity (NATS gains no dependency). Screenshot sizes via
  `stat_object` — `execute_actions` returns paths only. Heartbeat task every
  `playwright_heartbeat_seconds`. Docstring states the caller's start-to-close: ≥ 2 ×
  `timeout_seconds` + actions + upload, per job (BUG-015 gives `goto` and the load wait a full budget each).
- **`worker/temporal_main.py`:** signal handlers → Temporal → MinIO → Chrome (same launch as
  `main.py`) → activity-only `Worker`, `max_concurrent_activities = playwright_max_workers`; browser
  closed after the drain. `main.py` branches on `WORKER_MODE` and installs an early SIGTERM exit
  before its imports (B.6's fix). **`entrypoint.sh` is unchanged** — both modes exec `worker.main`.
- ~~`playwright_graceful_shutdown_seconds = 150` is a placeholder~~ → **630 s** (owner delegated the
  call, 2026-09-30): covers the 300 s `timeout_seconds` maximum (`goto` + `wait_for_load_state` at 300 s
  each) + 30 s upload. The wait is paid only while a scrape is in flight — an idle pod exits at once —
  and the Temporal worker stops polling on SIGTERM, so the new pod takes new work during the drain.
  Page actions are uncapped (a list of `wait`s has no ceiling), so no value covers every job; a longer
  one is cancelled and re-rendered on the retry. **B.9's `terminationGracePeriodSeconds` = 660 s**
  (compose `stop_grace_period`: 660 s).
- **Lockfile (BUG-013):** `uv.lock`; the builder runs `uv sync --frozen --no-install-project` into
  `/opt/venv` against the image's Python 3.10 (`UV_PYTHON=python3`, no downloads); the
  `python3.10-venv` apt step is gone. Resolved: `patchright` 1.63.0, `temporalio` 1.33.0 (= api),
  `xxhash` 4.0.1. ✅ **Compared against prod's `pip freeze` 2026-09-30 (owner ran it):** identical
  except `cryptography` 50.0.1 → 50.0.2 (patch; Fernet only) and the additions (`temporalio` and its
  deps, `xxhash`). **`patchright` 1.63.0 on both — the stealth layer's Python half is unchanged.**
  ⚠️ **The Chrome binary is not locked:** `RUN patchright install chrome` fetches the current Google
  Chrome stable at build time, so every image build can ship a different Chrome (true of every
  release since ADR-008, not new in B.8). B.9 records `google-chrome --version` in both pods after the
  rollout — the first thing to check if a working target starts walling.
- **NATS behaviour, three small changes from the move:** a `new_context`/`new_page` failure is now
  classified (terminal → `failed` + ack) instead of escaping unacked; a `context.close()` failure is
  suppressed; the context closes before the ack/nak. Decryption still sits above `worker.py`'s `try`
  (a bad key escapes unacked — latent, NATS-only, deleted by H).
- **Tests:** `tests/test_activity.py` (16) — output/size/hash, hash keeps leading zeros, format →
  key, screenshot sizes, action warning, heartbeats, Myntra wall → `Blocked` not uploaded, robots
  disallow (no context) and fetch failure (proceeds), dead site, bad key (no context), proxy decoded,
  MinIO 5xx / unreachable → retryable, `NoSuchBucket` → non-retryable, cancel passes through and
  closes. `tests/test_entrypoint.py` (3) — ends in `exec python -m worker.main`, no `xvfb-run`, waits
  for the socket first. `test_main.py`: patches retargeted to `worker.scrape.*` + a NATS wall test.
  **202 worker, 51 contract.** Mutation-checked: wall retryable, no `BlockedPage` branch (activity
  and NATS — the NATS one was uncovered until the new test), no heartbeat, hash via `%x`, decrypt
  outside the `try`, robots fail-closed, no context close.
- **Local end to end:** compose `playwright-worker-temporal` (`WORKER_MODE=temporal`, no NATS env,
  `stop_grace_period: 170s`) — PID 1 `python -m worker.main` as `appuser`, headed Chrome,
  `temporal_worker_started … task_queue=scrape-playwright`. A throwaway workflow in the api container
  → `Scrape` on `scrape-playwright` → example.com markdown, 1359 B, `6cc61c07b397fc58`, deleted.
  SIGTERM at 0.5/1/2/4 s → exit 0; running → `stopping` → `stopped`, exit 0.
- ⚠️ **Found, pre-existing, not fixed (entrypoint preserved exactly):** restarting a *stopped* compose
  container reuses its filesystem, so `/tmp/.X99-lock` from the last run makes Xvfb fail (`Server is
  already active for display 99`), the socket wait passes on the stale socket, and Chrome dies with
  `Missing X server`. Both compose playwright services; `restart: unless-stopped` would loop on it.
  **Not prod:** the manifest mounts no `/tmp` volume, so a k8s restart starts from a fresh layer.
  Fix if wanted: `rm -f /tmp/.X${SERVERNUM}-lock /tmp/.X11-unix/X${SERVERNUM}` before Xvfb.

#### B.9 — Playwright second Deployment + 🚀 release + pre-gate on both engines

**What:** `app/playwright-worker-temporal.yaml` (dshm volume, resources, Xvfb env — copy the NATS
one exactly; add `terminationGracePeriodSeconds: 660` — above the 630 s graceful shutdown, B.8).
Release; `rollout status` on both; `google-chrome --version` in both pods (the binary is not locked, B.8). Then B.4's probe on `scrape-playwright`
against a real page (⚠️ the probe hard-codes `SCRAPE_HTTP_QUEUE` and `ScrapeInput` has no `engine`
— give the workflow and script a queue/engine argument here): v1 vs probe outputs compared on structure (headed Chrome is not byte-stable).
**Depends on:** B.8, B.4

**Done (2026-09-30, released):**
- **Probe (`a11cce6`):** `ScrapeProbeInput(scrape, engine)`; `scripts/probe_scrape.py --engine
  http|playwright`. Playwright → `scrape-playwright`, `start_to_close = 2 × timeout_seconds + 60 s`
  (default 60 → 180 s), `heartbeat_timeout = 90 s`; http unchanged (90 s). One new test pins queue +
  both timeouts (mutation-checked: `1 ×` fails it); 303 API tests. Compose: `example.com` markdown
  through both engines, objects deleted.
- **Graceful shutdown 150 → 630 s (`cdbb8c2`, owner delegated the call)** — see B.8's note;
  `terminationGracePeriodSeconds: 660`. The NATS manifest has no dshm volume or Xvfb env (this
  task's *What* said so; `--disable-dev-shm-usage` handles it in-app) — nothing to copy.
- **Release:** `main` ff `1717032..a11cce6` — `api` + `playwright-worker` built, no Alembic revision.
  Flux bumped api / workflow-worker / cleanup CronJob / NATS playwright-worker (infra `47ca730`,
  `9e69931`); the new manifest was rebased over them, **tag hand-set** to
  `main-1790793252-a11cce6…`, pushed as infra **`9ebb305`** (the classifier allowed the push).
- **Verified:** `rollout status` green on `scrapeflow-api`, `-workflow-worker`, `-playwright-worker`,
  `-playwright-worker-temporal`. New pod logs `browser_launched channel=chrome headless=False` →
  `temporal_worker_started … graceful_shutdown_s=630.0 task_queue=scrape-playwright`; `task-queue
  describe` (one-off admin-tools pod) lists its poller. The NATS pod re-subscribed
  (`python-playwright-worker`, durable untouched). **Chrome 154.0.8037.92 in both pods** — the first
  recorded prod Chrome version; the pre-release one was not captured.
- **Capacity** before → after: CPU requests 32 → 39 %, **limits 191 → 216 %**; memory requests 14 →
  17 %, limits 62 → 75 %. Idle, each Playwright pod ≈ 5m CPU / ~300 Mi.
- ✅ **Gate passed (owner ran it):** `example.com`, `html` — `probe_scrape --engine playwright` →
  12,747 B, `18f1a13f59dcc2ad`; v1 job `fe5b27eb…` (`engine: playwright`) → `job_runs.content_hash`
  `18f1a13f59dcc2ad`. **Headed Chrome is byte-stable on this page** — two local renders gave the same
  hash — so an exact match is the test. The 12.7 KB (vs 713 B raw) is example.com's own script
  splitting its text into one `<span>` per letter, not an injection.
  A first v1 submission was sent as `engine: http` by mistake (owner) — it returned B.4's HTTP hash
  `a6cdea39c93062d1`; not a routing fault.
  ⚠️ v1 jobs may run through the platform-default proxy (the HTTP job logged `Using proxy`); the probe
  sends no credentials. A mismatch on a geo-sensitive page is the proxy first.

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

**Built 2026-10-01 (local) — `9bdb3a5`, migration 4.4 `cff9ec8fedbe`. Not released.** Owner: "build the
models", then "do the migration locally". `api/app/models/pipeline.py` (four classes, no ORM relationships).
- **Owner calls (2026-10-01):** (a) `pipeline_runs.error` added — set on every `failed` run, as
  `"<block_id>: <block error>"` when a block caused it, the run-level reason otherwise (e.g. C.5's
  start failure); `NULL` on `cancelled`. Blocks keep their own `error`. (b) `pipeline_runs.result_path`
  is `NULL` unless the run is `completed` — **not** enforced by a CHECK; C.6's mirror owns it.
  Indexes delegated to me.
- **Delete rules:** CASCADE on every `user_id`, on `pipeline_versions.pipeline_id` and on
  `pipeline_run_blocks.pipeline_run_id`. **No `ondelete`** on `pipeline_runs.pipeline_id` /
  `pipeline_version_id` — a pipeline or version with runs cannot be deleted. That closes C.4's
  check-then-delete race (a run inserted between "any runs?" and `DELETE`); C.4 catches the FK error
  and falls back to the soft delete. RESTRICT vs NO ACTION verified identical on Postgres 16 (multi-path
  user cascade succeeds both ways) — no trap.
- **Indexes:** `(user_id, created_at)` and `(pipeline_id, created_at)` on runs; `UNIQUE (workflow_id)`;
  `UNIQUE (pipeline_run_id, block_id)` on blocks. None on `pipeline_version_id`; the `status = 'running'`
  partial index is C.2's, beside the view that reads it.
- No CHECK on block `type` (catalog is code, C.3). Runs carry `created_at` + `finished_at`, no
  `started_at` (inserted `running`). `pipelines.updated_at` uses `onupdate` — set it explicitly on any
  `update()` path.
- **Verified:** upgrade → downgrade → upgrade clean; `alembic check` shows only the dedup false
  positive; in a rolled-back transaction: block status defaults `pending`, pipeline delete with a run
  refused, `processing` refused, soft-deleted name still 409s, user delete removes all four. 303 API
  tests. Pre-commit ruff reformatted the migration (cosmetic).

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

**Built 2026-10-01 (local) — `b5d0f94`, migration 4.5 `9db1dcda44f7`. Not released.** Owner: "build this".
- **Owner calls (2026-10-01):** (a) its own revision, not folded into 4.4 — hand-written view SQL stays
  out of the autogenerated table file. (b) **The concurrency arm also requires `pipeline_runs.status =
  'running'`** — a run holds a slot iff the run is `running` **and** ≥1 block is `running`. Narrower
  than 15a's literal wording: a block left `running` by a failed mirror write (11c) on a terminal run
  must not pin the slot until someone repairs the row.
- **Arms:** `quota_run_units` — one row per `pipeline_runs` row (`unit_id = submission_id = run.id`),
  any status. `quota_active_submissions` — `EXISTS` on `pipeline_run_blocks`, so one slot per run
  however many blocks run. v1 arms byte-identical (`pg_get_viewdef` diffed before/after; R5).
- **Ledger:** `storage_objects.pipeline_run_block_id` (FK `fk_storage_objects_pipeline_run_block_id`,
  CASCADE, partial index); CHECK `num_nonnulls(job_run_id, crawl_page_id, pipeline_run_block_id) = 1`.
  `record_object(pipeline_run_block_id=…)`; `objects_for_pipeline_run` + `release_pipeline_run_objects(run_id)`
  — no legacy branch; the 503 refusal stays the caller's (C.4), as for crawls.
- **Index:** `idx_pipeline_runs_user_id_running` — `(user_id) WHERE status = 'running'`, the partial
  index C.1 deferred here. The block lookup rides `uq_pipeline_run_blocks_pipeline_run_id_block_id`.
- **Downgrade** restores 4.2's views and CHECK byte-identical, and **fails while any pipeline ledger
  row exists** (the old CHECK rejects it) — release those objects first. Deliberate, loud.
- **Reconcile needs no change:** it matches existing rows by key before attributing, so a recorded
  pipeline object is never an orphan; an unrecorded one is (= "a run that fails accounting holds nothing").
  C.6's object path convention decides whether `_attribute` ever needs a pipeline arm.
- ⚠️ **The views now also bind** `pipeline_runs.status`/`user_id`/`created_at`/`id` and
  `pipeline_run_blocks.status`/`pipeline_run_id` — drop-alter-recreate applies to them (`CLAUDE.md`).
- **Verified:** round trip clean; `alembic check` = dedup false positive only. 8 tests (5 quota, 3 ledger),
  each mutation-checked: old views → the two arm tests fail; block-rule-only → stale-block test fails;
  run-status-only → `waiting` test fails; per-block rows → one-slot test fails; old CHECK → all 3 ledger
  tests fail. **311 API tests.**

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

**Built 2026-10-02 (local) — `9fc7198` (committed 2026-10-05), not released.** Owner: "build c3 but do not commit", while AFK —
so every call below is **mine and reversible**; the owner walked through the catalog, the API → Temporal
flow and the validator on 2026-10-05 and closed it without changing any. `api/app/pipelines/catalog.py`
(pure: five `BlockType`s, versioned config models, reference types, timing) + `validator.py`
(`validate(raw) -> ValidatedPipeline` or `PipelineValidationError` listing **every** problem, each naming
its block). Settings `max_blocks_per_pipeline` (20), `max_pipelines_per_user` (50, enforced by C.4),
`pipeline_max_run_seconds` (86400). `schemas/jobs.py`: the actions check extracted to
`validate_page_actions()`, shared with Scrape — job behaviour unchanged.
- **Definition shape:** `{"inputs": {name: {"type": "url"}}, "time_budget_seconds": int|null,
  "blocks": [{"id", "type", "input": <previous block id>|null, "config_schema_version", "config"}]}`.
  A binding is `{"$input": "<name>"}` in place of a bindable field's value (Scrape `url` only — §4).
  `validate()` returns a **normalized** definition for C.4 to store: versions stamped, config defaults
  filled (so a later default change does not alter a pinned version), bindings kept.
- **Reference types carry the page format:** `page:html|markdown|json`, `extraction`. Clean consumes
  `page:html` only (a markdown scrape → Clean is refused at save); LLM consumes any page, not an
  extraction; effect blocks pass their input type through.
- **Validate rules:** `json_schema`, `present`, `type`, `compare` (JSON Pointer paths; ordered ops need
  a number) need JSON input (`page:json`/`extraction`); **added** `contains` and `min_length` for text
  pages — R2's "guard scraped content before an LLM call" had no rule that could read a page.
- **Timing lives in the catalog** (`Timing`, read by C.7): retry numbers 5 s / ×2 / 60 s / 3 for
  Scrape, Clean, Validate, LLM (all three NATS workers' numbers); Webhook = 15c's ladder (20 s attempt,
  30 s ×10 cap 7200 s, 5 attempts). Attempts: Scrape http 90 s, playwright `2×timeout_seconds+60` +
  90 s heartbeat (the probe's), LLM 400 s + heartbeat, **Clean/Validate 60 s (a guess — C.8 confirms)**.
  A block's budget = every attempt at its limit incl. 60 s `schedule_to_start` + every backoff wait =
  its `schedule_to_close`. Run need = Σ(budget + **60 s per-block allowance for C.6's activities** —
  C.6 must fit inside it). R6 recipe: 12,970 s. **No per-block budget override** — `maximum_attempts`
  ends retries first, so a larger `schedule_to_close` would be a dead knob; Playwright's
  `timeout_seconds` is the user's real one.
- **Stricter than jobs, deliberately (save-time is the point):** `actions`/`playwright_options` refused
  on `engine: http` (jobs refuse actions, silently ignore options); LLM `output_schema` must have
  `"type": "object"` at its root (both providers need it); a declared-but-unused run input is refused.
  **`proxy_provider` left out** — stored on jobs, read nowhere.
- **Starting-block rule (§8) is its own function**, and a test proves it: a second Scrape naming the
  previous block passes the chain rule and is still refused.
- ⚠️ **For C.4:** (1) Scrape's `proxy_url`/`cookies` are user secrets (`BlockType.secret_fields`) — the
  stored definition is returned by GET and becomes workflow input, so C.4 must encrypt them out
  (the activity contract already carries Fernet ciphertext). (2) DB/network checks are C.4's: LLM key
  ownership, SSRF on a literal Scrape/Webhook URL and the key's `base_url`, the per-user count.
  (3) Block IDs are required here — C.4 assigns missing ones before calling `validate()`.
- ⚠️ **Not done — `jsonschema` is not an API dependency.** The `json_schema` rule's schema and the LLM
  `output_schema` are checked for shape only, not meta-validated. Owner's call whether to add it to the
  API lock; C.8 needs it in the LLM image either way to evaluate the rule.
- ⚠️ **For C.7:** do not call `validate()` in the workflow body — it reads settings, so an operator
  limit change could fail a replay. Use the catalog (`config_versions[v].model_validate`, `.timing`).
- **Verified:** 60 tests (one per rule, positive + negative; R6 recipe validates; a stored definition
  re-validates to itself); 13 mutations — each rule switched off in turn — each failed its own tests.
  **371 API tests.** Ruff clean except two pre-existing UP042 hits in `schemas/jobs.py`.
- **Known weakness:** the type walk and the budget check run only once every block's config parses and
  the chain holds, so a definition with a config error *and* a type mismatch needs two saves to see both.
  Steps before that gate report together.
- **2026-10-05:** effect blocks' `produces` is `_pass_through` (asserts an input exists) — the lambda
  was typed `str | None` against a `str` field.
- ⚠️ **Gap found in the walkthrough — for C.5 / C.7:** `LLMExtract` needs the user's provider, `base_url`
  and **encrypted key**, but the definition holds only `llm_key_id` and the workflow body cannot read
  `user_llm_keys`. Either C.5 resolves the key and passes the ciphertext in the workflow arguments (as
  Scrape's `Credentials` already travel), or a C.6 DB activity fetches it. Open item 9.

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
6. *(Added at the A.8 review, 2026-09-29.)* **The API gets its Temporal client here** — a stored
   client on `app.state`; decide **lazy vs eager connect** (eager = a Temporal outage stops the
   whole API from starting; carried from A.5). The prod env it reads was added in **A.9**.
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
  (non-retryable errors are raised by the activity, not listed here — §10; **LLM: B.6's owner
  call — 5 s / ×2 / 60 s cap / 3 attempts, never the unlimited default**) → `record_storage` for
  content blocks → mirror `completed` with refs.
- References only: an activity returns `result_path`, never bytes (§5). Effect blocks pass their
  input ref through.
- Terminal failure: mirror `failed` (block + run), remaining blocks `skipped`; stop.
  ⚠️ **The error text is the workflow's to finish:** neither `ApplicationError` nor the
  `ActivityError` the workflow catches carries an attempt count — only `retry_state`. On
  `MAXIMUM_ATTEMPTS_REACHED` append `(gave up after {maximum_attempts} attempts)` to the cause's
  message, as the NATS worker does today; the activity cannot, since it does not know which
  attempt is the last (B.6).
  Read the label from `ActivityError.cause` — one level deeper is the original exception, also an
  `ApplicationError`, typed by its class name (B.6).
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
**Corrected 2026-09-29 (B.1 review)** — two gaps in the text above:
- **Mirror every transition users see today, not only `completed`** (R5): `running` before Scrape,
  `processing` before LLM, `completed` / `failed` at the end — the same four states on the same
  `job_status` channel. The scrape worker no longer reports `running`; the workflow does, through
  C.6's mirror activity. ⚠️ **`running` now means *scheduled*, not *picked up*** — set a
  `schedule_to_start_timeout` on the scrape call so a queue nobody polls fails the run instead of
  showing `running` indefinitely. `nats_stream_seq` is no longer written (its reader, the
  MaxDeliver advisory, dissolves with NATS).
- **The input is not what `dispatch.py` builds today.** B.1's `ScrapeInput` drops `run_id`,
  `engine`, `crawl_context` and `schema_version`, so the existing builders cannot produce it — add
  a `ScrapeInput` builder per lane beside them, and keep the dispatch-vs-recovery equality tests
  on it. Engine → task queue is **one helper beside `queues.py`**, not a ternary per call site
  (today's is copied at five).

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
8. ~~**A.8** — where the Temporal Postgres backup lives.~~ **Answered 2026-09-29: nowhere, for
   either Postgres — deferred past the migration by the owner** (data is junk today). `phase4-backlog.md` §4.
9. **C.5 / C.7** — where the LLM block's key comes from: the definition holds `llm_key_id` only, the
   workflow cannot read `user_llm_keys`. C.5 resolving it into the workflow arguments (Fernet ciphertext,
   like Scrape's `Credentials`) vs a C.6 DB activity. Found 2026-10-05 (C.3 *Built* note).
