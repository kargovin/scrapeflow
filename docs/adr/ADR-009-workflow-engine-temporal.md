# ADR-009: Workflow Engine — Temporal, and the v1/v2 Coexistence Contract

**Status:** Draft — under section-by-section review by @karthik. **Nothing here is settled yet**;
do not implement against it, and do not cite it as a decision in another document until the
document status is Accepted.
**Date:** 2026-08-04 (drafted) · 2026-08-08 (review in progress)
**Review log:** §1 taken as settled (engine decided pre-ADR). **§2 resolved 2026-08-08** — three
open points closed in place (2a separate Postgres instance · 2b Web UI not exposed · 2c retention
30 days). **§3 reviewed 2026-08-08** — two factual corrections applied (crawls are already an
uncounted lane; the `storage_bytes` exemption was wrong — all three meters are blind to crawls,
not two), plus the recount race named. Crawl metering answered by the PM (PRD-016 OQ-4 round 3)
and wired into §3/§8 — **✅ confirmed by owner 2026-08-08**; no §3 item remains open.
The workflow-ID format is **settled**: it stays user-free, and **§12 was reversed** to match
(its "user identity in the workflow ID" claim is withdrawn — Temporal never parses the ID, so the
property was never structural). §4–§11 and §13–§17 not yet reviewed.
**Deciders:** @karthik
**Inputs:** [PRD-016](../project/phase4-prd/PRD-016-workflows-pipelines.md) (11 open questions),
`docs/project/phase4-backlog.md` §2/§3, `docs/project/workflows-scoping.md` §7 (engine
comparison), `docs/project/temporal-full-migration.md` (change inventory + sequence),
`docs/project/open-questions.md` **Q8** (the incident), `docs/project/open-bugs.md` **BUG-005**
**Supersedes:** nothing yet — see [§17](#17-relationship-to-adr-001002004).

---

## Context

ScrapeFlow's orchestration is hand-rolled: five polling loops plus Postgres state, with
idempotency guards, backoff schedules and crash recovery written by hand in every component
(`result_consumer.py`, `scheduler.py`, `webhook_loop.py`, `advisory.py`, `coordinator/`).

That approach has already cost a production incident. **Q8**: `job_runs.status` encodes both
*which stage* a run is in and *what that stage is doing*, so the LLM worker's `running` message
clobbered `processing` back to `running`, the next `completed` re-matched the scrape-completed
branch, and the system re-dispatched to the LLM subject in a tight loop — roughly 200 billable
LLM calls in five minutes. The fix was a `source ==` guard on two branches. The invariant that
fix depends on is *"every branch that reads `run.status` also reads `source`, forever, in every
code path anyone ever adds"* — not enforced by the type system, and not reliably caught in
review. Q8 is closed as do-not-fix precisely because the code holding it is deleted here.

Phase 4 adds a product layer — user-defined **Pipelines** (PRD-016) — that makes this worse
before it makes it better. Every step added to the fixed recipe multiplies the Q8 class of bug,
and a pipeline is by definition a variable number of steps.

**This ADR does three things:** records the engine decision and why; answers PRD-016's eleven
open questions; and defines the contract under which the existing NATS path (**v1**) and the
Temporal path (**v2**) run side by side.

### The one fact that reframes several answers

**Temporal Server does not run our logic, and its database is not an application database.**
Temporal is infrastructure: durable event history, timers, retries, task routing. Our workflow
and activity *code* runs in **our own worker pods** that connect to it. Its Postgres holds event
history and timer state; we never read or write it, and no user-facing question — "how many runs
has this user made", "list this user's pipelines" — is ever answered from it. Every such question
is answered from the **app** Postgres. Logic moves *out of the API pod* into worker pods; it does
not move "into the engine."

---

## Decisions

### 1. The engine is Temporal

Chosen over DBOS, Restate, Prefect, Windmill, and extending NATS+Postgres. The comparison table
in `workflows-scoping.md` §7 is the record; the deciding factors:

- **First-class Python *and* Go SDKs.** We run both — the http-worker is Go, everything else is
  Python. Prefect has no Go. This is the only hard technical filter, and it eliminates a
  candidate outright.
- **Durable timers, signals, and saga are all first-class.** Monitors (layer B) is built entirely
  from durable sleep plus wait-for-signal; Delivery sinks (layer C) is built from compensation.
  Both are marquee Temporal features rather than things we assemble.
- **Portfolio value.** This is a stated goal of the project, not a rationalisation. Temporal is
  the industry-standard name in this category.

Accepted cost: it is the heaviest dependency we will run on a single-node k3s homelab — server
components plus its own Postgres. Mitigated by using **standard visibility** (no
Elasticsearch/OpenSearch) initially and accepting **no Temporal HA** at homelab scale.

**Rejected — DBOS.** The lightest path (a library on the Postgres we already run, near-zero new
infra) and genuinely tempting. Rejected because `workflows-scoping.md` §7's "prototype on DBOS,
migrate to Temporal later" plan front-loads the cheap half and defers the expensive half: we
would write the workflow layer twice and still take the Temporal operational cost eventually.
Its Python-only strength also fails the Go filter for the http-worker's eventual activity
conversion.

**Rejected — Restate.** Light (single binary), rising, has both SDKs. The honest reason it loses
is the portfolio factor plus ecosystem maturity, not a technical deficiency. Worth revisiting
only if Temporal's operational weight proves unsustainable on this cluster.

**Rejected — extending NATS+Postgres.** This is the status quo that produced Q8. It means
re-solving retries, timers, idempotency and replay by hand, which is the definition of the
problem.

### 2. Topology

New infrastructure:

- **Temporal Server** + **Web UI**, plus a **separate Postgres instance** for Temporal
  persistence — see 2a.
- **Workflow-worker pod(s)** — hosts workflow and activity definitions (Python SDK).
- A **namespace-registration init job**, analogous to today's `nats-init-job.yaml`. This is where
  retention is set — see 2c.
- The three scrapers eventually become **activity workers** (Go SDK for http-worker, Python for
  the others). Not in the first increment — see [§9](#9-oq-5--reuse-existing-workers-via-option-a-first).

The API keeps auth, CRUD, and quota enforcement, and gains "start / signal / query workflow." It
loses all five background loops. That is what lifts the current single-replica + `Recreate`
constraint (`app/api.yaml`) and makes the API horizontally scalable — a concrete architectural
payoff, recorded here so it is not mistaken for incidental. **The workflow worker is horizontally
scalable too** (stateless task-queue polling), so this removes the bottleneck rather than moving
it. The playwright worker stays vertically constrained regardless — that is a headed-Chrome fact,
not something the engine change addresses.

#### 2a. Temporal persistence is a separate Postgres *instance*

**Decision: a second Postgres StatefulSet, not a second database on the existing one.**

A separate *database* on the shared instance would not buy the isolation the separation is for: a
history-write burst would still degrade the app DB through shared connections, shared buffers and
shared disk. The instance boundary is the one that actually holds.

Two further reasons, both about lifecycle rather than load:

- **Backup/restore discipline differs.** The Temporal DB holds in-flight execution state — losing
  it loses running work, not just history (see Consequences). It wants a different restore posture
  than app metadata.
- **Schema ownership differs, and this is the one most likely to bite.** The app DB's schema is
  Alembic's, auto-applied from `api/app/main.py` on API startup. Temporal's schema is owned by
  **`temporal-sql-tool`** and versioned on Temporal's release cadence — a server image bump can
  require a schema step that nothing in our deploy flow performs. Two migration mechanisms is
  already a cost; entangling them on one instance compounds it.

Cost is small: cloning the `infrastructure/postgres.yaml` pattern (`postgres:16` StatefulSet,
100m/256Mi requests, 10Gi PVC) is roughly **+100–250m CPU and +256–512Mi memory in requests**,
plus a PVC.

**Note that Temporal wants two databases *inside* that instance** — `temporal` and
`temporal_visibility` — even on standard visibility. Provisioning one and wondering why startup
fails is the predictable way to lose an hour here.

#### 2b. The Web UI is not ingress-exposed

**Decision: no ingress, no DNS record, no cert. Access via `kubectl port-forward` only.**

The governing fact is that **the Temporal Web UI is not a read-only dashboard.** It can terminate
and cancel workflows, send signals, and reset a run to a prior point. Exposing it publishes a
*write-capable control plane over production orchestration*, which is a materially higher bar than
the mlflow precedent on this cluster (a `basicAuth` Middleware in front of experiment tracking).
OSS Temporal UI also ships with **no authentication of its own**, so whatever gates it we supply.

Compounding it: [§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary)
settles on a **single namespace** with the API as the *only* tenant boundary, so every tenant's
runs share one engine-side listing — a surface with none of the 404-not-403 discipline applied
everywhere else, and one the API's ownership check does not reach, because the UI talks to
Temporal directly.

Not exposing it removes both problems outright instead of mitigating them, adds no components, and
is **fully reversible** — an ingress is purely additive later. The accepted cost is that the UI is
unavailable without cluster access, which is tolerable because its value peaks during incidents and
those are worked from a machine with `kubectl`.

**Deferred to post-Phase 4, tracked in "Deliberately not decided here":** if always-on access is
wanted later, the two candidates are **Traefik `basicAuth`** (the mlflow pattern already in the
infra repo — cheap, but one shared credential, no attribution of who terminated what) and
**Traefik `forwardAuth`** against an API admin endpoint (one identity system, real revocation and
attribution, but it must be built, it couples UI availability to API availability, and it depends
on Clerk's session **cookie** being scoped to a domain the Temporal host shares — a browser
navigating directly sends cookies, not the SPA's bearer token). Neither is worth building for a
single operator today.

#### 2c. Namespace retention is 30 days

**Decision: register the namespace with a 30-day retention period.**

Retention governs how long a **closed** workflow's event history survives before cleanup deletes
it. It buys post-hoc debugging — the per-run timeline that replaces `grep status=` log-spelunking —
and the ability to *reset* a run, which only works while its history exists. It costs disk in the
Temporal DB, scaling with runs × events-per-run × payload size in history.

Three things make 30 days a low-stakes pick rather than a load-bearing one:

- **It has no correctness role.** [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
  forbids answering any user-facing question from Temporal's DB — run counting moves onto a view
  over app Postgres. So retention cannot silently redefine "runs this month". It is purely an
  operator dial.
- **It is changeable after creation.** Not a one-way door. The namespace needs *a* number to exist,
  not the *right* number.
- **Volume is nowhere near the constraint.** The BUG-003 prod audit had 15 completed runs to
  examine in total. At that scale the difference between 7 and 30 days is megabytes.

30 rather than 7 because retroactive archaeology is worth protecting — the BUG-003 investigation
found the 40% bot-wall rate by auditing runs *after the fact*, and a 7-day window would have to be
lucky. 30 rather than 90 because 90 retains history for runs nobody will open. 30 also aligns with
the monthly quota window and is Temporal's default, so it matches every doc and default encountered
later.

**The coupling worth stating explicitly: retention is cheap only because
[§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity) holds.** Under
references-not-payloads a five-block run is plausibly tens of KB of history. The moment an activity
returns page *content*, one run goes from ~50 KB to ~4 MB (real pages measured at 291 KiB–4.1 MiB)
and retention becomes the binding constraint overnight. If this number ever needs revisiting
urgently, suspect §5 first.

**Archival** — shipping closed histories to MinIO before deletion — is the escape hatch that would
make a *shorter* retention safe. Deliberately not enabled: another moving part, for data currently
worth very little.

#### 2d. Capacity

For the record, since "heaviest dependency we run" invites the question. The node (`kimsufi-server`,
8 CPU / 32 GiB) currently sits at **28% CPU requests and 11% memory requests**. Temporal Server +
its Postgres + Web UI + a workflow worker is roughly **+1.5–2 CPU and +2–3 GiB in requests** —
landing near 50% CPU requests, with memory still under 20%. Requests are not the constraint.

**This figure already is the coexistence peak, which is the number that matters.** The 28% baseline
includes NATS, the coordinator and all three workers, so adding Temporal on top describes the
cluster at migration steps 2–3 (drawn in `temporal-full-migration.md` §9a) — both orchestrators running, both
worker sets alive, five loops still serving in-flight v1 work. Nothing later in the sequence is
heavier; every subsequent step *removes* a component. Sizing to steady state would have understated
it, and the several weeks spent at the peak are exactly when a capacity surprise would land.

**CPU *limits* are.** The node is already at **162% limit overcommit**, and the playwright worker
alone is 500m request / 2000m limit for headed Chrome. A simultaneous headed render and a Temporal
history burst means CFS throttling on the history service, which surfaces as workflow task timeouts
and retries. **That looks exactly like a workflow bug and is not one** — recorded here so it is not
debugged twice.

### 3. OQ-1(a) — Run identity: pipeline runs get their own table, and quota counting stops naming a table

**Decision: a new `pipeline_runs` table with a `pipeline_run_blocks` child table. Pipeline runs
are never `job_runs` rows. Quota counting moves off tables and onto a database view.**

Three options existed:

| Option | Verdict |
|---|---|
| (a) Pipeline runs write `job_runs` rows (a third parent FK) | Rejected |
| (b) New `pipeline_runs` + `pipeline_run_blocks` | **Chosen** |
| (c) No app-side run record; read run state from Temporal | Rejected |

**(a) buys nothing and pollutes.** `job_runs` carries job-shaped columns — `content_hash`,
`diff_detected`, `diff_summary`, `nats_stream_seq`, a single `result_path` — and its
`chk_job_runs_single_parent` constraint is a two-way exclusive-or that would become three-way.
More decisively, R3's biggest win is **per-block** status and timing, which a single row cannot
express: a child table is required either way. Once you have the child table, reusing the parent
row buys only the quota query, which (b) solves better.

**(c) fails on enforcement.** Quota checks would require a live engine call on every trigger, and
Temporal's retention policy would silently determine what counts as a run this month. Listing,
admin views and cross-tenant 404 checks all need SQL.

**The load-bearing half of this decision is the quota fix, not the table choice.** Today
`quota.py` hardcodes `FROM job_runs` in both `_count_monthly_runs` and `_count_concurrent_jobs`.
Neither is a stored counter — both recount on every check. A new table is therefore *invisible to
the meters by construction*: a user who exhausts 500 monthly job runs could trigger unlimited
pipeline runs, not because anyone decided pipelines are free, but because the query never looked.

> **This is not a prediction. It has already happened, to crawls.** `JobRun(...)` is constructed
> in exactly three places — `routers/jobs.py:207`, `routers/batch.py:105`, `core/scheduler.py:80`
> — and **never for a crawl**. Crawl work lives in `crawl_pages`, with its own `status` and
> `result_path`. So both count meters are already blind to the crawl lane, and
> `routers/crawls.py` carries **no quota check at all** (zero references to `check_user_quota`).
> A 500-page crawl costs the user **zero** monthly runs and **zero** concurrent slots today.
> Pipelines would be the *third* lane to arrive invisible, not the first — which makes this a
> live gap rather than migration preparation.

So: **introduce a database view that is the single definition of "a run this user started," and
point the quota queries at it.** Adding Monitors later is one view change rather than an audit of
every call site. This survives the migration — when `job_runs` becomes a read-model mirror of
Temporal state, the view is unaffected.

**PM decision — PRD-016 OQ-4, review round 3 (2026-08-08). ✅ Confirmed by owner 2026-08-08.**
Whether crawls join the view was a **product** question, not an architectural one: it changes what
a shipped, live feature costs. The answer is **yes, and the view carries four lanes, not two** —
job `job_runs`, batch `job_runs`, **`crawl_pages`**, and `pipeline_runs`.

The view is **one row per countable unit**, and the two meters aggregate it differently:

- **`monthly_runs` counts rows.**
- **`concurrent_jobs` counts distinct concurrency *groups*** among non-terminal rows, where the
  group is the **crawl** for crawl pages and the row itself on every other lane.

That split is deliberate: cost and contention are different quantities. Per-page concurrency would
put every default crawl (`max_pages` 100) permanently over the default ceiling of 5 — a meter that
makes a shipped feature unrunnable is misconfigured, not strict — and enforcing it would need
dispatch throttling inside `coordinator/`, which [§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block)
deletes.

**What the view fixes, and what it does not.** It fixes *which rows count*. It does **not** fix
*when they are counted*: both meters recount on every check, so two concurrent creations can each
read 499/500 and both proceed. That race is pre-existing and low-impact at current volume, but a
second lane and an engine that makes concurrent starts easy both push against it. Recorded so a
reader does not assume the view closed it — converting the meters to stored counters is a separate
change, deliberately not made here.

> **This is BUG-005's lesson generalised.** That bug was not "batch used the wrong table." It was
> that three separate contracts — two message schemas and the MinIO path convention — hardcoded
> the assumption that every run has a `job_id`. Batch was the first run that wasn't a job, and it
> broke in three places. Pipelines are the second and larger instance of the same shape. Every
> decision in this section exists to make the identity explicit rather than assumed.

**`storage_bytes_used` is not exempt, and an earlier draft of this section wrongly said it was.**
It is a stored counter rather than a recount, which is why it looks lane-agnostic — but it is
incremented from exactly **one** call site, `result_consumer.py:85`, and guarded by
`run.storage_accounted_at`, a **`job_runs` column**. The crawl coordinator writes page bytes to
MinIO (`coordinator/result_handler.py:150` sets `page.result_path`) and never touches storage
accounting at all. So crawl bytes are uncounted today for the same structural reason crawl runs
are: the accounting is keyed on a column only one lane has.

All **three** meters are therefore blind to the crawl lane, not two. The correction matters because
the exemption told an implementer that storage needed no thought — and pipeline artifacts written
by an activity rather than by `result_consumer.py` would be uncounted by exactly the same
mechanism. [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-only-the-final-artifact-is-charged)'s
"only the final artifact is charged" presumes something does the charging; on the v2 lane that
something must be named, not inherited.

**The workflow ID is the correlation key** between an app record and its engine execution —
`pipeline-run-{pipeline_run_id}`, and `job-run-{run_id}` for migrated jobs. This replaces today's
`nats_stream_seq` correlation, and `nats_stream_seq` is dropped when v1 retires.

> **✅ Settled on review (2026-08-08).** These formats are final and carry **no user identity** —
> [§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary) previously
> claimed `user_id` was encoded in the workflow ID and has been reversed to match. The ID does
> exactly two jobs: correlate an app row with its engine execution (here), and give
> [§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee)'s mechanism 2
> its engine-level double-start guarantee. It is **not** a tenant-isolation mechanism; the API's
> ownership check is the only one.

### 4. OQ-1(b) — Block model: fixed typed catalog, JSON in Postgres, explicit named wiring

**Decision: a fixed catalog of typed blocks, stored as JSON in Postgres. Every block carries a
stable identifier unique within its pipeline, and names its input by explicit reference to an
earlier block or a declared run input. Execution is linear; the wiring is expressed as a graph.**

- **Fixed catalog, not a general DAG schema.** R2's catalog is closed, user-authored code is a
  non-goal, and R1's save-time validation requires knowing each type's declared consumes/produces.
  A general DAG schema would defer all of that to run time.
- **JSON in Postgres, not a DSL.** A DSL needs a grammar, a parser, versioning *of the grammar*,
  and its own error messages. At five block types that is pure cost. JSON validates against a
  schema and produces field-level errors for free.
- **Explicit named input references, not implicit previous-block wiring.** This is the part that
  matters, and PRD-016 is internally inconsistent without it: R1's validation clause says a
  block's input must be producible by "anything before it," while Non-goals says chains are
  linear. Both hold only if a block can name an earlier block. The Problem section settles it —
  *"run two extractions on one fetched page"* requires the second LLM block to consume the
  **page**, not the first LLM's JSON, and is unsatisfiable under implicit wiring.
- **Block identifiers are immutable once assigned.** This is what makes OQ-2's version pinning
  meaningful and what lets per-block run history reference a block that a later edit removed.
- **Linearity is enforced by validation, not by the schema.** The stored shape is a graph; R1's
  validator rejects anything that is not a single chain. Relaxing that later is a validator
  change, not a migration of every stored definition. That is the entire forward-compatibility
  requirement, satisfied at zero cost now.

**The halt-early obligation (from the PM's OQ-10 decision) is satisfied structurally.** Monitors
(B) will need a block that ends a run before its last block *with the run still reporting
success*. Two rules make that additive rather than breaking:

- **Run outcomes stay three** — `completed`, `failed`, `cancelled`. A halted-early run is
  `completed`.
- **Block state is a separate vocabulary from run outcome, and includes `skipped` from day one.**
  Nothing in R2's catalog produces it, but the column admits it now so that B is a new block type
  rather than a schema change plus a backfill.

Overloading a single status vocabulary across two levels of a hierarchy is exactly what Q8 was.
Keeping block state and run outcome distinct is that lesson applied before the fact.

### 5. OQ-1(c) — Blocks pass references; artifacts are keyed on run identity

**Decision: block inputs and outputs are MinIO object references. Page content never enters
workflow history. The v2 artifact path is keyed on the pipeline run and block, not on a job.**

Activity inputs and outputs are recorded in workflow history, which caps individual payload size
(low single-digit MB — confirm the current figure against Temporal's own documentation, not this
ADR) and bounds total history size. The BUG-003 audit measured genuine pages between **291 KiB
and 4.1 MiB**. A content-passing model therefore fails on large pages for reasons that have
nothing to do with scraping.

The subtler cost: **workflow history is retained after completion by design** — that retention is
what buys replay and resumption. Today's NATS stream is `--retention work`, so acked messages are
deleted and orchestration state is effectively free and self-cleaning. History is not. Keeping
payloads out of it is what keeps that bill proportionate, and this is **the largest new
operator-side cost in the migration**.

R2's "each block declares what it consumes and produces" is therefore read as declaring **types
of reference**, not types of payload.

**The v2 path convention:**

```
pipelines/{pipeline_run_id}/{block_id}.{ext}      — one immutable object per block per run
```

Two deliberate departures from ADR-002 §8:

- **Keyed on the run, not on a definition.** `history/{job_id}/…` assumes a stable parent that a
  pipeline with run inputs does not have. This is the same assumption BUG-005 broke.
- **No `latest/` write.** Its semantics — "the newest result for this thing" — are job-shaped. A
  pipeline that takes a URL as a run input has no single "this thing," which is the same reason
  the cost gate cannot live in layer A (OQ-10). Writing a `latest/` object anyway would recreate
  BUG-005's shared-object collision exactly.

This **partially supersedes ADR-002 §8 for the v2 lane only**. v1 keeps its convention until
retired. Note that BUG-005's fix re-keys the v1 batch path on `run_id`, which converges the two
conventions rather than forking them further.

### 6. OQ-2 — In-flight edits: definitions are pinned, and that is a different problem from code versioning

**Decision: a run executes the definition version it started with. Pipeline definitions are
immutable versioned rows; an edit creates a new version; a run records the version it pinned.**

Adopting an edit mid-run is not merely undesirable, it is incoherent: a run that has already
executed blocks 1–3 of the old shape cannot meaningfully continue into a *different* block 2.
Under a replaying engine it is worse than incoherent — replay reconstructs in-memory state by
re-reading history against the current definition, so a changed definition makes replay produce a
different answer than the original execution. That is a determinism violation, and it surfaces
only *after* a restart, which is the hardest possible failure to reproduce.

R1's "deleting a pipeline must not destroy the history of runs already executed from it" falls
out of this for free: versions are retained as long as any run references them.

**Two versioning problems are easy to conflate, and only one is solved above.**

- **The user's definition** (data) — solved by pinning.
- **Our workflow code** (the interpreter) — *not* solved by pinning, and still requires
  Temporal's own versioning discipline (patched APIs / Worker Versioning). Changing how the
  engine interprets a Clean block affects in-flight runs regardless of which definition version
  they pinned.

The second brings a **new failure class**: workflow code must be deterministic. No I/O, no
`datetime.now()`, no `random()`, no direct database or MinIO access inside a workflow body —
those belong in activities. This is a standing review rule, not a one-time cleanup.

### 7. OQ-3 — One lane: disjoint identity, plus an engine-level uniqueness guarantee

**Decision: "exactly one lane" is enforced by three stacked mechanisms, none of which is a
routing flag or a convention.**

1. **Disjoint identity spaces.** A pipeline run is a `pipeline_runs` row and never a `job_runs`
   row. v1 executors read `job_runs` and NATS subjects; v2 executors read Temporal task queues.
   No object exists that *could* be picked up by both. For layer A this makes R5 trivially true —
   see [§16](#16-the-v1v2-coexistence-contract).
2. **Workflow ID uniqueness, for flows that do migrate.** When jobs move to `JobWorkflow`, the
   workflow ID is derived from the run identifier (`job-run-{run_id}`). Temporal refuses to start
   a second execution with a workflow ID that is already running. Double-start becomes impossible
   *at the engine*, rather than prevented by a check we wrote.
3. **`schedule_status` is the interlock for recurring work** (cutover gotcha #2). A job moved to
   a Temporal Schedule must be `paused` in v1 or it fires on both lanes. This is what Q4's
   deliberately tri-state flag is for.

**Ordering matters for mechanism 3, and the safe order is the counter-intuitive one:** pause in
v1 → confirm no v1 dispatch is in flight → *then* create the Temporal Schedule. The reverse order
leaves a window in which both lanes are armed. A double scrape costs a double render; a double
LLM stage bills the user twice.

One property helps: the NATS stream is `--retention work`, so a message acked by v1 is deleted.
There is no replayable backlog a v2 executor could later re-consume.

### 8. OQ-4 — Metering: one run is one unit, pools are shared, and only the final artifact is charged

**Decision:**

**The unit, stated once for every lane** (PM, PRD-016 OQ-4 round 3 — ✅ owner-confirmed
2026-08-08): **one fetch of one target URL that produces one stored result.** Not one user
action, and not one step. So: job run = 1; batch of N = N; **crawl of N pages = N**; pipeline run
= 1, because R1 fixes one run to one URL and the non-Scrape blocks fetch nothing. **Admission
checks the declared ceiling; the meter charges actuals** — a crawl is pre-checked against
`max_pages` exactly as a batch is against `len(urls)`.

- **`monthly_runs_limit`: a pipeline run is one unit, regardless of block count.** Per-block
  metering makes the R6 gate pipeline cost 3 units where the identical job costs 1 — a direct
  violation of the PM's hard constraint (a), and it makes pipelines a *penalty* for expressing
  the same work differently. The arbitrage risk in the other direction is bounded structurally by
  R1's max-blocks-per-pipeline, and the genuinely expensive resource — LLM tokens — is billed to
  the user's own provider key regardless.
  **⚠️ Rider from the PM:** "regardless of block count" holds only while a pipeline fetches once.
  If layer A ever permits more than one Scrape block in a chain, that run fetches twice and must
  cost 2. The PM's preference is to **count executed Scrape blocks** rather than cap Scrape at one
  — the one-Webhook cap had a layer-ownership reason (fan-out belongs to C) with no analogue here.
- **`concurrent_jobs_limit`: one shared pool, not two.** The limit exists to protect worker
  capacity, and worker capacity is shared between lanes. A separate pipeline pool would let one
  user consume twice the capacity, which inverts the limit's purpose. **The ceiling counts runs
  actively executing a block, not runs that exist** — see [§15](#15-oq-11--webhook-delivery-is-a-step-the-run-waits-for).
  **A crawl is one concurrency unit, not N** ([§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)) —
  the one place cost and contention deliberately disagree.
- **`storage_bytes_used`: every stored *final* artifact is charged, on every lane.** The mechanism
  is unchanged; the **call sites are not lane-agnostic and must be fixed** (see
  [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)).
  **Every crawl page is a final artifact** — a crawl has no intermediates — so the
  only-the-final-artifact rule applies to crawls with no exception.

  Storage is the axis where crawls are most dangerous: 10,000 pages at BUG-003's measured
  291 KiB–4.1 MiB is **2.8–40 GB from a single API call** against a 5 GB wall. Two conditions the
  PM attaches, both of which are architectural obligations rather than product preferences:

  - **Reclaim ships with counting.** Verified independently: **nothing frees crawl artifacts
    today.** `DELETE /crawls/{id}` (`routers/crawls.py:127`) is cancel-only and removes no objects;
    the admin user-delete (`admin.py:195`) and job hard-delete both enumerate `JobRun.result_path`
    only, so **deleting a user orphans their crawl artifacts in MinIO**. Charging against a hard
    wall with no way to free space is a support incident by construction.
  - **Accounting starts at cutover.** No backfill, no reconciliation of history — and deleting a
    pre-cutover artifact must not decrement, or the counter goes negative.

That last clause is what satisfies the PM's metering-parity constraint on the storage axis.
Because blocks pass references, every block output is a real MinIO object; charging all of them
would make a 3-block pipeline consume 3× the storage of the job it reproduces for identical work.
So: **intermediate block outputs are an operator-side cost, retained for a bounded,
operator-configurable window for debuggability, then garbage-collected. They are never charged to
the user's quota.** The user is charged for the result they keep, exactly as with a job.

Failure context is retained unconditionally (the PM's stated position): the failing block, its
input reference, and its error survive garbage collection, because "see why a step failed" is
R3's purpose.

### 9. OQ-5 — Reuse existing workers via option (a) first

**Decision: activities dispatch to the existing NATS workers for the R6 gate. Move to option (b)
— workers as native activity workers — on a named trigger, not on a schedule.**

R6 is a test of the **pipeline model**, not of the transport. Rewriting three worker entry points
in the same increment confounds the experiment: a failing gate could mean the model is wrong or
the rewrite is buggy, and you cannot tell which. Option (a) leaves workers **completely
unchanged**, so a failed gate is unambiguously the model's fault. That is the entire value of
having an acceptance gate.

**Move to (b) when R6 has passed *and* any one of:** the extra orchestration hop becomes
user-visible latency; we need activity-level heartbeating or cancellation that the NATS hop
cannot express (a future abortable-Scrape needs exactly this); or per-activity retry visibility
matters more than the migration cost.

> **⚠️ Option (a) recreates the Q5/Q6/Q7 failure mode unless explicitly handled, and this is the
> most likely way to reintroduce a solved bug.** Under (a) there are **two retry layers** on the
> same work: Temporal's `RetryPolicy` on the dispatch activity, and JetStream redelivery
> underneath it. That is precisely R4's "retry must live in exactly one visible layer" violated,
> by the migration itself. The whole Q5/Q6/Q7 cluster was retries hidden in a layer nobody was
> looking at, and the compounding billed users for it.
>
> **Requirement: for v2-dispatched work, the NATS layer must not retry.** The activity owns
> retry; the worker-side nak/backoff path and JetStream redelivery must be neutralised for
> messages originating from a workflow. This is a contract obligation on the option-(a) bridge,
> not an optimisation.

### 10. OQ-6 — The do-not-delete list

These live inside code the migration removes, and are **requirements of the activities that
replace it**. Each was paid for with a production incident.

| Behaviour | Where it lives now | Where it goes |
|---|---|---|
| **LLM cold-start handling** — `ensure_ready()` warm-up probe against `/models` + 180s request timeout | `llm-worker/worker/llm.py` | Into the LLM activity. Temporal has no idea a scale-to-zero endpoint is cold; it would simply retry a timing-out activity and re-bill the user's key |
| **Transient/terminal classification** for storage and provider faults, incl. the aiohttp-unreachable case | `llm-worker/worker/errors.py`, `playwright-worker/worker/errors.py`, `http-worker/internal/worker/errors.go` | Activity `RetryPolicy` non-retryable error types. **Fail-closed default preserved** (unknown → terminal) |
| **Bot-wall detection**, tiered, terminal, `blocked:<vendor>` | `playwright-worker/worker/blocking.py` | Stays in the Scrape activity, unchanged semantics |
| **SSRF re-validation on every delivery attempt** | `webhook_loop.py` | Inside the webhook activity — see [§15](#15-oq-11--webhook-delivery-is-a-step-the-run-waits-for) |

**A constraint the port must respect:** the LLM activity's start-to-close timeout must exceed
*warm-up budget + request budget*, or a legitimate cold start times out the activity. This is Q6
in a new costume — an outer layer less patient than the work inside it — and it is exactly the
composition rule R4 makes a hard requirement.

**One addition to the published list.** `temporal-full-migration.md` §4 assigns content dedup
(`xxhash`) and `diff.py` to "a diff/dedup activity, pure logic reused verbatim." The PM has since
assigned **both halves of change detection to Monitors (B)**, which is not yet specified. So
these are **relocated, not deleted, and not yet re-homed**: `diff.py` and the content-hash logic
must survive the deletion of `result_consumer.py` and wait for B. Deleting them with their
caller is the single most likely accidental loss in this migration.

### 11. OQ-7 — Run state to the SPA: mirror activity plus `pg_notify`

**Decision: preserve the existing contract. A status-mirror activity writes the app-side mirror
row and emits `pg_notify` at each stage, exactly as `result_consumer.py` does today. Do not
stream Temporal events to the browser.**

- The SPA and its WebSocket path do not learn that Temporal exists — zero frontend change for the
  job path.
- The mirror row is needed anyway for listing, querying and admin views ([§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)),
  so the notify is nearly free on top of a write that has to happen regardless.
- Streaming engine events would put Temporal on the request path for a pure UI concern and couple
  the SPA's behaviour to engine retention settings.

**Pipelines get their own notify channel with a JSON payload — they do not reuse `job_status`.**
That channel's payload is a positional colon-delimited string (`job_id:run_id:status`) parsed by
`JobNotifier`; it cannot carry per-block detail, and widening it would break every existing
subscriber. `batch_status` already demonstrates the JSON-payload pattern to follow. Overloading
one status vocabulary to serve two different shapes of consumer is the Q8 mistake at a different
altitude.

**Temporal Web UI is an operator tool** and is not part of any user-facing surface. Per
[§2b](#2b-the-web-ui-is-not-ingress-exposed) it is not exposed at all — `kubectl port-forward`
only — so nothing user-facing may depend on it being reachable.

### 12. OQ-8 — Tenant isolation: single namespace, and the API is the only boundary

**Decision: one Temporal namespace. The API is the enforcement boundary — the sole one. Tenant
identity is deliberately *not* encoded in the workflow ID.**

Namespace-per-user is rejected: namespace provisioning would become part of signup, per-namespace
configuration drifts, and Temporal namespaces are a much heavier boundary than the isolation we
need. Namespace-per-*tier* is a reasonable future step for noisy-neighbour isolation and is
explicitly left open, but buys nothing at current scale.

**The real boundary does not move.** Cross-tenant access returns 404 because the API checks
ownership of the `pipelines` / `pipeline_runs` row before it makes any engine call. The engine
never receives an unauthorised request because the API never issues one.

**Reversed on review (2026-08-08): an earlier draft of this section claimed that encoding
`user_id` in the workflow ID "adds a second, structural property" — operator queries scopable by
tenant, and signals that cannot reach another tenant's run by accident.** Both claims are
withdrawn, and the ID keeps the plain form
[§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
defines (`pipeline-run-{pipeline_run_id}`, `job-run-{run_id}`). Four reasons:

- **It would not be structural.** Temporal treats a workflow ID as an opaque string and never
  parses it. An embedded `user_id` protects nothing unless some other layer reads it back and
  validates it — which is *a check we wrote*, precisely what
  [§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee)'s mechanism 2
  is valuable for **not** being. Claiming both under one banner would attribute the uniqueness
  guarantee's strength to something that does not have it.
- **Tenant-scoped operator queries contradict [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table).**
  "List this user's runs" is exactly the class of question this ADR says is *never* answered from
  Temporal. `pipeline_runs` holds both `user_id` and the workflow ID, so the app database answers
  it better and hands back the precise IDs to look up.
- **The accidental-signal case is already closed upstream.** The API loads the run row, checks
  ownership, then derives the workflow ID *from that row*. Once ownership is proven the format is
  irrelevant. The only path an embedded `user_id` would rescue is one that builds an ID from
  user-supplied input without loading the row — a path that must not exist, and whose real defect
  would not be the ID format.
- **It keeps tenant identity out of engine-side data.** Workflow IDs travel into event history,
  task-queue metadata, logs and metrics, and into archival if that is ever enabled
  ([§2c](#2c-namespace-retention-is-30-days)). There is no reason to seed user identifiers across
  all of it for a property we just established is not real.

[§2b](#2b-the-web-ui-is-not-ingress-exposed) independently removed what little the operator-query
argument had left: with the UI unexposed, its audience is one operator who owns all the data, at a
workstation, during incidents — a filter convenience, not an isolation mechanism.

**The consequence to hold onto: there is exactly one tenant boundary, and it is the API's ownership
check.** Nothing structural backs it up at the engine, and this section previously implied
otherwise. Any future code path that reaches Temporal without first loading and ownership-checking
the app row is a tenant-isolation bug with no second line of defence.

Task queues are **shared** across tenants — per-tenant queues would require per-tenant workers.

### 13. OQ-9 — The crawl coordinator migrates, last, and a crawl is not a block

**Decision: yes, `coordinator/` migrates to a `CrawlWorkflow` and the service is deleted — but
after jobs and batches, and a Crawl block is *not* added to layer A's catalog.**

A crawl is a fan-out over an unbounded, dynamically discovered set. That is a workflow shape
(child workflows, or a frontier held in workflow state), not a step in a linear chain. Modelling
it as a block would smuggle unbounded fan-out into a model whose non-goals explicitly exclude
fan-out, and would force the block model to express something no other block needs.

`crawl_queue` retires with the service; `crawl_pages` may be kept as a per-page result mirror for
the UI.

**The frontier model is deliberately not decided here.** Visited-set-in-workflow-state with
`continue-as-new` versus child-workflow-per-page is a real trade-off whose binding constraint is
**history size**, and it should be decided against measurements from the crawl migration step
rather than guessed now. Recording the constraint is the useful part; picking a winner today
would be false precision.

### 14. OQ-10 (remaining half) — Conditional execution gets its own layer-A PRD, before Monitors

The PM resolved change detection: both halves go to **Monitors (B)**, because *"the previous run
of this same thing"* is undefinable in layer A once R1's run inputs exist. That decision handed
back the sequencing of conditional execution.

**Decision: conditional execution is specified in a follow-up layer-A PRD, written before
PRD-018 (Monitors). It is not absorbed into B.**

If B absorbs it, the reviewer of B's PRD is designing layer A's block model again under a
different heading. That is the "unplanned layer-A work" OQ-10 was raised to prevent — absorbing
it does not prevent the work, it only renames it and hides it inside a PRD nobody will read as a
layer-A change.

The cost of a separate PRD is low precisely because [§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)
already left the room: explicit named wiring, immutable block identifiers, graph-shaped storage
with linearity enforced in the validator, and a `skipped` block state. The follow-up PRD is
additive to the model rather than a re-specification of it. That is the payoff of the
forward-compatibility constraint, and it is worth stating that the constraint has now been *spent*
on something concrete rather than held as a vague intention.

**Resulting order:** A ships → **C (Delivery sinks) is unblocked and may proceed in parallel**,
since it adds block types without extending what a pipeline can express → conditional-execution
PRD → **B (Monitors)**.

### 15. OQ-11 — Webhook delivery is a step the run waits for

**Decision: option (c). The Webhook block waits for real delivery on its own durable horizon.
There is no `webhook_deliveries` row for the v2 lane.**

- **The activity's retry policy *is* the delivery loop.** Workflow history is the attempt record
  and the Web UI shows it. A parallel table would be a second source of truth for "did it
  deliver."
- **The horizon is matched to today's reach** so no capability is lost. `BACKOFF_SECONDS`
  (`[0, 30, 300, 1800, 7200]`) across `webhook_max_attempts = 5` gives a total reach of
  **≈2.6 hours**. A block horizon in that range reproduces today's "a receiver down for two hours
  still gets its delivery" exactly. This removes one dimension of R6 divergence rather than
  accepting it.
- **The PRD's objection to (c) dissolves.** It worried that runs waiting hours on dead receivers
  would consume a user's concurrency budget. But that ceiling protects **worker capacity**, and a
  workflow sleeping on a durable timer occupies none — it is not resident anywhere. Hence
  [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-only-the-final-artifact-is-charged)'s
  rule: the ceiling counts runs **actively executing a block**. The collision was an artefact of
  counting the wrong thing.
- **The PM's one-Webhook-block cap bounds this to at most one open delivery per run**, which
  removes the pathological case entirely.

**Rejected — (b), "succeeds once durably queued."** It makes "the block succeeded" mean *queued*
rather than *delivered*, which is the quiet lie R3's per-block status exists to eliminate. It is
also not the cheap reuse it appears to be: `webhook_deliveries` carries two CHECK constraints —
`num_nonnulls(job_id, batch_id, crawl_id) = 1` and `num_nonnulls(run_id, crawl_id) = 1`, with
`run_id` an FK into `job_runs` — so a pipeline delivery row is **rejected by the database**. (b)
requires a migration loosening both constraints before it can be considered free.

**Rejected — (a), "fail the run, retries bounded by the block budget."** Consistent and simple,
but it silently loses today's multi-hour reach.

SSRF re-validation stays **inside the activity, on every attempt** — DNS rebinding is why it is
per-attempt rather than per-creation, and that reason is unchanged by the transport.

**Recorded against R6:** an undelivered webhook fails a pipeline run where it never fails a job.
That is a known exclusion, already in PRD-016.

**One consequence for v1:** because layer A writes no `webhook_deliveries` rows, the existing
`idx_webhook_deliveries_dedup` unique index on `(run_id, event)` keeps its assumption true and the
existing machinery stays correct for the job lane. The PM's one-block cap makes this durable
rather than coincidental.

### 16. The v1/v2 coexistence contract

**Definitions.** **v1** = NATS JetStream + `result_consumer.py` + `scheduler.py` +
`webhook_loop.py` + `advisory.py` + `coordinator/`. **v2** = Temporal (server, workflow workers,
and eventually activity workers).

**Routing rule.** New **pipelines** are v2-only from the first day they exist — there is no v1
pipeline implementation, so there is nothing to route between. Existing **jobs, batches and
crawls** stay on v1 until their flow is explicitly migrated.

This is worth stating plainly because it changes the risk profile of the first increment: layer A
**adds a lane** rather than splitting an existing one. R5's "exactly one lane" and OQ-3's
structural enforcement are trivially satisfied for pipelines, and become a real problem only at
migration step 2, when jobs move to `JobWorkflow` and the same unit of work has two possible
executors.

**Cutover obligations** (from `phase4-backlog.md` §2, restated as contract terms):

1. A unit of work executes on **exactly one lane**, enforced per [§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee).
2. A recurring job moved to a Temporal Schedule is **paused in v1 first**, in that order, with
   the pause verified before the Schedule is created.
3. NATS workers stay alive under option (a) until v1 is drained. Worker cutover to activities is
   what removes v1's executors, and it happens after, not during, the flow migration.

**Sequence** (`temporal-full-migration.md` §9 is the detailed version): stand up Temporal → jobs
onto `JobWorkflow` via option (a) → workers to activity workers (option b) → batches and crawls →
scheduling and webhooks → delete `result_consumer.py` → remove NATS → lift the API's
single-replica/`Recreate` constraint.

**The shape of coexistence is drawn in `temporal-full-migration.md` §9a**, not here: it changes at
four of the seven steps, so it is sequence material rather than a decision. Two things it shows
that this contract only states in words — the API keeps `replicas: 1` and all four loops until
step 5 (the thinning is the *last* payoff, not the first), and the three workers serve **both
lanes at once**, which is what makes option (a) cheap and the [§9](#9-oq-5--reuse-existing-workers-via-option-a-first)
retry-stacking hazard real.

**Reversibility.** Every step is a reversible increment: a misbehaving flow falls back to the v1
path until fixed. This holds **only while both lanes exist**, which is the reason the deletion
step is last and gated.

**Deletion gate.** A v1 component is deleted when its flow is fully drained *and* its NATS
consumers report zero unprocessed messages and zero outstanding acks. Verify consumer state with
`nats consumer info --json` — the table output omits `Max Deliver` when it is `-1`, so it cannot
distinguish a capped consumer from an uncapped one.

**What explicitly does not change:** the scraping muscle (Patchright/headed-Chrome stealth per
ADR-008, the Go fetcher, LLM call logic, formatters, robots handling), MinIO result storage for
the v1 lane, Clerk auth, Redis rate limiting, Fernet secret encryption, the cross-tenant = 404
invariant, and the bulk of the existing CRUD test suite.

### 17. Relationship to ADR-001/002/004

ADR-009 **will** supersede parts of ADR-001 (§2 subjects, §3 schemas, §8 MinIO paths), ADR-002
(the Phase 2 worker contract), and ADR-004 (fat message schema v2) — but **not yet**. Those
contracts remain authoritative for as long as v1 serves traffic, and marking them superseded now
would mislead anyone maintaining the live system.

The one partial exception is recorded in [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity):
the v2 artifact path convention departs from ADR-002 §8, for the v2 lane only.

The supersession notices are added when the corresponding v1 component is deleted, per the ADR
index's own rule.

---

## Consequences

**Gained**

- Retry, timers, and crash recovery become declarative configuration instead of five hand-rolled
  loops. The Q8 class of bug — an overloaded status value producing an unintended transition —
  cannot occur, because the engine owns the state machine.
- The ack_wait/redelivery class of bug (Q6) disappears with NATS.
- Per-block status and timing, which is R3's largest observability gain over today's opaque
  `job_runs.status`.
- The API becomes stateless and horizontally scalable once the background loops leave it.
- A per-workflow timeline in the Temporal Web UI replaces `grep status=` log-spelunking — a real
  debugging upgrade for exactly the Q8 class of incident.
- Temporal's time-skipping test framework makes long sleeps and monitors testable in
  milliseconds, which is what makes layer B testable at all.

**Paid**

- The heaviest infrastructure dependency in the project: Temporal Server plus its own Postgres,
  on a single-node cluster with no HA.
- **The Temporal database becomes critical in-flight state** and needs backup/restore discipline
  that did not previously exist. Losing it loses running work, not just history.
- **Workflow history retention is a new, ongoing storage cost** that scales with completed runs
  and does not self-clean the way `--retention work` does. [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)'s
  references-not-payloads rule is what keeps it proportionate; retention is set at **30 days**
  ([§2c](#2c-namespace-retention-is-30-days)) and archival stays off.
- **A second migration mechanism.** Temporal's schema is `temporal-sql-tool`'s, on Temporal's
  release cadence — separate from Alembic, and not applied by anything in the current deploy flow
  ([§2a](#2a-temporal-persistence-is-a-separate-postgres-instance)).
- **Determinism bugs are a new failure class.** Non-deterministic workflow code breaks replay,
  and it fails *after* a restart rather than at the point of the mistake.
- Two SDKs (Go and Python) means a small duplication in activity-worker setup.
- During option (a), two orchestration systems run concurrently, with the retry-layering hazard
  called out in [§9](#9-oq-5--reuse-existing-workers-via-option-a-first).

**Risk explicitly named**

The end state is clean, and that is precisely what makes jumping to it directly tempting. The
strangler-fig sequence exists to prevent that, and the deletion gate exists to make "we are
finished with v1" a measured fact rather than an assumption.

---

## Deliberately not decided here

| Item | Why deferred, and to what |
|---|---|
| Crawl frontier model (visited-set + `continue-as-new` vs child-workflow-per-page) | Binding constraint is history size; decide against measurements at the crawl migration step ([§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block)) |
| Temporal **archival** (closed histories → MinIO) | Retention itself is now decided — 30 days ([§2c](#2c-namespace-retention-is-30-days)). Archival stays off: another moving part, for data currently worth little. Revisit if history growth ever forces a *shorter* retention |
| **Web UI ingress exposure** (post-Phase 4) | Not exposed for now — `kubectl port-forward` only ([§2b](#2b-the-web-ui-is-not-ingress-exposed)). If always-on access is wanted later, choose between Traefik `basicAuth` (the mlflow pattern; cheap, one shared credential, no attribution) and `forwardAuth` against an API admin endpoint (one identity system; must be built, couples UI to API availability, depends on Clerk's session cookie domain). Owner's call, 2026-08-08 |
| Namespace-per-tier | Buys nothing at current scale; revisit under noisy-neighbour pressure ([§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary)) |
| Conditional execution's design | Its own layer-A PRD, before PRD-018 ([§14](#14-oq-10-remaining-half--conditional-execution-gets-its-own-layer-a-prd-before-monitors)) |
| Run-failure notification (R6's fourth exclusion) | The PM left it unassigned on purpose: it is either an on-failure branch or a run-level setting, and that depends on the conditional-execution decision above |
| Whether `webhook_deliveries` / `crawl_pages` survive as v1-only audit mirrors | Decide at the step that retires each flow, not now |
| Retention window for intermediate block outputs | Operator dial; needs a real number from the first pipelines ([§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-only-the-final-artifact-is-charged)) |

---

## Implementation reference

- **PRD-016** — the product spec this design serves. R6 is the acceptance gate: reproduce
  `scrape → LLM → webhook` as a pipeline before designing any block outside R2's catalog.
- **`temporal-full-migration.md`** — component-by-component change inventory and the
  strangler-fig sequence.
- **`phase4-backlog.md`** §3 — bugs the migration dissolves; do not design fixes for them.
  **§1 P6 / BUG-005** — the batch identity failure that grounds [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
  and [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity).
- **`open-questions.md` Q5–Q8** — the incidents behind [§9](#9-oq-5--reuse-existing-workers-via-option-a-first)'s
  retry-layering warning and [§10](#10-oq-6--the-do-not-delete-list)'s port list.
- **ADR-008** — the scraping behaviour that must survive the transport change untouched.
