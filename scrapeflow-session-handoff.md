# ScrapeFlow Session Handoff

You are a **coding mentor** helping the user build ScrapeFlow, a self-hosted multi-tenant web
scraping platform. Read @CLAUDE.md for the full architecture.

## Your role in this session

**Do not write code unless the user explicitly says "build it", "implement it", or similar.**

Instead:
- Explain what needs to be built and why
- Walk through design decisions, trade-offs, and patterns
- Point out relevant existing code the user should look at before writing
- Review code the user writes and give feedback
- Answer questions about the spec, architecture, or implementation approach

When the user is ready to build something, they will say so. Until then, guide and explain.

---

## Project reference

| What | Where |
|------|-------|
| Architecture + key decisions | `CLAUDE.md` |
| Docs index (ADRs, reference, archive) | `docs/README.md` |
| ADR index + per-record status | `docs/adr/README.md` |
| **Phase 4 scope — single source of truth** | `docs/project/phase4-backlog.md` |
| **Phase 4 engine decision + coexistence contract** | `docs/adr/ADR-009-workflow-engine-temporal.md` |
| Crawl admission + scheduled-quota decisions (Draft) | `docs/adr/ADR-010-crawl-admission-and-scheduled-quota.md` |
| **Artifact identity — the live path convention (Accepted)** | `docs/adr/ADR-011-artifact-identity-and-paths.md` |
| Open bugs (BUG-004 → BUG-019) | `docs/project/open-bugs.md` |
| Open questions (Q1–Q8) | `docs/project/open-questions.md` |
| Usage findings (UF-00x) + test counts | `docs/project/usage-findings.md` |
| PRDs | `docs/project/phase4-prd/` (PRD-016 only, so far) |
| Feature scoping + engine comparison (redrawn 2026-09-08) | `docs/project/workflows-scoping.md` |
| Change inventory + migration sequence (redrawn 2026-09-08) | `docs/project/temporal-full-migration.md` |
| **Phase 4 implementation backlog — the ordered task list (Tech Lead, 2026-09-20)** | `docs/project/phase4-implementation-backlog.md` — groups A–I are ADR-009 §16's named steps; one task per session; 🚀 marks the release points; its status table is the tracker. **Eight open items at the bottom need Architect/owner answers before the tasks that cite them** |
| **Temporal persistence (A.1 — ✅ 2026-09-21, both halves)** | Local: `docker/docker-compose.yml` → `temporal-postgres` (host port **5434**) + `docker/temporal-postgres/init.sql`. Prod: infra repo `clusters/k3s-server/scrapeflow/infrastructure/temporal-postgres.yaml` (ConfigMap + StatefulSet `scrapeflow-temporal-postgresql` + Service; infra `de903a2`), Secret `scrapeflow-temporal-db-credentials` (owner-created, README §1), PVC `data-scrapeflow-temporal-postgresql-0` (10Gi, `local-path`). `POSTGRES_DB` creates `temporal`; the script creates `temporal_visibility`. Both verified in prod, 0 tables each. A.2's schema step fills them |
| **Temporal server (A.2 — ✅ 2026-09-22, both halves)** | Local: `docker/docker-compose.yml` → `temporal-schema` (admin-tools one-shot, runs `docker/temporal/setup-schema.sh` — **no `create`**, `SQL_PASSWORD`) then `temporal` (`temporalio/server`, `service_completed_successfully`, port 7233, `nc` healthcheck); `docker/temporal/dynamicconfig/development.yaml` (comment-only; C.13 writes the first key). Both images pinned through `${TEMPORAL_VERSION:-1.31.0}` — **bump the two together**. ⚠️ **`temporalio/auto-setup` is deprecated** (found by the owner 2026-09-22); the TL call was revised before build — backlog A.2 has the original struck through. Cluster health from the CLI: `docker compose run --rm --no-deps --entrypoint temporal temporal-schema operator cluster health --address temporal:7233`. Prod: infra `clusters/k3s-server/scrapeflow/infrastructure/temporal.yaml` (infra `e8f32e1`) — ConfigMaps `scrapeflow-temporal-schema` (copy of the app-repo script, which is canonical) + `scrapeflow-temporal-dynamicconfig` (`production.yaml`, per-environment, not a copy), Deployment `scrapeflow-temporal` (`Recreate`; `schema` init container on admin-tools + `server`, same tag), ClusterIP **`scrapeflow-temporal:7233`** — the address A.5's worker will use. **`NUM_HISTORY_SHARDS=4` explicit on both halves; immutable after first start.** Prod CLI: `kubectl -n scrapeflow run <name> --restart=Never --image=temporalio/admin-tools:1.31.0 --command -- temporal operator cluster health --address scrapeflow-temporal:7233`, then `kubectl logs <name>` |
| **The wire contract the API publishes through (P6)** | `api/app/messages.py` — and `coordinator/coordinator/messages.py`, a **deliberate duplicate** for the crawl lane (ADR-011 §6 rejected a shared package) |
| **The dispatch-message builders (P9)** | `api/app/core/dispatch.py` — one builder per lane; every scrape dispatch site (`create_job`, `create_batch`, both scheduler paths) calls one. The only place a `ScrapeMessage` is constructed on the API side |
| **Cross-service contract test + Go fixtures** | `contracts/` — the only test that feeds an API-produced message into each worker's real parser. Command in *Commands* below |
| **The storage ledger (P8)** | `api/app/core/ledger.py` — the only writer (`record_object`) and releaser (`release_*`) of `storage_objects` rows; every delete path calls it. Model: `api/app/models/storage_object.py`. Regression tests: `api/tests/test_storage_ledger.py` |
| **The run-counting views (P7)** | `api/app/models/quota_views.py` — `Table` objects on a private `MetaData` for the two views migration `86c780f55969` creates; `core/quota.py` reads them and nothing else does. `quota_run_units` = one row per attempted fetch (`monthly_runs`); `quota_active_submissions` = one row per submission holding a slot (`concurrent_jobs`). A new lane is one migration widening both |
| **Crawl-quota audit (owner-run, read-only; ✅ run in prod 2026-09-19)** | `api/scripts/audit_crawl_quota.py` — what 90 days of crawls would have cost per user under P7's meters, and who holds slots now. Reads the views. Prod: *"No crawl pages since 2026-06-01. Nobody's numbers change."* ⚠️ On v1 every crawl is `running` forever (BUG-008) and holds a slot until cancelled — the script says so |
| `latest/` production sweep (✅ **applied in prod 2026-09-19**) | `api/scripts/sweep_latest_objects.py` — dry-run by default. Prod: 36 objects / 21,938,965 bytes deleted, 0 failed, re-run finds 0. One-time; nothing writes `latest/` any more |
| **Storage ledger reconcile (✅ applied in prod 2026-09-19; the standing auditor, idempotent)** | `api/scripts/reconcile_storage_ledger.py` — dry-run by default. Walks the bucket, records pre-ledger objects, deletes orphans, **recomputes** every counter. Prod: 35 recorded, 11 orphans deleted, counter already exact; second run a no-op. ⚠️ Its dry-run counter preview was wrong on a first run (`after=0`) — fixed `8608c0c`, see the release block. **How to run in prod:** `kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- /app/.venv/bin/python -m scripts.<name> [--apply]` (⚠️ the `scripts/<name>.py` form fails to import `app` in a fresh container of the image — BUG-019) — the image bakes `scripts/` and the pod has the DB/MinIO credentials |
| Multi-persona process starter prompts | `docs/process/` |
| Anti-bot hardening record (ADR-008 companion) | `docs/guides/anti-bot-hardening.md` |
| **crw engine comparison — DEFERRED until the Temporal pipeline is done** | `docs/guides/competitor-research.md` §crw (2026-09-19). §A = seven v1 bugs, none filed; §C = mechanisms owed at the batch-and-crawl cutover (per-host limiter, interactive/batch lanes) |
| Phase 1–3 history (specs, backlogs, reviews, audits) | `docs/archive/` |

---

## Commands

**API tests** (must run inside Docker — `uv` manages the venv inside the container):
```bash
# from ./docker
docker compose exec api uv run pytest tests/ -v
docker compose exec api uv run pytest tests/test_jobs.py -v
```
⚠️ `api/scripts/` is mounted into the api container **since 2026-09-15** (`docker-compose.yml`).
Before that the container held a *baked* copy, so `python scripts/x.py` and any test that loaded a
script ran the image's version, not the working tree's — a green "import ok" proved nothing. If the
container predates the mount, `docker compose up -d api` once.

**Worker tests** — not wired into compose; mount the source over the built image:
```bash
# from repo root
docker run --rm -v "$PWD/llm-worker:/app" -w /app docker-llm-worker python -m pytest -q

docker run --rm -v "$PWD/playwright-worker/worker:/app/worker:ro" \
  -v "$PWD/playwright-worker/tests:/app/tests:ro" -w /app \
  --entrypoint python scrapeflow-playwright:ackfix -m pytest tests/ -q -p no:cacheprovider
# ⚠️ the docker-playwright-worker image on disk is stale (predates credentials —
#    no cryptography, so test_main.py fails to import). Use a newer tag.

cd http-worker && go test ./...          # non-integration
go vet -tags integration ./...           # compile the integration-tagged tests

# ⚠️ the coordinator needs env vars even for pure-unit tests — coordinator/config.py
#    instantiates Settings() at import time, so collection fails without them.
docker run --rm -e DATABASE_URL="postgresql+asyncpg://u:p@localhost/db" \
  -e NATS_URL="nats://localhost:4222" \
  -v "$PWD/coordinator:/app" -w /app docker-coordinator python -m pytest -q
```

**Cross-service contract test** (ADR-011 §6) — the only test that feeds an API-produced
message into each worker's *real* parser. Needs the repo root mounted, because the four
message modules live in four build contexts:
```bash
# from repo root
docker run --rm -v "$PWD:/repo" -w /repo docker-api \
  /app/.venv/bin/python -m pytest contracts -q

# after an intentional contract change, to refresh the Go fixtures
docker run --rm -v "$PWD:/repo" -w /repo -e UPDATE_CONTRACT_FIXTURES=1 docker-api \
  /app/.venv/bin/python -m pytest contracts -q
```
⚠️ The Go arm reads `contracts/fixtures/*.json` and is **not optional**: the silent half of
BUG-005 (a JSON `null` unmarshalled to `""` with no error) exists *only* on the Python→Go
wire, so a Python-only contract test would not reach the failure that actually shipped.

**MCP tests** (standalone image, not in compose):
```bash
docker build -t scrapeflow-mcp mcp/
docker run --rm -e SCRAPEFLOW_API_KEY=test-key scrapeflow-mcp python -m pytest tests/ -v
```

**Migrations** (Alembic auto-runs on API startup, from `api/app/main.py`):
```bash
# from ./docker
docker compose exec api uv run alembic upgrade head
docker compose exec api uv run alembic current
docker compose exec api uv run alembic revision --autogenerate -m "migration_3_N_description"
```
⚠️ **Autogenerate under the running API is the hot-reload trap** (the reloader applies the new file
before you have edited it — it has dropped `idx_webhook_deliveries_dedup` once and mis-recorded a
partial index once). **Standing procedure since 2026-09-19 — sidestep it, don't race it:**
```bash
# from ./docker — the api container is paused, so nothing reloads; the one-off container
# shares the image, volumes and network, so alembic still reaches Postgres
docker compose pause api
docker compose run --rm --no-deps -T api uv run alembic revision --autogenerate -m "migration_4_N_description"
#   → edit the file: delete the drop_index("idx_webhook_deliveries_dedup") op it always emits
#     (and its create_index in downgrade); name any create_foreign_key(None, …) explicitly,
#     or the downgrade's drop_constraint(None, …) cannot run
docker compose run --rm --no-deps -T api uv run alembic upgrade head
docker compose unpause api
docker compose exec api uv run alembic check      # only the dedup false positive should remain
```

---

## Current state — as of 2026-09-25 *(clock)*

Phases 1–3 complete and production-verified at `scrapeflow.govindappa.com`. **Phase 4 is in
progress, and Phase 4 *is* the Temporal durable-workflows migration.** The design phase closed on
2026-09-03. **The pre-migration queue — P6 / BUG-005 (2026-09-04), P9 / BUG-011 (2026-09-11,
`ed4d63c`), P8 / BUG-007 (2026-09-15, `f503f8b`) and P7 (2026-09-18) — was RELEASED 2026-09-19 as
one `main` fast-forward (`421cbfe`).** Production is on it, reconciled and swept. **The entry
condition for Phase 4 build work (16e) is met; the next step is ADR-009 §16's *engine up*.**

🔷 **A.4 is done on both halves (2026-09-25, second session) — Temporal Web UI, ClusterIP only.**
Owner: "code up both and commit and push; this to dev and k8s to prod". Local: `temporal-ui` in
compose (`temporalio/ui:${TEMPORAL_UI_VERSION:-2.54.1}`, `localhost:8080`). Prod: infra
`infrastructure/temporal-ui.yaml` (Deployment + ClusterIP `scrapeflow-temporal-ui:8080`, `/healthz`
probes, no Ingress) + kustomization line + README section and DNS row, infra `cd7b36c` — **the
infra push was allowed this time**. Flux applied it; rollout green; through a port-forward:
`/healthz` OK, namespace `scrapeflow` `REGISTERED` at 2592000 s (30 d), server 1.31.0; no real
error lines. Notes:

- **The UI is its own version line** (2.x) — not bumped with `TEMPORAL_VERSION`; separate compose var.
- **Service is `scrapeflow-temporal-ui`**, not the backlog's `temporal-ui` — namespace prefix convention.
  Port-forward: `kubectl -n scrapeflow port-forward svc/scrapeflow-temporal-ui 8081:8080` — **8081**,
  because the local compose UI holds 8080 (the owner hit the clash; the first README said 8080).
- **The forwarded port does not matter.** `TEMPORAL_CORS_ORIGINS=http://localhost:8080` is set on both
  halves but is not a gate for the UI's own pages: a terminate call carrying `Origin:
  http://localhost:8081` reached Temporal (404 *workflow not found*), and the same call without the
  CSRF token got 400 — the CSRF token is the gate. My first write-up said "keep 8080"; it was untested
  and wrong. Owner verified the prod UI in a browser on 8081: `scrapeflow`, 0 workflows, 2.54.1.
- **Next: A.5** — workflow-worker scaffold in `api/` + `HelloWorkflow` (first application code of Phase 4's build).

🔷 **A.3 is done on both halves (2026-09-25) — namespace `scrapeflow` registered, retention 30 d,
local and prod.** Owner's "build on both, don't commit", then "push and verify". Local:
`docker/temporal/create-namespace.sh` (canonical) + `temporal-namespace` one-shot in compose
(app commit on `develop`). Prod: infra `app/temporal-init-job.yaml` — ConfigMap (byte-identical
copy, `diff`-verified) + Job `scrapeflow-temporal-init` — first `kubectl apply`'d uncommitted
(Complete in 11 s), then committed and pushed as infra `60b0aee`; **Flux adopted both objects in
place** (its labels added, Job not recreated — `force: true` on `flux-system` was never needed).
`namespace describe` from a one-off pod → `Registered`, `720h0m0s`, before and after adoption; 0
server error lines. Locally also tested: re-run with retention hand-set to 72h → reset to 720h;
unreachable address → exit 1. Things from the build:

- ⚠️ **The CLI's `namespace create --retention` default is 72h, not 30 d.** ADR-009 §2c says 30 d
  "is Temporal's default" — true of nothing we run. Unset would have meant 3 days. Recorded in the
  script and the backlog; the ADR is immutable.
- **The script converges, it does not only create** (the `nats-init-job.yaml` precedent):
  existing → `update --retention`. The manifest owns retention; a hand change is reverted on the
  next run. Re-running is also the recovery after a Temporal DB wipe (`kubectl delete job
  scrapeflow-temporal-init`, Flux recreates it). No `ttlSecondsAfterFinished` on purpose — Flux
  would recreate the deleted Job every 10 min.
- **A Temporal namespace is a row in `temporal.namespaces`** (settings in an encoded `data` blob,
  so `describe` is the check, not SQL), beside the server's own `temporal-system`. Not a tenant
  boundary (ADR-009 §12) and not access control — self-hosted Temporal has no authorizer.
- ⚠️ **The auto-mode classifier allowed `git push` to the infra repo this time** (contrary to the
  A.2 note below) but **refused `kubectl exec` into the API pod** (*Production Reads*). Do not rely
  on either.
- 🔴 **BUG-019 filed** — found while checking A.3's Job list: the nightly cleanup CronJob has
  **never succeeded in prod** (system Python without deps; `scripts/x.py` cannot import `app`; two
  Fernet keys unset). Reproduced on the deployed image, not from a log (the pod went with the Job).
  Owner's call: pick up later. ⚠️ Knock-on: the *How to run in prod* line in the reference table
  (`/app/.venv/bin/python scripts/<name>.py`) fails in a fresh container of this image; use
  `python -m scripts.<name>`.
- ~~**Next: A.4**~~ ✅ done the same day — the block above.

🔷 **A.2 is done on both halves (2026-09-22, second session) — the engine is up in prod.** Owner's
"build all" → `infrastructure/temporal.yaml` (four objects, A.1's ConfigMap pattern; the script
spliced in by `sed` and `diff`-verified byte-identical) + the kustomization line, dry-run clean,
committed as infra `e8f32e1`; the owner pushed (the classifier blocks a GitOps push — see below).
Flux applied in ~60 s; the `schema` init container took prod's empty databases 0.0 → **1.19** and
0.0 → **1.14** in ~30 s; pod Ready at +70 s; `Updated dynamic config`; **zero** `error` lines (the
eight local boot-noise lines did not occur); `operator cluster health` → **SERVING**; 40 + 3 tables.
`rollout restart` → 20 s → `found zero updates from current version 1.19` / `1.14` — the
idempotency line, in prod. (That restart then cost a second rollout and one self-healing crash —
the two bullets below; the pod has been up and `SERVING` with 0 error lines since 09:58 UTC.) Node after: CPU limits 168 % → **175 %**, memory 56 % → 59 %. The app
side got one commit (`a0008af`, `develop`): `NUM_HISTORY_SHARDS: 4` on the compose `temporal`
service, verified by recreating the local server against its existing database. Things from the build:

- ⚠️ **`NUM_HISTORY_SHARDS` is immutable after the first start** — persisted in
  `cluster_metadata_info` (proto field 2; decoded `4` in both prod and local). Nothing in the
  backlog said so. Set explicitly to the image default on both halves so it is a written decision;
  changing it means wiping the Temporal database.
- ⚠️ **The auto-mode classifier refuses `git push` to the infra repo** (*Protected-Scope IaC
  Apply*) — a push there *is* a prod deploy. Commit, rebase, then the owner pushes with
  `! git -C <infra repo> push origin main`. It allowed every read (`kubectl logs/exec/describe`),
  `rollout restart`, and one-off `kubectl run` pods.
- 🔴 **`kubectl rollout restart` on a Flux-managed Deployment causes a SECOND rollout.** Flux
  strips the `restartedAt` annotation on its next reconcile; that is another pod-template change,
  so the Deployment rolls again (~10 min after the manual one here, back to the original
  ReplicaSet hash). **Restart a Flux-managed pod with `kubectl delete pod`** — no template change,
  nothing for Flux to revert. This applies to every Deployment in the namespace, not just Temporal.
- ⚠️ **A replacement Temporal pod can fail its first ringpop bootstrap, and it self-heals.** The
  new pod joins the ring from `cluster_membership`, whose rows still hold the *previous* pod's IP
  with a heartbeat seconds old; it retries ~55 s, exceeds the 30 s max join, and exits
  `fatal … failed to start ringpop` (exit 1). The kubelet restarts the container and the second
  attempt joins. Seen once in two rollouts (the Flux-reverted one), ~90 s, healthy since with **0**
  error lines and `SERVING`. **Expected on a pod replacement — do not chase it**; if it ever loops,
  the stale rows are `temporal.cluster_membership` (aged out by heartbeat; `record_expiry` is 2 days
  out and is not the mechanism). Worth knowing at A.8 and at every server bump.
- ⚠️ **`kubectl run --rm -i` loses a short-lived pod's output to its own teardown** — `cluster
  health` printed nothing. Use `--restart=Never` without `--rm`, poll the phase, `kubectl logs`,
  delete.
- **The schema copy is deliberate and annotated** (A.1's precedent): kustomize cannot read across
  repos, so the ConfigMap embeds the script; the app-repo file is canonical because the local half
  runs first. The dynamic config is **not** a copy — per-environment by nature, so prod's is
  `production.yaml`, allowed to diverge from local's `development.yaml`.
- **`strategy: Recreate` is a decision here, not a default**: on a tag bump the new pod's init
  container migrates the schema, and Recreate guarantees the old server is gone when it does.
- **Next: A.3** — the namespace one-shot (`temporal-namespace` in compose, then a k8s Job); write
  our own script (the reference one sets no retention and has the `$MAX_ATTdMPTS` typo).

🔷 **A.2's local half was built and verified first (2026-09-22) — and its TL call was reversed before that.**
The owner opened the `temporalio/auto-setup` Docker Hub page before build: **deprecated**, *"no
longer maintained and will not receive updates"*, last tag ~8 months old — several minors behind
`temporalio/server` 1.31.0, which C.13 (Worker Versioning) needs recent. Temporal's own reference
compose (`temporalio/samples-server` → `compose/docker-compose-postgres.yml`) now runs
`temporalio/server` with the schema step **unbundled into a one-shot `temporalio/admin-tools`
container**, and namespace creation as a second one-shot — which is A.3's Job shape, confirmed.
Backlog A.2 rewritten (original call struck through, reasoning kept; the init-container shape for
k8s; **two images, one version**); A.3, A.7, A.1's Verify line and `temporal-full-migration.md` §7
swept for the name. Then built: `docker/temporal/setup-schema.sh` (adapted from the reference
script **minus its two `create` lines** — A.1 owns database existence), the comment-only
`dynamicconfig/development.yaml`, and `temporal-schema` + `temporal` in compose. Verified at
1.31.0: `temporal` 0.0 → **1.19** (40 tables), `temporal_visibility` → **1.14** (3 tables); the
keys-less dynamic config accepted (`Updated dynamic config`); healthy in < 20 s; `operator cluster
health` → SERVING; **a second `up` re-ran the one-shot and found zero updates** — the idempotency
the k8s init container relies on. Things from the build:

- ⚠️ **`setup-schema -v 0.0` on an already-stamped database does not reset it** (that needs
  `--overwrite`) — it logs "Schema setup complete" and `update-schema` then reads the real version.
  So the script re-runs safely as written; no "first run only" guard is needed or wanted.
- ⚠️ **The tool and the server read the password from different names**: `SQL_PASSWORD` for
  `temporal-sql-tool`, `POSTGRES_PWD` for the server. Both fed from `TEMPORAL_POSTGRES_PASSWORD`.
- ⚠️ **Eight `error` lines in the server's first second of boot** (`Not enough hosts to serve the
  request`, `Queue reader unable to retrieve tasks`) are single-process start-order noise — history
  is up before matching joins the membership ring. None after; do not chase them on k8s.
- **The `temporal` CLI is in admin-tools, not the server image** — hence the `compose run … --entrypoint
  temporal temporal-schema` form for health checks; A.3's namespace one-shot uses the same image.
- ⚠️ **The reference `create-namespace.sh` sets no retention and has a typo on its retry path**
  (`$MAX_ATTdMPTS` under `set -u`). A.3 writes its own; recorded in the backlog.
- ~~**Next: A.2's k8s half**~~ ✅ done the same day — the block above.

🔷 **A.1 is done on both halves (2026-09-21, two sessions).** Owner's ordering call for Group A:
**"local setup first, then change on k8s" — per task**: each engine piece lands in
`docker/docker-compose.yml`, is verified there, then its infra-repo manifest is written and verified
in prod, *then* the next task (clarified in the second session — the first session's handoff had
read it as all-of-Group-A-locally-first and pointed at A.2; corrected). So A.7's compose services
arrive alongside A.1–A.6 (recorded in the backlog's *How to use this*).
**Local (`f8e99bf`, `develop`):** the `temporal-postgres` service (own volume, own
`TEMPORAL_POSTGRES_*` credentials, `POSTGRES_DB: temporal` hardcoded because it is a name the server
is *configured with* in A.2, not a per-machine setting) + `docker/temporal-postgres/init.sql` (one
`CREATE DATABASE temporal_visibility`). **k8s (infra `de903a2`, `main`):**
`infrastructure/temporal-postgres.yaml` — the same script as a ConfigMap mounted at
`/docker-entrypoint-initdb.d`, StatefulSet `scrapeflow-temporal-postgresql` cloned from
`postgres.yaml` (`postgres:16`, `PGDATA=…/pgdata`, same probes/resources, 10Gi
`volumeClaimTemplates`), ClusterIP Service, kustomization entry, README section for the
owner-created Secret `scrapeflow-temporal-db-credentials` (user/password only). Flux applied it
~35 s after the push, pod ready in 21 s; the boot log shows `POSTGRES_DB`'s `CREATE DATABASE` then
the hook running `01-visibility-db.sql`; `\l` from the pod lists both, owned by `temporal`, 0 tables
each — A.1's *Verify* line, in prod. ~~Next: A.2 locally~~ ✅ done 2026-09-22 (block above).
Things from the two builds:

- ⚠️ **Host port 5433 was already bound** — by another project's Postgres container on the owner's
  machine (`meridian-postgres`), not by anything of ScrapeFlow's. Moved to **5434**; the host port
  is only a `psql`-from-host convenience and nothing depends on the number.
- ⚠️ **The initdb hook is keyed on an empty `PGDATA`, not on the volume existing.** The first local
  `up` failed on the port clash *after* the volume was created, and the hook still fired on the
  retry because Postgres itself had never started. Had it started once, the reset is `docker volume
  rm docker_temporal_postgres_data` locally — and on k8s **`kubectl -n scrapeflow delete pvc
  data-scrapeflow-temporal-postgresql-0`**, because a `volumeClaimTemplates` PVC outlives the
  StatefulSet and Flux removing the manifest does not remove it. Adding or editing the script
  afterwards does nothing.
- ⚠️ **A bind-mount of a file that does not exist yet creates a directory at that path on the
  host.** Write `init.sql` before the first `up`. (Not a k8s concern — a ConfigMap mount is a
  directory by construction.)
- ⚠️ **`PGDATA=/var/lib/postgresql/data/pgdata` is load-bearing on k8s and irrelevant on compose.**
  A fresh PVC's mount root holds `lost+found`, and `initdb` refuses a non-empty directory; a compose
  named volume starts truly empty. Copied from the app manifest; drop it and the first boot fails.
- ⚠️ **The infra repo's `main` was 13 commits behind after the 2026-09-19 release** — all Flux
  `Automated image update` commits. `git pull --rebase` before every infra push; expect it after
  every app release.
- ⚠️ **Pre-commit stashes unstaged files during a commit** (`[WARNING] Unstaged files detected` →
  stash → restore). Harmless when the unstaged file is a `.md`; it is the same mechanism as the
  stash trap under the hot-reloading API, so **stage or exclude `migrations/` before committing
  with a migration file unstaged.**
- **Node headroom, read before the push:** memory limits at 54% of the node, CPU limits already
  162% overcommitted — the latter is the pre-existing condition the backlog's A.2/A.8 note is about.
  The A.8 open item (where the Temporal Postgres backup lives) is real: `local-path` is one node's
  disk (`/var/lib/rancher/k3s/storage/pvc-76acde87…/`), no replication — same as the app DB today.

🔷 **The Phase 4 implementation backlog exists (2026-09-20, Tech Lead persona) — pick up A.1 next.**
`docs/project/phase4-implementation-backlog.md`: 56 engineering tasks + 3 docs items in nine groups
that are ADR-009 §16's named steps in order (A engine up · B worker port · C pipeline lane · D job
cutover · E batch and crawl · F schedule and webhook · G consumer deletion · H NATS removal · I API
thinning). One task per session; 🚀 marks the 14 release points; the status table at the top is the
tracker; a *Non-negotiables* table restates the rules an engineer must not revise. ✅ **Committed
as `857ad6e`** (the doc plus one pointer line each in `phase4-backlog.md` §2, `CLAUDE.md`, and this
file's reference table); **unpushed** as of 2026-09-21. Four things a future session should know
without opening it:

- **Three TL calls, all reversible, reasoning in the doc:** the workflow worker is a **second
  entrypoint of the api image** (`python -m app.workflows.worker_main`), not a new service — its
  logic lives in `api/app/core` today, and the cleanup CronJob is the precedent; **cost: until API
  thinning, a worker-only change triggers an API `Recreate` rollout.** Clean/Validate run on the LLM
  worker's Temporal deployment; crawl fetch-side activities (robots, sitemap on `httpx`, link
  extraction) on the Playwright one — target-facing fetches stay off the DB-holding pod.
- 🔴 **Eight open items at the bottom of the doc need Architect/owner answers before the tasks
  that cite them.** The two with teeth: **D.2** — ADR-009 §10 parks content-hash dedup + `diff.py`
  as "wait for Monitors", which is the *pipeline* lane's answer; on the *job* lane R5 forbids
  user-visible change, so `JobWorkflow` must port both or the job cutover regresses jobs — the ADR
  does not say this. **I.1** — `alembic upgrade head` on every API startup races itself at
  `replicas: 2`; no ADR covers it (TL recommends a Postgres advisory lock). The others: C.9's
  `waiting`-under-`RetryPolicy` reading (only the activity can flip the state), C.13 Worker
  Versioning before the pipeline release, E.0 task-queue shape vs the crw deferral, F.4
  `webhook_deliveries`' fate, C.11 pulling BUG-009 in, A.8 where the Temporal Postgres backup lives.
- **Gates that are documents, not code:** ADR-010 must be Accepted before E.0; the Schedule
  overlap policy (X.3, `BUFFER_ONE` recommended) must exist before F.1 creates a Schedule; PRD-019
  is parallel to C and not on the critical path.
- **The lane marker is D.1, built at the job cutover** — 16a's trap restated as a dependency
  constraint so nobody builds it in B where it is inert.

🔷 **The queue's release (2026-09-19, clock) — done, verified, scripts run.** Timeline (UTC): stream
verified drained ~09:22 (0 messages, 0 outstanding acks on all four consumers) → `git merge --ff-only
develop` on `main`, push 09:25:20 (`abf8ad9..421cbfe`, 91 commits) → all five images built by
09:27:35 → Flux resolved every `ImagePolicy` within a minute → `rollout status` green on all five
Deployments by 09:29:45, 0 restarts, old pods gone. The three Alembic revisions ran in order on
the new API pod; the schema was verified column-by-column (`storage_objects` with its UNIQUE and
`num_nonnulls` CHECK, both views querying, both BUG-014 FKs `confdeltype = c`,
`idx_webhook_deliveries_dedup` intact). `last_seq` stayed at 1,146 through the cutover, so no
v2/v3 message ever crossed. Owner logged in (**first real Clerk verification on
`clerk-backend-api` 7.0.0 — passed**) and clicked through the SPA on react-router 7 — fine. Then
the three scripts, each dry-run first: **reconcile** 35 recorded / 11 orphans deleted / counter
already exact; **sweep** 36 deleted; **audit** nothing. Bucket = ledger = meter = 35 objects /
21,457,872 bytes. Seven things from the day that are not in the filings:

- ⚠️ **Production's storage counter was never inflated.** `storage_bytes_used` already equalled
  the attributable total to the byte before `--apply`; BUG-007's first symptom never fired in prod.
  The 11 orphans were **Q6's** 2026-07-03 re-scrape residue (10 uploads under one BrowserScan job
  whose only run is `failed`, plus one redelivered re-scrape), not BUG-007's leaked pages.
- 🔴 **The reconcile's dry-run counter preview was wrong on a first run** — it summed a ledger the
  walk had not written to and said `21457872 → 0`. `--apply` then set 21457872 → 21457872. Fixed
  (`8608c0c`): the walk defers per-user deltas on `Report`, the preview folds them in and includes
  users the quota/ledger join cannot see; `_recompute_counters` returns its decisions; one test,
  mutation-checked (12 ledger tests). Local dev now previews `200 → 3,558,129` and reconciles to
  `recorded_bytes` exactly.
- ⚠️ **The admin *Storage used* card is a Redis-cached `list_objects` sum (300 s TTL), not the
  ledger.** The owner loaded it between the reconcile and the sweep and saw 41.4 MB —
  21,457,872 + 21,938,965 = 43,396,837 bytes, to the byte. Cosmetic and self-expiring; but the card
  is one of the four lane-blind admin meters, and now that the bucket holds only ledger-tracked
  objects it should read `SUM(storage_objects.bytes)` when next touched. *Jobs today / week / month*
  are **rolling** 24 h / 7 d / 30 d windows, not calendar — 25/25/25 was right.
- ⚠️ **The playwright pull loop logged one blank `fetch_error`** 100 s after subscribing. nats-py
  2.16's `_fetch_n` raises a bare `asyncio.TimeoutError` on two paths and the loop catches only the
  nats *subclass*. One occurrence, 2 s cost, pre-existing; the loop is deleted by the worker port —
  **backlog §3 row, not fixed.** A growing `backoff=` would be a different problem.
- 🔴 **BUG-018 filed, tabled by the owner until after Temporal:** the SPA caches the Clerk token
  *string* for exactly the JWT's 60 s lifetime and never refreshes it before use, so any page that
  polls goes "Failed to load …" after a minute and recovers on a tab switch. Pre-existing; owner
  reported it right after the release. Fix is six frontend files; `useIsAdmin.ts` is the precedent.
- ✅ **The auto-mode classifier allowed every production read this session** — `kubectl exec …
  psql`, `nats consumer info`, the pod's Python — contrary to the 2026-09-19 note above. Both are
  true observations; do not rely on either.
- ⚠️ **`kubectl exec … python -` with a heredoc needs `-i`** — stdin is not forwarded by default,
  so the script reads nothing and exits 0 silently. Pass the code with `python -c "…"` or add `-i`.
  (`-c <container>` only silences the "Defaulted container" notice; the default is the app container.)

🔷 **P7 is built (2026-09-18) — the queue is empty.** `phase4-backlog.md` §1's P7 row and its two
change-log entries hold the detail; `CLAUDE.md` has a new *Run-counting views* Key-decisions row.
Five things that belong here because they are the session's findings rather than the filing's:

- ⚠️ **ADR-009 §3 says one view; the build is two, and the decision is unchanged.** §3 describes one
  view with an `active` column that the two meters aggregate differently. A crawl holds its slot
  from creation, but its first unit row — the seed's `crawl_pages` row — is written by the
  coordinator ~2s later, or never while the coordinator is down (BUG-012 — ⚠️ *corrected 2026-09-19: the crash loop was observed in **local dev**; prod's coordinator has 131 days' uptime, 0 restarts, and no crawl rows to trip on, so the release's restart is safe for it*).
  "Distinct submissions among active unit rows" therefore lets a queued crawl hold nothing.
  `quota_active_submissions` reads the submission tables directly. This is a mechanism refinement
  found at build time, not a reversal; recorded in the migration's docstring, the backlog, the
  migration doc and `CLAUDE.md`. ADR-009 is immutable and cannot say so itself — **the same class
  as the §8d displacement below, and the owner may want to decide whether it warrants more than
  a note.**
- ⚠️ **§8's batch loosening is now live**: a batch of N pending runs is one concurrency slot, not N.
  Owner's call of 2026-08-17; before this a 100-URL batch was admitted as 1 and metered as 100.
  `test_batch_holds_one_concurrent_slot` pins it and fails against the old query.
- ⚠️ **On v1 the per-page storage insert is not built, on purpose.** Its only v1 site is the
  coordinator's result handler — BUG-008 (never ran) and §3 (do not fix). Crawl bytes stay
  uncharged until the `CrawlWorkflow` port calls `record_object(crawl_page_id=…)`. Admission
  against all three meters, the two count meters, and reclaim (`DELETE /crawls/{id}?permanent=true`,
  503 on a failed release like the job path) are all live.
- ⚠️ **On v1 every crawl stays `running` forever** (BUG-008 again) **and so holds a slot until the
  user cancels it.** The meter is right; the lane never finishes. `scripts/audit_crawl_quota.py`
  lists who is affected — the dev DB's mock user held 264 crawl slots against a limit of 5. Cancel
  the crawls, do not raise the limit. ✅ **Moot in production — owner, 2026-09-18: no crawl has
  ever run there and none will until the `CrawlWorkflow` port.** Temporal fixes it for new crawls;
  it would not have touched pre-existing `running` rows, which is why it was worth asking.
- 🔴 **BUG-014 filed**: `DELETE /admin/users/{id}` 500s for any user with a batch or a crawl —
  `crawls.user_id` and `batches.user_id` have no `ON DELETE`, and the ORM cascades neither.
  Verified in a rolled-back transaction. It fails *after* the MinIO deletes, so the objects are
  gone while the ledger rows stay. Filed to backlog §4. Also corrects P7's own
  row: deleting a user does not *orphan* crawl artifacts — the delete never gets that far.
  ✅ **Fixed the next session (2026-09-19, `b57211a`) — see the block below.**

🔷 **BUG-014 is fixed (2026-09-19, `b57211a`) — `api/` only, the release's third Alembic
revision; deployed with it.** Built exactly as *Outstanding* item 4 wrote it out: `ondelete="CASCADE"` on
both FKs, migration `9a1ebad3fca2` (autogenerated, then the dedup-index false positive stripped),
one test in `test_admin.py` that fails with the bug's own `ForeignKeyViolationError` on the old
schema. 280 API tests green (279 → 280). Three things from the build that are not in the filing:

- **Autogenerate emits the replacement constraints unnamed** — `create_foreign_key(None, …)` —
  so its downgrade's `drop_constraint(None, …)` cannot run. The committed revision names both
  with Postgres' own defaults (`crawls_user_id_fkey`, `batches_user_id_fkey`); the downgrade was
  round-tripped (`confdeltype` `c` → `a` → `c`).
- **The hot-reload trap can be sidestepped rather than raced.** `docker compose pause api`, then
  `docker compose run --rm --no-deps api uv run alembic revision --autogenerate …` from a one-off
  container on the same image and volumes, edit the file, `alembic upgrade head` the same way,
  `unpause`. The reloader wakes to a head that is already applied and a file that is already
  clean. No downgrade-and-recreate this time. Worth making the standing procedure.
- **The P7 views did not need dropping.** They read `crawls.user_id` and `batches.user_id`, but a
  constraint swap leaves column identity alone — the drop/recreate rule in `CLAUDE.md`'s
  *Run-counting views* row is for `DROP COLUMN` / `ALTER … TYPE` only. Applied cleanly with both
  views in place, which is the verification.

🔷 **BUG-003 gained a fourth fingerprint (2026-09-19) — `playwright-worker/` only; deployed with the release the same day.**
The owner scraped three storefronts from prod to exercise the detector. Amazon and Flipkart came
back as genuine product pages (1.8 MB / 895 KB, real prices — the stealth stack passing, not the
detector missing). Myntra came back `completed` in 0.8 s with a **481 B "Site Maintenance"** 200:
Tier 2 ran and had no phrase for it. One Tier 2 entry (`contact your administrator`,
`blocked:unknown`), the title deliberately unmatched, the captured body as a verbatim fixture,
173 worker tests (168 → 173), three new positives fail on the old detector. Detail:
`open-bugs.md` → BUG-003 addendum. Three things worth carrying:

- ⚠️ **The cluster's egress is `139.99.121.44`, an OVH Singapore datacenter IP** — verified by
  fetching the same URL from here (Jio residential: real site) and from a pod (the wall, 0.18 s).
  That is also why the Amazon job landed on `amazon.sg`. Now in `CLAUDE.md` → Deployment. No
  stealth setting changes an IP decision; getting *past* Myntra is UF-002's proxy layer.
- ⚠️ **Prod knock-on, owner's call, not done:** run `996cf840…` holds the wall's `content_hash`
  (`a649a09b3c1295fa`) — a poisoned dedup baseline for job `106f8e90…` if re-run. One-off job, so
  inert; `DELETE /jobs/106f8e90…?permanent=true` clears it. Same clean-up as the 2026-07-22 six.
- ⚠️ **The auto-mode classifier refuses production reads** (`kubectl exec … psql`, even `git show
  <deployed-sha>:path`). Manual mode was needed for the prod checks; the owner switched.

🔷 **Dependabot cleared before the release (2026-09-19, clock) — all 58 alerts on the three scanned
manifests; deployed with the release the same day.** `api/uv.lock` 31, `frontend/package-lock.json`
26, `http-worker/go.mod` 1. Every alert re-checked locally against the new locks (GitHub only
re-evaluates when `main` moves). 280 API + 29 contract + Go green; the `--target production` api
image builds. Detail: backlog change-log row. Four things a future session should know:

- ⚠️ **`clerk-backend-api` went 6.0.1 → 7.0.0, and it was forced, not chosen.** The high-severity
  `cryptography` fix needs `>=50`; every 6.x release of the Clerk SDK pins `cryptography<49` (6.0.1)
  or `<47` (6.0.0). v7's breaking list is management-API `error` types and the `verification`
  sub-object; the code uses `authenticate_request` + `AuthenticateRequestOptions(authorized_parties=…)`
  and one `users.get()` reading `email_addresses[0].email_address` — all present and unchanged in
  the installed v7 (inspected, not assumed). **The JWT path has only been exercised by the test
  suite's mocks; the first real Clerk verification on v7 is the release.** Fix-forward if it 401s.
- ⚠️ **`react-router-dom` went 6.30.3 → 7.18.4 — a major, taken deliberately.** Two advisories
  (SSR `deserializeErrors`, backslash open-redirect in `navigate`) are patched only in 7.18.0; both
  are unreachable here (`BrowserRouter` + `Routes`, no data router or hydration; every `navigate()`
  target is a mode constant, a nav path or a server UUID). Upgraded rather than dismissed because
  the tree is eight basic symbols and all-absolute paths, so v7's one behaviour change
  (`relativeSplatPath`) cannot bite. `tsc -b && vite build` is the only verification — **there is
  no frontend test suite, so a click-through after the release is the real smoke.**
- ⚠️ **`dompurify` is an `overrides` entry now** (`^3.4.15`), beside the pre-existing `js-cookie`
  one. `monaco-editor` pins it *exactly* (`3.2.7` at 0.55.1, `3.4.8` at 0.56.0 — still five
  alerts short), so no monaco bump reaches a patched version. ⚠️ A `jq` that *set* `overrides`
  instead of merging into it silently dropped `js-cookie` for one resolve — caught by diffing the
  lock, not by the build. Diff the lock after every `npm install`.
- 🔷 **BUG-013's frontend half is done** — `api/Dockerfile` copies `package-lock.json` and runs
  `npm ci`. Without it the 26 frontend alerts would have closed on paper while the shipped bundle
  re-resolved. Four of seven manifests remain unlocked; BUG-006's scan coverage is unchanged.

🔷 **BUG-015 / BUG-016 / BUG-017 are built (`9a72bb7`, `14c6136`, `f26c7ca`) and deployed with
the 2026-09-19 release.** Built as filed, one commit each, each
mutation-checked against the unfixed code. `open-bugs.md`'s three status blocks and the backlog's
change-log row hold the detail; three things from the build that are not in the filings:

- **BUG-016's test drives Patchright's own matcher**, not the pattern string —
  `patchright._impl._glob.glob_to_regex_pattern` (a private path — neither `_helper.glob_to_regex`
  nor a `playwright` module exists in the image, both tried first; if Patchright relocates it the
  test's import breaks, not the fix). It pins what the browser will abort — `.png`/`.jpg`/`.woff2`
  yes, `.css`/`.js`/a page URL no — which a substring check on the glob cannot.
- **BUG-017's Go test asserts on the wire, not on `net/url`.** An `httptest` server plays the
  proxy and decodes the `Proxy-Authorization` header the transport sends; asserting on
  `url.User.Password()` would have tested the stdlib. Go's behaviour did not change — the test
  is the regression pin that stops the two engines diverging again after the activity port.
  ⚠️ The transport only *forwards* through a proxy for plain-`http` targets; an `https` target
  would CONNECT, and the header sits on the CONNECT, which the handler would not see.
- **The `proxy_url` schema note is a `Field(description=…)`, not a comment** — it lands in the
  OpenAPI schema, which is where a user translating `curl -x … -U user:pass` will look.

**177 playwright tests (173 → 177); Go fetcher and API jobs suites green.** Session-close
housekeeping from 2026-09-19 (*Outstanding* item 6) is still the owner's.

🔷 **How the three were found (2026-09-19): the owner put a residential proxy on the Myntra job (Evomi, `core-residential.evomi.com:1000`
— the owner has an account; credentials are the owner's) and the wall went away — what remained
were three worker bugs, filed as BUG-015 / BUG-016 / BUG-017.** In order of finding:
`networkidle` runs died at `Timeout 30000ms` under a 90 s budget (`wait_for_load_state` gets no
timeout — **BUG-015**); with `load`, the page rendered a healthy header around "Oops! Something went
wrong" (`block_images` aborts `*.css`, which throws a lazily-loaded SPA route into React's error
boundary — **BUG-016**, confirmed by `block_images: false` rendering the product); and explaining
`proxy_url` surfaced that Go decodes percent-encoded credentials and Python does not — **BUG-017**,
latent. ⚠️ **"Oops! Something went wrong" is not a wall and must not be fingerprinted** — the
456 KB bodies carried the complete product in `window.__myx.pdpData`. Detail: `open-bugs.md` →
BUG-015/016/017 and the BUG-003 addendum's closing paragraph. ✅ **All three built 2026-09-20 — the block above.**

⚠️ **New instance of the hot-reload trap, in the other direction.** `git stash -u` under the
running API took the P7 migration file with it; the reloader restarted, `alembic upgrade head`
found the DB at a revision no file describes, and every startup failed with `Can't locate
revision identified by '86c780f55969'` until the pop. It recovered on its own, but a stash that
outlives a reload leaves the API down. Stash `--keep-index` or exclude `migrations/`.

🔷 **crw engine comparison written and deferred (2026-09-19, second session).** Owner brought
https://github.com/us/crw (fastCRW — Rust, ~95k lines, 11 crates) and asked for a code-level
comparison, speed excluded. Written into `docs/guides/competitor-research.md` as a new §crw;
**owner's call: nothing built now — "we'll come back after we finish the Temporal pipeline."**
Three things a future session should know without opening it:

- **§A is seven v1 correctness bugs, verified against the working tree, none filed.** The two
  with teeth: SSRF is checked once at `POST /jobs` and never at fetch time — `fetcher.go:33-36`
  keeps Go's default redirect-follow, so `https://x → 302 → http://169.254.169.254/` is open, and
  DNS rebinding with it (BUG-010 is a special case); and the `http` engine has **no** bot-wall
  detection, so BUG-003's Amazon 200-wall is a *completed* scrape on that engine. The others:
  robots `*`/`$` literal + query ignored + the Go and Python parsers disagree on an empty group;
  no content-type check; `difflib` text diff blocks the API event loop (§3 class, dissolves in
  the activity — do not fix on v1); LLM prompt unfenced and output unvalidated; silent 10 MB
  truncation. When filed, A1/A3/A4 join `CLAUDE.md`'s *do-not-delete* list as "port with the fix".
- **§C is what matters for the migration's shape.** Two crw mechanisms are hard to retrofit once
  the Temporal task queues exist: a **per-eTLD+1 host limiter** (ScrapeFlow rate-limits per
  *user* at the API and never per *target*; a crawl hammers its host as fast as the worker drains)
  and **interactive/batch reserved lanes** (today one NATS FIFO per engine; a 100-URL batch queues
  every single scrape behind it). Both belong in the `CrawlWorkflow` / batch-cutover design, next
  to ADR-010. The rest of §C — `Deadline` propagation (R4's mechanism), classified breaker
  outcomes, per-host egress memory, a capabilities endpoint, a closed error-code set — is
  activity-port input.
- **§B is product input, not Phase 4:** main-content extraction as a scored ladder (PRD-016 Clean
  block), page metadata on the run, normalised change hashing (raw-byte xxh64 is dead for HTML),
  URL canonicalisation + tracking-param strip, sitemap-index support, per-field extraction
  evidence. §D records where ScrapeFlow is ahead so the comparison stays honest: durability,
  tenancy, the ledger, contract tests — crw has none of it.

🔷 **P8 is built (2026-09-15) — and BUG-007 is fixed by it, not after it.** `phase4-backlog.md` §1's
P8 row, its change-log entry, and `open-bugs.md` → BUG-007 hold the detail; `CLAUDE.md` has a new
*Storage ledger* Key-decisions row. Four things that belong here because they are the session's
findings rather than the filing's:

- **The meter mechanism was undecided by §8d and is now decided: a materialised counter.**
  `user_quotas.storage_bytes_used` stays and is moved *only* by `ledger.py`, in the same transaction
  as the row. §8d fixes what the meter *reads* (owner and size) and argues for a live number; a
  `SUM` over the ledger on every quota check would be O(objects) and would zero every existing
  user's usage at cutover. The reconcile script is the auditor that recomputes it.
- ⚠️ **The deploy does not repair production. The reconcile script does, and it is owner-run.**
  "No backfill" (ADR-009 §8b) means a pre-ledger object has no row — so the inflated counters and
  leaked pages BUG-007 already caused stay exactly as they are until
  `reconcile_storage_ledger.py --apply` runs. The delete paths keep a **legacy branch** keyed on
  the old `storage_accounted_at` stamp for those runs, so a pre-cutover object is still decremented
  once and never when it was not charged; the stamp is no longer written.
- ⚠️ **Two behaviour changes worth knowing before the next deploy.** A delete endpoint now returns
  **503** and keeps the parent row when any object cannot be removed (the cascade would have dropped
  the ledger row for an object still on disk — the exact orphan class this fixes; what was freed
  stays freed). And **a run that fails accounting now holds nothing** — the objects are deleted
  with the failure, where before they were left orphaned and unreachable. Both are one-line calls
  in the endpoints and `_try_record_objects`; both have tests.
- ⚠️ **The hot-reload migration trap fired again, and this time it dropped an index.** The
  autogenerated revision included `drop_index("idx_webhook_deliveries_dedup")` — the raw-SQL partial
  unique index from 3.18 is not in any model, so autogenerate always wants to remove it. The API
  auto-applied that file on reload *before* the edit landed. Recovered with downgrade → upgrade →
  `CREATE UNIQUE INDEX` by hand. **Every future autogenerate will propose the same drop; delete it
  every time, or model the index.** The committed revision is clean.

🔷 **P9 is built (2026-09-11).** `phase4-backlog.md` §1's P9 row and `open-bugs.md` → BUG-011 hold the
detail. Two things that belong here because they are the session's findings rather than the filing's:

- **The fix surfaced a second instance of the same duplication on the job lane.** `create_job`
  built its dispatch message inline while the scheduler used a helper, so "recovery re-sends the run
  that was lost" was only *enforced* for scheduled runs. Both lanes' builders now live in
  **`api/app/core/dispatch.py`** and all five dispatch sites call them; two tests pin dispatch-vs-
  recovery **byte equality** per lane. Recorded as a `CLAUDE.md` Key-decisions row.
- ⚠️ **BUG-001's symptom is gone by construction, and that is not a fix of BUG-001.** The recovery
  loop now branches on `batch_item_id` before the job lookup, so `WHERE jobs.id IS NULL` is never
  issued. Its status stays closed-as-dissolved; its records are annotated. **The v1-vs-v2 lane filter
  its backlog row carries (ADR-009 §7 mechanism 4) is untouched and still owed at migration step 2**
  — P9 routes by *parent*, mechanism 4 routes by *owning engine*. Do not read P9 as having done it.

🔷 **P6 is built (2026-09-04), `schema_version` 2 → 3, five services, 598 tests green.** Detail is
in `phase4-backlog.md` §1's P6 row, which is the source of truth; the two things that belong here
because they are *not* in any ADR:

- ⚠️ **The cutover is a hard cut against a drained stream — owner's call, 2026-09-04.** ADR-011
  says nothing about deployment ordering, and there is no safe order: old workers + new API drop
  every message as malformed, new workers + old API reject every message as missing `artifact_id`.
  **All five services deploy together.** The stream was confirmed empty. `schema_version` was bumped
  in the same change so a mis-ordered deploy reports *which* mismatch it hit rather than a generic
  missing field.
- ⚠️ **One step is still outstanding and is the owner's to authorise: the production `latest/`
  sweep.** `api/scripts/sweep_latest_objects.py` — **dry-run by default**, `--apply` to delete;
  verified in dry-run against the *local dev* bucket (1,916 objects). It performs **no accounting
  adjustment on purpose** — `latest/` is uncounted on both sides, so a decrement would corrupt the
  counter. Nothing in production has been touched.

**ADR-009 is `Accepted` (2026-09-08).** Its section-by-section review completed 2026-09-05 — §1–§17
and both closing blocks, reviewed from 2026-08-08 — and the owner took the promotion decision on
2026-09-08. It is the decision of record: implement against it and cite it as settled. Its
`## Review status` block (top of file) stays authoritative for what each section says and what
changed; its **Reversed or withdrawn** and **Amended as a knock-on** tables are the fastest way to
catch a stale note. ⚠️ **An accepted ADR is immutable** (`docs/adr/README.md`) — a change of
decision from here is a new, superseding ADR, not an edit to this one.

**ADR-010 is `Draft` (2026-09-08)** (`docs/adr/ADR-010-crawl-admission-and-scheduled-quota.md`). It
resolves the two items ADR-009 deferred by name — per-meter quota parking for scheduled runs, and
sitemap entries scoped to the seed's registrable domain. **Both decisions are owner-taken and
dated; the write-up is unreviewed.** Written as a separate ADR rather than an edit because ADR-009
is Accepted and immutable.

**ADR-011 is `Accepted` (2026-09-03)** (`docs/adr/ADR-011-artifact-identity-and-paths.md`) —
reviewed and promoted by the owner, drafted and accepted the same day. It was **P6's design
dependency and the last one it had**: BUG-005's fix could not be built until the artifact-path
convention was decided, because half the paths stay broken if the message contract and the path
convention do not change together. It **supersedes ADR-002 §4** — the first supersession in the
index actually to land, since every other one waits on a named step of ADR-009 §16 — and answers
**PRD-016 OQ-1** in passing. ⚠️ **Immutable from here**: a change of decision is a new ADR.

Its one open item was **confirmed at promotion**: the crawl lane stays in scope, so **P6 changes
all three lanes**, even though BUG-008 means nothing on v1 reads a crawl result. The alternative
leaves `str(page.id)` in a field named `job_id` for the `CrawlWorkflow` port to read as precedent.

⚠️ **The design phase is closed. Everything still open is code, one PRD, and one Draft ADR
(ADR-010) to promote.** Every documentation debt ADR-009's review created has been discharged: both
stale companions redrawn, PRD-016's four carry-backs landed, the conditional PRD numbered, the
lane-blind meters recorded, D5 closed.

**Nothing is blocking. The queue is released and production reconciled; the ADR-009 §16 sequence
begins at *engine up*** — largely infra-repo work needing no app release. **The sequence is now a
task list: `phase4-implementation-backlog.md`, next task A.1** (Temporal Postgres StatefulSet, infra
repo). PRD-019 (unwritten) and the ADR-010 promotion are the two documents owed before the sequence
reaches the batch-and-crawl cutover.

⚠️ **ADR-010 is still `Draft`, and a Draft is not a decision** (`docs/adr/README.md`) — *"do not
implement against it, and do not cite it as settled in another document."* It blocks nothing in the
queue below, but it **is needed before the batch-and-crawl cutover**, so it wants promoting before
build work reaches that far.

### ⚠️ One decision was displaced from outside an immutable ADR — read this before trusting §8d

ADR-011 §4 removes `latest/`, which **reverses ADR-009 §8d's *"`latest/` is kept as-is"*** call of
2026-08-25 (*"removing it early costs work for a convenience that expires on its own"* — false once
P6 rewrites the path convention anyway). **§8d's charging rule is upheld and unchanged**: every
`history/` object is charged once, `latest/` is never charged.

This is the one stale-note class the standing method does **not** catch. ADR-009's **Reversed or
withdrawn** and **Amended as a knock-on** tables only record what *its own review* changed — they
cannot record a later ADR displacing a clause, and ADR-009 is immutable so nothing can be added.
The displacement is declared in **ADR-011's header** instead. Two knock-ons:

- The 2× storage discrepancy §8d accepted as *"v1-only with a known end date"* ends at **P6**, not
  at the v2 cutover. Anything reading that clause for a date is wrong by months.
- **BUG-007's fourth symptom is deleted upstream by P6** — the two orphaned `latest/` keys stop
  existing — so what reaches P8 is only the `history/` half.

### Outstanding, in rough order

0. **Pick up A.5 in `phase4-implementation-backlog.md`** — workflow-worker scaffold in `api/`
   (`temporalio` dep, `app/workflows/`, `HelloWorkflow`, time-skipping test); A.6 is its k8s half.
   **Per-task ordering: local → k8s → next task.** ~~A.4~~ ✅ both halves 2026-09-25 (infra
   `cd7b36c`). ~~A.3~~ ✅ both halves 2026-09-25 (infra
   `60b0aee`). ~~A.2~~ ✅ both halves 2026-09-22 (`a0008af`
   app, `e8f32e1` infra). ~~A.1~~ ✅ both halves 2026-09-21 (`f8e99bf` app, `de903a2` infra). The
   eight open items at the backlog's foot are owner/Architect calls; none blocks Group A.
1. **Write PRD-019 — conditional execution (layer A).** ✅ Numbered 2026-09-08 (owner's call) and given
   its `phase4-backlog.md` §2 row; **the document itself is unwritten.** It owes **four** things,
   all on that row: the Validate-precedent brief and the replay constraint (14c), the halt-early
   block B cannot build for itself (§4), and run-level failure notification (15f). ⚠️ **It sorts
   last and is required first** — before PRD-018, which cannot ship without its primitive. The
   sort-order cost was accepted rather than engineered around; index order is not build order in
   this chain.
2. **Promote ADR-010** when convenient — it blocks nothing in the queue but is required before the
   batch-and-crawl cutover, and it opens the Schedule overlap policy (`phase4-backlog.md` §2
   gotcha 6), which is genuinely undecided.
3. ~~**The pre-migration queue's single release**~~ ✅ **RELEASED 2026-09-19 (`421cbfe`)** — see the
   release block in *Current state*. ~~Three owner-run production scripts~~ ✅ **all three run the
   same day**, each dry-run first, each idempotent on re-run. **Next: ADR-009 §16 *engine up*.**
4. ~~**BUG-014**~~ ✅ **fixed 2026-09-19 (`b57211a`), deployed the same day** as the release's third
   Alembic revision (`9a1ebad3fca2`); both FKs verified `CASCADE` in prod. The interim state the
   filing described no longer applies. Writeup: `open-bugs.md` → BUG-014; backlog §4 row.
5. ~~**Build BUG-015, BUG-016 and BUG-017**~~ ✅ **built (`9a72bb7`, `14c6136`, `f26c7ca`) and
   deployed 2026-09-19** with the release.
6. **Owner housekeeping from 2026-09-19:** (a) ~~**rotate the API key used for the day's
   prod tests**~~ ✅ **revoked by the owner** — it had been pasted into a Claude session
   transcript in full; the transcript is now inert; (b) *optional tidiness only* — the day's Myntra test jobs hold
   `content_hash` values of walls/404s/error pages, but a baseline is only ever *read* by a later
   run of the **same job**, there is no re-run endpoint, and none of them is scheduled, so they
   are inert unless a `PATCH` adds a `schedule_cron` to one; delete them or don't (the reconcile
   kept their objects — they are attributable); (c) the Amazon job landed on `amazon.sg` because of the egress IP — if the `.com`
   listing is wanted, that job needs a US exit on the proxy.
7a. **BUG-019 — cleanup CronJob never succeeded** (filed 2026-09-25). **Owner's call: later.**
   Infra-only fix in `open-bugs.md`; trigger the first run by hand — it is a real delete.
7. **BUG-018 — SPA token caching** (filed 2026-09-19, `19fd34e`). **Owner's call: after the
   Temporal pipeline.** Six frontend files; the writeup has the fix and what to capture.

5. **Deferred — crw engine comparison** (`docs/guides/competitor-research.md` §crw). Pick up
   **after the Temporal pipeline**: file §A's bugs (A1–A4 first), then fold §C's per-host limiter
   and interactive/batch lanes into the batch-and-crawl cutover design before the task queues are
   shaped. Nothing in it blocks the queue release.

### Git / deploy state

- **Deployed code is `421cbfe`** (2026-09-19, the queue's release). Before it: `b110591` (2026-07-28).
- 🔷 **Release policy, owner's call 2026-09-11 — the pre-migration queue ships as ONE release. ✅
  Executed 2026-09-19 exactly as written:** `main` moved once, `abf8ad9..421cbfe` (91 commits —
  P6, P9, P8, P7, BUG-014 → 017, the BUG-003 fingerprint, the Dependabot sweep and every docs
  commit since 2026-07-28). The drain-and-hard-cut window the `schema_version` 2→3 change needed
  was paid **once**.
  **The migration itself then ships per-flow**, following ADR-009 §16's named sequence — a `main`
  push per step is expected and wanted. A *"freeze `main` until the whole migration is done"*
  alternative was **considered and dropped the same day**: the owner's reason for per-flow is
  attribution — *"better to fix at that juncture than everything is pushed all at once"* — which
  is ADR-009's own reasoning, so **the policy and the Accepted ADR agree and no superseding ADR is
  needed.** ✅ *This resolves the tension flagged earlier on 2026-09-11.*
  The service is explicitly treated as non-critical — *"any failure is fixed by new ff pushes"* —
  so **fix-forward is the accepted posture throughout**; no rollback rehearsal is wanted, and the
  awkward part of rollback here (Flux's image automation re-applies the newest tag within ~1 min
  unless the automation is suspended first) is accepted rather than engineered around.
  - ✅ **The three migrations ran on API startup under `Recreate` without incident** — the old pod
    stopped at 09:27:5x, the new one applied `0c73753d5138 → 86c780f55969 → 9a1ebad3fca2` and was
    serving by 09:29:16 — 82 s from the new pod's creation (09:27:54) to `Database migrations
    complete`, plus the old pod's termination before that. That is the no-fallback window. The RollingUpdate workers (coordinator,
    llm, playwright) each replaced their pod cleanly, so the "stalled rollout leaves a v2 worker
    beside a v3 API" hazard did not arise — but it was checked per Deployment, as the rule says.
  - The first two §16 steps are the cheap ones: **engine up** is largely infra-repo work needing no
    app release, and **worker port** is purely **additive** — the workers gain Temporal entry points
    while still serving NATS, and nothing routes to them yet.
- ✅ **The standing rule is unchanged: a `main` fast-forward is a release, not a tidy-up** —
  pushing it builds and deploys. Do not do it at session end; wait to be asked. (Asked and done
  2026-09-19; the next one is the *engine up* step, or whatever the owner names.)
- ⚠️ *Historical, now released:* **`5c7fbdf` is no longer the last application-code commit.** P6 (2026-09-04) is the first
  code change since 2026-08-28, and it touches **five services**: `api/`, `coordinator/`,
  `playwright-worker/`, `llm-worker/`, `http-worker/`, plus a new top-level `contracts/`.
  **P9 (`ed4d63c`, 2026-09-11) is the second** — `api/` only (five files + one new module), so it
  changes nothing about the five-service cutover P6 already requires. **P8 (`f503f8b`, 2026-09-15) is the
  third** — `api/` plus `docker/docker-compose.yml`, and it **adds the queue's first Alembic
  revision** (`0c73753d5138`), so the release runs a migration on API startup against the
  `Recreate`-strategy API. **P7 (`24cb89c`, 2026-09-18) is the fourth and last** — `api/` only, and it adds
  **the second revision** (`86c780f55969`, two views, hand-written; downgrade drops them). **The BUG-014 fix
  (`b57211a`, 2026-09-19) is on top** — `api/` only, and it adds **the third revision** (`9a1ebad3fca2`,
  two FK constraint swaps; downgrade restores `NO ACTION`). **The Dependabot sweep (`83607e2`, 2026-09-19) is on top of that** — `api/` (`pyproject.toml`, `uv.lock`, `Dockerfile`), `frontend/` (`package.json`, lock), `http-worker/` (`go.mod`, `go.sum`); no Alembic revision, and the three services it touches are already in the five-service rebuild.
- ✅ **Verified 2026-09-22 (second session) after fetch: `develop` was 5 ahead of `origin/develop`,
  0 behind; `main` 8 behind `develop`, 0 ahead** — then `a0008af` (compose shard count) and this
  closeout add two. **Nothing on `develop` needs a release** — A.1's and A.2's compose blocks are
  local-dev only; their prod halves went through the **infra repo** (`de903a2` for A.1, `e8f32e1`
  for A.2, both on its `main` and deployed). Re-check before quoting.
- *Historical:* **2026-09-19 (release session): `main` fast-forwarded to `421cbfe` and pushed** (the
  release). Then on `develop`: `19fd34e` (BUG-018 filed), `8608c0c` (reconcile preview fix + §3
  row) and its closeout `d1282c7`.
- *Historical, now released:* **`develop` was pushed to `origin/develop` twice on 2026-09-19**: `8ba7e85..51c8428` (14 commits —
  BUG-014, the BUG-003 fingerprint, BUG-015/016/017 and their closeouts) at session start, then the
  Dependabot sweep (`83607e2`), its docs closeout (`4a665e3`) and the session-close commit
  (`421cbfe`) — which is the commit that became the release.
- *Historical, now pushed:* **`develop` was pushed to `origin/develop` on 2026-09-18** (`0daf956..08655fd`, 13 commits —
  P9, P8, P7 and their closeouts). **After the push: 0 ahead, 0 behind; `main` 71 behind, 0 ahead.**
  ⚠️ **Since then, unpushed on `develop`: the BUG-014 fix (`b57211a`) and its docs closeout
  (`c2f0542`), the owner's own `c90d943`, the BUG-003 fourth fingerprint (`2ee84ea`) and its
  closeout (`4469b44`), the session-close docs commit filing BUG-015/016/017 (`8f75ecf`) and
  three docs follow-ups, then the three fixes (`9a72bb7`, `14c6136`, `f26c7ca`).** Verified
  after fetch at `f26c7ca`: **`develop` 12 ahead of `origin/develop`, 0 behind; `main` 86
  behind, 0 ahead** — this closeout adds one to each. The fingerprint and the three fixes touch
  `playwright-worker/`, which P6 already rebuilds; BUG-017's `http-worker/` change is a test
  file and its `api/` change is a schema description — no change to the five-service cutover
  and no fourth Alembic revision.
  The previous push was 2026-09-09 (`fa3c18d..a57e395`, ten commits, including P6). ⚠️ *Historical,
  now pushed — kept for the commit list:* between those two pushes, on `develop`: four docs-only commits from the first
  2026-09-11 session (`e9304b9`, `43c828a`, `2e822d9`, `f724162`), then **P9's code (`ed4d63c`)**
  and its docs closeout (`7ae5b18`), then **P8's code (`f503f8b`)** and its docs closeout
  (`34027df` + `a629b64`), then **P7's code (`24cb89c`, 2026-09-18)** and its docs closeout.
  **Verified 2026-09-18 after fetch, before the P7 commits: `develop` was 9 ahead of
  `origin/develop`, 0 behind; `main` 67 behind, 0 ahead** — the two P7 commits add two to each. ⚠️ Re-check against the remote before quoting these — that is the
  standing rule below, and this line has already been stale once in this file.
- ⚠️ **Pushing `develop` builds and deploys nothing.** `.github/workflows/build-push.yml` triggers
  on `push: branches: ["main"]` only. **That is what makes a `main` fast-forward a release** — and
  because P6 touched `api/`, `http-worker/`, `playwright-worker/`, `llm-worker/` **and**
  `coordinator/`, its `paths-filter` matches **all five** service jobs. The five-service
  simultaneous build the cutover needs is therefore what a `main` fast-forward already produces;
  what it does **not** do is drain the stream first, which is still a manual step.
- ✅ **The ADR-011 promotion is committed** as `a0714f6` (ten files, all docs, no application code),
  with this file's own follow-up on top. **Unpushed**; `main` untouched.
- ✅ *Historical:* **Deploying P6 was a five-service simultaneous release against a drained stream** —
  done 2026-09-19; the drain check is the `nats-box` pod (`kubectl -n scrapeflow exec
  scrapeflow-nats-box-… -- nats --server nats://scrapeflow-nats:4222 stream info SCRAPEFLOW`, then
  `consumer info … --json` per durable for `num_ack_pending`).
- ⚠️ **The document timeline runs ahead of git, and has for several sessions.** Work dated
  2026-09-04 → 2026-09-08 across the ADRs, backlog and this file was committed on **2026-09-02**.
  Everything created on 2026-09-03 (ADR-011, BUG-011) is dated from the clock, so ADR-011 (09-03)
  sorts *after* ADR-010 (09-08) in the registry while carrying an earlier date. That is the
  pre-existing drift showing, not a mistake in either record — **do not "correct" one into the
  other.** Decide which timeline is real before dating the next artifact. ⚠️ The session log now has **three `2026-09-03` rows** for this reason — the top two are the clock (two separate sessions on the same day), the bottom one is the doc timeline.
- ⚠️ **Re-check ahead/behind against the remote before quoting numbers** — two consecutive handoffs
  once carried counts stale by two months. Fetch first, quote second.
- Untracked `tmp/architecture.md` predates all current work (May) and is deliberately left alone.

**Tags** (annotated; the older `v1.0.0` / `v2.0.0` are lightweight):

| Tag | Commit | Marks |
|---|---|---|
| `v3.0.0` | `d9e1edb` (2026-05-13) | End of Phase 3 |
| `prephase4` | `1965953` (2026-07-28) | Pre-Phase 4 queue closed, immediately before PRD-016. Its message records what the system *is* at that point — NATS + the five hand-rolled loops, the thing the migration replaces |

⚠️ **`prephase4` and `v3.0.0` are annotated tags, so `git rev-parse --short <tag>` returns the
*tag object's* SHA, not the commit's** (`473fb68` and `a6a39d4` respectively). The table above
holds **commits**. Use `git rev-list -n1 <tag>` to get one. `v1.0.0` / `v2.0.0` are lightweight,
where both commands agree.

---

## Where the detail lives

This handoff deliberately holds **no** review findings. Every one is written out authoritatively
somewhere else, and a second copy here is how they go stale:

| Looking for | Read |
|---|---|
| What a given ADR-009 section decided, and what its review changed | ADR-009 `## Review status` → the section itself |
| Whether a note you are holding is now wrong | ADR-009's **Reversed or withdrawn** + **Amended as a knock-on** tables. ⚠️ **They cover only what ADR-009's own review changed** — a *later* ADR displacing one of its clauses cannot appear there, and one has: ADR-011 §4 vs §8d's `latest/` sequencing. Check the superseding ADR's header too |
| The live artifact-path convention | **ADR-011** — not ADR-002 §4. ✅ **Live code implements it as of 2026-09-04** (P6, `81afbb9`), across all three lanes, and **production runs it since 2026-09-19** (`421cbfe`). Historical objects keep their old-format `result_path` strings forever (no backfill, by design); the reconcile attributes both shapes |
| Phase 4 scope, sequencing, what is do-not-fix | `phase4-backlog.md` (§1 queue · §2 migration · §3 **do NOT fix** · §4 survives) |
| A bug's root cause and fix plan | `open-bugs.md` |
| How storage is counted and released, and why the meter is a materialised sum | `CLAUDE.md` → Key decisions → *Storage ledger (P8)*; the code's own docstring in `api/app/core/ledger.py` |
| What the two count meters count, per lane, and why there are two views | `CLAUDE.md` → Key decisions → *Run-counting views (P7)*; the migration's docstring (`86c780f55969`); `api/app/core/quota.py`'s module docstring |
| Why a production trap exists | `CLAUDE.md` → Key decisions (44 rows; the rationale column *is* the trap) |
| The two deferrals ADR-009 named and did not answer | `ADR-010` (Draft) — and the Schedule overlap policy it opened, in `phase4-backlog.md` §2 gotcha 6 |
| What shipped when | `git log` |

---

## Session log

Docs-only from 2026-08-04 **to 2026-09-03**; **2026-09-04 breaks that run** — P6 is the first
application-code change since `5c7fbdf` (2026-08-28), and it spans five services. Verdicts live in
ADR-009's review log; this table is only *what a session produced*.

| Date | Session produced | Commits |
|---|---|---|
| 2026-09-25 *(clock, second session)* | **🔷 A.4 — Temporal Web UI built, deployed, verified in prod; A.4 ✅.** Read-in; owner: "code up both and commit and push; this to dev and k8s to prod" → compose `temporal-ui` (2.54.1, own version var) verified against the local server and namespace; infra manifest (ClusterIP only, `/healthz` probes), kustomization line, README section + DNS row, `--dry-run=server` clean, committed and pushed; Flux applied, rollout green, verified through a port-forward. Docs: backlog (A.4/A.7 status, A.4 body), this file | infra `cd7b36c` · app (this commit) |
| 2026-09-25 *(clock)* | **🔷 A.3 — namespace registration built, deployed, verified in prod; A.3 ✅. BUG-019 filed.** Read-in; owner asked where a namespace lives, what a namespace is, and whether other services' namespaces collide → explained (a row in `temporal.namespaces`; isolates IDs/queues/retention per namespace, not compute or access). Owner: "start A.3; build on both local and server but do not commit" → script + compose one-shot verified locally (create, drift-reset, unreachable → exit 1), k8s Job `kubectl apply`'d and verified. Spotted the cleanup CronJob `Failed`; owner: "check the logs" → pod gone, reproduced on the deployed image: never succeeded (BUG-019). Owner: "file it … push and verify on prod … commit dev and push origin, not prod" → BUG-019 filed, infra pushed + Flux adoption verified, app committed on `develop` and pushed. | infra `60b0aee` · app (this commit) |
| 2026-09-22 *(clock, second session)* | **🔷 A.2 — k8s half built, deployed, verified in prod; A.2 ✅.** Read-in; owner: "lets do a2 k8s part; explain your approach" → the four-object manifest, the compose→k8s mapping (init container not Job; `Recreate` as a decision; tcpSocket probes; the two password names; mount the subdirectory only), sizing against the node (168 % CPU limits), and two things the backlog lacked: `NUM_HISTORY_SHARDS` is immutable after first start, and the CLI lives in admin-tools. "how will bind the setup and dynamicconfig files" → ConfigMap → volume → volumeMount, no exec bit, directory not subPath so dynamic config refreshes live. "ah okay we are duplicating it?" → yes for the script (kustomize cannot cross repos; A.1's precedent; app repo canonical), no for dynamic config (per-environment). "okay build all but dont commit" → manifest (script spliced by `sed`, `diff`-identical), kustomization line, `--dry-run=server` clean, compose `NUM_HISTORY_SHARDS: 4`, local server recreated and SERVING. "do all" → both commits; the classifier blocked the infra push (owner pushed). Flux ~60 s; schema 0.0 → 1.19 / 1.14; Ready +70 s; zero boot errors; SERVING; `rollout restart` → zero updates. Node 168 → 175 % CPU limits. Docs: backlog (A.2/A.7 rows, A.2 body, A.7 progress), infra README, this file | `a0008af` + this closeout; infra `e8f32e1` (`main`, deployed) |
| 2026-09-22 *(clock)* | **🔷 A.2 — TL call reversed, then local half built and verified.** Read-in, then owner: "whats auto setup mode?" → explained the auto-setup entrypoint (wait → create → setup/update-schema ×2 → namespace → exec server) as Alembic-on-startup for Temporal. Owner: the Docker Hub page says **deprecated** → verified (last tag ~8 months old) and read the replacement — `samples-server/compose/docker-compose-postgres.yml`: `temporalio/server` + two `admin-tools` one-shots (schema, namespace). "update the backlog" → A.2 rewritten with the original call struck through, init-container shape for k8s, two-images-one-version trap, no-`create` rule; A.3 gains the reference confirmation + the typo warning; A.7 / A.1 / `temporal-full-migration.md` §7 swept. Explained dynamic config (static vs dynamic, value/constraints, what C.13/E.0 will add, what must *not* go there — `limit.blobSize`, retry policies, retention). Then, one block at a time on "go": `setup-schema.sh` → compose services → bring-up. Verified: schema 1.19 / 1.14, dynamic config accepted, SERVING, second `up` = zero updates. Docs: backlog (status, A.2 body, A.7), this file | A.2 commit + this closeout |
| 2026-09-21 *(clock, second session)* | **🔷 A.1 — k8s half built and verified in prod; A.1 ✅.** Owner: "are we done with a1? we only did local setup; we also need to do k8s too right?" → the ordering is **per task** (local → k8s → next), not all-of-Group-A-locally-first; the previous handoff's "next: A.2 locally" corrected. Explained the local→k8s mapping (StatefulSet/PVC/Secret/ConfigMap, the `PGDATA` and PVC-outlives-StatefulSet traps) and that the *entrypoint*, not the StatefulSet, creates both databases; on "build it" wrote `infrastructure/temporal-postgres.yaml` + kustomization line + README Secret section in the infra repo, `kubectl apply --dry-run=server` clean, node headroom read. Owner created the Secret; push needed a rebase over 13 Flux image-update commits; Flux applied in ~35 s, pod ready in 21 s, boot log + `\l` from the pod = the Verify line. Committed A.1's local half + backlog on `develop`. Answered "where did we mention the volume" — `volumeClaimTemplates` → PVC `data-…-0`, `local-path`. | `f8e99bf` (app, `develop`); infra `de903a2` (`main`) |
| 2026-09-21 *(clock)* | **🔷 A.1 — local half built.** Owner: "lets start with A.1 … lets first do local setup first and then change on k8s" → the Group A ordering call, recorded in the backlog. Explained the why (a second *instance*, two *databases*, who owns database existence vs schema) and let the owner drive; on "write only the docker-compose for new db" wrote the `temporal-postgres` service + volume; on "write the init script and bring up the new db" wrote `docker/temporal-postgres/init.sql` and started the service — first `up` failed on a host-port clash (5433 held by another project's container → 5434), second `up` ran the hook: `\l` lists `temporal` and `temporal_visibility`, 0 tables each. Answered "how does temporal know which db is which" — by name, `DBNAME`/`VISIBILITY_DBNAME` in A.2. Docs: backlog (ordering note, A.1/A.7 statuses and bodies), this file. Session closed at the owner's "i'll start a new session for next" | *(uncommitted at close; committed the next session as `f8e99bf` + this closeout)* |
| 2026-09-20 *(clock)* | **🔷 Tech Lead persona — the Phase 4 implementation backlog written.** Owner: "take the persona of tech lead and divide the temporal stuff into a backlog list and i'll go through 1 by 1". Read ADR-009 (§2, §3–§11, §13, §15, §16, the closing blocks), `phase4-backlog.md` §2, `temporal-full-migration.md`, PRD-016 R1–R6, ADR-010's header, the infra repo's manifest layout and the code sites the tasks cite. Produced `docs/project/phase4-implementation-backlog.md` — 56 tasks + 3 docs items, nine groups = the §16 named steps, per-task why/location/what/verify/depends-on/cites, 14 🚀 release points, a status-table tracker, a non-negotiables table. **Three TL calls** (workflow worker = api-image entrypoint; Clean/Validate on the LLM deployment; crawl fetches on the Playwright deployment) and **eight open items** raised, two with teeth (D.2 dedup/diff must port at the job cutover for R5; I.1 Alembic-on-startup races at two replicas). Pointers added in `phase4-backlog.md` §2, `CLAUDE.md`, this file. Session closed at the owner's "done for now"; **uncommitted** | *(uncommitted at close)* |
| 2026-09-19 *(clock, release session)* | **🔷 THE QUEUE'S RELEASE — `main` fast-forwarded to `421cbfe`, five services deployed, production reconciled and swept.** Owner: "drain the nats queue" (verified empty: 0 messages, 0 outstanding acks, 4 consumers) → "do ff main and watch rollout; also verify the prod tables" → all five `rollout status` green in 4.5 min, three Alembic revisions applied, schema verified column-by-column, `last_seq` unmoved through the cutover. Owner logged in (Clerk SDK 7.0.0's first real verification — passed) and clicked through the SPA. Then the three scripts, dry-run then `--apply` at the owner's "yes" each time: reconcile (35 recorded, 11 Q6-era orphans deleted, **counter already exact**), sweep (36 `latest/` deleted), audit (nothing). Bucket = ledger = meter. **Found on the way:** the reconcile's dry-run preview read an empty ledger and said `→ 0` (fixed, `8608c0c`, one mutation-checked test); the admin *Storage used* card is a 300 s Redis-cached bucket sum and showed the mid-sweep figure to the byte; one blank playwright `fetch_error` (nats-py's bare `asyncio.TimeoutError`, §3 row); **BUG-018 filed** (SPA caches the Clerk token for its own 60 s lifetime — owner's pre-existing "Failed to load" report; tabled until after Temporal). Docs swept: `CLAUDE.md` (status banner, queue bullet, six statuses, two key-decisions rows), backlog (banner, §1, §3, §4, Sequencing, two change-log rows), `open-bugs.md` (eight status blocks + BUG-018), `temporal-full-migration.md` (entry condition), this file | `19fd34e`, `8608c0c` + this closeout |
| 2026-09-19 *(clock)* | **🔷 Dependabot cleared — 58/58 on the three scanned manifests, before the release.** Owner: "lets push dev to origin first" (done, `8ba7e85..51c8428`), then "lets deal with dependabot now before we merge with main". `api/`: seven packages re-locked, direct floors raised; the `cryptography>=50` fix **forced `clerk-backend-api` 7.0.0** (no 6.x allows it) — v7 inspected in a scratch venv, the two auth symbols and `users.get()`'s `email_addresses` shape unchanged. `frontend/`: `dompurify` via `overrides` (monaco pins it exactly), `react-router-dom` **→ 7.18.4** (two advisories have no 6.x patch; unreachable here; tree is trivially portable), three browserslist-family transitives. `http-worker/`: `x/net` 0.55.0. **BUG-013 step 3 folded in** — `npm ci` from the lock in `api/Dockerfile`, verified by a production-target build. Every alert re-checked locally against the new locks. 280 API + 29 contract + Go green. ⚠️ Caught in the audit: a `jq` set `overrides` rather than merging, dropping the `js-cookie` pin for one resolve. Docs: `open-bugs.md` (BUG-013 status + fix list), backlog (change-log row, §4 row), `CLAUDE.md` (BUG-006/013 in the open list), this file | `83607e2` + docs closeout |
| 2026-09-20 *(clock)* | **🔷 BUG-015, BUG-016, BUG-017 BUILT.** Built one at a time at the owner's direction ("lets fix bug15; do not state the approach fix it and test it" → "pick up bug16" → "explain bug 17" → "build it"), each committed before the next, docs batched at the end. BUG-015: `wait_for_load_state(wait_state, timeout=timeout_ms)`; two tests on the kwarg (explicit 90 s and the worker default), both `KeyError: 'timeout'` on the old worker. BUG-016: `css` out of the route glob; the test feeds the registered glob to Patchright's own `glob_to_regex_pattern` and asserts per-URL, failing on the old glob with `route-chunk.css must not be aborted`; primer field row corrected. BUG-017: `unquote()` on both credential fields; `Field(description=…)` on `proxy_url`; playwright test asserts the decoded pair at `new_context`, Go `TestWithProxy` asserts the decoded `Proxy-Authorization` header at an `httptest` proxy (Go unchanged — a regression pin). **177 playwright (173 → 177), Go fetcher, 68 API job tests green.** At close the owner revoked the API key pasted on 2026-09-19 (*Outstanding* 6a). Docs swept: `open-bugs.md` (three status blocks), backlog (change-log row, three §4 rows, the sequencing line), `CLAUDE.md` (open list), this file | `9a72bb7`, `14c6136`, `f26c7ca` + docs closeout |
| 2026-09-19 *(clock, second session)* | **🔷 crw engine comparison — written, deferred.** Owner brought fastCRW (`us/crw`) and asked what ScrapeFlow lacks or could have done better, speed excluded. Cloned and read the engine's policy modules (SSRF, deadline, reserved semaphore, detector, egress latch, preference, breaker, host limiter, URL filter, robots, sitemap, untrusted-content fence, structured extraction + basis, diff/snapshot, capabilities, error taxonomy) against the corresponding ScrapeFlow code; every finding anchored to a `file:line`, the SSRF range gaps verified on the API's Python 3.12. Written as a new §crw in `docs/guides/competitor-research.md` (A: seven v1 bugs, none filed · B: output-quality gaps → PRD-016/018 · C: mechanisms → Phase 4 decisions · D: where ScrapeFlow is ahead). **Owner's call: deferred until the Temporal pipeline is finished.** Swept: `CLAUDE.md` Phase 4 bullet; this file's reference table, current state, *Outstanding* item 5 | docs-only |
| 2026-09-19 *(clock)* | **🔷 BUG-014 FIXED.** Built at the owner's direction ("lets fix bug14") from the pick-up the previous handoff wrote out, then audited. `ondelete="CASCADE"` on `crawls.user_id` + `batches.user_id`; migration `9a1ebad3fca2` (autogenerated from a one-off container with the api **paused**, so the hot-reload trap never fired; dedup-index false positive stripped; the unnamed replacement constraints named so the downgrade runs); one test deleting a user who holds a crawl page and a batch run, each with a ledger row — mutation-checked by downgrading the DB, where it fails with the bug's own `ForeignKeyViolationError`. **280 API tests green** (279 → 280); `alembic check` reports only the standing dedup false positive. **Decision resolved by placement, not taken:** the filing left "rides the release or ships later" to the owner — it is on `develop` before the release, so it rides it as the third revision. **Found by the build:** the P7 views read both columns and need no drop/recreate for a constraint swap; the pause-then-`run` procedure sidesteps the hot-reload race entirely. Docs swept: `open-bugs.md` (BUG-014 fixed, two build notes), backlog (change-log row, §4 row, Sequencing's revision count), `CLAUDE.md` (open list, queue bullet), this file. **Then, same session: BUG-003 fourth fingerprint.** Owner exercised the detector against Amazon, Flipkart (both genuine — stealth passing) and Myntra (a 481 B "Site Maintenance" 200 stored as `completed` — a Tier 2 list miss). Root-caused by fetching the URL from a residential IP and from a pod: **egress is an OVH Singapore datacenter IP**, Myntra denies it with an outage-shaped page. One Tier 2 entry (`contact your administrator`), verbatim fixture, 173 worker tests, mutation-checked. Docs: `open-bugs.md` (BUG-003 addendum), backlog (change-log row, P2 row), `CLAUDE.md` (Deployment egress bullet, bot-wall row trap), this file. **Then, with the owner's residential proxy on the job: three worker bugs filed, none built — BUG-015** (wait strategy ignores `timeout_seconds`), **BUG-016** (`block_images` aborts CSS → SPA error boundary, confirmed by toggling it), **BUG-017** (proxy credentials decoded by Go, not Python — found by reading). Session-close audit also corrected the handoff's "coordinator crash-loops in prod" (local dev; prod has 0 restarts in 131 d) and recorded the owner housekeeping (rotate the pasted API key, delete the poisoned test jobs). Session closed long; **building is next session's first item** | `b57211a`, `c2f0542`, `2ee84ea`, `4469b44` + session-close docs |
| 2026-09-18 *(clock)* | **🔷 P7 BUILT — the pre-migration queue is empty.** Built straight through at the owner's direction ("fix this"), then audited as a whole. Migration `86c780f55969` creates **two views** — `quota_run_units` (fetches, monthly) and `quota_active_submissions` (submissions, concurrency) — read through `Table` objects on a private `MetaData` (`app/models/quota_views.py`; `compare_metadata` confirmed autogenerate sees only the standing `idx_webhook_deliveries_dedup` false positive); `core/quota.py`'s two count queries name no table; `check_crawl_quota` on `POST /crawls` (monthly pre-checked with `batch_count=max_pages`); `DELETE /crawls/{id}?permanent=true` through `ledger.release_crawl_objects` with the job path's 503 rule, deleting the crawl with a Core `DELETE` so Postgres cascades (the ORM would NULL the children's NOT NULL `crawl_id`); `scripts/audit_crawl_quota.py` (read-only). **279 API + 29 contract tests green** (266 → 279: 9 quota + 4 crawl); the three meter-change tests were mutation-checked against the old queries and fail exactly as they should; `EXPLAIN` confirmed the `user_id` predicate is pushed into every view arm. **Decisions taken in the build, not in any filing:** two views not one (queued crawl has no unit row — ADR-009 §3's mechanism refined, decision unchanged, and the ADR cannot say so itself); the per-page storage insert deferred to the `CrawlWorkflow` port (its only v1 site is BUG-008). **Found by the build:** 🔴 **BUG-014** (admin user delete 500s on the `crawls`/`batches` FKs — verified in a rolled-back transaction, filed to §4, not fixed); on v1 every crawl holds a slot forever (BUG-008, the audit script says so); a stashed migration file under the hot-reloading API fails every startup until it is back; "for a year" nearly went into a docstring again (crawls shipped 2026-04-17 — five months). Docs swept: backlog (P7 row, two change-log entries, queue, sequencing, BUG-014 row), `open-bugs.md` (BUG-014, two P7 notes under BUG-008/BUG-012), `temporal-full-migration.md` (view entry, entry condition), `CLAUDE.md` (queue, P7 bullet, a new Key-decisions row, BUG-014 in the open list), this file | `24cb89c` + docs closeout |
| 2026-09-15 *(clock)* | **🔷 P8 BUILT — the storage ledger, and BUG-007 with it.** Built straight through at the owner's direction ("fix this completely"), then audited as a whole. `storage_objects` + migration `0c73753d5138`; `app/core/ledger.py` as the single writer/releaser; the consumer records every stored object — the LLM extraction *and* `screenshot_paths`, which it had ignored since the field was added (BUG-004 facet 2 closes for free); all three delete endpoints and the nightly cleanup enumerate rows; `reconcile_storage_ledger.py` for production's history. **266 API + 29 contract tests green** (255 → 266: 6 converted, 11 new); the two load-bearing tests were mutation-checked. **Decisions taken in the build, not in any filing:** materialised counter (not a live `SUM`); 503 on a failed release; a run that fails accounting holds nothing; `crawl_page_id` added now so P7 is a consumer. **Found by the build:** the hot-reload migration trap dropped `idx_webhook_deliveries_dedup` (recovered by hand — see *Current state*); `api/scripts/` was never mounted into the api container, so script imports and the P6-era "verified" sweep ran the image's baked copy (mounted now). Docs swept: `open-bugs.md` (BUG-007 fixed, BUG-004 facet 2), backlog (P8 row, queue, change log, sequencing), `CLAUDE.md` (queue + a new Key-decisions row), this file | `f503f8b` + docs closeout |
| 2026-09-11 *(clock, second session)* | **🔷 P9 / BUG-011 BUILT — the second code item of the pre-migration queue.** Built piecemeal at the owner's direction (job branch → batch branch → builder move → tests), then audited as a whole. `_recover_stale_pending` routes by whichever FK is set; batch runs rebuild from `batch_items` + `batches`; all four skip sites log at warning; the SAWarning that was BUG-011 showing in the test output is gone. **One finding not in the filing:** `create_job` had its own inline message builder, so the job lane already had the duplication the batch lane was about to gain — both lanes' builders moved to **`api/app/core/dispatch.py`** and all five dispatch sites call them (owner accepted the scope). Two tests pin dispatch-vs-recovery byte equality per lane; mutation-checked. **255 API + 29 contract tests green.** 🔷 **Knock-on recorded, not claimed as a fix: BUG-001's symptom is gone by construction**; mechanism 4 is untouched. Two small things caught in the audit: a docstring I wrote said batch went unrecovered "for a year" (it shipped 2026-04-22 — under five months; corrected), and `ScrapeMessage`'s docstring listed dispatchers by file (stale after the move; corrected). Docs swept: `open-bugs.md` (BUG-011 fixed, BUG-001 annotated), backlog (P9 row, queue, change log, BUG-001 §3 row — its `scheduler.py:131` pointer dropped), `CLAUDE.md` (queue bullet + a new Key-decisions row), this file | `ed4d63c` + docs closeout |
| 2026-09-11 *(clock)* | **Docs + decisions only — no application code.** Deploy-path audit of what a `main` fast-forward actually does, against the infra repo: **the deploy is automatic** (five `ImagePolicy` objects at 1m + `ImageUpdateAutomation` writing tags back to the infra repo), so a push needs no manual tag bump. **Four findings, none of them in any doc.** 🔴 **`api` and `http-worker` are `strategy: Recreate` at `replicas: 1`** — the old pod stops *before* the new one starts, so there is a real outage window on every release **and no fallback if the new image fails**; ⚠️ **the other three default to `RollingUpdate`, which at `replicas: 1` resolves to maxSurge 1 / maxUnavailable 0**, so a crash-looping image **stalls the rollout and leaves the old pod running** — good for uptime, **wrong for the P6 cutover**, because it silently leaves a **v2 worker against a v3 API**, and a bad `schema_version` is *acked and discarded*, not retried. **So verify `rollout status` per Deployment, not pod health.** ✅ The `flux-system` Kustomization has **no `wait: true`**, so a crash-looping coordinator (BUG-012) cannot block the other four reconciling — **ignoring crawls is safe**. ✅ The nightly `scrapeflow-cleanup` CronJob runs **from the API image** with MinIO delete rights and upgrades silently with it — checked and **unaffected by ADR-011**: it reads `result_path` from the DB and matches `startswith("history/")`, never constructing or parsing a path. ✅ **Zero Alembic revisions `main..develop`** — this release runs no migration. 🔷 **BUG-013 filed** (5 of 7 dependency manifests resolve at build time, so the tested image and the deployed image are different artifacts) — **carved out of BUG-006 deliberately: visibility vs reproducibility, neither closes the other.** Its new half is the **frontend**, which BUG-006 counts among the three *scanned* manifests: `api/Dockerfile` copies only `package.json` before `npm install`, so **the lockfile is absent from the step that resolves versions** and those 21 alerts have never described the shipped bundle. 🔷 **Two release-cadence decisions taken** — the pre-migration queue ships as **one** `main` fast-forward, and the migration then ships **per-flow, one push per ADR-009 §16 step**; a `main` freeze was weighed and dropped, so **§16 stands as written and no superseding ADR is needed** | `e9304b9`, `43c828a`, `2e822d9` |
| 2026-09-04 | **🔷 P6 / BUG-005 BUILT — the first code of Phase 4's pre-migration queue.** `schema_version` 2 → 3 across five services; 598 tests green (API 251 · **cross-service contract 29** · Go · playwright 168 · llm 101 · coordinator 49). Two typed producer models (`api/app/messages.py`) with all seven dict-construction sites routed through them; `artifact_id` on the wire, `job_id` off it; `run_id` optional and absent on the crawl lane; stage-named objects; `latest/` deleted from three workers and the delete path; the result-consumer parse-order move. **The ADR-011 §6 contract test exists** (`contracts/`) and was mutation-checked in both directions — it catches a reverted Go guard and a stale Go fixture. **Three findings not in the ADR**, all from the code rather than the docs: 🔴 the Go worker's `history/` write **depended on `latest/`** via `CopyObject`, so the removal is a rewrite there, not a deleted line; dropping the timestamp from screenshot keys makes redelivery **idempotent**, shrinking **BUG-004** by construction the way `latest/`'s removal shrank BUG-007; and 🔴 **ADR-011 decides nothing about deployment ordering, and no safe order exists** — owner's call taken to hard-cut against a drained stream, which is why the version was bumped. ⚠️ **The production `latest/` sweep did not ship** — written as `api/scripts/sweep_latest_objects.py`, dry-run by default, owner-authorised. **Then the deploy rehearsal found two pre-existing bugs, neither from P6.** 🔴 **BUG-012 filed** — `reenqueue_stalled` deletes `crawl_pages` while `crawl_queue` still references them, so the **coordinator crash-loops on startup** and cannot self-clear (31 restarts, 194 stalled items observed). Owner **declined the §3 override**; filed to §3, dissolves cleanly. **Its unit test pins the broken order as correct** — the second instance of that shape after BUG-005's. 🔴 **BUG-006 addendum** — `llm-worker` imported `httpx` without declaring it; the provider SDKs jumped majors and moved to **`httpx2`**, so a rebuild produced an image with no `httpx` and the worker crash-looped. Fixed (`1c456a4`); **lockfiles, not scanning, are BUG-006's real fix**, and the SDK majors are now unpinned and undecided | `81afbb9`, `d4330f4`, `1c456a4`, `427f7ce`, `5d91134` |
| 2026-09-03 ⬅ *clock, second session* | **🔷 ADR-011 reviewed and promoted to `Accepted` (owner decision) — P6's last design dependency is closed and nothing blocks the pre-migration queue.** Its one open item confirmed by name: **the crawl lane stays in scope**, so P6 changes all three lanes. Promotion sweep across ten files (ADR-002 → `Partially Superseded`; both ADR indexes; `CLAUDE.md`; backlog; `open-bugs.md`; `temporal-full-migration.md`; `docs/process/`). **Three stale premises corrected, none found by reading the passage that was marked** — the `run_id` recommendation ADR-011 rejects by name, `latest/` listed under *what does NOT change*, and 🔴 **an undeclared reversal of a clause in the Accepted ADR-009 §8d**. The third is a new class and has its own section above; the method lesson is in *Trimming the docs*. Also recorded: the `latest/` sweep is a **production data deletion**, owner-authorised; ADR-003's `result_path` shape has drifted but is **not** superseded | `a0714f6` |
| 2026-09-03 ⬅ *clock, first session* | **🔷 Owner decisions taken on artifact identity, written up as ADR-011 (Draft)** — artifacts key on the producing row via a lane-neutral `artifact_id`; `job_id` leaves the wire; objects named by **stage**; **`latest/` removed**; the crawl lane's fabricated `run_id` removed. **This closes P6's last design dependency.** Two findings that changed the design mid-session, neither from reading the docs: a flat `history/{artifact_id}.{ext}` would have **silently reintroduced the collision** (the LLM worker hardcodes `ext="json"`, so an `output_format=json` job's extraction overwrites its own scraped page — only the timestamp prevents it today, and only because LLM calls are slow); and **`run_id` cannot be the universal key**, because the coordinator fabricates one with `uuid4()` for a lane that creates no `job_runs` rows, so `crawl_pages.id` is in the message twice — once honestly, once as `job_id`. **BUG-011 filed as P9**: stale-pending recovery silently skips every batch run, under a comment asserting the case is impossible — **BUG-005's fix does not close it**, since that removes the cause of stuck items rather than the hole in the net. A contracts package was weighed and **not taken**, recorded in ADR-011 §6 with the trigger to revisit (pipelines). Also verified: change detection is **path-agnostic** — "which run came before" is a SQL query and the differs take opaque paths, so the convention change needs **no backfill** | `fa3c18d`, `b00e0f2` |
| 2026-09-08 | **🔷 Two owner decisions taken: ADR-009 promoted to `Accepted`, and the conditional-execution PRD numbered `PRD-019`** (sort-order cost accepted; index order is not build order). **Both stale companions redrawn** — `temporal-full-migration.md` (five 🔴 divergences resolved, plus three the redraw found: Web UI exposure, tenancy, the SPA contract) and `workflows-scoping.md` (six 🔴 cleared, §9's open questions turned into a table of answers). ADR-009's three pre-redraw notes **corrected in place**, which is what surfaced that the second document was owed a redraw at all. **PRD-016 carry-back pass** — four items ADR-009 owed it, no decision changed. Draft caveat cleared from the ADR header and eight downstream documents. **The four lane-blind admin meters recorded** as cutover gotchas — which also caught gotcha 3 still describing the rejected NATS bridge. **ADR-009's D5 closed**: both deferrals decided by the owner and written up as **ADR-010** (Draft), which opens one new item — a Schedule overlap policy | `1d94d5d`, `041921d`, `33d58f2`, `c76edf8`, `c8f381f` |
| 2026-09-07 | This handoff condensed 250,550 → ~15,000 chars (−94%); `phase4-backlog.md`'s header change log 9,882 → ~2,500. Corrected the `prephase4` hash back to `1965953` | `2404193` |
| 2026-09-06 | `CLAUDE.md` cleanup — the duplicated ADR-009 summary stripped, 93,141 → 27,527 chars (−70%); backlog's ADR-009 row 16,500 → 914 | `530fca3`, `e201980` |
| 2026-09-05 | Both closing blocks reviewed (14 corrections, no decision changed) — **the section review closes**; then ADR-009 condensed, review log 476 → 83 lines as `## Review status` | `3e7f32e`, `21fadd2`, `3b91bab` |
| 2026-09-04 | §17 reviewed; `ADR-002 §8` → **§4** corrected across five live docs | `87d2396` |
| 2026-09-03 *(doc timeline — see the drift note above; this row is an earlier session)* | §16 reviewed — the sequence becomes **named, not numbered** | `400cda7` |
| 2026-09-01 → 09-02 | §14 and §15 reviewed | `9b2df55` |
| 2026-08-28 | §13 reviewed; BUG-010 filed; one live fix shipped | `e4a19fc`, `5c7fbdf`, `205acd4` |
| 2026-08-26 | §10 and §11 reviewed; BUG-009 filed; consistency sweep for the §8/§9 reversals | `4d27475`, `7b9f9d5`, `1770b70`, `a40b89b` |
| 2026-08-25 | §8's two blockers closed (4 owner calls); **P8 filed**; BUG-008 filed | `2caaddc`, `9f37992`, `7e6d9ec` |
| 2026-08-23 | §9 **reversed** — the NATS bridge rejected, workers port first | `72432b2` |
| 2026-08-17 | §8 **reversed** — the meter measures bytes on disk; BUG-007 filed | `2849da3` |
| 2026-08-10 | §4–§7 reviewed; §7 gains a fourth mechanism | `8a31ec5` |
| 2026-08-08 | §2, §3, §12 reviewed (§12 **reversed**); P7 filed | — |
| 2026-08-04/05 | ADR-009 drafted; BUG-005 and BUG-006 filed | — |

---

## Historical notes not recorded elsewhere

**Clerk production cutover (2026-07-03).** The load-bearing facts are in `CLAUDE.md` → Deployment
(key split, manual grey-cloud DNS, own OAuth credentials, the `JWK_FAILED_TO_LOAD` trap, the
rotation runbook). Two things only recorded here:

- **Fresh start**: prod Postgres app tables were truncated and MinIO `scrapeflow-results` emptied
  on 2026-07-03 (schema + `alembic_version` preserved, bucket kept) — a prod Clerk instance issues
  **new `sub` IDs**, so `users` rows keyed on the dev `clerk_id` would have been orphaned. The
  Fernet keys (`llm-key-encryption-key`, `credentials-encryption-key`) were **not** rotated, which
  is why encrypted-at-rest data survived.
- **Loose end**: GitHub OAuth custom credentials are still unconfigured. Only needed if GitHub
  sign-in is offered; Google is done.

**Q5 / Q6 / Q7 are closed in code and production**; Q8 is closed as do-not-fix (the code holding
it is deleted by the migration). Status blocks and the reusable NATS consumer-recreate procedure
are in `open-questions.md`; the recreate rules are also a Key decisions row in `CLAUDE.md`.

---

## ⚠️ Trimming the docs — the method, and what it keeps catching

Used three times now (ADR-009's review log, `CLAUDE.md`, this file). **Run it in this order; the
verification is the part worth trusting, not the reading.**

1. **Forward sweep** — extract distinctive tokens from the text you mean to cut (backticked
   identifiers, `file:line` refs, figures, IDs) and confirm each has a home in the authoritative
   doc. ⚠️ **Match across newlines** — a line-wrapped `ADR-002\n§8` survived one line-based pass.
2. **Only then cut.**
3. **Reverse sweep** — check that nothing which left is now homeless, and re-verify every path
   and link in the result.

**What it has caught, none of it by reading:**

- ⚠️ **The copy being cut may be the one holding the correction.** The Backlog said Dependabot
  scanned "3 of 6" manifests; the ADR-009 bullet had corrected it to **7**. Deleting the corrected
  copy would have left the wrong number standing alone. Fixed to **3 of 7** (2026-09-06).
- ⚠️ **Line-number pointers rot faster than anyone re-checks them.** `result_consumer.py:125` had
  already drifted to ~`:126`. Keep the fact, drop the line number.
- ⚠️ **Two near-identical hashes are in play; do not "correct" one into the other.** `5c7fbdf`
  (2026-08-28) is the crawl page status-filter fix, cited in ADR-009. `5cb8c7f` (2026-07-01) is
  the **Q8 LLM-dispatch-loop source guard**, cited in `open-questions.md`. Both are correct where
  they stand. Verified 2026-09-07.
- 🔴 **A sweep can manufacture a defect, and this one did.** The 2026-09-06 pass reported
  `CLAUDE.md`'s `prephase4` hash `1965953` as wrong "because the tag resolves to `473fb68`", and
  dropped it. **`1965953` was correct.** `prephase4` is an *annotated* tag, so
  `git rev-parse --short` returned the **tag object's** SHA; the tag points at commit `1965953`.
  `git log -1 473fb68` then showed the right commit — git silently dereferences the tag object —
  which made the bogus finding look confirmed. ⚠️ **Verify a hash with the command that answers
  the question you are actually asking** (`git rev-list -n1 <tag>` for "which commit"), and treat
  a sweep finding as a hypothesis until a second, differently-shaped check agrees. Caught
  2026-09-07 while re-verifying this file; nothing downstream was affected, because the same pass
  had dropped the hash from `CLAUDE.md` rather than writing the wrong one in.

**What must NOT be trimmed further:**

- ⚠️ **`CLAUDE.md`'s 44 Key-decisions rows, in full.** *(Re-counted 2026-09-18 by `awk` over the table: 44 — P8 and P7 each added one since the 2026-09-11 count of 43, so one of those counts was off by one; the awk number is the one to trust.)* The rationale column is not explanatory
  padding — it is the trap that stops the bug returning, and cutting it is the one edit that would
  make that file worse. Specifically: `xvfb-run`-as-pid-1, the **`nats consumer info --json`**
  requirement, the `llm_max_retries=0` pin and why the Q6 pin and the timeout bump are *safe
  together and unsafe apart*, the aiohttp-vs-`S3Error.code` split, the Go `*uploadError` scoping,
  and bot-wall posture being the **inverse** of the LLM classifier.
- ⚠️ **ADR-009's eight `✅ Settled on review` blockquotes** (103 lines total). They sit directly
  beneath the detail they summarise, which is the right home for a summary.
- ⚠️ **Repeated facts across ADR-009 sections** (BUG-005 ×15, the ≈2.6 h horizon ×15, the
  light-worker rule ×6) were checked and left. Each is a **cross-reference doing local work**;
  removing them would make sections stop being self-contained, which is worse than the repetition.

**Done, in order:** ADR-009's review log (2026-09-05) → `CLAUDE.md` (09-06) → this handoff and
`phase4-backlog.md`'s header change log (09-07). Nothing obvious is left; the remaining large
documents are load-bearing content rather than duplicated summary.

### The same discipline, applied to reconciling rather than trimming (2026-09-08)

Redrawing two documents against the Accepted ADR turned up four things, **none of them by reading
the passages that were marked**:

- 🔴 **A 🔴 marker is not a fix, and it outlives the thing it was meant to prompt.** The rejected
  NATS bridge survived in **five** places — `temporal-full-migration.md`, `workflows-scoping.md`
  (twice), ADR-009's own §14 table, and `phase4-backlog.md`'s cutover gotcha 3. Some were marked;
  the marked ones still read as live recommendations underneath the marker, and the gotcha was not
  marked at all. **Search for the rejected thing by name, not for its markers.**
- ⚠️ **One instruction covering two documents gets acted on for one.** §14's disposition said
  *"🔴 markers now, one redraw after the review closes"* about `temporal-full-migration.md` **and**
  `workflows-scoping.md`. Only the first was on any list. Nothing in either marker would ever have
  surfaced the other.
- ⚠️ **A reversed premise under an unchanged conclusion is invisible.** Two PRD-016 passages argued
  from ADR-009 §8's *"only the final artifact is charged"* — reversed 2026-08-17 — and reached the
  right answer anyway. They read as sound because they *are* sound; nothing flags them until
  someone reuses the stale rule on a lane where it gives a different answer. **Grep for the
  withdrawn wording, not for wrong conclusions.**
- ⚠️ **Line pointers rotted again**, second confirmed instance: `routers/crawls.py:70` had drifted
  to `:75`. Five of six spot-checked refs were exact, which is the trap — a mostly-accurate set
  invites trusting the rest. Dropped the numbers, kept the fact, per the standing rule.

### The same discipline, applied to promoting an ADR (2026-09-03)

Promoting ADR-011 and sweeping its knock-ons through ten files turned up three stale premises.
**None was found by reading the passage that was marked** — the standing lesson, third confirmation
— but one of them is a class the method did not previously cover:

- 🔴 **An immutable record can be displaced from OUTSIDE, and it cannot say so itself.** ADR-011 §4
  reverses ADR-009 §8d's *"`latest/` is kept as-is"*. ADR-009's **Reversed or withdrawn** table —
  the thing this handoff tells you to check first — records only what *its own review* changed, and
  the ADR is immutable, so nothing can be added to it. **A `git grep` for the withdrawn wording
  finds §8d reading as live and correct.** The only place the displacement can live is the
  *superseding* ADR's header. ⚠️ **So when promoting an ADR, grep for what its decisions
  contradict, not only for what it supersedes by name** — ADR-011's `Supersedes:` line said
  "ADR-002 §4" and was complete on its own terms while silently reversing a second record.
- ⚠️ **A reversal usually splits a call in half, and the halves travel together.** §8d settled
  *charge one copy* **and** *keep `latest/` for now* in one owner call. Only the second is reversed.
  Recording "§8d is reversed" would have been as wrong as recording nothing — the tracker entry in
  `open-bugs.md` had them in a single bullet, which is how they would have gone together.
- ⚠️ **A superseded document's stale copies are not all in the ADRs.** `docs/process/tech-lead.md`
  still described ADR-002 as the current contract for MinIO paths — a persona starter prompt, not
  a design doc, and on nobody's list. **Sweep `docs/process/` too.**
