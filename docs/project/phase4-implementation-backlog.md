# ScrapeFlow Phase 4 — Temporal Implementation Backlog

> **Persona:** Tech Lead. This is the ordered, engineer-ready task list for the migration
> ADR-009 §16 names. It sequences; it does not decide. Where a task needed a call the ADR did
> not make, the call is marked **TL call** with its reasoning and is reversible; where a task
> exposes a gap in the design, it is marked **⚠️ raise to Architect** and the task is blocked on
> that answer, not on a guess.
>
> **Scope source of truth:** `phase4-backlog.md` (§2 the migration · §3 **do NOT fix** · §4 survives).
> **Decisions:** ADR-009 (Accepted), ADR-011 (Accepted), ADR-010 (Accepted 2026-10-13), ADR-012 (Draft), ADR-013 (Accepted 2026-10-27).
> **Inventory + shapes:** `temporal-full-migration.md`. **Product spec:** PRD-016.
> **Last updated:** 2026-10-12 (D.1, D.3, D.4 built; D.2 deferred by the owner; D.5 runbook ready) · **Tracking:** the status table below is the tracker.

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
| C.4 | Pipelines CRUD API + versioning + delete semantics + admin routes | ✅ Built 2026-10-07, committed 2026-10-11 (local, unreleased; owner's code review deferred); see *C.4–C.7 production refactor* |
| C.5 | Run trigger: admission (three meters + headroom buffer), row insert, workflow start | ✅ Built 2026-10-07, committed 2026-10-11 (local, unreleased; owner's code review deferred); see *production refactor* |
| C.6 | Workflow-worker DB activities: mirror, accounting, cancel-check | ✅ Built 2026-10-07, committed 2026-10-11 (local, unreleased; owner's code review deferred); `on_storage_wall` folded into `record_storage`; see *production refactor* |
| C.7 | `PipelineWorkflow` body | ✅ Built 2026-10-07, committed 2026-10-11 (local, unreleased; owner's code review deferred); scrape + llm_extract only until C.8/C.9; see *production refactor* |
| C.8 | Clean + Validate activities | ✅ Built 2026-10-11 (local, `d75c7b2`, unreleased); LLM worker's `content` queue; see C.8 *Built* |
| C.9 | Webhook activity: wire contract, SSRF, four-timeout ladder, `waiting` | ✅ Built 2026-10-11 (local, `5b14291`, unreleased); open item 2 built on the TL reading — see *Built* |
| C.10 | Cancellation: API cancel + workflow cancel + boundary re-check | ✅ Built 2026-10-11 (local, `66abdeb`, unreleased) |
| C.11 | Run/result read API + `pipeline_status` notify channel + WS route | ✅ Built 2026-10-11 (local, `88e8024`, unreleased); **BUG-009 pulled in and fixed** (open item 7) |
| C.12 | Frontend: Pipelines pages + WS reconnect (11b, both lanes) | ✅ Built 2026-10-11 (local, `c2590af`, unreleased); BUG-018 not ridden — click-through after release |
| C.13 | Workflow-code versioning: decide + build (⚠️ Architect) | ✅ Built 2026-10-12 — owner chose **`patched()` + a replay test that gates the build** (ADR-012, Draft); Worker Versioning not taken |
| C.14 | 🚀 Pipeline-lane release + **R6 acceptance gate** | ✅ **RELEASED 2026-10-12** (`6675f17..1793c60`, api + llm-worker; no infra change) — **R6 PASS 2026-10-12** (on `03f72f8`, BUG-022's fix) |
| **D** | **Job cutover** | |
| D.1 | Lane marker on `job_runs` (mechanism 4) + v1 dispatcher filters | ✅ Built 2026-10-12 (local, `db774d0`, migration 4.6 `56ab6118641d`, unreleased) |
| D.2 | `ChangeDetection` activity: content-hash dedup + `diff.py` port (**R5 obligation**) | ⏸ **Not built — owner, 2026-10-12: drop for now.** Jobs on v2 have no dedup and no diff (PRD-016 R5's second named exception) |
| D.3 | `JobWorkflow` + job-lane webhook row activity | ✅ Built 2026-10-12 (local, `5e61413` + `9bd761e`, unreleased) |
| D.4 | Single dispatch switch in `dispatch.py` (`create_job` + scheduler) + cancel path | ✅ Built 2026-10-12 (local, `de91a44`, unreleased); infra `JOB_LANE=v1` committed locally, **not pushed** (infra `9ef396a`) |
| D.5 | 🚀 Job cutover release: drain gate → flip → verify; rollback runbook | ✅ **RELEASED + FLIPPED 2026-10-12** (`main` `28c881b..0b4ac9e`; infra `9ef396a` v1, `99ed5c8` **v2**) — **new job runs run on Temporal in prod**; http + playwright verified |
| **E** | **Batch and crawl cutover** — ✅ **closed 2026-10-27** (owner) | |
| E.0 | Gate: ADR-010 Accepted; task-queue shape decision (per-host limiter / lanes) | ✅ 2026-10-13 — bounded fan-out on one queue per engine; no lanes yet; per-host limiter on every scrape, 1 req/s + 2 in flight per eTLD+1, fail open; k = 3, scrape queue timer 60 s → 5 min. Open: robots `Crawl-delay` |
| E.8 | eTLD+1 helper + per-host limiter (Redis leases, acquire/release activities, fail open) | ✅ **RELEASED 2026-10-27** with E.2 (`410f015`; infra `0f71515` `REDIS_URL`) |
| E.9 | Per-host limiter on `JobWorkflow` + `PipelineWorkflow` under `patched()`; scrape queue timer → 5 min; ships with E.2 | ✅ **RELEASED 2026-10-27** with E.2 (`ba18cf3`); budget audit after rollout: 0 versions declare a budget, 0 short |
| E.1 | `BatchWorkflow` (fan-out child `JobWorkflow`, absolute-total notify) | ✅ **RELEASED 2026-10-27** with E.2 (`71e43fa`; infra `1e38aaa` `BATCH_LANE=v1`) |
| E.2 | 🚀 Batch cutover release: drain gate → flip → verify | ✅ **RELEASED + FLIPPED 2026-10-27** (`main` `0b4ac9e..faa1f83`; infra `0f71515` + `1e38aaa` v1, `538ea8c` **v2**) — **new batches run on Temporal in prod**; 3-URL http batch verified |
| E.3 | Crawl frontier: table shape + admission activity (SSRF, eTLD+1, dedup index) | ✅ Built 2026-10-27 (local, `c36bf95`, migration 4.7 `532e93f0d629`, unreleased) — one table, refusals kept as `dropped` rows |
| E.4 | Crawl fetch-side activities: robots + sitemap on `httpx`, link extraction port | ✅ Built 2026-10-27 (local, `c36bf95`, unreleased) — `crawl` queue on the Playwright worker's Temporal Deployment |
| E.5 | `CrawlWorkflow`: BFS via activities, continue-as-new, per-page ledger + ceiling | ✅ Built 2026-10-27 (local, `c36bf95`, unreleased) |
| E.6 | Crawl compensating gate (no v1 reference exists) | ✅ Built + **PASSED on compose** 2026-10-27 (`aefc1d4`), http and playwright |
| E.7 | 🚀 Crawl cutover release; `coordinator/` deletion | ✅ **RELEASED 2026-10-27** (`main` `faa1f83..261f931`; infra `bf591bc`) — **crawls run on `CrawlWorkflow` in prod; the coordinator is gone**; owner verified a crawl + the SPA |
| E.10 | Batches + crawls in the SPA (list endpoints, list + detail pages, live status) — after E.7 | ✅ **RELEASED 2026-10-27** with E.7 (`6c1b847`) — owner clicked through in prod (crawl + batch results) |
| **F** | **Schedule and webhook cutover** — ✅ **closed 2026-10-28** (owner) | |
| F.0 | Gate: Schedule overlap policy decided (⚠️ Architect) | ✅ 2026-10-27 — ADR-013 Accepted (owner) |
| F.1 | Temporal Schedule per recurring job + quota admission (ADR-010 §1) | ✅ Built 2026-10-27 (`69fcd7b`, migration 4.8 `437442ce59b2`; **released 2026-10-27** with F.6) — outage collapse + pause/resume verified on compose |
| F.2 | `schedule_status` interlock guard + `active_recurring_jobs` scoping | ✅ Built 2026-10-27 (`e2cadca` + the lane guard in F.1; **released 2026-10-27** with F.6) |
| F.3 | Per-job migration runbook (pause v1 → verify → create Schedule) | ✅ Built 2026-10-27 (`a46109d`; rollback mode removed at F.6; **released 2026-10-27** with F.6) |
| F.4 | Job-lane webhook delivery as an activity; `webhook_deliveries` fate decided | ✅ Built 2026-10-27 (`35ada75`; **released 2026-10-27** with F.6) — a workflow per row + a sweep Schedule; the table stays — **owner confirmed 2026-10-28** |
| F.5 | Three webhook meters scoped to the job lane (15e) | ✅ Built 2026-10-27 (`539114e`; BUG-028 fixed with it; **released 2026-10-27** with F.6) |
| F.6 | 🚀 Release; delete `scheduler.py`, `webhook_loop.py`, `advisory.py` (drain gate) | ✅ **RELEASED 2026-10-27** (`main` `261f931..3b9cf9c`; no infra change) — schedules + webhooks on Temporal in prod; owner verified a 5-min schedule, its webhooks, pause, delete |
| F.7 | Scheduled jobs in the SPA (schedule fields on job reads, schedule view, pause/resume, next run) — after F.6 | ✅ **RELEASED** with F.6 (`56a06e6`) + run-history fix **`41a348f` 2026-10-28** — owner clicked through in prod |
| **G** | **Consumer deletion** | |
| G.1 | 🚀 Delete `result_consumer.py` (drain gate at deletion) | ✅ **RELEASED 2026-10-28** (`main` `41a348f..bc30025`, infra `682cc52`) — consumer + v1 dispatch arms + `JOB_LANE`/`BATCH_LANE` gone; owner verified a job + a batch |
| **H** | **NATS removal** | |
| H.1 | Delete NATS-bound worker Deployments (obligation 3) | ✅ **RELEASED 2026-10-28** (`main` `bc30025..722f54b`, infra `c92e7dd`, step 4 `51c2d40`) — NATS mode gone from all three workers; owner verified http + playwright jobs and a crawl |
| H.2 | 🚀 Remove NATS: stream init, StatefulSet, client deps, `nats_stream_seq`, contracts NATS arm | ✅ **RELEASED 2026-10-28** (`main` `722f54b..cd5c60e`, infra `432e014`) — NATS gone from the API, compose and the cluster; JetStream PVC deleted; owner verified http + playwright jobs. Column drop moved to I.1 |
| **I** | **API thinning** | |
| I.1 | Alembic-on-startup made multi-replica safe; carries H.2's `nats_stream_seq` drop | 🔧 **Built 2026-10-29** (local, unreleased) — advisory lock in `migrations/env.py`, migration 4.9 `47a8f9f3f4e2`; ships with I.2 |
| I.2 | 🚀 `replicas: 2`, `RollingUpdate`; JobNotifier per replica verified | ✅ **RELEASED 2026-10-29** — infra `5e9087e`, `main` `cd5c60e..eea8344` (carries I.1); rolling deploy 0/359 failed probes vs ~40 s down before. **Group I closed** |
| **Docs track** (parallel; not on the critical path until named) | | |
| X.1 | Write PRD-019 — before PRD-018; may be written during C | ⬜ |
| X.2 | Promote ADR-010 — needed by E.0 | ✅ 2026-10-13 — Accepted after review; §1 monthly skips, §2 sitemap files gated + submitted-seed scope + drops counted |
| X.3 | Schedule overlap policy → ADR — **ADR-013, `SKIP` (owner)** | ✅ Accepted 2026-10-27 |

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
6. **ADR-010 must be Accepted before E.0** — ✅ Accepted 2026-10-13. The overlap policy (X.3)
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
- ~~⚠️ **Not done — `jsonschema` is not an API dependency.** The `json_schema` rule's schema and the LLM
  `output_schema` are checked for shape only, not meta-validated. Owner's call whether to add it to the
  API lock; C.8 needs it in the LLM image either way to evaluate the rule.~~ ✅ **C.8** (owner: worker
  + API) — both are meta-validated at save.
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
- *(Added 2026-10-06, owner's call.)* **Run a chosen version:** `POST /pipelines/{id}/runs` takes
  an optional `version` (int ≥ 1; omitted = latest), so two versions can be run side by side and
  compared. Not a revert — a revert is a `PATCH` re-saving the old definition as a new version.
  Lands in C.5's trigger: only the version lookup branches; the rest of `create_run` reads
  `version.id` + `version.definition` either way. Unknown version → 404. Trigger-time checks (key
  ownership, SSRF, runnable block types) apply to an old version unchanged; `validate()` does not
  re-run — an old version is checked against the rules of the day it was saved. Built with
  `PATCH` (nothing to choose between before it). Test: v1 and v2 runs pin their own
  `pipeline_version_id` and pass their own definition to the workflow.
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
- *(Added 2026-10-06, owner's call — a refactor of the MVP slice.)* **Typed stored definitions.**
  The MVP reads `pipeline_versions.definition` back as a raw dict, in the trigger and in the
  workflow (`block["config"]["output_format"]`). ⚠️ **A missing key in a workflow body is a
  `KeyError`, not an `ApplicationError` — it fails the workflow *task*, which Temporal retries
  forever:** the run sits `running`, holding a concurrency slot, with no error and no mirror write.
  Not reachable in the MVP (every stored definition is normalized, one config version per type);
  reachable from the first `V2` config, a hand edit or a migration bug. Four parts:
  1. **Parse at the trigger, before any row is written** — the definition into a typed model, each
     block's `config` through `CATALOG[type].config_versions[config_schema_version]`. Unreadable →
     a named error, no run created. **Parse, do not re-run `validate()`'s rules** — a pinned
     version keeps the rules of the day it was saved (§4, §6); re-validating would strand old
     versions when a rule tightens.
  2. **Typed workflow input** — `PipelineRunInput` carries parsed blocks, not `dict[str, Any]`;
     `_execute` reads attributes. The Pydantic data converter is already on the client.
  3. **Historical config shapes** (§4's obligation) — when a `V2` config lands, either the
     interpreter handles each version, or a `v1 → v2` upgrade runs at parse time so it sees only
     the latest. Undecided; decide with the first `V2`.
  4. **Safety net** — any unexpected exception in `_execute` becomes a non-retryable
     `ApplicationError`, so a bug fails the run instead of hanging its workflow task.
     ✅ **Built 2026-10-07** (owner: "agree with your recommendations; build it"). One
     `except Exception` shares the known-failure path; `_failure_message()` labels the rest
     `internal error: <Type>: <message>` (owner's call — the type stays visible to the user, the
     traceback rides in the workflow failure's cause). ⚠️ **`workflow.NondeterminismError` (and its
     sandbox subclass `RestrictedWorkflowAccessError`) is re-raised, not netted** — both are plain
     `Exception` subclasses; a code-vs-history mismatch is fixed by a deploy, and failing the run
     would destroy it. ⚠️ **No Temporal knob replaces the net**: `failure_exception_types`, an
     `execution_timeout` and a workflow `retry_policy` each end (or never reach) the workflow
     without running the mirror, so `pipeline_runs` stays `running`. ⚠️ **A failing workflow task
     retries in real time — the time-skipping test server cannot skip it**, so tests bound
     `execution_timeout` (30 s default, 3 s for the pass-through test). Tests: a scrape config
     missing `output_format` → block + run `failed`, Scrape never called; a
     `RestrictedWorkflowAccessError` → no `failed` mirror, the workflow times out. Both mutations
     (net removed; pass-through removed) caught by their own test. 395 API tests. Parts 1–3 still
     owed — the `block["id"]` / `block.get("input")` reads in `run()` sit outside the net until
     part 2 makes them attributes. *(Parts 1–2 built later the same day — see the production
     refactor note.)*
  Tests: an unreadable stored definition → no run row, named error; a `KeyError` in `_execute`
  → run `failed`, not stuck.
- Tests (time-skipping env, activities mocked): R6 recipe happy path; block failure → skipped
  tail; cancel at boundary; storage wall → completed + named error; the budget composition.
**Depends on:** C.6, B.1

#### C.4–C.7 — MVP slice *(built 2026-10-05 → 10-07; local, uncommitted)*

**Owner's call 2026-10-05:** build a thin working slice end to end first, refactor to the full task
specs after ("MVP mode; once I'm comfortable we'll refactor"). C.4–C.7 stay 🟡 until their full
specs above are met; this note is what exists. *(Superseded by the production refactor note below.)*

- **C.4** — `POST /pipelines`, `GET /pipelines`, `GET /pipelines/{id}` (latest version)
  (`api/app/routers/pipelines.py`, `api/app/schemas/pipelines.py`). Missing block IDs get 8 hex
  chars before `validate()`; the normalized definition is stored as version 1. LLM key not owned →
  404; name taken → 409 from `uq_pipelines_user_id_name`. ⚠️ **IDs are generated in Python so both
  INSERTs run inside `commit()`** — a `flush()` to get the ID raised the `IntegrityError` outside
  the `try` → 500. Scrape's `proxy_url`/`cookies` refused with 422 (owner: no plaintext secrets
  stored; encryption is the refactor's).
- **C.5** — `POST /pipelines/{id}/runs` (202) + `GET /pipelines/{id}/runs/{run_id}` (run + blocks by
  position). Order: ownership → runnable block types (`scrape`, `llm_extract`) → run inputs
  exactly the declared names → **SSRF on every scrape URL (literal or bound) and every LLM key's
  `base_url`** → keys still exist (422) → `monthly_runs` + `concurrent_jobs` → commit run
  (`running`) + blocks (`pending`) → `start_workflow(id=pipeline-run-{id}, REJECT_DUPLICATE,
  queue workflow)`. Start failure → run `failed`, `could not start workflow: …`, 503. Temporal
  client **lazy** (`get_temporal` in `app/workflows/client.py`, kept on `app.state`) — owner's call;
  a Temporal outage fails runs, not API startup. Open item 9 answered (below).
- **C.6** — `api/app/workflows/activities/db.py`: `mirror_block_status`, `mirror_run_status` on the
  workflow queue; a mirror matching ≠ 1 row raises non-retryable. **A `failed` run marks its
  still-`pending` blocks `skipped` in the same transaction** (owner, 2026-10-07).
- **C.7** — `api/app/workflows/pipeline.py`: `PipelineWorkflow` interprets the definition as a dict;
  `artifact_id` = the block's `pipeline_run_blocks.id` (ADR-011); a block failure mirrors block +
  run `failed` (`"<block>: <Type>: <message>"`) and raises non-retryable.
  `api/app/workflows/steps.py`: the Scrape / LLMExtract calls, moved unchanged out of `probe.py`.
- **Verified:** 393 API tests (+22), mutation-checked (REJECT_DUPLICATE, commit-before-start, SSRF,
  input_ref mirror, rowcount guard, skipped tail). Live on compose: a scrape-only run `completed`; a
  scrape → LLM run with a fake Anthropic key `failed` with `extract: LLMFailed: AuthenticationError
  … 401`, fetch `completed`. Test user and objects removed after.
- ⚠️ **Neither scrape worker checks SSRF** — the API is the only check (`create_job`, now
  `create_run`). Any new dispatch site must check before the fetch is scheduled.
- **Deferred to the refactor** (each task's spec above): C.4 `PATCH` + ID carry-through, `DELETE`,
  `/versions`, admin routes, `max_pipelines_per_user`, secret encryption, run-a-chosen-version;
  C.5 storage meter + headroom buffer, typed parse of the stored definition; C.6 `pg_notify`,
  cancel precedence, **`record_storage` (MVP artifacts are uncharged, and nothing deletes them)**,
  `check_cancelled`, storage wall; C.7 typed definitions + safety net, budgets from the catalog,
  the `MAXIMUM_ATTEMPTS_REACHED` text, cancellation, Clean/Validate/Webhook, result = last
  *content* block.
- Prod: nothing — needs a release (api image; the workflow worker registers the new workflow).

#### C.4–C.7 — production refactor *(built 2026-10-07, committed 2026-10-11; local, unreleased)*

**Owner, 2026-10-07:** "build everything at once; prod refactoring of c4-c7 … do not commit". Built
while the owner was away, so **every call below is mine and reversible**. **2026-10-11, owner:**
"skip this review for now; whenever possible i'll read the code and ask … finish up to c7 and close
this session" — committed **without the owner's code review**; questions come as the owner reads.
Supersedes the MVP note above wherever they differ.

- **C.7 parts 1–2 — typed definitions.** New `api/app/pipelines/definition.py` (pure):
  `StoredDefinition`/`StoredBlock` = the stored JSON; `parse_definition()` → `ParsedDefinition` of
  typed run blocks (`ScrapeBlock`, `LLMExtractBlock`, … discriminated on `type`, config through
  `CATALOG[type].config_versions[v]`) + stored secrets by block id. **Parse, not re-validate.** A
  config version with no run-block model fails the parse with a named error — that is part 3's hook,
  still undecided. The trigger parses before any row is written: unreadable → **422**
  `this version cannot be run: …` per block, logged `pipeline_version_unreadable`, no run.
  `PipelineRunInput.blocks` is typed; the workflow reads attributes.
- **C.4.** `PATCH` (name and/or definition → next version; **an unchanged definition saves no
  version**; concurrent saves → 409 from `UNIQUE (pipeline_id, version)`), `DELETE` (no runs → hard;
  runs → soft, name held; the FK refusal on a run that lands between check and delete → soft),
  `GET /{id}/versions`, `GET /admin/pipelines[?user_id&include_deleted]` +
  `GET /admin/pipelines/{id}` (`routers/admin_pipelines.py`; admin sees soft-deleted with
  `deleted_at`). `max_pipelines_per_user` → **429** `quota_type: "pipelines"` (non-deleted count;
  check-then-insert, so two racing creates can pass the limit by one). Save-time SSRF on literal
  Scrape/Webhook URLs and owned keys' `base_url` (the trigger still re-checks). Missing block IDs
  never collide with sent ones.
- **Secrets (C.3's ⚠️ for C.4) — my design.** Stored per block as `"secrets": {field: Fernet
  ciphertext}` under `credentials_encryption_key` (the workers' key; cookies JSON-encoded, as jobs),
  with the config field stored `null`. **GET omits secret fields from `config`** and returns
  `"secrets": ["cookies", "proxy_url"]` (names only). **On `PATCH`, a kept block (same id) that leaves
  a secret field out keeps its ciphertext; sending it sets it; `null`/empty clears it** — so GET's
  definition round-trips unchanged. The trigger turns them into `ScrapeInput.credentials`, with the
  platform `default_proxy_url` fallback jobs use. No new table, no migration.
- **C.5.** Optional `version` on the trigger (unknown → 404). Storage admitted with the headroom
  buffer: new setting **`storage_headroom_bytes` = 50 MiB** (my number; `check_user_quota(…,
  headroom_bytes=)` — default 0, so v1 callers are unchanged). The run ceiling is resolved at the
  trigger (`time_budget_seconds` or the blocks' need) and passed in. A start failure now also marks
  the blocks `skipped`.
- **C.6.** All four DB activities lock the run row (`FOR UPDATE`) and `pg_notify('pipeline_status',
  {run_id, pipeline_id, status, block_id, block_status})` in the same transaction — no error text.
  **Precedence:** the run mirror writes only a `running` run and returns the status the run holds;
  the block mirror refuses `running` on a run that is not running or has `cancel_requested_at` and
  returns `cancelled`; a block's `completed`/`failed` is still recorded on a cancelled run (its object
  exists and is charged). `record_storage` reads the owner from the run, records every object
  (screenshots included) idempotently, returns `{used, limit}`. **`on_storage_wall` is folded into
  that return** — the workflow names the wall, so no fourth DB call per block. `check_cancelled`
  built and registered; **its consumer is C.9's backoff** — at block boundaries the running-mirror
  already is the check. DB activities: 10 s attempt, 20 s total; three per block fit
  `BLOCK_OVERHEAD_SECONDS` (asserted at import).
- **C.7.** Timeouts and retry policy per block from the catalog `Timing`; `schedule_to_close` =
  `min(block budget, what is left of the run's)`, and a spent run budget fails the block before it
  starts — no workflow `execution_timeout`, which would end the run without a mirror. The
  `(gave up after N attempts)` suffix on `MAXIMUM_ATTEMPTS_REACHED`. Result = the last *content*
  block's object (effect blocks pass their input ref on — **untested until C.8/C.9 make one
  runnable**; ✅ tested at C.8 with Validate). **Storage wall:** the run completes and its `error` reads `storage_wall: block 'x'
  took storage to U of L bytes; …`. **Cancellation:** every await goes through `_finish()`
  (`asyncio.shield` + re-await), so a Temporal cancel never aborts the running block (R3); it is
  honoured at the next block boundary → run `cancelled`, tail `skipped`, workflow ends Cancelled. A
  cancel during the **last** block completes the run — nothing was left to stop. ⚠️ **For C.9:**
  shielding also defers a cancel during a Webhook block's retry backoff to the block's end — 15d wants
  that wait interruptible; revisit when Webhook lands.
- **Verified:** 52 tests in `test_pipelines.py` + `test_workflows_pipeline.py`; 15 mutations, each
  caught by its own test; **423 API tests**, 51 contract tests. Live on compose through Temporal and
  the Go worker: create, PATCH → v2, version-pinned run, failure mirror + skipped tail, soft delete
  all behaved. The storage half was blocked on 2026-10-07 by a full host disk (MinIO refused
  writes); **re-run 2026-10-11 after the owner freed space — all green:** scrape-only `completed`;
  scrape → LLM (fake key) `failed` at `extract` with the 401, `fetch` `completed`; v1 re-run pinned
  and `completed`; three objects in the ledger with `pipeline_run_block_id`, counter = sum (1,731 B,
  the failed run's scrape object included — charged, C.1); `release_user_objects` freed all three.
- **Still open:** part 3 (historical config shapes); BUG-009 / the `pipeline_status` listener (C.11);
  the cancel endpoint (C.10); `jsonschema` meta-validation (C.3's ⚠️).

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

**Built 2026-10-11 (local) — `d75c7b2`. Not released.** Owner: "do c8". Three calls asked before
building, all answered with the recommendation: (a) **Clean = denylist MVP**, the scored ladder
(`competitor-research.md` §B1) stays a later refactor; (b) **object path `history/{artifact_id}/clean.md`**
— ADR-011's convention, as Scrape and LLM already use in C.7 (the `pipelines/{run}/{block}.md` above
predates it); (c) **`jsonschema` in the worker *and* the API** — closes C.3's ⚠️.
- **Worker** (`llm-worker/`): `worker/content.py` — `ContentActivities.clean` / `.validate`, registered
  as `Clean` / `Validate` on a **second `Worker` in the same process** polling `content` (`temporal_main.py`;
  `content_max_workers` = 4) — so a Clean never queues behind a slow LLM call and prod needs **no new
  Deployment and no infra change**: the existing `llm-worker-temporal` polls both queues on its next
  image. `worker/cleaner.py` — drops `head`, script/style/noscript/template/iframe/object/embed/svg/canvas,
  nav/aside/form/button/dialog, `role=` navigation/banner/contentinfo/complementary/search/dialog/alertdialog,
  `[hidden]`/`[aria-hidden=true]`, data-URI images, and `header`/`footer` **outside** `article`/`main`/`section`
  (an article's own title stays) → `markdownify` (ATX headings) → blank runs collapsed. `worker/rules.py` —
  RFC 6901 pointers; JSON types (`39.0` is an integer; `true` ≠ `1` for `eq`); every failing rule reported
  by index, one message (`rules[1] (contains): 'Add to cart' not found`). Contracts twin in
  `worker/contracts.py` (typed rules); `storage.py` gained `fetch_bytes` / `put`.
- **Errors:** a failing rule → `ValidationFailed`, non-retryable. Unreadable input (`NoSuchKey`), a bad
  schema on an old saved version, a conversion crash → `ContentFailed`, non-retryable. MinIO unreachable/5xx
  → `StorageTransient`, retried by the catalog's 5 s ×2 / 3-attempt policy (the LLM worker's classifier).
  Pages decode with `errors="replace"` — a latin-1 page is cleaned, not failed. CPU work runs in
  `asyncio.to_thread`: the LLM activities sharing the event loop must keep heartbeating.
- **API:** `CONTENT_QUEUE`, `CLEAN_ACTIVITY`/`VALIDATE_ACTIVITY`, `CleanInput`/`CleanOutput`/`ValidateInput`
  (rules travel as the catalog's **by-alias** dump — `"schema"`, not `schema_`; the worker twin parses
  them typed); two `_execute` branches; `SUPPORTED_BLOCK_TYPES` += `clean`, `validate`. Validate returns
  no objects, so the workflow's effect path passes its input ref on and charges nothing.
- **Save-time meta-validation** in `validator.py` (`_json_schema_problems`), **not** in the catalog models —
  those also parse pinned versions at the trigger, so a check there would strand old versions. The block
  still parses, so the type walk and budget run in the same save. A pre-C.8 version with a bad schema fails
  at its Validate block (`invalid schema: …`) instead.
- **Timing:** Clean/Validate keep C.3's 60 s attempt — a 577 B page takes milliseconds; nothing measured
  near the limit. Revisit only if a large page times out.
- **Verified:** **192 LLM-worker tests** (+72 in `tests/test_content.py`), **432 API tests** (+9), **59 contract
  tests** (+8: Clean in/out, every rule kind incl. bool-stays-bool, unknown kind refused, names/queues,
  twins). **15 mutations, each caught by its own test** (header/footer scope, data-URI, JSON equality,
  leading-zero index, pointer unescape, integer-as-float, worker schema check, non-retryable failure,
  stage-named key, lenient decode, by-alias dump, content queue, Validate stores nothing, supported types,
  save-time schema check). ⚠️ **Mutation-testing trap:** a same-length edit restored within the same second
  leaves a valid stale `.pyc` — run mutation passes with `PYTHONDONTWRITEBYTECODE=1`, or every later
  "catch" is the stale bytecode. **Live on compose** through Temporal, the Go worker and the rebuilt
  `llm-worker-temporal`: scrape → clean → validate `completed`, `result_path` = `clean.md` (last content
  block); scrape → clean → validate("Add to cart") → llm `failed` at `guard`, `extract` `skipped` — **no LLM
  call**; scrape(json) → validate(json_schema + type + compare) `completed`; a malformed schema → 422 at
  save. Ledger: five objects, `clean.md` charged with its block, counter = sum; all released after.
- ⚠️ **Known simplification — Clean drops `<title>`** with the rest of `<head>`. On today's `example.com`
  (no `<h1>`) the page's name survives nowhere in `clean.md`. Candidate for the refactor: emit the title as
  `# …` when the content has no `h1`.

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

**Built 2026-10-11 (local) — `5b14291`. Not released.** Owner: "i'll let you do everything until c13; no
need for any walkthrough" — **every call below is mine and reversible**; none was asked.
- **Activity** `api/app/workflows/activities/webhook.py` → `deliver_webhook` on the `workflow` queue (it
  writes the block's status, so it needs the DB). Wire contract copied from `webhook_loop.py`, byte for byte:
  HMAC-SHA256 over the raw body, `X-ScrapeFlow-Signature: sha256=<hex>` always sent (`b""` with no secret),
  success = `< 300`, 10 s POST, no redirects followed; every failed POST is retried, a 4xx included, as on
  the job lane. The repo's first fixture receiver (`tests/test_workflows_webhook.py`, a bare asyncio HTTP
  server) asserts header name, hex, content type and the 299/300 threshold.
- **Payload — the two R6 exceptions, recorded in PRD-016 R6:** `job_id` **omitted**; `pipeline_id` and
  `pipeline_run_id` **added**; `run_id` **kept**, carrying the pipeline run's id, so a receiver keyed on
  `run_id` keeps working (it equals `pipeline_run_id`). `event` = `pipeline.result` — not
  `pipeline.completed`, because a Webhook block need not be the last step. `diff_detected: false`,
  `diff_summary: null` constants. `timestamp` is fixed by the workflow (`workflow.now()`), so every
  attempt signs the same bytes.
- **Secret:** `WebhookConfigV1.secret` — optional, user-supplied, a C.4 secret field (Fernet, credentials
  key; GET lists its name only). Jobs generate theirs server-side and return it once; a pipeline version
  is an immutable stored definition, so a server-generated secret would need a rotate route C.4 does not
  have. New optional field with a default, so `config_schema_version` stays 1 (catalog rule).
- **SSRF** in the activity on every attempt → `WebhookBlocked`, non-retryable, nothing POSTed. The trigger
  also checks the webhook URL now (fails the run up front). An undecryptable secret → `WebhookSecret`,
  non-retryable.
- **Open item 2 — built on the TL reading, not separately confirmed:** the activity mirrors the block
  itself. Attempt > 1 first writes `running` through `mirror_block_status` — which refuses on a cancelled
  or cancel-requested run (15d's boundary re-check) → `RunCancelled`, non-retryable; a retryable failure
  writes `waiting` (`error = "attempt N: HTTP 503"`) then raises — except on the last attempt, where
  nothing follows. No sleeps, no second counter; `activity.info().attempt` is the counter and
  `maximum_attempts` rides in `WebhookInput` (the test server leaves `info().retry_policy` empty). The
  running mirror keeps the first `started_at` (`coalesce`) and clears the last attempt's error.
- **Cancel during the backoff (C.7's ⚠️ revisited):** `_finish(…, interruptible=…)` — for **effect**
  blocks (ADR-009 15a's category rule, not a type check) a workflow cancel is passed on to the activity,
  with `ActivityCancellationType.WAIT_CANCELLATION_COMPLETED`: a retry wait ends at once, an attempt in
  flight still completes (R3). Content blocks stay shielded. A cancelled block mirrors
  `failed` / `cancelled before it finished`, the run `cancelled`.
- **Timing:** the catalog's ladder, unchanged — POST 10 s · `start_to_close` 20 s · `schedule_to_close`
  10 930 s (5 × (60 + 20) + 30 + 300 + 3000 + 7200) · run budget via `run_need_seconds`. ⚠️ The catalog's
  default `schedule_to_start` = 60 s still applies per attempt, though 15c leaves it unset: a workflow
  worker down > 60 s exactly when an attempt is scheduled fails the block. Left as is (same as every
  block); revisit with C.13's deploy rule.
- `SUPPORTED_BLOCK_TYPES` and the trigger's "cannot run yet" check removed — every catalog type runs.
- **Verified:** 455 API tests, 59 contract tests; 15 mutations each caught by its own test (header name,
  threshold, HMAC over the sent bytes, per-attempt timestamp, SSRF retryable, no cancel re-check, `waiting`
  on the last attempt, `started_at` reset, error not cleared, not interruptible, `TRY_CANCEL`, `RunCancelled`
  as failure, trigger SSRF, secret not passed, secret not a secret field) — two first missed because my
  test selection was wrong, fixed and re-run. Retry spacing measured on the test server: 30 / 300 / 3000 /
  7200 s (15b). **Live on compose:** POST to `example.com` (405) → `waiting` "attempt 1: HTTP 405" at t+2 s,
  attempt 2 at t+32 s, cancel row written → attempt 3 at t+332 s refused → run `cancelled`. The success
  path was not sent to a third-party receiver live; the fixture receiver covers it.

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

**Built 2026-10-11 (local) — `66abdeb`. Not released.** Calls mine (delegated).
- `POST /pipelines/{id}/runs/{run_id}/cancel` → 202 `PipelineRunCancelResponse`: the run (still `running`),
  `still_running` (blocks `running`/`waiting`), `upper_bound_seconds`, `llm_billed` (an `llm_extract` block
  has started — a started call may bill), `workflow_notified`. Row lock, `cancel_requested_at` (first
  request kept) + notify, commit — **then** `handle.cancel()`, best-effort. A finished run → 409; another
  user's → 404. The route depends on `get_temporal_or_none`: the engine down must not 503 the cancel.
- **Upper bound:** the active block's declared budget minus its elapsed time; once the engine is told, an
  **effect** block's bound is one attempt (`start_to_close`), since its wait ends at once — live, the first
  version quoted 10 928 s for a webhook that stopped in 0.6 s.
- `pipeline_status` notify moved to `api/app/core/pipeline_status.py` (`notify_run`, shared by the mirrors
  and the API) and gained `cancel_requested` — the SPA shows *cancelling* from it (R3).
- **Verified:** 464 API tests; 10 mutations each caught. End to end on the **real mirrors** in the
  time-skipping env: a row cancel mid-block leaves the run `running`, the next block is refused, run
  `cancelled`, tail `skipped` — the engine never told. **Live:** cancel during a webhook's 30 s wait →
  `cancelled` 0.6 s later (engine cancel); a second cancel → 409.

#### C.11 — Read API + notify channel + WS

**What:** `GET /pipelines/{id}/runs`, `GET …/runs/{run_id}` (per-block status/timing/refs; a
collected output renders as `collected` — 8c), `GET …/runs/{run_id}/result` (ownership-checked
**before** resolving the bare path — §5/§12), admin twins. `JobNotifier`: third listener on
`pipeline_status`, subscriber map, `subscribe_pipeline_run`; WS route mirroring `subscribe_job`
(300 s timeout kept, 4029 kept — 11b). ⚠️ **BUG-009 (`JobNotifier` never reconnects) becomes more
load-bearing here** — a second channel and multi-hour runs. Backlog §4 defers it; **recommend the
owner pull it in at this task** (detect drop, re-register every channel on backoff, log loudly).
**Depends on:** C.1, C.6

**Built 2026-10-11 (local) — `88e8024`. Not released.** Calls mine (delegated); **BUG-009 pulled in** (open
item 7's recommendation).
- **Reads:** `GET /pipelines/{id}/runs` (newest first, `?status=`, summaries without blocks, `version`
  number, `cancel_requested_at`); run detail gains `version`, `cancel_requested_at` and per block
  `output_state` ∈ `none` / `passed_through` / `stored` / `collected` + `collected_at` (§8c — collected is
  never a 404). `GET …/runs/{run_id}/result` = the run's result; **`?block=<id>`** = one step's stored output
  (added — R3's "what did this step return"; 410 for collected, 404 naming why for passed-through / none).
  Ownership is checked before any path resolves (§12). Admin twins under `/admin/pipelines/{id}/runs…`.
- **WS** `/pipelines/{id}/runs/{run_id}/watch`: auth as the job route; **subscribes, then re-reads** the row
  (an update in between is queued, not lost), then `db.close()` — an hours-long watch must not hold a pooled
  connection (the job route still does). Messages carry `status`, `cancel_requested`, `block_id`,
  `block_status`; terminal type = status, `result_url` on completed; 300 s `timeout`; 4001/4004/4029.
- **`JobNotifier`:** third listener; the three subscribe methods share one `_subscribe` (one per-user limit
  across all channels). **BUG-009:** termination listener → `job_notifier_connection_lost` (error) →
  reconnect on 1/2/5/10/30 s, every channel re-registered, `job_notifier_reconnected` (warning); a 30 s
  `SELECT 1` health check terminates a half-open connection. Updates sent while disconnected are lost —
  the client reconnect (C.12) re-reads.
- **Verified:** 484 API tests; 13 mutations, 12 caught on the first pass — the miss showed a redundant
  guard in the termination handler (`stop()` clears the connection first), removed. Notifier tests run
  against real Postgres (`pg_terminate_backend` on its own backend; a patched half-open `execute`).
  **Live:** the API's LISTEN backend killed → reconnected in 1 s; then a WS stream through a run and its
  cancel (block-by-block messages, `cancel_requested` flips, terminal `cancelled`); list/detail/result on
  real MinIO. ⚠️ **Restart the workflow worker after changing `app/`** — it does not hot-reload; a stale one
  sent `cancel_requested: false` here until restarted.

#### C.12 — Frontend

**What:** Pipelines list / editor (JSON-first is acceptable for layer A; Monaco exists) / run
detail with per-block status and the cancel-in-progress state (R3). **11b reconnect on both
lanes:** `JobDetail.tsx` and the new page reconnect on any close without a terminal message, with
backoff, honouring 4029. `tsc -b && vite build` is the only verification; click-through after
release. ⚠️ BUG-018 (token caching, tabled) touches the same fetch path — owner's call whether it
rides here.
**Depends on:** C.11

**Built 2026-10-11 (local) — `c2590af`. Not released.** Calls mine (delegated).
- `src/lib/useLiveSocket.ts` — **11b on both lanes:** reconnect on any close not after a terminal message,
  backoff 1/2/5/10/30 s, 4029 → 30 s with a visible "too many live views", 4004 stops; a **fresh token per
  connect** (a Clerk JWT outlives no reconnect). `JobDetail.tsx` uses it (the old socket died on its first
  close). `LiveBadge` shows live / reconnecting / limited.
- Pages, one component set with `mode` (admin = read-only, polls instead of a socket — the WS is owner-only):
  `Pipelines` (list; admin: include deleted), `PipelineEditor` (JSON in Monaco, R6-shaped template, every
  422 problem listed by block; **lazy** — Monaco stays out of the main chunk), `PipelineDetail` (block chain,
  run form from declared inputs + optional version, runs table, delete), `PipelineRunDetail` (per-step status
  / time / output state, `view` a stored step's output, the result, cancel → *cancelling* with the response's
  bound and billing note; after a reload, from `cancel_requested_at`).
- **BUG-018 not ridden** (owner's earlier call stands), but nothing new copies it: new pages use `useApi()`,
  a fresh `getToken()` per request — BUG-018's proposed fix, scoped to new code. `apiFetch` throws
  `ApiError` (status + parsed `detail`); message format unchanged.
- **Verified:** `tsc -b && vite build` in `node:22-slim` — clean; the editor and viewer are 3 kB / 1 kB lazy
  chunks. **Not clicked through** (Clerk sign-in) — after the release, as the task says.

#### C.13 — Worker Versioning (⚠️ Architect decision + infra task)

**Why:** the deferral table's trigger arrives **in layer A**: a Webhook block parks a run ≈2.6 h,
and a rolling deploy inside that window cannot be drained. Worker Versioning is GA and the stated
default; `patched()` is the alternative. Needs **server-side enablement** (A.2's `dynamicconfig`).
**What:** the decision (Architect), the dynamicconfig entry, the worker's deployment-version
stamp in `worker_main.py` and the three activity workers, and a written deploy rule in the handoff.
**Depends on:** A.2, C.7 — **must be in place before C.14's release** or the first mid-run deploy
breaks a parked run.

**Decided and built 2026-10-12.** The owner asked what the decision was, then what `patched()` means and
whether it covers worker code (it does not need to — activities are not replayed; their *shapes* do matter),
then: **"go with patched and the replay test"**. ~~Worker Versioning, dynamicconfig entry, deployment-version
stamp on four workers~~ — **none of that is built**; record: [ADR-012](../adr/ADR-012-workflow-code-versioning.md)
(Draft, decision the owner's).
- **Replay corpus** `api/tests/replay/histories/` — 9 histories: 4 real compose runs (clean → validate → LLM
  failed, json validate completed, Playwright completed, webhook cancelled mid-backoff) + 5 from the
  mocked harness (webhook delivered after retries, webhook `RunCancelled`, clean/validate/LLM completed, LLM
  retries exhausted, storage wall). Scanned before commit: no credentials; the only ciphertext is a
  one-letter test secret under the dev key. Tools: `tests/replay/export_history.py` (compose; refuses to
  overwrite), `tests/replay/record_harness.py` (writes only missing files); `Harness.run` keeps `history`.
- **Test** `tests/test_workflow_replay.py` — `Replayer` + pydantic converter per history. **Gate:** new
  `replay` job in `build-push.yml`, `build-api` needs it; runs with `--noconftest` and throwaway Fernet keys
  (no services). Verified offline (`--network none`) and in a clean `uv` container as the job runs it.
- **Proven:** an unwrapped new activity call → 9/9 fail; a swapped call order → 9/9 fail; the same call in
  `if workflow.patched(…)` → pass; a required field added to `ScrapeOutput` → fail. Moving an activity to
  another queue → pass (replay-safe — Temporal compares types, not queues).
- **Deploy rule** written into the handoff and `CLAUDE.md` (Key decisions row).

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

**Released 2026-10-12 — `main` `6675f17..1793c60`** (owner ran the ff; the classifier refuses it as a
production deploy). Before it: **`PIPELINES_ENABLED`** (`626192b`) — 16d's switch: off refuses create, edit
and run with 503, reads and cancel stay on (`PIPELINES_ENABLED=false` on the api Deployment, then restart);
the R6 script `api/scripts/r6_gate.py` (`1793c60`, stdlib only, run by the owner against prod).
- **CI:** the new `replay` job ran first (10 passed), then `build-api` and `build-llm-worker`; the other three
  skipped. **No infra change** — the workflow worker already had both Fernet keys and the DB; the LLM Temporal
  worker polls `content` from its new image. No NATS contract changed → no drain.
- **Rollout:** Flux bumped all four Deployments to `1793c60` within ~2 min; `rollout status` green on api,
  workflow-worker, llm-worker-temporal, llm-worker; 0 restarts. API startup ran
  `9a1ebad3fca2 → cff9ec8fedbe → 9db1dcda44f7` and the notifier listens on `job_status`, `batch_status`,
  `pipeline_status`. Pollers (admin-tools pod): workflow + workflow-queue activities on `workflow`, Clean/Validate
  on `content`, LLM on `llm`, all from the new pods. `GET /pipelines` unauthenticated → 401, `/health` 200,
  `/app/` serves the new bundle.
- **R6 — PASS 2026-10-12** (owner-run, on `03f72f8`): every structural check and both signatures pass; repeat run
  = 1 LLM call + 2 stored objects vs the job's 0 + 0. Verdict and detail: PRD-016 R6 → *Gate result*. The run
  carried BUG-022's loop (3 of 6 LLM attempts hit the cap). First found BUG-022 (`4965e9d`).
- **C.12 click-through — done by the owner 2026-10-12**: "at a glance everything looks good"; one gap — a pipeline
  output had no Preview / Download (the job lane's Result card has both). Fixed: one shared `ContentViewer`
  (Source/Preview for markdown and HTML, Copy, Download) on both lanes; `OutputViewer` removed. Build-only verified;
  **released 2026-10-12 (`cf9d2fb..28c881b`, owner; rollout not watched, not yet seen in the browser).**

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

> ⏸ **Not built — owner's answer 2026-10-12: drop for now.** Jobs on the v2 lane have no content-hash
> dedup and no reporting diff (the webhook's `diff_detected`/`diff_summary` stay `null`; every run
> calls the LLM). Recorded as PRD-016 R5's second named exception; change detection is re-homed with
> Monitors (PRD-018). The spec below stands for whenever it is picked up — `JobWorkflow` still writes
> `content_hash`, so the baseline exists.
> **Owner's reasoning (confirmed after an explanation, same day):** change detection comes with Monitors,
> and two ways to do the same thing are not wanted; no jobs run in production today, so nothing is lost now.

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

#### D.1–D.4 — Built *(2026-10-12, one session; local, unreleased)*

> Built under the owner's delegation ("continue building the entire D … without needing my input"),
> after four questions asked up front: **D.2 → drop for now**; **full spec**, not an MVP slice; a v2
> run whose workflow cannot start → **failed + 503**; **commit to `develop`**, infra committed but
> not pushed. Every other call below is mine and reversible.

**What exists.** `develop` `db774d0` (D.1) · `5e61413` (D.3) · `de91a44` (D.4) · `9bd761e` (D.3 guard).

- **D.1** — `job_runs.lane VARCHAR(2) NOT NULL DEFAULT 'v1'` + `chk_job_runs_lane` (migration 4.6
  `56ab6118641d`; the CHECK is hand-written — autogenerate does not see CHECKs). Every `JobRun(...)`
  sets it: `create_job` and `_dispatch_due_jobs` from **`settings.job_lane`** (`JOB_LANE`, default
  `v1`), `create_batch` **always `v1`** (the batch lane moves at E.2, not with this switch).
  `_recover_stale_pending` selects `lane='v1'` only; `result_consumer._handle_result` acks and
  discards a NATS result for a `v2` row (the optional belt-and-braces — built).
- **D.3** — `app/workflows/job.py` (`JobWorkflow`, models, activity names) +
  `app/workflows/activities/job_runs.py` (five DB activities on the `workflow` queue). Flow:
  `job_run_started` (pending → running) → Scrape (`steps.scrape`, engine → `queues.scrape_queue`)
  → `read_llm_settings` → `record_job_objects` → [`job_run_processing` → LLMExtract →
  `record_job_objects`] → `finish_job_run` (row + webhook row in one transaction). Every write
  notifies `job_status` with the positional `{job_id}:{run_id}:{status}` payload. Input = the run id,
  the engine and a `ScrapeInput` from **`dispatch.build_scrape_input`** (beside
  `build_scrape_message`; a test pins that the two carry the same values).
- **D.4** — `dispatch.dispatch_run(job, run, credentials, js, temporal)` routes by **`run.lane`** (not
  by the setting: the lane is fixed at insert, so a flip mid-request cannot split a run). `create_job`
  and the scheduler both call it. `dispatch.cancel_workflows` — best effort, **after** the row write —
  is called by the user cancel, user permanent delete, admin cancel and admin hard delete.
  `workflows/client.lazy_client(state)` split out of `get_temporal` so the scheduler (no request)
  can reach the engine, and so a `v1` request never connects to Temporal at all.

**Calls made (reversible):**
1. **The LLM step is read by an activity after the scrape** (`read_llm_settings`), not resolved into
   the workflow input at dispatch as pipelines do (open item 9). v1 reads `jobs.llm_config` and the
   key when the scrape result arrives, so a key deleted meanwhile fails the run with v1's exact
   error (`LLM key not found or deleted`) and the uncharged scrape is deleted. It also suits F.1: a
   Temporal Schedule's arguments are fixed at creation, and key rotation must not break it. The
   ciphertext lands in history either way (activity results are recorded).
2. **Cancel = row first, engine second, steps never aborted.** The API writes `cancelled` exactly as
   on v1; every mirror refuses to move a run out of a state it did not set (§11a); all steps are
   shielded like the pipeline lane's content blocks, so a Temporal cancel only ends the workflow at
   the next boundary (`CANCELED` in the UI). **Objects a stopped run wrote are deleted, uncharged** —
   v1 left them orphaned for the reconcile sweep. "Delete" always means *unless a ledger row holds
   it* (`_discard_unrecorded`), so a retried activity can never delete a charged object.
3. **v1's job-lane rules kept** (R5): over the storage limit the objects are deleted and the run fails
   `storage_quota_exceeded` (the quota check covers the LLM output too, as on v1), no webhook; an
   accounting failure → `storage_accounting_failed` + objects deleted + `job.failed`; a failed LLM
   leaves the charged scrape in place; `warnings` are stored only on a run with no LLM step (v1 drops
   them on the LLM path — looks like a v1 oversight, kept for parity; a one-line change if wanted).
   Error text is the worker's own message (`ApplicationError.message`), e.g. `blocked:akamai`.
4. **`content_hash` is still written** (from `ScrapeOutput`, no re-read) though nothing reads it on
   v2 — it keeps the baseline for when change detection returns.
5. **A v2 run whose workflow cannot start** (owner's answer): `fail_unstarted_run` — `failed`,
   `could not start workflow: …`, a `job.failed` webhook row, notify. `create_job` returns **503**
   with `{job_id, run_id, error}`; the scheduler logs and continues.
6. **Internal-error net** as on the pipeline lane: an unexpected exception in the body fails the run
   (`internal error: <Type>: …`) instead of leaving a workflow task retrying forever;
   `NondeterminismError` is left to the task.
7. **An engine-only cancel** (Temporal UI, row still `running`) is honoured: the workflow writes
   `cancelled` itself at the next boundary.
8. **The job cancel routes are a conditional UPDATE** (owner, 2026-10-12, reading D.3):
   `dispatch.cancel_active_runs` — `UPDATE job_runs SET status='cancelled' WHERE job_id=… AND status IN
   (active) RETURNING …`, used by the user and admin cancel. Before, both read the active runs and then
   wrote by primary key, so a cancel that waited on a v2 activity's row lock relabelled a run that had
   just completed (webhook row already written) as `cancelled`. Now it leaves it `completed` and says
   "no active run". A test holds the lock to reproduce it; it fails on the old route. Both lanes
   benefit; v1's other race (the result consumer writes without a lock) is left to die with it at G.

**Verified.** 543 API tests (13 lane/switch + 30 workflow/activity, incl. end-to-end on the real
mirrors), 59 contract tests, replay corpus 15 (**5 new JobWorkflow histories**, harness-recorded —
`record_harness.py` `JOB_SCENARIOS`). **33 mutations, every one caught by its named test**
(2 D.1 · 19 D.3 · 11 D.4 · 1 guard). **Live on compose with `JOB_LANE=v2`**
(temporary override file, not `.env`): http markdown job `completed` (result readable, 1 ledger row,
`job.completed` webhook row, workflow `COMPLETED`); playwright job `completed`; LLM job with a fake
Anthropic key → `processing` → `failed` with the provider's 401 text, scrape still charged; cancel
mid-render → row `cancelled`, workflow `CANCELED`, nothing charged, no object left; a due cron job
picked up by the **real scheduler loop** → `v2`, `completed`. Cleanup through the delete paths; test
user removed after its objects were released.

⚠️ **Found:** a mutation run that disables a delete leaves objects in the dev bucket — three were
removed by hand after checking no ledger row held them. Sweep recent objects after a mutation pass
on MinIO-deleting code.

#### D.5 — 🚀 Job cutover release

**Ships:** api image (also the workflow worker's) — migration 4.6 applies on API startup; infra
`9ef396a` (`JOB_LANE=v1` on the api Deployment, committed locally, **not pushed**). The scrape and
LLM `-temporal` workers are unchanged (B.5/B.7/B.9) — no worker image in this release.

**Runbook** (owner-run; ⚠️ the infra commit is behind any Flux image-automation commits that land
meanwhile — `git pull --rebase` in the infra repo before pushing it):
1. **Code lands dormant.** ff `main` to the D commits **and** push infra `9ef396a` (`JOB_LANE=v1` —
   the default, so the order does not matter). Watch: api + workflow-worker rollouts green;
   `alembic current` = `56ab6118641d`; `\d job_runs` shows `lane` with every row `v1`; the workflow
   worker's startup log lists no error (it now registers `JobWorkflow` + five activities). The CI
   `replay` job must pass (15 histories).
2. **Drain gate** (16b) on the job flow. In prod: no job-lane run active —
   `SELECT count(*) FROM job_runs WHERE job_id IS NOT NULL AND status IN ('pending','running','processing')`
   = 0 — and, from the `nats-box` pod, `nats --server nats://scrapeflow-nats:4222 consumer info
   SCRAPEFLOW <durable> --json` shows `num_pending` = 0 and `num_ack_pending` = 0 for **`go-worker`,
   `python-playwright-worker`, `python-llm-worker`, `api-result-consumer`**. (Batch runs share those
   consumers; a batch in flight blocks the gate — wait for it.)
3. **Flip:** `JOB_LANE` → `"v2"` in `clusters/k3s-server/scrapeflow/app/api.yaml`, push. An env
   change on the Deployment = an api rollout (no rebuild). Confirm the new pod reads it:
   `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- /app/.venv/bin/python -c "from app.settings import settings; print(settings.job_lane)"`.
4. **Verify:** submit one **http** and one **playwright** job (`example.com`) **with `curl` and an API key**
   — ⚠️ the SPA cannot create jobs (it lists, views and cancels them; corrected 2026-10-12 during the
   release) — run the `curl` in your own terminal so the key stays out of any transcript. Each run's
   row says `lane = 'v2'`; the SPA shows `running` → `completed` live (WS); the result opens; the
   Temporal UI (port-forward `scrapeflow-temporal-ui`) shows `job-run-<run_id>` **Completed**; NATS
   `go-worker` / `python-playwright-worker` `delivered` counters **did not move**. Optional: a job with
   an LLM step (proves `processing` + the `llm` queue), a cancel mid-render (row `cancelled`,
   workflow `Canceled`), and a webhook job (a `job.completed` row in `webhook_deliveries`, delivered by
   `webhook_loop`).
5. **Scheduled jobs** follow the switch from the next tick: the scheduler creates `v2` runs and
   starts a `JobWorkflow` per run (Temporal Schedules are F.1, not this).

**Rollback:** set `JOB_LANE` back to `"v1"` and push. New runs go to NATS; v2 runs in flight finish on
v2 (they are marked, and recovery never touches them); **no drain needed in this direction**. A v2 run
stranded by a workflow-worker outage stays `pending`/`running` until the worker returns (it holds a
concurrency slot meanwhile) — the same property as the pipeline lane.

**Behaviour changes users can see on v2** (all recorded): no dedup and no diff (owner, PRD-016 R5);
`running` means *scheduled*, not *picked up* — `started_at` is the scheduling time; a run whose
workflow cannot start fails at once (503 on create) instead of waiting for stale-pending recovery.

**Depends on:** D.1, D.3, D.4 (D.2 deferred)

**Released 2026-10-12** (owner ran the ff and both infra pushes; checks run from here, read-only):
- **Step 1:** CI `replay` + `build-api` green (others skipped); api and workflow worker on
  `0b4ac9e`, migration `56ab6118641d` head, all 85 existing `job_runs` rows `v1`, workflow worker
  started clean.
- **Step 2 (drain gate):** 0 active `job_runs` (any lane); `go-worker`, `python-playwright-worker`,
  `python-llm-worker`, `api-result-consumer` all `num_pending` 0 / `num_ack_pending` 0.
- **Step 3:** infra `99ed5c8` — `JOB_LANE: 'v2'` (the owner's YAML linter reformatted the file; parsed,
  it is identical but for that value, and the Flux `$imagepolicy` marker survived). New api pod
  reads `job_lane v2`.
- **Step 4:** http job → run `e2d6928c…` `v2` `completed` in 0.9 s, `scrape.md` 172 B charged;
  playwright job → run `ee41a07f…` `v2` `completed` in 1.4 s, `scrape.html` 2,373 B charged. Both
  `job-run-…` workflows COMPLETED with `job_run_started → Scrape → read_llm_settings →
  record_job_objects → finish_job_run` on the right queues. NATS `delivered` on all four consumers
  unchanged across the test (38 / 130 / 13 / 823).
- **LLM path, same day (owner):** http + LLM → run `fff0b755…` `completed` in 7.1 s; playwright + LLM
  → run `f3d28777…` in 4.3 s (Modal warm). Each holds `scrape.*` + `llm.json` (198 B) in the ledger;
  workflows COMPLETED with `job_run_processing → LLMExtract` on the `llm` queue; extraction
  `{"title": "Example Domain", "summary": …}`. NATS `python-llm-worker` still 13. **Not exercised in
  prod:** a cancel, a scheduled job (both verified on compose).
- **Webhook path, same day (owner):** job + LLM + `webhook_url` → run `859b3923…` `v2` `completed`;
  `finish_job_run` wrote the `job.completed` row in the completing transaction (2 ms apart) and
  `webhook_loop` delivered it on attempt 1, 14 s later (its poll interval); received at webhook.site.
  Payload as v1 but `diff_detected` / `diff_summary` `null` (D.2).

---

## Group E — Batch and crawl cutover

> Cites: ADR-009 §13 (13a–13d), §8d (per-page ceiling), ADR-010 §2, BUG-010 part 1,
> `competitor-research.md` §C.

#### E.0 — Gate

1. ~~**ADR-010 Accepted** (X.2).~~ ✅ Accepted 2026-10-13.
2. **⚠️ Owner decision:** `CLAUDE.md` records two crw mechanisms "owed before the batch-and-crawl
   cutover shapes the task queues" — a **per-eTLD+1 host limiter** and **interactive/batch reserved
   lanes** — while the owner deferred the crw work "until after the Temporal pipeline". Both
   statements are on record. Decide here whether E.1/E.5 build against a single scraper queue per
   engine (today's shape; a 100-URL batch queues every single job behind it) or two lanes. This is
   the one-way-ish door; everything else in §C is activity-port input.
   **Decided 2026-10-13 (owner):**
   - **Bounded fan-out, one queue per engine.** `BatchWorkflow` and `CrawlWorkflow` keep at most
     *k* scrapes in flight and start the next as one finishes. Required whatever else is chosen:
     every scrape has a 60 s `schedule_to_start` (`steps.py`), so an unbounded 100-URL fan-out
     onto 3 Playwright slots times out most of its own items in the queue. **k = 3, a setting.**
   - **The queue timer (`schedule_to_start`) goes from 60 s to 5 min on every scrape.** *k* is per
     batch, so concurrent batches still stack up (3 batches × 3 = 9 scrapes on 3 Playwright slots;
     the last wait ~74 s, a single job behind them ~111 s), and the timer is not retried. 60 s was
     chosen to catch a queue nobody polls, not to cap a busy one; 5 min still catches a dead worker.
     If waits grow, the next step is a global per-engine cap on E.8's lease code.
   - **Reserved interactive/batch lanes: not now.** Moving an activity to another queue is
     replay-safe (ADR-012), so the split can be added later without stranding runs.
   - **Per-host limiter on every scrape** (job, pipeline, batch, crawl): **1 request/s and 2 in
     flight per registrable domain (eTLD+1)**, **fail open** when Redis is unavailable (scrape
     proceeds, loud log). Acquire/release are activities on the `workflow` queue around the scrape;
     a wait is a workflow timer, so it holds no worker slot and does not spend the queue timeout.
     Slots are leases that expire on their own. Built in E.8/E.9. Open: whether robots.txt
     `Crawl-delay` tightens a host's rate.
**Depends on:** D.5

#### E.8 — Registrable-domain helper + per-host limiter

**What:** (1) the eTLD+1 helper ADR-010 §2 specifies — pinned offline public-suffix list with a
lockfile, `include_psl_private_domains=True`, exact-host fallback for IP / single-label hosts,
normalised hosts — built here once, reused by E.3's sitemap scope. (2) `host_limiter`: one atomic
Redis Lua script per call on `host:{etld1}` — expire stale leases, admit if in-flight < 2 **and**
requests in the last 1 s < 1, else return the wait; release removes the lease by id. Lease TTL =
the scrape's longest possible run + margin, so a lost release frees itself. Same sorted-set pattern
as `core/rate_limit.py`. (3) `acquire_host_slot` / `release_host_slot` activities on the `workflow`
queue; a Redis error **fails open** (GO + loud log). (4) A workflow helper: acquire → `workflow.sleep`
on WAIT → scrape → release in `finally`. ⚠️ Check that the prod workflow-worker Deployment has
`REDIS_URL` (compose gets it from `.env`).
**Tests:** two concurrent acquires on one host admit 2, the third waits; rate window; subdomains
share a key, `alice.github.io` ≠ `bob.github.io`; expired lease frees the slot; Redis down → GO.
**Depends on:** E.0

**Built** *(2026-10-15; local, unreleased)*. Nothing calls the helper yet, so no released workflow
body changed and the replay corpus is untouched. The owner decided the four open questions before the
build:
- **Busy host → poll ~1 s + up to 250 ms jitter.** The rate rule's wait is exact (the `last` key's
  remaining PTTL) plus the same jitter.
- **Lease TTL derived from `steps.scrape()`'s own numbers** (`host_lease_ttl`): attempts × (queue
  timer + start-to-close) + retry backoff + 60 s. `scrape()`'s timeouts and retry policy were lifted
  into `_scrape_timeouts` / `SCRAPE_RETRY` for this — same `execute_activity` arguments, replay
  unaffected. Today: http 513 s, playwright (60 s render) 783 s; E.9's 5-min queue timer raises both.
- **Wait cap 10 min, then scrape anyway** (`HOST_WAIT_CAP`) — the limiter delays runs, never fails them.
- **Lease id from the workflow** (`workflow.uuid4()`, one per scrape, reused across re-asks), so a
  retried acquire re-admits its own lease instead of taking a second one; **an acquire activity
  that fails (2 attempts, 5 s each) means GO** — fail open at both layers.

What landed: `app/core/domains.py` (`registrable_domain` — tldextract 5.4.0 pinned by `uv.lock`, no
fetch, no disk cache, private suffixes on; IP / suffix-only / single-label → the exact host; lowercase,
port/userinfo/trailing dot stripped, IDN → punycode), `app/core/host_limiter.py` (one Lua script on
`host:{domain}:leases` + `host:{domain}:last`, Redis `TIME` as the clock; settings
`host_limit_in_flight=2`, `host_limit_interval_ms=1000`), `app/workflows/activities/host_slots.py`
(process-level Redis client, 2 s socket timeouts, `RedisError` → GO / no-op + error log), and
`steps.scrape_with_host_slot` (acquire → durable sleep → scrape → release in `finally`). Compose's
workflow-worker now waits for Redis. **Prod had no `REDIS_URL` on the workflow-worker** — every acquire
would have failed open silently; infra `app/workflow-worker.yaml` gains it (committed locally, not
pushed — must ship before E.9's release).
Verified: 38 new tests (full suite 582 incl. the replay test), 21 mutations all caught. A cancelled
scrape still releases (the `finally` activity runs after cancellation — tested, not assumed). Not
tried live: nothing calls the helper until E.9.

#### E.9 — Per-host limiter on the job and pipeline lanes

**What:** wrap the scrape in `JobWorkflow` and `PipelineWorkflow` with E.8's helper, **inside
`workflow.patched()`** (ADR-012 — adds activity calls to a released workflow body); new replay
histories for both, old ones untouched. **Raise `schedule_to_start` to 5 min** for scrapes
(`steps.py` `SCHEDULE_TO_START`, `pipelines/catalog.py` `SCHEDULE_TO_START_SECONDS`) — pipelines'
run budgets include it, so recheck the catalog budgets compose; the replay test says whether the
changed timeout needs `patched()`. Ships with E.2.
**Depends on:** E.8

**Built** *(2026-10-27; local, unreleased — ships with E.2)*. The owner took all four recommendations
from the walkthrough before the build:
- **A cancel ends the host wait** — nothing has started, so nothing is lost. The scrape itself stays
  shielded (R3); the wait is not. The acquire activity *is* shielded, so a cancel cannot land between
  Redis granting a lease and the workflow learning of it — a lease granted under a cancel is released.
- **A Scrape block's need counts the host-slot wait**: `catalog.HOST_SLOT_SECONDS = 660` (10 min cap +
  the last acquire + the release, 15 s each), on `Timing.host_slot`; `Timing.need` = budget + host slot
  + `BLOCK_OVERHEAD_SECONDS` is what `run_need_seconds` and the validator's breakdown read.
  `pipeline.py` asserts it covers `steps.py`'s numbers. Without it a full host wait came out of later
  blocks' time and the last block failed *"the run's time budget is spent"*.
- **Re-asks back off**: the limiter's own answer first (exact for the rate window), then 2, 4, 8, 16 s,
  capped at 30 s, + ≤250 ms jitter (`workflow.random()`); the last sleep is clamped to what is left of the
  cap. A wait to the cap is **25 acquires** (measured), not ~600 — ~600 × ~8 events per scrape would have
  put a pipeline with ~10 waiting Scrape blocks near Temporal's 51,200-event limit.
- **Only scrapes get the 5-min queue timer**: `steps.SCRAPE_SCHEDULE_TO_START` and
  `catalog.SCRAPE_SCHEDULE_TO_START_SECONDS`; LLM, Clean, Validate and Webhook stay at 60 s.

What landed: E.8's `scrape_with_host_slot` is split into `steps.wait_for_host_slot(url, lease_ttl)` →
`HostSlotRelease | None` and `steps.release_host_slot(held)`, so each workflow puts its own shield around
its own scrape. `JobWorkflow._scrape` and `PipelineWorkflow._scrape_in_host_slot` call them under
`workflow.patched("host-slot")` (`steps.HOST_SLOT_PATCH`); the release runs through each workflow's
`_finish`. The pipeline's lease TTL is the block's `Timing.budget` + 60 s (its `schedule_to_close`), and
its block options are computed *after* the wait, so the scrape's budget is cut to what the run has left.
A wait past the cap now returns no lease (E.8 released one that was never granted). Host-slot activities
gained `schedule_to_close = 15 s`, so the allowance composes. New numbers: http Scrape budget
465 → 1,185 s, need 1,905 s; playwright (60 s render) 735 → 1,455 s, need 2,175 s; leases http 1,233 s,
playwright 1,503 s.
- **The timer change needs no `patched()`** — answered: all 14 pre-E.9 histories replay on the new code,
  which runs every scrape with the 5-min timer (Temporal matches recorded commands by activity type, not
  timeouts). Runs already in flight keep the timer recorded when their activity was scheduled.
- **Saved versions with a declared budget** may now be shorter than their blocks need (budgets are only
  checked at save; versions never change) — such a run is not refused, its blocks' `schedule_to_close`
  is cut. `api/scripts/audit_pipeline_budgets.py` (read-only) lists them; **run it in prod before the
  release** (⚠️ corrected 2026-10-27: the script is not in the pre-release image — run it right after E.2's step 1, before the flip): `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- /app/.venv/bin/python -m
  scripts.audit_pipeline_budgets`. Dev has none. Versions with no declared budget are unaffected
  (computed at trigger).

Verified: 27 new tests (full suite 609, replay included); 20 mutations all caught, each by a named test
(one — the cap clamp — only after a test was added for it). **Tried live on compose**: three pipeline
runs fired at once at `example.com` / `www.example.com` shared one key; scrapes started 1.4 s and 2.4 s
apart, re-asks backed off (1.2 s → 2.1 s), no `host:` keys left in Redis. Corpus +9 histories (7 harness
— granted, waited, failed open, scrape failed, cancel during the acquire, cancel during a timer; 2
compose exports of those live runs); the corpus floor is 23. ⚠️ Histories written from inside the api
container are root-owned and the pre-commit hook cannot open them — `chown 1000:1000` them first.

#### E.1 — `BatchWorkflow`

**What:** fan out one child `JobWorkflow` per item (`job-run-{run_id}` ids, `REJECT_DUPLICATE`,
lane marker on each row), at most *k* children in flight (E.0 — bounded fan-out), fan-in = await all; `batch_status` notify with **absolute totals** (11d —
the existing JSON channel); batch webhook row at completion via the D.3 activity; one concurrency
slot per batch (already the view's rule). `dispatch.py`'s batch builder feeds the child inputs.
**Tests:** N children; never more than *k* in flight; partial failure totals; cancel fans out.
**Depends on:** D.3, E.0, E.9 (children are `JobWorkflow`s, which carry the limiter)

**Built** *(2026-10-27; local, unreleased — owner: "build it and get ready to deploy for E.2", no
walkthrough)*. `develop` `71e43fa`; infra `1e38aaa` (`BATCH_LANE: 'v1'` on the api Deployment).

- **`BATCH_LANE=v1|v2`** (`settings.batch_lane`, default `v1`) — `create_batch` writes it on every run
  of the batch in the insert transaction; nothing reads the setting after that. `JOB_LANE` no longer
  touches batches. `batch_max_in_flight` (default **3**, ≥ 1) is *k*, read at dispatch and carried in
  the workflow input (a setting read in the body would not be deterministic).
- **`BatchWorkflow`** (`app/workflows/batch.py`, id **`batch-{batch_id}`**, `REJECT_DUPLICATE`) — one
  child **`JobWorkflow`** per item (`job-run-{run_id}`, `REJECT_DUPLICATE`, `workflow` queue),
  `wait_condition(in_flight < k)` before each start, fan-in = await all, then **`finish_batch`**. A
  failed child is logged and not re-raised: the child has written its own outcome. Inputs come from
  **`dispatch.build_batch_scrape_input`**, the twin of `build_batch_scrape_message` (a test pins equal
  values).
- **The children are the job lane's own workflow, unchanged** — so they carry E.9's host slot, the
  5-min scrape timer, the LLM step and every mirror rule. What changed is the five **activities**
  (`activities/job_runs.py`): for a run with `job_id` null they take the owner and the LLM step from the
  **batch**, copy each status to the **item** (`processing` → item stays `running`), **recount** the
  batch's `completed`/`failed` from its items and notify **`batch_status`** with absolute totals +
  `item_url`/`item_status` (`app/core/batch_progress.py`, 11d). No per-item webhook. The storage-wall
  path now uses the module's own discard + notify instead of v1's `handle_storage_quota_exceeded` — same
  outcome on a job run (the job suite is unchanged and green), and on a batch run it fails the item and
  leaves closing the batch to `finish_batch`.
- **`finish_batch`** (`activities/batches.py`) — one transaction: any run still active (its child never
  started, or was terminated) is **failed** `internal error: the run's workflow ended without an outcome`
  — or **cancelled** on a cancel; recount; `completed` if every item completed, else `partial_failure`;
  the **`batch.completed`** webhook row (dedup key: the first item's run); notify. A batch already closed
  is returned as it is (a retry); one the API cancelled stays `cancelled`, no webhook.
- **Cancel** (`DELETE /batch/{id}`, both lanes, `dispatch.cancel_batch`): rows first — conditional
  `UPDATE job_runs … WHERE status IN (pending, running, processing)`, items `pending/running` →
  `cancelled`, batch `cancelled`, **a `batch_status` notify** — then, for a v2 batch, a best-effort cancel
  of `batch-{id}`; the workflow passes it to the children in flight (each stops at its next boundary and
  drops what it had not charged) and starts no more. A cancel that loses the race with `finish_batch` →
  409.
- **Locks: run → batch → item** everywhere (mirrors, `finish_batch`, the cancel route). The recount reads
  after the batch lock, so it sees an item another child committed meanwhile.
- **A v2 batch whose workflow cannot start**: every run and item `failed` `could not start workflow: …`,
  batch `partial_failure` + its `batch.completed` row, notify, **503** `{batch_id, error}` — the job
  lane's rule (D.4 call 5).
- `create_batch`'s `queued → running` is now conditional on `queued`, so a fast v2 batch that already
  closed is not reopened.

**Calls made (reversible):** children are real `JobWorkflow`s rather than a batch-only body (one body,
one set of mirror rules, the limiter for free); counters are recounted, never incremented; the parent
closes the batch, not the last mirror (no "last one out" race between children).

**Knock-ons on v1 (deliberate, small):** the cancel route now also cancels a run in its LLM step
(`processing`) — v1 used to leave it running, and its result then overwrote the cancelled item — and it
notifies `batch_status`, so a watcher sees `cancelled` at once instead of at its 300 s timeout.

**Verified.** 24 new tests (`tests/test_workflows_batch.py`: workflow on mocked activities, activities on
the real DB, one end-to-end on the real activities + real limiter, router/lane/cancel), **full suite
636**; **26 mutations, every one caught by its named test** (one — the recount — only after its test was
given unequal counts). Corpus 23 → **26** (`harness_batch_completed`, `…_child_failed`,
`…_cancelled_mid_scrape`; the parent's history is ~10 events per child, so a 500-URL batch is ~5,000 —
far below 51,200). **Tried live on compose** (`BATCH_LANE=v2` via a temporary override file, `.env`
untouched; workflow worker restarted on the new code): 4 × `example.com` + `expired.badssl.com` →
`partial_failure` 4/1, three children started at once and the 4th and 5th as slots freed, one
`batch.completed` row, 4 ledger rows, workflow `COMPLETED`, children 4 `COMPLETED` + 1 `FAILED`; a
3-URL playwright batch cancelled mid-render → batch/runs/items `cancelled`, workflow and all three
children `CANCELED`, nothing charged; no `host:` keys left in Redis. Test user and its objects removed;
compose back on `v1`.

#### E.2 — 🚀 Batch cutover release

**Ships:** the api image (also the workflow worker's) — E.8 (`410f015`), E.9 (`ba18cf3`), E.1
(`71e43fa`) and the docs since `0b4ac9e`. **`api/` only; no Alembic revision**; one new dependency
(`tldextract` 5.4.0). No scrape/LLM worker image. Infra: **`0f71515`** (`REDIS_URL` on the
workflow-worker — without it the limiter fails open on every scrape, silently but for a log line) and
**`1e38aaa`** (`BATCH_LANE: 'v1'`), both committed locally, **not pushed**.

**Runbook** (owner-run; ⚠️ `git pull --rebase` in the infra repo first — Flux's image-automation
commits land on its `main`):
1. **Code lands dormant.** Push infra (`0f71515` + `1e38aaa`) **first** — the env change rolls the
   current images, harmless — then ff `main` to `develop` and push. Watch: CI `replay` (26 histories)
   and `build-api` green; api + workflow-worker rollouts on the new SHA; the workflow worker's startup
   log clean (it now registers `BatchWorkflow` + `finish_batch` + the host-slot activities). Confirm:
   `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- /app/.venv/bin/python -c "from app.settings import settings; print(settings.batch_lane, settings.batch_max_in_flight)"`
   → `v1 3`, and `kubectl -n scrapeflow exec deploy/scrapeflow-workflow-worker -c workflow-worker -- printenv REDIS_URL`.
   **Then the budget audit** — ⚠️ correction to E.9's note: the script is new in E.9, so it is not in the
   image running before this release and cannot be run "before" it; run it now, while the batch lane is
   still `v1`:
   `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- /app/.venv/bin/python -m scripts.audit_pipeline_budgets`.
   Any `SHORT` line = a saved version whose blocks will be cut; fix by saving a new version with a
   larger (or no) budget. Job runs pick up the host slot from this point (`patched("host-slot")`) —
   watch the workflow worker for `Host slot acquire failed` (= Redis unreachable, failing open).
2. **Drain gate** (16b) on the batch flow:
   `SELECT count(*) FROM job_runs WHERE batch_item_id IS NOT NULL AND lane = 'v1' AND status IN ('pending','running','processing')`
   = 0, and from the `nats-box` pod `nats --server nats://scrapeflow-nats:4222 consumer info SCRAPEFLOW
   <durable> --json` → `num_pending` 0 and `num_ack_pending` 0 for `go-worker`,
   `python-playwright-worker`, `python-llm-worker`, `api-result-consumer`. (Jobs are on v2 since D.5,
   so only a v1 batch can hold these.)
3. **Flip:** `BATCH_LANE` → `'v2'` in `clusters/k3s-server/scrapeflow/app/api.yaml`, push (an api
   rollout, no rebuild). Confirm with the step-1 `print` → `v2 3`.
4. **Verify:** one **3-URL batch** with `curl` and an API key, in your own terminal (the SPA cannot
   create batches):
   `curl -s -X POST https://scrapeflow.govindappa.com/batch -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"urls":["https://example.com/?a","https://example.com/?b","https://example.com/?c"]}'`,
   then `GET /batch/{id}` until `completed` (3/0). Each run's row says `lane = 'v2'`; the Temporal UI
   (port-forward `scrapeflow-temporal-ui`) shows `batch-<id>` **Completed** with three `job-run-*`
   children **Completed**; NATS `go-worker` `delivered` did not move; `storage_objects` has 3 rows for
   the runs. The three scrapes run ≤ 2 at a time, ≥ 1 s apart (one host). Optional: a playwright batch
   cancelled mid-render (`DELETE /batch/{id}` → batch `cancelled`, workflow `Canceled`), and one with a
   `webhook_url` (a `batch.completed` row in `webhook_deliveries`).

**Rollback:** `BATCH_LANE` back to `'v1'`, push. New batches go to NATS; v2 batches in flight finish
on v2 (their runs are marked; recovery and the result consumer never touch them); no drain needed in
this direction. Rolling back E.8/E.9 themselves is a code revert (the `host-slot` patch stays in
recorded histories — ADR-012).

**Behaviour changes users can see on v2:** each item's `started_at` is its *scheduling* time (as jobs on
v2); a batch on one site runs at most 2 scrapes at a time, ≥ 1 s apart (per-host limiter), and at most
*k* = 3 items of a batch are in flight; a batch whose workflow cannot start fails at once (503). Both
lanes: cancel reaches an item in its LLM step and notifies watchers.

**Depends on:** E.1

**Released** *(2026-10-27; owner ran the pushes, I watched)*. Infra `0f71515` + `1e38aaa` pushed
first (api + workflow-worker rolled on `0b4ac9e`, `REDIS_URL` and `BATCH_LANE=v1` present); `main`
`0b4ac9e..faa1f83` — CI `replay` 27 passed, `build-api` green; Flux bumped both Deployments to
`faa1f83` (infra `72fbb8d`, which also reflowed the init containers' `command:` lists — content
unchanged); workflow worker started clean; `v1 3`; budget audit: 0 versions declare a budget.
Drain gate: 0 active v1 batch runs (prod's `batches` table was empty), all four NATS consumers
0 pending / 0 ack-pending. Flip infra `538ea8c` → `v2 3`. **Verify:** batch `ee92b25d…` (3 ×
`example.com`) `completed` 3/0 in 8.8 s; every run `lane = 'v2'`; `batch-ee92b25d…` `COMPLETED`
(result `completed`, 32 events, one `finish_batch`) with three `JobWorkflow` children `COMPLETED`;
one `storage_objects` row per run; the children finished 2.1–2.4 s apart (one host, limiter);
`go-worker` delivered 1218 before and after; no host-slot keys left in Redis; no `Host slot acquire
failed` in the worker log. The playwright-cancel and webhook checks (optional) were not run in prod.
⚠️ The server pod has no `temporal`/`tctl` CLI — read workflows with the SDK from the api pod.

#### E.3 — Crawl frontier: table shape + admission activity

**What:** decide `crawl_queue`'s shape at build time (13b — one table for queue + seen-set, or two;
**the dedup mechanism is the index `UNIQUE (crawl_id, url)` + `ON CONFLICT DO NOTHING`**, keep it).
`admit_urls(crawl_id, urls)` activity on the workflow-worker queue — the **one** place seed, extracted
links and sitemap entries converge (13d): `validate_no_ssrf` per URL (**BUG-010 part 1**; rejected
⇒ skipped, crawl continues, never retried); **eTLD+1 scope** for sitemap entries (ADR-010 §2, via
E.8's helper; scope from the **submitted** seed; each drop
logged with its reason and counted per crawl); include/exclude filters; insert with the
index. `crawl_pages` row per admitted URL (P7's unit; 13c).
**Depends on:** E.8

**Built** *(2026-10-27; local, unreleased — owner: "plan and build from e3-e6 … get ready to deploy
at e7"; no walkthrough)*. `c36bf95`, migration 4.7 `532e93f0d629`.
- **13b's open item, decided: one table.** `crawl_queue` stays the queue **and** the seen-set; the
  dedup is still `UNIQUE (crawl_id, url)` + `ON CONFLICT DO NOTHING`. A refused URL is kept as a
  `status = 'dropped'` row with `drop_reason`, so a rediscovered one is neither re-checked nor counted
  twice, and a retried admission is exact (counts come from the rows the insert returns). URLs past
  `max_pages` are **not** stored (unbounded) and not counted — logged as `crawl_frontier_full`.
- **`admit_urls(UrlBatch)`** on the workflow queue — the one gate for seed, links and sitemap entries,
  under the crawl row lock (which also makes the `max_pages` cap exact under parallel pages). Order:
  normalise (http(s) only, fragment dropped) → **scope** (sitemap entries and files only: eTLD+1 of the
  **submitted** seed, E.8's helper) → **path filters** (links *and* sitemap entries now — v1 filtered
  links only; never the seed) → **SSRF** (`validate_no_ssrf_core`, cached per host for the call only).
  Reasons: `invalid`, `out_of_scope`, `filtered`, `ssrf`, `unresolved` (an unresolvable host).
  Counts by reason in **`crawls.dropped`** (JSONB); each drop logged `crawl_url_dropped` with url,
  reason, source. **The seed goes through it too** — the API no longer inserts it; a refused seed
  fails the crawl (`the seed URL was refused: ssrf`).
- **`vet_urls`** — the same gate for sitemap *files* before they are fetched; refusals counted as
  `sitemap_<reason>`, never stored (a file is not a page).
- `crawls.error` (new) — why a crawl failed, or the storage-wall note on a completed one.

#### E.4 — Crawl fetch-side activities

**TL call — home: the Playwright worker's Temporal deployment** (Python, `httpx` already there in
`robots.py`, no DB; target-facing fetches stay on scraper pods — the aiohttp CVE was a response
parser, so "not a browser" is not "not hostile"). **`sitemap.py` ported to `httpx` — a change, not
a copy (13d; BUG-006 is why).** `link_extractor.py` ported intact. Both "port and then actually
run" (13a — neither has ever executed). **Sitemap *files* pass both gates before they are fetched**
(ADR-010 §2 amendment): every `Sitemap:` line and sitemap-index child is SSRF-checked and scoped like
a frontier URL; redirects not followed (or re-checked per hop); indexes expanded as indexes, with a
nesting-depth and file-count bound — v1's `.//loc` crawls index children as pages.
**Tests:** sitemap parse fixtures incl. a sitemap index; robots fetch; the SSRF-bait fixture from
13d's chain is **not fetched** as a sitemap file and, as an entry, is skipped at E.3's admission; an
off-domain `Sitemap:` line and a redirecting sitemap are not followed.
**Depends on:** B.8, E.3

**Built** *(2026-10-27; local, unreleased)*. `playwright-worker/worker/crawl.py`, `link_extractor.py`,
a **second `Worker` in `temporal_main.py` on the new `crawl` queue** (`CRAWL_MAX_ACTIVITIES` 10, not
bounded by browser slots), contracts twinned in `worker/contracts.py` + `contracts/test_activity_contract.py`.
- **`DiscoverSitemaps(seed_url)`** — robots.txt `Sitemap:` lines (relative ones made absolute), else
  `/sitemap.xml`; **returned, not fetched**. robots.txt follows a redirect only on its own host (http →
  https), ≤ 2 hops, 512 KB cap.
- **`FetchSitemap(url, max_entries)`** — one file, `follow_redirects=False`: a redirect comes back as
  `kind="redirect"` + `location` for the workflow to vet (≤ 3 hops). `urlset` vs `sitemapindex` read by
  root tag (v1's `.//loc` read index children as pages); gzip (bounded inflate), plain-text sitemaps;
  **DOCTYPE/ENTITY refused** (entity expansion); 10 MB cap; results capped at `max_entries` and 256 KiB
  of URL text (history payload). Anything unreadable is `unavailable` — the crawl goes on.
- **TL call made here:** the workflow, not the worker, decides what is fetched next. Every sitemap
  URL (robots line, index child, redirect target) passes `vet_urls` first, so **SSRF has one
  implementation** (the API's) and the scraper image gains no second copy to drift. The workflow bounds
  expansion: ≤ 20 files, index depth ≤ 2, entries ≤ `max_pages`.
- **`ExtractLinks(path, base_url)`** — reads the stored page from MinIO; `link_extractor.py` ported
  intact but for **`html.parser` instead of lxml** (not in this image); called with no path filters —
  admission applies and counts them. Capped at 10,000 links / 256 KiB.
- ⚠️ **Coupling:** an **http** crawl's sitemap and link steps run on the Playwright pod. If it is down,
  those activities time out (60 s queue timer) and the crawl completes with what it fetched.
- Tests: 35 new (`tests/test_crawl.py` on an `httpx.MockTransport` that records every request, so
  "not fetched" is asserted; the coordinator's link tests ported). Suite 239.

#### E.5 — `CrawlWorkflow`

**What:** BFS through activities only (never reads Postgres in the body — §6): pop batch →
`JobWorkflow`-shaped scrape per page (or the scrape activity directly — TL call: **direct
activity**, a crawl page is not a job and must not create `job_runs` rows) → `record_storage(crawl_page_id=…)`
(**the per-page insert P7 deferred**) — at most *k* pages in flight, each scrape through E.8's
limiter → **per-page storage ceiling check** (§8d — the buffer does
not cover crawls) → extract links / sitemap on the seed → `admit_urls` → **`continue-as-new`** every
N pages (13b — mandatory) → completion when the frontier is empty or `max_pages` reached → crawl
webhook via **`create_webhook_delivery`** (not the coordinator's bypass — §3 row) → `crawls.status`
terminal. `crawl_status` notify if the SPA needs it (product call — 13c).
**Tests:** time-skipping env; frontier exhaustion; `max_pages` cap; continue-as-new boundary;
ceiling hit mid-crawl completes and charges.
**Depends on:** E.3, E.4, E.8

**Built** *(2026-10-27; local, unreleased)*. `app/workflows/crawl.py`, `activities/crawls.py`,
`core/crawl_progress.py`, `dispatch.start_crawl` / `fail_unstarted_crawl` / `cancel_crawl` /
`cancel_crawl_workflow`; `crawl-{id}`, `REJECT_DUPLICATE`.
- **Loop:** `crawl_started` → admit the seed → `claim_crawl_pages(limit = free slots, in_flight)` →
  one task per page: host slot (E.8, `wait_for_host_slot`) → `Scrape` (**always `html`** — links are
  read from the stored page, v1's rule; the crawl's `output_format` is still not applied) → release →
  `record_crawl_page` → links (depth < `max_depth`) → sitemaps (seed only, unless `ignore_sitemap`) →
  admit. ≤ `CRAWL_MAX_IN_FLIGHT` (3) pages at once — in practice 2 on one site (the limiter).
- **Claim is retry-safe**: it hands back any page claimed but not in the workflow's `in_flight` list
  first (a retried claim whose answer was lost), so no page is stranded `running`. The `crawl_pages`
  row is written at claim — **P7's unit, counted from here** — and starts `running` (= scheduled, as
  jobs on v2). BFS order: `depth`, then `created_at`.
- **Per-page charge** (the insert P7 deferred): `record_object(crawl_page_id=…)` to the crawl's owner.
  **Ceiling per page (§8d):** the page that crosses the limit is kept and charged; the record says
  `storage_full`, the claim refuses at the limit, and the crawl finishes `completed` with
  `error = "storage limit reached: the crawl stopped early"`.
- **Counters recounted** from `crawl_pages` under the crawl lock, never incremented (11d);
  **`crawl_status` notify** (JSON, absolute totals + the page) on every write — 13c's product call taken
  so E.10 has a live view; `JobNotifier` gained the channel, `/crawls/{id}/watch` the route.
- **Cancel:** row first (`dispatch.cancel_crawl` locks the crawl, skips the pending frontier), engine
  second (best effort). A host wait ends; a scrape in flight finishes and its record drops the object
  uncharged (`cancelled` page, `skipped` queue row); `finish_crawl` leaves an API-cancelled crawl
  cancelled and writes no webhook. A crawl cancelled by row only stops at its next claim/record.
- **`finish_crawl`:** the crawl → its outcome (unless already terminal), leftover frontier `skipped`,
  a page still `running` → `cancelled`/`failed` (`NO_OUTCOME`), webhook **`crawl.completed` /
  `crawl.failed`** via the new `create_crawl_webhook_delivery` (not the coordinator's bypass; written
  once, on the move to terminal). ⚠️ Crawls have no `webhook_secret` column, so `webhook_loop` signs
  them with an empty key — as v1 would have; not fixed here.
- **continue-as-new** every `CRAWL_PAGES_PER_RUN` (100) pages or when Temporal suggests it, after the
  in-flight pages drain; the seed is admitted once. Measured: **~34 events a page** (13b's budget was
  ~5/page *without* continue-as-new; with it, 100 pages ≈ 4,000 events).
- An internal error / accounting failure fails the crawl (`storage_accounting_failed`) instead of
  failing the workflow task forever. A crawl whose workflow cannot start is failed at once (503 on
  `POST /crawls`, `crawl.failed` row).
- **Tests:** 17 workflow (time-skipping, fake frontier: exhaustion, `max_depth`, k bound, claim
  hand-back, continue-as-new, failed scrape, link failure, refused seed, accounting failure, storage
  wall, cancel mid-scrape / by row only / during the host wait, sitemap expansion through the gate,
  bounds, history size) + 25 activity tests on the real DB (every admission rule incl. `github.io`
  siblings and the real check on `169.254.169.254`, quota views see the pages, ledger, wall,
  idempotency) + router tests updated. Replay corpus **26 → 31** (`harness_crawl_*`, five).
  Full API suite **695**. ⚠️ **No mutation pass this time** (E.8/E.9/E.1 had one) — not run.

#### E.6 — Crawl compensating gate

**Why:** 13a — no v1 crawl ever completed, so §9's diff-against-v1 pre-gate **cannot exist**.
**What:** a controlled fixture site (a small static site in `contracts/fixtures/crawl-site/` served
from a pod or compose service) with a known link graph, a sitemap, a robots.txt, an SSRF-bait
sitemap entry, a cross-domain sitemap entry and an out-of-scope subdomain. Expected page set is
written down; the crawl must produce exactly it, charge exactly its bytes, skip the bait.
**Depends on:** E.5

**Built + PASSED on compose** *(2026-10-27)*. `aefc1d4`. `contracts/fixtures/crawl-site/` (README has
the case table), `expected.json`, `contracts/test_crawl_fixture.py` (keeps the expected file true to
the site), `api/scripts/crawl_gate.py`.
- **How the fixture is reachable without weakening SSRF:** a compose network `crawl-fixture` on
  **`11.222.33.0/24`** — a public range — so `www.`/`docs.crawlfixture.com` look like outside sites to
  the unchanged check; on the default network `internal.crawlfixture.com` resolves to the container's
  private address (the in-scope SSRF bait). ⚠️ An `SSRF_ALLOWED_HOSTS` setting was tried first and
  **refused by the permission classifier as a security weakening** — rightly; it is not in the code.
- **The gate:** one crawl through `POST /crawls` as a throwaway user; must match the page set (URL,
  depth, status, error), counters, `dropped` (`out_of_scope` 2, `ssrf` 1, `sitemap_ssrf` 1,
  `sitemap_out_of_scope` 1) and charge exactly the bytes served (http) or stored (playwright).
  **http: PASSED, 1,098 bytes, 11.5–22 s; playwright: PASSED, 1,122 bytes, 14.7 s.** nginx log: no
  request for `bait-sitemap.xml` or `/secret.html`; every page once; robots.txt 8× (7 pages + discovery).
- Also run live: a playwright crawl cancelled after 4 s → workflow `CANCELED`, in-flight pages
  `cancelled`, counter = ledger (339 B), no `host:` keys left.
- **Not built:** E.6's "served from a pod" — there is no prod gate; the prod check at E.7 is a crawl of
  a real public site.

#### E.7 — 🚀 Crawl cutover release + `coordinator/` deletion

**Runbook:** `POST /crawls` routes to `CrawlWorkflow` (no drain gate needed — the crawl flow has
never completed on v1; **cancel any `running` v1 crawl rows first**, `audit_crawl_quota.py` lists
them — none in prod as of 2026-09-19). Delete `coordinator/` (service, image, CI job, compose,
infra Deployment, `coordinator/messages.py`'s deliberate duplicate). BUG-008 and BUG-012 dissolve
here — close them as dissolved, not fixed.
**Depends on:** E.6

**Prepared** *(2026-10-27; nothing pushed)*. `coordinator/` deleted with its CI job and compose service
(`56cff93`); the NATS contract test's crawl fixture is now built from the API's own `ScrapeMessage`
(identical bytes; the Go fixture unchanged) and goes with NATS at H.2. Infra **`bf591bc`** (local, 1
ahead of `origin/main` at the fetch): removes `app/coordinator.yaml`, its kustomization entry, and the
`scrapeflow-coordinator` ImageRepository + ImagePolicy — Flux prunes (`prune: true`). **No lane
switch:** a crawl has no v1 to return to (13a), so `POST /crawls` always starts `CrawlWorkflow`.

**Ships:** api image (also the workflow worker) and **playwright-worker image** (both its Deployments
roll; the NATS one is unaffected by the crawl code) — `c36bf95..` (E.3–E.7, E.10, BUG-025) + docs.
**One Alembic revision** (4.7 `532e93f0d629`, three `ADD COLUMN`s; the quota views read none of them).
No new env vars (defaults: `CRAWL_MAX_IN_FLIGHT` 3, `CRAWL_PAGES_PER_RUN` 100, `CRAWL_MAX_ACTIVITIES` 10).

**Runbook** (owner-run; ⚠️ `git pull --rebase` in the infra repo first):
0. **Before:** `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- /app/.venv/bin/python
   scripts/audit_crawl_quota.py` — any v1 crawl `queued`/`running` would hold a slot forever (no
   workflow will ever pick it up). None in prod as of 2026-09-19; if there are, `UPDATE crawls SET
   status='cancelled' WHERE status IN ('queued','running')` (all v1 by construction).
1. **Infra first — the coordinator goes before the new API arrives.** Push `bf591bc`; confirm
   `kubectl -n scrapeflow get deploy scrapeflow-coordinator` → NotFound. ⚠️ Order matters: a running
   coordinator polls `crawl_queue` for `pending` rows — which `admit_urls` now writes — and would
   dispatch them over NATS beside the workflow.
2. **ff `main` to `develop`, push.** Watch CI `replay` (31 histories), `build-api`, `build-playwright-worker`;
   Flux bumps api, workflow-worker, playwright-worker, playwright-worker-temporal. Check: api log
   `Running upgrade 56ab6118641d -> 532e93f0d629`; workflow-worker started clean (registers
   `CrawlWorkflow` + six crawl activities); `kubectl -n scrapeflow logs deploy/scrapeflow-playwright-worker-temporal | grep temporal_worker_started`
   shows `crawl_queue=crawl`.
3. **Verify** (your terminal, an API key): `POST /crawls` `{"seed_url":"https://quotes.toscrape.com/","max_pages":5,"max_depth":1}`
   → poll `GET /crawls/{id}` to `completed`: 5 pages `completed` (the cap), `total_queued` 5,
   `dropped` likely `{}`; `GET /crawls/{id}/pages/{page}/result` returns HTML; `storage_objects` rows
   carry `crawl_page_id`; the Temporal UI shows `crawl-{id}` Completed. Then `DELETE
   /crawls/{id}?permanent=true` → 204 and the counter back. Optional: `https://example.com/` (1 page,
   the iana link is off-origin; `/sitemap.xml` 404 → unavailable) and a cancel mid-crawl.
4. **Close** BUG-008 and BUG-012 as **dissolved**; BUG-010 part 1 as **fixed** (the worker-side check,
   part 2, stays filed). E.10 click-through on prod (batches + crawls pages, both modes).

**Rollback:** a code revert (ff `main` to a revert commit) + re-adding the infra manifests. There is
nothing working to roll back *to* — v1 crawls never completed (13a) — so prefer a forward fix.

**Released** *(2026-10-27; owner: "lets do e7; skip audit as crawl never ran before as it was broken")*.
Step 0 skipped by the owner (no crawl ever ran in prod). Infra `bf591bc` pushed first → Flux pruned
`scrapeflow-coordinator` (Deployment, pods, ImageRepository, ImagePolicy) within ~10 s. `develop`
pushed; the owner ran the `main` ff `faa1f83..261f931`. CI: `replay` 32 passed (31 histories),
`build-api` and `build-playwright-worker` green. Flux bumped api, workflow-worker, playwright-worker and
playwright-worker-temporal to `261f931`; all four rollouts complete; api log `Running upgrade
56ab6118641d -> 532e93f0d629`; workflow worker started clean; the new Temporal Playwright pod logged
`temporal_worker_started … crawl_queue=crawl`; the NATS Playwright pod resubscribed (one `fetch_error`
came from the old pod during its drain). **Verify (owner):** a crawl of a public site worked end to end,
and the SPA's Crawls and Batches pages showed the results — E.10's click-through, done in prod.
⚠️ My read-only check of the crawl's rows (psql on the Postgres pod) was **refused by the permission
classifier as a production read**, so page counts, ledger bytes and the workflow history were not
recorded here. **Group E closed by the owner.**

#### E.10 — Batches + crawls in the SPA

**Why:** neither lane is visible in the SPA. A batch item's run has `job_id` NULL, so the `Jobs` pages
never list it; there are no batch or crawl pages at all (PRD-011 scoped the SPA to the `/admin/*`
routes of the day; Phase 3's `prod-todo.md` #2 assumed "job/batch detail views" that were never
built). Not migration work — filed by the owner 2026-10-27 to land **after E.7**, so both pages are
built once, against lanes that finish.
**What:**
- **API reads:** `GET /batch` and `GET /crawls` (owner's, paged, newest first) and `GET /admin/batches`,
  `GET /admin/crawls` (all users, `user_email` joined, as `/admin/jobs`). Today only by-id reads exist.
  An item's / page's **result content** read (owner-scoped, plus the admin twin) through
  `load_completed_result()`'s pattern — `GET /batch/{id}/items` returns `result_path` only.
- **SPA:** Batches and Crawls list + detail pages under both `/app/dashboard/` and `/app/admin/`, one
  component set switched by `mode` (the `Jobs`/`JobDetail` rule). Detail = counters + items/pages
  table; a row opens its result in `ResultViewer`/`ContentViewer`. Cancel from the detail page (user
  mode: `DELETE /batch/{id}`, `DELETE /crawls/{id}`).
- **Live status:** batch detail on `GET /batch/{id}/watch` through `useLiveSocket` (C.12's reconnecting
  socket); the `batch_status` payload already carries absolute totals + `item_url`/`item_status` (E.1).
  Crawls have no watch endpoint — add one only if E.5 emits a `crawl_status` notify (13c's product call).
- No create forms (the SPA creates no jobs either; creation stays API/MCP).
**Tests:** API read tests incl. cross-tenant 404 and admin-only 403; SPA build; click-through on compose
(a batch with a failed item, a cancelled batch, a finished crawl).
**Depends on:** E.2, E.7

**Built** *(2026-10-27; local, unreleased — the owner asked for it before the E.7 release)*. `6c1b847`
(+ the crawl reads in `c36bf95`).
- **API:** `GET /batch`, `GET /crawls` (owner, newest first, paged); `GET /batch/{id}/items/{item}/result`,
  `GET /crawls/{id}/pages/{page}/result` through a shared `routers/jobs.read_stored_result` (format from
  the object's extension — an item with an LLM step stored `llm.json`); admin twins in
  `routers/admin_lanes.py` (`/admin/batches[/{id}[/items[/{item}/result]]]`, same for crawls; `user_id`
  and `status` filters; `user_email` joined). `/crawls/{id}/watch` (E.5's `crawl_status`).
- **SPA:** `Batches`, `BatchDetail`, `Crawls`, `CrawlDetail` under `/app/dashboard/` and `/app/admin/`,
  switched by `mode`; nav entries in both shells; detail = counters + items/pages table, `view` opens the
  output in `ContentViewer` (`components/StoredResult.tsx`); the crawl page shows the refusals by reason
  and the page-status filter; cancel in user mode; live on the owner's view via `useLiveSocket`, admin
  polls 5 s. New pages use `useApi()` (a fresh token per request), not the `['token']` query — BUG-018
  is not ridden on them.
- **Tests:** 11 API (`tests/test_lane_reads.py`: owner scope, cross-tenant 404 incl. another batch's item
  through mine, admin 403 on all eight routes, admin lists with email/filters); SPA `npm run build` (tsc)
  clean. ⚠️ **Click-through on compose not done** — no browser in this session (Clerk login).

---

## Group F — Schedule and webhook cutover

> Cites: ADR-009 §7 mechanism 3, 16c, §15 (15e), ADR-010 §1 + rider, `phase4-backlog.md` gotcha 6.

#### F.0 — Gate: overlap policy (⚠️ Architect)

✅ **Done 2026-10-27 — ADR-013 Accepted (owner).** Overlap decided 2026-10-13: **ADR-013**: `SKIP`,
`pause_on_failure=False` and the catch-up window set explicitly on every Schedule, plus the
**per-user cap on active scheduled jobs** (429 `quota_type: "scheduled_jobs"` on create, PATCH
`schedule_cron` and resume, under BUG-023's lock). §5, taken as recommended (owner, 2026-10-27):
`default_quota_scheduled_jobs = 20` (per-user override), only `schedule_status = 'active'` counts,
`catchup_window = 365 days` pinned.
**Depends on:** X.3

#### F.1 — Temporal Schedule per recurring job + quota admission

**What:** `ScheduledJobWorkflow` (or `JobWorkflow` with a first step): **fires unconditionally**;
first step consults the views and **acts per meter** (ADR-010 §1, amended 2026-10-13: concurrency
parks on a durable timer and re-checks; **monthly skips the firing** — the workflow ends, the skip is
recorded where the user sees it and ⚠️ **not as a `job_runs` row**, which `quota_run_units` would
count; storage fails now naming the remedy). The admission check and the run insert go through
BUG-023's per-user lock — woken parked runs re-check in parallel. The API enforces the per-user
**active scheduled jobs** cap (ADR-013) on create, PATCH `schedule_cron` and resume, under the same lock.
Every Schedule is created with ADR-013's three policies set explicitly. **Verify on compose:** after a
`temporal` outage across several ticks, a recovered Schedule runs once, not once per missed tick (ADR-013 §5);
and a Schedule resumed after a pause does not replay the ticks it missed while paused. Schedule created/paused/deleted
by the API on `schedule_cron` / `schedule_status` writes for **v2-owned** schedules; overlap policy
from F.0. `next_run_at` for v2 comes from the Schedule (`describe`), not the row.
**Tests:** time-skipping: parked run releases when the meter clears; monthly breach skips without
writing a counted row; storage breach fails; N parked runs waking together admit only up to the limit.
**Depends on:** D.3, F.0, BUG-023 (✅ fixed 2026-10-27, `2983a47` — the activity takes `quota.lock_user`, then the checks, then inserts, in one transaction)

**Built** *(2026-10-27; owner: "pls plan and build f1-f7; wait for me to come back to do the release" —
built under that delegation, no walkthrough; `69fcd7b`, local, unreleased)*.
- **Shape:** the Schedule (`job-schedule-{job_id}`, `core/schedules.py`) starts
  `ScheduledJobWorkflow` (`scheduled-job-{job_id}-<tick>`, `workflows/scheduled_job.py`) per tick. Its
  one activity `admit_scheduled_run` (`activities/schedules.py`) takes `quota.lock_user`, then: job gone
  / paused / not v2 → `stopped`; **monthly** → `skipped` (`jobs.schedule_skipped_at` +
  `schedule_skip_reason`, no row); **storage** → a `failed` `job_runs` row naming the remedy + `job.failed`
  webhook (a run, as v1's same breach was — it costs a monthly unit); **concurrency** → `waiting`
  (`jobs.schedule_waiting_since`), re-asked on a 15 s → 5 min doubling timer, skipped
  (`concurrent_jobs`) past **24 h**; else a `pending` v2 run. The run is a **child `JobWorkflow`**
  (`job-run-{run_id}`, `ParentClosePolicy.ABANDON`), so the API's cancel works unchanged and the tick
  stays open while it runs — that is what makes `SKIP` drop an overrunning tick.
- **Calls made (reversible):** the run id is minted by the workflow (`workflow.uuid4()`), so a retried
  admission finds its own row. The jobs row is the truth for *armed*; the Schedule is only the clock:
  a write that disarms commits and tells the engine best effort (admission stops any tick the Schedule
  still fires); a write that arms is undone with a 503 if the engine refuses. A job created with a cron
  whose Schedule cannot be put is created **paused**, its first run failed, 503 with the job id.
  `next_run_at` for v2 is **computed from the cron** on read (Temporal's spec in UTC), and the column
  stays NULL for v2 — v1's loop selected on it. ADR-013's cap (`user_quotas.scheduled_jobs_limit`,
  default 20) is checked on create, a cron PATCH that arms, and resume, under the user lock.
  `schedule_cron: null` removes a schedule; `schedule_status` is now `active | paused` (422 otherwise).
- **Found:** **BUG-026** — `create_job` never armed a schedule (no status, no `next_run_at`); fixed here.
  Migration 4.8 backfills `schedule_lane='v1'` only where a status is set — the 176 cron-only dev rows
  stay unarmed on purpose.
- **Verified:** 39 new tests (33 DB/API incl. a widened-race "N waking ticks admit only up to the
  limit" and a held-lock wiring test; 6 time-skipping); corpus +7 (5 harness, 2 compose). **On compose**
  (1-minute Schedule on a test job, real workers): ticks run end to end; **`temporal` stopped 5 min
  across 5 ticks → one late run, 4 `skipped_overlap`** (ADR-013 §5's collapse, now tested); **paused
  3 min → no replay on resume**, next tick normal; concurrency 0 → the tick waited, later ticks dropped,
  no row; monthly 0 → the waiting tick ended `skipped` (`monthly_runs`), no row. Test job, objects and
  user removed after.

#### F.2 — `schedule_status` interlock guard + meter scoping

**What (16c, both recorded obligations of this step):** (1) a job whose schedule is on v2 must
not be re-armed on v1 by `PATCH {"schedule_status": "active"}` — a `schedule_lane` column (or the
D.1 marker's sibling on `jobs`) that the v1 scheduler filters on and the PATCH handler respects;
(2) `active_recurring_jobs` renamed/scoped to *v1 recurring jobs* or made lane-aware — **naming,
not features**. `UsageStats.tsx` label updated.
**Depends on:** F.1

**Built** *(2026-10-27, `e2cadca`; the guard itself landed in F.1)*. `jobs.schedule_lane` is the
"which engine owns this schedule" half that `schedule_status` was overloaded with (16c): v1's loop
selected `schedule_lane = 'v1'` only, and a resume keeps the row's lane — so a v2 schedule could not be
re-armed on v1. The schedule stays `active` on its row when it moves to Temporal, so
`active_recurring_jobs` is right across lanes without renaming; `active_recurring_jobs_by_lane` is
added (after F.6 a `v1` count is a stranded schedule) and `next_scheduled_run_at` reads v2's cron. The
Usage tile shows "N (M on v1)" when any remain. 6 tests (rewritten at F.6 for the post-loop rules).

#### F.3 — Per-job migration runbook

**Order (mechanism 3, counter-intuitive):** pause in v1 → confirm no v1 dispatch in flight →
create the Schedule. **Rollback is the mirror:** pause the Schedule → confirm no v2 execution →
set `schedule_status` back. Script it (`api/scripts/migrate_schedule.py`, dry-run default). No
scheduled jobs exist in prod today — the script is still the artefact.
**Depends on:** F.2

**Built** *(2026-10-27, `a46109d`)*. `api/scripts/migrate_schedule.py`, dry-run default:
`--all | --job …` moves v1 schedules forward in mechanism 3's order (pause → no v1 run in flight, else
left paused and reported → put the Schedule → hand the row to v2); `--reconcile` puts every v2 row's
Schedule and deletes orphan `job-schedule-*` Schedules (the repair for a put the API could not make).
The `--rollback` mirror was built and **removed at F.6** — with v1's loop gone there is nothing to roll
back to. 8 tests (ordering asserted: the row is already paused when Temporal is told). No scheduled
jobs in prod: after the release `--all` should report 0.

#### F.4 — Job-lane delivery as an activity; `webhook_deliveries` fate

**What:** `JobWorkflow`/`BatchWorkflow`/`CrawlWorkflow` call the C.9 activity after completion
(job-lane semantics: delivery failure never fails the run; `job.failed` still fires on failure).
**⚠️ Owner decision (deferral table):** does `webhook_deliveries` survive as a v1-only audit
mirror (the activity writes the row for the admin endpoints) or retire with the loop? Decide here.
**Depends on:** C.9, D.3

**Built** *(2026-10-27, `35ada75`)*. **`webhook_deliveries` survives — not as an audit mirror but as
the delivery record** (TL call; ✅ **owner confirmed 2026-10-28: "keep webhook_del"** — this is the deferral
table's decision in ADR-009, made at the step that retires the job lane's loop): every writer of a row (job, batch and crawl
finish activities, the storage-full scheduled tick, the API's unstarted-run paths, an admin retry) then
starts `WebhookDeliveryWorkflow` (`webhook-delivery-{row_id}`, `USE_EXISTING` + `ALLOW_DUPLICATE`)
best effort, and a system **`webhook-sweep` Schedule** (every 5 min, `SKIP`, created by the workflow
worker at startup) starts one for any pending row 60 s past its attempt time — a lost start, an
unstarted-run row written while Temporal was down, an admin retry, a v1 result-consumer row. The
activity `attempt_webhook_delivery` is `webhook_loop._attempt_delivery` verbatim (payload bytes, HMAC
with the job's/batch's secret or `b""`, header, 10 s, `< 300`, SSRF per attempt not counted, backoff
`[0, 30, 300, 1800, 7200]`, cap `WEBHOOK_MAX_ATTEMPTS`); the workflow sleeps the row's backoff on a
durable timer. Why the row stays: the admin list/retry endpoints and the three meters read it, and a
row is what lets the sweep guarantee delivery without a polling loop in the API. **The API no longer
starts `webhook_loop`** from this commit. Delivery stays separate from the run's workflow, so a
retrying webhook never holds a Schedule's tick open. 17 tests; corpus +2 (compose). **On compose:** a
v2 job's `job.completed` → 503, retried 30 s later → 200, row `delivered`/2, signature verified against
the secret; an orphan pending row delivered by a triggered sweep.

#### F.5 — Three webhook meters (15e)

`webhook_deliveries_pending` / `_exhausted` / `_success_rate_7d` renamed or scoped to the job lane;
`UsageStats.tsx` updated. The two admin endpoints stay job-lane-only by design.
**Depends on:** F.4

**Built** *(2026-10-27, `539114e`)*. Renamed to `job_lane_webhooks_pending`,
`job_lane_webhooks_exhausted`, `job_lane_webhook_success_rate_7d` (jobs, batches, crawls — a pipeline
delivers through its Webhook block, 15e); Usage label "Webhooks pending (jobs, batches, crawls)".
**BUG-028** fixed with it: the per-user scope went through `job_id` only. 1 test.

#### F.6 — 🚀 Release; delete three loops

**Drain gate at deletion** for each: `scheduler.py` (no `active` v1 schedules; no v1 `pending`
rows for `_recover_stale_pending` to find), `webhook_loop.py` (no `pending` deliveries),
`advisory.py` (no NATS publishes remain — true after E.7). Remove them from `main.py`'s lifespan.
**Depends on:** F.1–F.5

**Built** *(2026-10-27, `981e0fc`; not released)*. Deleted `core/scheduler.py` (cron dispatch +
`_recover_stale_pending`), `core/webhook_loop.py`, `core/advisory.py` and their tests; the lifespan
starts the result consumer only. `SCHEDULE_LANE`, `STALE_PENDING_THRESHOLD_MINUTES` and the advisory
subject constant are gone. Every armed schedule is a Temporal Schedule: a v1 row left **paused** moves
to v2 when resumed (or by `migrate_schedule --all`); a v1 row left **active** fires nothing and shows in
`active_recurring_jobs_by_lane`. ⚠️ **After this release the v1 lanes are not a working rollback**:
`JOB_LANE=v1` / `BATCH_LANE=v1` runs lose stale-pending recovery and the MaxDeliver advisory (their
webhooks still go, via the sweep). G deletes them. Suite **757** at this commit (+ F.7: 766).

**Mutation pass (2026-10-27, before release, owner's ask): 55/55 caught** across F.1–F.7 (admission per meter,
the lock, the tick workflow, the Schedule's policies, arming/disarming and the cap, the interlock, the
migrate script, the webhook attempt/backoff/sweep/starts, the meters, the reads). Two real gaps found and
closed by tests: the child's `ParentClosePolicy.ABANDON`, and a resume one below the cap (the job's own row
autoflushes as `active` before the count). Suite **767**.

**Ships (one release, F.1–F.7):** api image only (also the workflow worker) — `69fcd7b..56a06e6` +
docs. **One Alembic revision** (4.8 `437442ce59b2`: five `ADD COLUMN`s, one CHECK, the v1 backfill;
the quota views read none of them). **No infra change, no new env vars** (`DEFAULT_QUOTA_SCHEDULED_JOBS`
defaults to 20). CI `replay`: 40 histories.

**Runbook** (owner-run):
0. **Before (read-only, prod):** `SELECT schedule_status, count(*) FROM jobs WHERE schedule_status IS
   NOT NULL GROUP BY 1` → expect none (prod has never armed a schedule; any would be tagged `v1` by
   4.8 and moved by step 3). ⚠️ No `schedule_lane` here — 4.8 adds it in this release. `SELECT lane, status, count(*) FROM job_runs WHERE status IN
   ('pending','running','processing') GROUP BY 1, 2` → expect no `v1` rows (recovery is going).
   `SELECT status, count(*) FROM webhook_deliveries GROUP BY 1` → note any `pending` (the sweep adopts
   them within 5 min of the new worker).
1. **ff `main` to `develop`, push.** Watch CI `replay` (40) + `build-api`; Flux bumps api and
   workflow-worker. Check: api log `Running upgrade 532e93f0d629 -> 437442ce59b2` and no
   scheduler/webhook/advisory start lines; workflow-worker started clean. ⚠️ In the minutes both images
   overlap, the old API's `webhook_loop` and the new worker can both POST one row — at-least-once, as
   always; webhook receivers must already tolerate a repeat.
2. **Verify the sweep exists** (api pod, SDK): `get_schedule_handle("webhook-sweep").describe()` →
   interval 5 min, overlap `SKIP`.
3. `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- /app/.venv/bin/python -m
   scripts.migrate_schedule --all` → expect "0 migrated"; same with `--reconcile` → "0 put, 0 orphan(s)".
4. **Verify a schedule** (your terminal, an API key): `POST /jobs`
   `{"url":"https://example.com/","schedule_cron":"*/5 * * * *","webhook_url":"<a receiver>"}` → 201;
   `GET /jobs/{id}` shows `schedule_status: active` and a `next_run_at`; `GET /users/me/scheduled-jobs`
   → `{"active":1,"limit":20}`; after the next 5-minute mark a second run `completed` (lane `v2`) and
   its webhook delivered (`GET /admin/webhooks/deliveries`); `PATCH {"schedule_status":"paused"}` →
   `next_run_at` null, no run at the next mark; `DELETE /jobs/{id}?permanent=true` → 204, the Schedule
   gone (`describe` → not found). Then click through F.7 (both modes): Jobs → "Recurring only", the
   Schedule column, JobDetail's panel, pause / resume, the admin quota form's new field.

**Rollback:** an image revert to `261f931`. Delete the Schedules first or every tick fails a workflow
task on a worker that does not know `ScheduledJobWorkflow` (SDK from the api pod: delete each
`job-schedule-*` and `webhook-sweep`). Migration 4.8 is additive — the old code ignores the columns, but
its loop selects `next_run_at`, which v2 rows leave NULL, so those schedules stay silent until set.
Prefer a forward fix.

**Released** *(2026-10-27 / 2026-10-28; owner: "doing f6 runbook")*. Step 0 (owner): no armed schedules, no v1
runs in flight, no pending webhooks — ⚠️ the runbook's first query named `schedule_lane`, which 4.8 only adds;
corrected (`3b9cf9c`). `develop` pushed; **the owner ran the `main` ff `261f931..3b9cf9c`** (it also carried
BUG-023's fix, `2983a47`). CI `replay` 41 passed, `build-api` green; Flux rolled api + workflow-worker to
`3b9cf9c`, 0 restarts; api log `Running upgrade 532e93f0d629 -> 437442ce59b2`, no loop start lines; worker
started clean. Steps 2–3 (owner — my SDK read was refused by the classifier as a production read):
`webhook-sweep` every 5 min, `SKIP`, the only Schedule; `migrate_schedule --all` and `--reconcile` both 0.
Step 4 (owner): a `*/5` job — both webhook POSTs arrived, pause worked, `DELETE ?permanent=true` 204. The
owner's click-through found two F.7 gaps (below), fixed and released as `41a348f` 2026-10-28 (CI green, both
Deployments on it); owner: "see both the changes". F.4's open call **confirmed by the owner** ("keep
webhook_del"). **Group F closed by the owner.**

#### F.7 — Scheduled jobs in the SPA

**Why:** a recurring job is invisible as one. `JobResponse` carries none of `schedule_cron`,
`schedule_status`, `next_run_at`, so neither the user nor the admin `Jobs` pages can tell a recurring job
from a one-off, see when it next fires, or pause it; the only schedule data in the SPA is
`active_recurring_jobs` and `next_scheduled_run_at` on the admin stats page. Filed by the owner
2026-10-27 to land **after F.6**, when every schedule is a Temporal Schedule.
**What:**
- **API:** schedule fields on `JobResponse` (user + admin reads); a `?scheduled=true` filter on
  `GET /jobs` and `GET /admin/jobs`. ⚠️ After F.1 the next fire time is the Schedule's, not
  `jobs.next_run_at` (the v1 scheduler's column) — read it from the Schedule (`describe()`'s next action
  times) or keep the column mirrored; decide at build. Skipped firings (ADR-010 §1 amendment, ADR-013
  `SKIP`) are not `job_runs` rows — show them only if F.1 records them somewhere readable.
- **SPA:** a schedule badge + cron (human-readable) + next run on the `Jobs` list and `JobDetail`;
  pause / resume on `JobDetail` in user mode (`PATCH /jobs/{id}` `{"schedule_status": …}`, which exists);
  the job's run history (already listed per job) labelled as scheduled runs. Same `mode` component set.
- ADR-013's per-user cap: show active scheduled jobs against the cap where the 429 can be hit.
**Tests:** API read tests (fields, filter, cross-tenant 404); SPA build; click-through on compose
(pause → no new runs, resume → next run shown).
**Depends on:** F.6

---

**Built** *(2026-10-27, `56a06e6`; ships with F.6)*. **API:** `JobResponse` carries `schedule_cron`,
`schedule_status`, `next_run_at` (computed from the cron — the column is v1's), `schedule_skipped_at`,
`schedule_skip_reason`, `schedule_waiting_since` on every user and admin job read; `?scheduled=true|false`
on `GET /jobs` and `GET /admin/jobs`; `GET /users/me/scheduled-jobs` → `{active, limit}`; the admin quota
PATCH takes `scheduled_jobs_limit` and gains the `GET` the Users page always called (**BUG-027**).
**SPA:** a Schedule column (recurring/paused pill, plain-words cron via `lib/cron.ts`, next run) and a
"Recurring only" filter on `Jobs`; `components/SchedulePanel.tsx` on `JobDetail` — schedule, next run,
the waiting and skipped notes, pause / resume (user mode; the cap's 429 message shown), "N of 20 active
schedules", and the job's latest 20 runs (user mode — no admin runs endpoint exists). New calls use
`useApi()` (BUG-018 not ridden). Skipped ticks show as the last-skip note only: they are not rows, and a
tick `SKIP` drops is counted by Temporal (`num_actions_skipped_overlap`), not shown (ADR-013 §4).
9 API tests; `npm run build` (tsc) clean. ⚠️ **Click-through not done** — no browser here (Clerk login).

**Fixed after the prod check** *(2026-10-28; owner's click-through on the released F.6)*: the runs list showed the
**job's** creation time on every run (`JobResponse.created_at` is the job's) — `run_created_at` added to every job
read; and the result viewer did not say which run it showed — `JobResultResponse` now carries `run_id` +
`completed_at` ("latest completed run · …"), and `GET /jobs/{id}/runs/{run_id}/result` opens any completed run,
with view / hide per run as on a batch's items. 3 tests, 4 mutants caught; suite 770. Ships in a second F release.

## Group G — Consumer deletion

#### G.1 — 🚀 Delete `result_consumer.py`

**Drain gate at deletion:** `api-result-consumer` zero/zero; no `lane='v1'` rows non-terminal.
~~D.2 already rescued the homeless pair~~ (D.2 was dropped by the owner 2026-10-12 — see below); verify nothing else in the file is unported (the `quota.py`
wall branch is job-lane behaviour D.3 kept). `JobNotifier` stays (BUG-009 survives).
**Depends on:** F.6

**Built** *(2026-10-28; not released)*. Deleted `core/result_consumer.py` and its lifespan task — the API
runs **no background task** now. Audit of the file against the v2 lane:
- **Ported:** the job and batch status paths, LLM dispatch, ledger accounting, the storage wall
  (`activities/job_runs.py`, D.3), the cancel-wins rule, the batch close (`activities/batches.py`, E.1).
- **Not ported, by the owner's D.2 call:** content-hash dedup and the reporting diff — they go with the
  file. D.2's spec above records the algorithm for Monitors; **`core/diff.py` stays** (no caller now) per
  the *Do not delete* list. `JobWorkflow` still writes `content_hash`, so the baseline exists.
- **Dead with it, deleted:** `quota.handle_storage_quota_exceeded` (its only caller).

**Calls made at build (TL, reversible):** the **v1 dispatch arms go too** — `JOB_LANE` / `BATCH_LANE` are
removed and every new job and batch run is `lane='v2'` (constructors explicit; the ORM default is now
`v2`, the server default stays `v1` for the pre-D.1 rows — no migration). Without the consumer a `v1`
run would sit `pending` forever, and both settings defaulted to `v1` (compose sets neither — **dev ran jobs
and batches on v1 until this commit**). Same move as F.6's `SCHEDULE_LANE`. `dispatch_run` loses its
`JetStreamContext`; `get_jetstream` is gone. **Left for H.2:** the API's NATS connection (`/health` reads
it), `ScrapeMessage`/`LLMMessage` and their builders (the contract test's NATS arm), the NATS subjects.
The cancel paths still filter `lane='v2'` before telling the engine — harmless with no `v1` row active.

**Tests:** 30 consumer tests deleted with the file (`test_deduplication.py` entirely) and 3 v1-lane tests;
`test_permanent_delete_releases_every_ledger_row` kept, its setup now writes the ledger rows directly.
`conftest.mock_jetstream` → **`mock_temporal`, autouse**: a fake client on `app.state.temporal`, so no
test starts a real workflow or Schedule on compose (a test's own fake, set later, wins);
`started_scrape()` reads the one started `JobWorkflow`'s input. Assertions on the NATS payload now read the
`ScrapeInput`. Suite **737** (770 − 33), then **739** with the two below.

**Mutation pass (2026-10-28, owner: "do a mutation test before we release"): 20/20 caught.** Six on what G.1
changed (both run constructors' lane, the ORM default, both start-failure paths, the workflow id) and
fourteen on the v2 code that now carries the 30 deleted tests' behaviour (the storage wall, its boundary
and its deletes, every object of a stage charged, `content_hash`, a stopped run's objects, cancel wins, the
webhook event filter, a failed run's error, a deleted LLM key, an unstarted run's webhook, a batch's
`partial_failure`, the ledger release on permanent delete). First run 18/20: **two gaps opened by the
deletions**, closed by new tests in `test_workflows_job.py` — a stage's second object charged (v1's
`test_screenshots_are_recorded_and_charged` had it) and objects filling the limit exactly (`<=` vs `<`).

**Compose (2026-10-28):** a job (`completed`, v2, one ledger row) and a two-URL batch (`partial_failure` —
example.com's genuine 404 on `/a`) through the HTTP API against the real engine; cleaned up through the
ledger. ⚠️ **Found: the drain hazard, live in dev.** `docker compose up -d` started the three NATS-mode
workers, whose dev stream holds a months-old backlog (8.37M messages; `go-worker` 7.4M pending,
`python-playwright-worker` 716k): they kept scraping and uploading, and with no consumer **180 objects
landed unrecorded** in an hour. Stopped; the three services now sit behind **`profiles: [v1]`** in
`docker-compose.yml` (opt in with `--profile v1`; H.1 deletes them). The dev bucket's ~2,130 unrecorded
objects and the stream are left as they are (`reconcile_storage_ledger --apply` and a stream purge are
the owner's call). Prod is guarded by step 0's durables check — and H.1 should follow soon, since the prod
NATS Deployments keep running after this release.

**Ships:** api image only (also the workflow worker). **No migration, no infra change needed** — the
infra repo's `JOB_LANE` / `BATCH_LANE` env vars are ignored by the new image (`extra="ignore"`).

**Runbook** (owner-run):
0. **Before (read-only, prod):** `SELECT lane, status, count(*) FROM job_runs WHERE status IN
   ('pending','running','processing') GROUP BY 1, 2` → expect **no `v1` rows**. Any there are already
   stranded since F.6 unless a NATS worker still holds them: cancel them (`DELETE /jobs/{id}`, `DELETE
   /batch/{id}`) — a result arriving after the release is read by nothing. NATS (nats-box pod):
   `nats --server nats://scrapeflow-nats:4222 consumer ls SCRAPEFLOW`, then `consumer info SCRAPEFLOW
   <durable> --json` for each → `num_pending` 0 and `num_ack_pending` 0 (`api-result-consumer` and the
   three worker durables — a scrape in flight would publish a result nothing reads).
1. **ff `main` to `develop`, push.** Watch CI `replay` (41) + `build-api`; Flux bumps api and
   workflow-worker. Check: api log has `NATS connected` and **no** `NATS result consumer started`;
   workflow-worker started clean.
2. **Verify** (your terminal, an API key): `POST /jobs` `{"url":"https://example.com/"}` → `completed`;
   `POST /batch` with two URLs → `completed`. Both lanes were already v2 in prod; this proves nothing
   regressed.
3. **After step 1 has rolled out** (infra, tidy-up, optional): drop the `JOB_LANE` / `BATCH_LANE` entries
   from `app/api.yaml`. ⚠️ **Never before** — the old image defaults both to `v1`.

**Rollback:** an image revert to `41a348f` — safe while step 3 is undone (the env still says `v2`).
Prefer a forward fix.

**Released** *(2026-10-28; owner: "doing g1 runbook; skip step 0; i did that")*. Step 0 run by the owner. `develop`
pushed (`843909e..bc30025`); **the owner ran the `main` ff `41a348f..bc30025`**. CI `replay` 41 passed,
`build-api` green, the worker builds skipped. Flux rolled api + workflow-worker to `bc30025`; owner: api and
worker started, `NATS connected`, no consumer line. Step 2 (owner): a job and a two-URL batch both
`completed`. Step 3: infra **`682cc52`** (`JOB_LANE` / `BATCH_LANE` dropped from `app/api.yaml`, after
checking both manifests were on `bc30025`) — the api pod restarted once for the env change; owner: neither variable in its env. ⚠️ **The rollback
above no longer applies after step 3**: `41a348f` without the env vars sends new runs to `v1`, which
nothing finishes. Fix forward.

---

## Group H — NATS removal

#### H.1 — Delete the NATS-bound worker Deployments (obligation 3)

After G.1: delete `http-worker.yaml`, `playwright-worker.yaml`, `llm-worker.yaml` (the NATS-bound
ones); the Temporal-bound ones become the only ones. `WORKER_MODE` flag and the NATS code paths
removed from all three workers.
**Depends on:** G.1

**Built** *(2026-10-28; `8a62da8`, infra `c92e7dd`; nothing pushed)*. Owner: "build it".
- **Go:** `internal/worker/` deleted whole (it was the NATS transport only — `internal/scrape` and
  `internal/activity` never imported it); `main.go` serves the activity, no mode branch; `config.go` loses
  `WORKER_MODE`, `NATS_URL`, `NATS_MAX_DELIVER`; `nats.go` out of `go.mod`.
- **LLM + Playwright:** `worker.py` (the NATS handler) and `models.py` (the NATS message schemas) deleted;
  `temporal_main.py` **became `main.py`** — the Dockerfile `CMD` and `entrypoint.sh` still run
  `python -m worker.main`, so the Xvfb/PID-1 contract is untouched; the early-SIGTERM guard before the slow
  imports is kept. Settings removed: `worker_mode`, `nats_url`, `*_ack_wait_seconds`,
  `*_max_delivery_attempts`, `*_retry_{base,max}_delay_seconds`; `errors.retry_delay` (the `nak` backoff)
  went too — the classifier itself is unchanged. `nats-py` out of both `pyproject.toml` + `uv.lock`.
- **Compose:** the three `profiles: [v1]` services deleted; `WORKER_MODE` dropped from the `-temporal` ones.
  Service and Deployment names keep `-temporal` (a rename is a delete + create in prod for no gain).
- **Pulled forward from H.2 (TL call, reversible):** the **contracts NATS arm** (`test_message_contract.py`
  + its five fixtures and the Go `contract_test.go`) — it fed API messages into the worker parsers this
  task deletes, so it could not outlive them. The API side (`messages.py`, the builders, subjects, the
  API's `nats-py`) stays for H.2. The workers' NATS deps went here for the same reason: nothing imports them.

**Tests:** ⚠️ **`playwright-worker/tests/test_main.py` was the only coverage of the render pipeline's
options** — BUG-015 (wait shares goto's budget), BUG-016 (CSS never blocked), BUG-017 (proxy userinfo
decoded), cookies before goto, the CSP route, actions after goto — reached through the NATS handler. Ported
to **`tests/test_scrape.py`** against `scrape.render()` directly (15 tests). Found porting: the old ordering
tests passed only because the NATS handler swallowed a `BlockedPage` — a `goto` stub returning `None` on the
75-byte default page trips Tier 1's `empty_body`; the new file renders a real-sized page. Go: two
`worker_robots_test.go` cases had no activity equivalent — `respect_robots=false` skips the check and a
malformed proxy URL is non-retryable — ported to `internal/activity/scrape_test.go`. Deleted: the NATS
handler, message-model and `retry_delay` tests. **Counts:** llm-worker 166, playwright-worker 202, Go all
green (`vet` incl. `-tags integration`, golangci-lint 0), contracts **41** (69 − 28 NATS-arm tests).
**Mutation pass: 12/12 caught** — ten on `scrape.render()` (CSS back in the glob, wait without timeout, no
`unquote`, no proxy, no cookie domain, no CSP route, CSP never fulfils, context never closed, actions before
goto, cookies after goto), two on the Go activity (robots checked regardless, proxy parse error ignored);
each by the test named for it. One Go mutant did not compile on the first try — rerun, not counted.
**Compose (2026-10-28):** the three workers rebuilt and recreated — each logs its started line on its
queues (`scrape-http`; `llm` + `content`; `scrape-playwright` + `crawl`). Through the HTTP API: an http job
and a playwright job (`block_images`) `completed`; an LLM job on a fake Anthropic key `failed` with the
provider's 401 (the activity ran); a one-page playwright crawl `completed`. Cleaned up through the API
(permanent deletes → ledger 0, counter 0, objects gone) and the test user deleted.

**Ships:** the three worker images (CI's path filter; **no api image, no replay job, no migration**).
Infra: `c92e7dd` deletes `app/{http,playwright,llm}-worker.yaml` (Flux `prune: true` removes the
Deployments, as at E.7); the `ImagePolicy`s stay — the `-temporal` Deployments use them.

**Runbook** (owner-run):
0. **Before (read-only, prod):** `kubectl -n scrapeflow get deploy` → the three NATS-mode and the three
   `-temporal` worker Deployments. Nothing publishes to NATS since G.1, so a worker durable's pending count
   is old backlog only and a result from it is read by nothing — not a gate.
1. **Infra first: push `c92e7dd`.** Check `kubectl -n scrapeflow get deploy scrapeflow-http-worker
   scrapeflow-playwright-worker scrapeflow-llm-worker` → NotFound ×3; the `-temporal` three unchanged.
   ⚠️ **Order matters:** a new image on a NATS-mode Deployment has no NATS mode, dials Temporal at the
   default `localhost:7233` and crash-loops.
2. **ff `main` to `develop`, push.** Watch `build-http-worker`, `build-playwright-worker`,
   `build-llm-worker` (api and replay skip). Flux bumps the three `-temporal` Deployments. Logs:
   `Temporal worker started … task_queue=scrape-http`; `temporal_worker_started … task_queues=['llm',
   'content']`; `temporal_worker_started … task_queue=scrape-playwright … crawl_queue=crawl`. The
   playwright and LLM pods drain up to 630 s / 400 s if a call is in flight.
3. **Verify** (your terminal, an API key): `POST /jobs` `{"url":"https://example.com/"}` and the same
   with `"engine":"playwright","playwright_options":{}` (the API 422s a playwright job without the
   object) → `completed`; optionally `POST /crawls` `{"seed_url":"https://example.com/",
   "max_pages":1,"engine":"playwright"}` → `completed`.
4. **After step 2 has rolled out** (infra, tidy-up, optional): drop `WORKER_MODE` from the three
   `-temporal` manifests, their "Needs an image with `WORKER_MODE`" comments and the README's three
   matching warnings. ⚠️ **Never before** — an older image defaults to NATS mode.

**Rollback:** revert the three `-temporal` images to their previous tags — safe while step 4 is undone
(`WORKER_MODE=temporal` still set). The NATS-mode Deployments are not needed for it. After step 4, fix
forward.

**Released** *(2026-10-28)*. Step 1: the owner pushed infra `c92e7dd`; Flux pruned the three NATS-mode
Deployments, the `-temporal` three unchanged on their old images. Step 2: the owner pushed `develop` and ran
the `main` ff `bc30025..722f54b`; CI built the three worker images (api + replay skipped); Flux rolled all three
to `722f54b` — each logged its started line on its queues, no errors. Step 3 (owner): an http job, a playwright
job and a crawl `completed` — ⚠️ the playwright job needs `"playwright_options":{}` (runbook corrected).
The owner found **BUG-029** while checking (the lists' "Engine" column; filed, fix later). Step 4: infra
**`51c2d40`** (all three Deployments were on `722f54b` first), pushed by the owner; the three pods restarted
without `WORKER_MODE` and logged their started lines. ⚠️ **After step 4
the rollback above no longer applies** — an older image defaults to NATS mode.

#### H.2 — 🚀 Remove NATS

`nats-init-job.yaml`, `infrastructure/nats.yaml`, `nats-py`/`nats.go` deps, `messages.py`'s
`to_nats_bytes`, `constants.py` subjects, the `contracts/` NATS arm (the activity arm stays),
compose services, `nats_stream_seq` dropped (the views do not read it — no drop/recreate).
**Depends on:** H.1

⚠️ **Already done at H.1:** the `contracts/` NATS arm and the **workers'** `nats-py`/`nats.go` — what is
left is the API side (`app/core/nats.py`, the `/health` NATS check, `messages.py` + the dispatch
builders, `constants.py` subjects, the API's `nats-py`), the infra, compose and `nats_stream_seq`.

**Built** *(2026-10-28, tenth session; `17c2903`, infra `c69b5f9`; nothing pushed).* Owner: "build it".
- **API:** `app/core/nats.py`, `app/messages.py`, `app/constants.py` (subjects only) deleted; `main.py`
  no longer connects; `nats_url` / `nats_max_deliver` out of settings; `nats-py` out of `pyproject.toml`
  + `uv.lock`. `build_scrape_message` / `build_batch_scrape_message` deleted — `build_scrape_input` /
  `build_batch_scrape_input` now carry their docstrings. `/health/ready` reports `db`, `redis`;
  `/health/deps` adds `minio`. Comments that named NATS as the mechanism (ledger, webhooks dedup,
  credentials) now name a retried activity / the activity input.
- **Compose:** `nats` + `nats-init` services and the `nats_data` volume declaration removed; the api no
  longer waits on them. ⚠️ The dev **`docker_nats_data` volume is left on disk** (old backlog only) —
  `docker volume rm docker_nats_data` when convenient. Root `.env` still sets `NATS_URL`: harmless
  (`extra="ignore"`). `.env.example` loses `NATS_URL` and G.1's stale `JOB_LANE`.
- **README:** architecture diagram, stack, design decisions and project tree describe Temporal (the
  coordinator, deleted at E.7, was still drawn).
- **Infra (`c69b5f9`):** `infrastructure/nats.yaml` (the HelmRelease) and `app/nats-init-job.yaml`
  deleted, both out of `kustomization.yaml`; the `nats` HelmRepository out of `infrastructure/sources.yaml`
  (nothing else used it); `api.yaml` loses `wait-for-nats` and `NATS_URL`. The `create-namespace.sh` copy
  in `temporal-init-job.yaml` stays byte-identical to the app repo's (both lost the nats-init analogy).

**Call made at build (TL, reversible): `nats_stream_seq` is unmapped now and dropped at I.1, not here.**
The api runs `alembic upgrade` on startup, and the workflow worker (same image, `RollingUpdate`) keeps
its old pod until the new one is ready — an old pod's `select(JobRun)` names every mapped column, so a
drop in this release fails its DB activities for that window. Unmapped, the column is invisible to the
new code; nothing has written it since G.1. `alembic check` therefore reports the column and
`idx_job_runs_nats_stream_seq` as removed (with the usual dedup false positive) until I.1's migration.

**Tests:** `test_nats.py` deleted (2). The two parity tests that compared an activity input with the
NATS message now assert the input's values; a third pins `playwright_options` to the playwright engine
(the mutation pass found it unpinned — the old parity test shared the gate with the builder it compared
against). `test_health` asserts the exact key sets. Suite **738** (739 − 2 + 1), contracts **41**, ruff
clean (the 7 pre-existing `UP042`/`RUF059` unchanged). **Mutation pass 6/6** after the new test (5/6
before it). **Pre-release mutation pass (owner: "do a mutation test before we release"): 17/17** over
everything H.2 touched — nine on the two input builders (artifact key, format, actions, robots,
credentials, engine gate), seven on `/health` (each dependency dropped from each endpoint's verdict, a
`nats` key back, `minio` gone from `/deps`), and `import nats` in `main.py` (fails: `ModuleNotFoundError`
— the image is NATS-free). First run 12/17; the five survivors were two real gaps, closed: the job-input
test used the default `html` (now `markdown`), and **no test ever took db or redis down** on either
endpoint (pre-existing; H.2 rewrote those lines) — `test_a_serving_dependency_down_is_degraded`, four
cases. Suite **742**. **Compose:** api rebuilt without `nats-py`, stack recreated with `--remove-orphans` (the
NATS containers went); `/health/ready` → `{"status":"ok","db":"ok","redis":"ok"}`; through the HTTP API
an http job, a playwright job and a 2-URL batch `completed`. ⚠️ Cleanup slip: `DELETE /batch/{id}`
only cancels (409 on a finished batch — existing behaviour), and I deleted the test user by SQL before
checking, so the batch's two objects (1,154 B) were removed from MinIO by key by hand. Jobs went through
`?permanent=true`.

**Ships:** the api image (api + workflow-worker; CI `replay` + `build-api`). **No migration, no worker
image.** Infra `c69b5f9` **after** the image.

**Runbook** (owner-run):
0. **Before (read-only, prod):** `kubectl -n scrapeflow get helmrelease,statefulset,job,pvc | grep -i nats`
   → the HelmRelease, its StatefulSet, `scrapeflow-nats-init` and the JetStream PVC. Note the PVC's name.
1. **App first: push `develop`, ff `main` to it, push.** Watch `replay` + `build-api`. Flux bumps `api` +
   `workflow-worker`. The api log has no `NATS connected`; `GET /health/ready` →
   `{"status":"ok","db":"ok","redis":"ok"}`. ⚠️ **Order matters:** infra first restarts the api (its env changes) on the
   old image, whose startup dials NATS (now `localhost:4222`, `NATS_URL` gone) and crash-loops.
2. **Verify** (your terminal, an API key): `POST /jobs` `{"url":"https://example.com/"}` and the same with
   `"engine":"playwright","playwright_options":{}` → `completed`.
3. **Infra: `git pull --rebase`** (image automation will have committed the api tag), **push `c69b5f9`**.
   Flux restarts the api without `wait-for-nats` (`Recreate`), helm-controller uninstalls `scrapeflow-nats`,
   the init Job is pruned. Check the step 0 command → only the PVC left; the api `1/1`, ready.
4. **Optional, destructive — your call:** the JetStream PVC survives a Helm uninstall.
   `kubectl -n scrapeflow delete pvc <name from step 0>` frees its 5Gi; it holds only backlog nothing reads.

**Rollback:** before step 3, revert the api image to `bc30025`'s tag (NATS still runs). After step 3,
fix forward — `bc30025` needs NATS to start.

**Released** *(2026-10-28, 18:26–18:31 UTC)*. Step 1: the owner pushed `develop` and ran the `main` ff
`722f54b..cd5c60e`; CI built the api image; Flux's image automation bumped `api`, `workflow-worker` and the
cleanup CronJob to `cd5c60e` (infra `1c636fb`, 18:26). Step 3: the owner rebased and pushed the infra commit
as **`432e014`** (18:30); Flux applied it — the `scrapeflow-nats` HelmRelease, its StatefulSet, `nats-box`
and `scrapeflow-nats-init` gone; the api restarted without `wait-for-nats` / `NATS_URL` and went `1/1`
(readiness = the new `db` + `redis` probe); the workflow worker `1/1` on `cd5c60e`. Step 2 (owner, after
step 3): an http job and a playwright job `completed`. Step 4 (owner, k9s): PVC
`scrapeflow-nats-js-scrapeflow-nats-0` deleted; its `local-path` PV went with it. ⚠️ My `kubectl logs` /
`exec` on the api pod were refused by the classifier ("Production Reads") — the api's startup log and
`/health/ready` body were not read; `get` / `wait` were allowed. **Group H closed.**

---

## Group I — API thinning

#### I.1 — Alembic-on-startup made multi-replica safe

**Carries H.2's `nats_stream_seq` drop** — a migration dropping `idx_job_runs_nats_stream_seq` then the
column (downgrade re-adds both, empty); nothing reads it, and H.2's release unmapped it. Safe here
because every pod runs code that no longer maps it.

**⚠️ Not in any ADR; found sequencing this group.** `main.py` runs `alembic upgrade head` on every
startup. Two replicas starting together race the migration. Options: a Postgres advisory lock
around the upgrade (smallest change), or an init container / Job that runs it once. **TL call:
advisory lock** — keeps the auto-run contract every handoff relies on.
**Depends on:** H.2

**Built 2026-10-29 (owner: "build it"; local, unreleased).**
- **Lock:** `migrations/env.py` takes `pg_advisory_xact_lock(MIGRATION_LOCK_KEY)` (`app/core/db.py`) on the
  migration connection before `run_migrations()` — so it covers the startup upgrade *and* any hand-run
  `alembic` command. Transaction-scoped: every migration already runs in the one `engine.begin()`
  transaction, so the lock is released at commit/rollback, or when a dead pod's connection drops — no
  unlock path to forget. A `pg_try_` first, so a waiter logs `Another process holds the migration lock;
  waiting for it` (`alembic.env`) — the line to look for if a pod hangs at startup. The waiter blocks
  unbounded (no `lock_timeout`); a probe killing a slow waiter is safe, it retries on restart.
  ⚠️ The lock must precede the read of `alembic_version` (READ COMMITTED: the waiter's next statement
  sees the winner's commit). **That ordering is not covered by a test** — only the code order and its comment.
- **Migration 4.9 `47a8f9f3f4e2`:** drops `idx_job_runs_nats_stream_seq` then the column; downgrade re-adds
  both, empty (index identical to `655b7e9b461b`'s). Autogenerate under `pause api`; the dedup false positive
  deleted. `alembic check` now reports only that false positive. Safe against `cd5c60e` — H.2 unmapped it.
- **Verified:** `tests/test_migration_lock.py` (2: an upgrade waits on a held lock then completes; an
  uncontended one doesn't wait — 0.27 s vs a 2 s hold); suite **744**; mutation **3/3** (no lock call,
  try-lock only, wrong key). Compose: round trip up/down/up; **two concurrent `alembic upgrade head` from 4.8
  behind a 30 s `psql`-held lock** — both logged the wait, one ran 4.9, the other found head and exited clean.
- **Release:** none of its own — ships with I.2 (api image; migration 4.9 on startup).

#### I.2 — 🚀 `replicas: 2`, `RollingUpdate`

`api.yaml`: drop `Recreate`; `JobNotifier` per replica is correct by construction (`pg_notify`
fans out to every listener); `ws_max_connections_per_user` is per-process — note it. The
migration's stated payoff; record the before/after in the handoff.
**Depends on:** I.1

**Built 2026-10-29 (owner: "build it"; local, unreleased).**
- **Infra `5e9087e`** (`app/api.yaml` + README *API* section): `replicas: 2`; `RollingUpdate` with
  `maxSurge: 1`, `maxUnavailable: 0` (two serving pods throughout a rollout; one surge pod — the node has
  room: 30 % CPU / 13 % memory requested); `lifecycle.preStop.sleep: 5` so Traefik stops routing to a pod
  before it gets SIGTERM. Server-side dry run accepted (k3s v1.33.6 — the native `sleep` action).
  **Workflow worker stays at 1** — not in this task; Temporal workers compete on a queue, so scaling it is
  config only, and `ensure_sweep` already handles `ScheduleAlreadyRunningError`.
- **App `d1eedcf`:** no runtime change. `ws_max_connections_per_user` gained its trap note (per process —
  N replicas admit N times it). Two tests in `test_job_notifier.py` against the dev Postgres: two notifiers
  on separate connections each receive every channel (`job_status`, `batch_status`, `pipeline_status`,
  `crawl_status`); the connection limit is per instance.
- **Audit of per-process state in the API** (what two replicas could split): the WebSocket count (above) is
  the only one. Rate limiting and the host limiter are Redis; quota admission is a Postgres row lock
  (BUG-023); the Temporal client is a per-process connection; no background task since G.1.
- **Verified:** suite **746**; mutation **2/2** (crawl listener dropped; counts shared across instances).
  **Compose, cross-process:** a second uvicorn on :8001 in the api container; a playwright job created on
  :8000 and watched over a WebSocket on :8001 — `running` then `completed` relayed live; job deleted (204),
  test user removed (scratchpad script, not committed).
- ⚠️ **Not verified:** whether `uv run` (PID 1, BUG-021) forwards SIGTERM to uvicorn once started, and how
  long an old pod with an open WebSocket takes to stop — a watch loop waits up to 300 s on its queue, so it
  may sit out the 30 s grace and be SIGKILLed. With `maxUnavailable: 0` that slows a rollout; it does not
  drop requests. The SPA reconnects (C.12).

**Runbook (owner).** Infra first: it changes only the replica count and strategy, on code already running.
0. *(Optional, the "before")* in a spare terminal, a probe loop — leave it running through steps 1 and 3:
   ```bash
   while true; do printf '%s %s\n' "$(date +%T)" \
     "$(curl -s -o /dev/null -m 5 -w '%{http_code}' https://scrapeflow.govindappa.com/health)"; sleep 0.5
   done | tee i2-rollout.log
   ```
   Today's `Recreate` gap is not recorded anywhere; to measure it before the change, `kubectl -n scrapeflow
   rollout restart deploy/scrapeflow-api` with the loop running (a real outage of a few seconds), then
   `grep -vc ' 200$' i2-rollout.log`.
1. **Infra:** `git pull --rebase` (Flux image commits), push `5e9087e`. Flux applies it; the template change
   (`preStop`) rolls the api under the new strategy on `cd5c60e`.
   `kubectl -n scrapeflow rollout status deploy/scrapeflow-api` → `2/2`; `kubectl -n scrapeflow get pods
   -l app=scrapeflow-api` → two `Running`, `1/1`. ⚠️ **This rollout is still an outage** — in the
   2026-10-29 release the old pod was scaled to 0 before the two new ones were created (events 12:40:09 /
   12:40:11 UTC; ~46 s of 503s), so the strategy switch itself ran as a `Recreate`. Cause not confirmed.
2. **App:** push `develop`, ff `main` to it (api image; replay job gates the build). Flux bumps api,
   workflow-worker and the cleanup CronJob. The api rolls one pod at a time; the first new pod runs
   **migration 4.9** (`437442ce59b2 -> 47a8f9f3f4e2`) under I.1's lock while an old `cd5c60e` pod still
   serves — safe, H.2 unmapped `nats_stream_seq`.
3. **Verify:** `rollout status` `2/2` on the new tag; probe loop: no non-200 through the rollout (this is
   the "after"); `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- /app/.venv/bin/alembic current`
   → `47a8f9f3f4e2 (head)`; in the SPA, run a job and watch its status go live (the socket lands on either
   pod). If a pod hangs at startup, look for `Another process holds the migration lock` in its log.
4. Record the before/after in the handoff.

**Rollback:** revert infra `5e9087e` (back to one pod, `Recreate`); the app needs none — 4.9 is applied
and nothing reads the column. Do not revert `main` past `bc30025` (G.1).

**Released 2026-10-29 (owner ran steps 1–2; I watched with `get`/`wait` and the probe loop).** Probe: `curl`
on `https://scrapeflow.govindappa.com/health` every ~0.9 s from the dev machine.

| Rollout | Strategy in effect | Failed probes | Outage |
|---|---|---|---|
| Step 0 — `rollout restart` on `cd5c60e`, one pod | `Recreate` | 43 (all `503`) | **~40 s** (18:08:01 → 18:08:41 IST) |
| Step 1 — infra `5e9087e` (1 → 2 pods, strategy switch) | ran as `Recreate` (see step 1's ⚠️) | 49 (all `503`) | ~46 s |
| Step 2 — `main` `cd5c60e..eea8344` (api image, migration 4.9) | `RollingUpdate` | **0 of 359** | **none** |

- Step 2's rollout (events, UTC): new pod 1 at 12:45:19, ready and old pod 1 scaled down at 12:46:23
  (~64 s — init containers, migration 4.9, readiness); new pod 2 at 12:46:23, old pod 2 gone 12:47:02. Whole
  rollout 1 min 43 s. Two serving pods throughout. Workflow worker on `eea8344`.
- ⚠️ My `kubectl exec … alembic current` was refused by the classifier ("Production Reads") — **migration
  4.9 in prod and the migration-lock log line are the owner's to read**, as is the SPA's live status.
  The new pods went ready, which they cannot do if `upgrade head` failed (startup raises).

---

## Docs track

| # | What | Needed by |
|---|---|---|
| X.1 | **PRD-019** — conditional execution (four obligations on its `phase4-backlog.md` §2 row) | before PRD-018; not on this critical path |
| X.2 | **Promote ADR-010** (owner review) | E.0 |
| X.3 | ~~**Schedule overlap policy** ADR — ADR-013, `SKIP`~~ ✅ Accepted 2026-10-27 | F.0 |

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

1. ~~**D.2** — the job cutover must port dedup + diff or R5 breaks; ADR-009 §10 frames them as
   waiting for Monitors, which is the pipeline lane's answer only.~~ **Answered 2026-10-12 (owner):
   drop for now** — jobs on v2 have no dedup and no diff until change detection is re-homed;
   recorded as PRD-016 R5's second named exception. D.2 not built. Why: one place for change
   detection (Monitors), not two; no jobs run in production, so nothing is lost now.
2. **C.9** — `waiting` under a `RetryPolicy` can only be written by the activity itself; confirm
   that reading of 15a/15b/15d. **Built on that reading 2026-10-11 (`5b14291`)** under the owner's
   delegation; not separately confirmed — the C.9 *Built* note says exactly what the activity writes.
3. ~~**C.13** — Worker Versioning vs `patched()`: due before C.14, not after.~~ **Answered
   2026-10-12 (owner): `patched()` + a replay test that gates the api build** (ADR-012, Draft).
4. ~~**E.0** — task-queue shape (per-host limiter, reserved lanes) vs the crw deferral.~~
   **Answered 2026-10-13 (owner):** bounded fan-out on one queue per engine; reserved lanes not now
   (replay-safe to add later); per-host limiter on every scrape — 1 req/s, 2 in flight per eTLD+1,
   fail open. E.0 has the detail; built in E.8/E.9.
5. **F.4** — `webhook_deliveries` as a v1-only audit mirror or retired.
6. **I.1** — Alembic-on-startup under two replicas; no ADR covers it.
7. ~~**C.11** — BUG-009 is deferred past Phase 4 in `phase4-backlog.md` §4 but is made load-bearing
   by the pipeline lane; recommend pulling it in.~~ **Pulled in and fixed at C.11 (`88e8024`, 2026-10-11)**,
   under the owner's delegation.
8. ~~**A.8** — where the Temporal Postgres backup lives.~~ **Answered 2026-09-29: nowhere, for
   either Postgres — deferred past the migration by the owner** (data is junk today). `phase4-backlog.md` §4.
9. ~~**C.5 / C.7** — where the LLM block's key comes from: the definition holds `llm_key_id` only, the
   workflow cannot read `user_llm_keys`. C.5 resolving it into the workflow arguments (Fernet ciphertext,
   like Scrape's `Credentials`) vs a C.6 DB activity. Found 2026-10-05 (C.3 *Built* note).~~
   **Answered 2026-10-06 (owner): C.5 resolves it** — the trigger loads the owned keys and passes
   `{provider, encrypted_api_key, base_url}` per `llm_key_id` in `PipelineRunInput.llm_keys`, as
   `LLMProbeWorkflow` does. Accepted cost: a key deleted or rotated mid-run is not seen by that run.
   Built in the *C.4–C.7 MVP slice*.
10. **Should `/jobs` become a front end over pipelines?** *(raised by the owner 2026-10-12, reading D;
    deferred — decide with Monitors / PRD-018, not before D.5.)* A job is a pipeline of
    `scrape → (llm_extract) → (webhook)` — R6 proved the equivalence — and both workflows already call
    the same `Scrape` / `LLMExtract` activities; what `JobWorkflow` duplicates is the orchestration and
    its five DB activities (~250 lines). ADR-009 and `temporal-full-migration.md` assumed a separate
    `JobWorkflow` and never weighed the alternative. It is not a drop-in swap: the lanes differ where
    users can see it — storage wall (job fails vs pipeline completes and is charged), webhook (side
    delivery via `webhook_loop` that never fails a job vs a block that can fail the run), payload
    (`job.completed` + `job_id` + diff fields vs `pipeline.result` + `pipeline_id`), webhook secret
    (generated vs optional), and separate tables, routes, SPA pages and MCP tools. Folding jobs in means
    either accepting those changes (a named R5 exception) or a job mode inside `PipelineWorkflow`, plus
    deciding the fate of `jobs` / `job_runs`. Same principle as D.2's answer: one way to do a thing.
    Monitors overlap scheduled jobs too, which is why that is the moment to settle what a job is.
