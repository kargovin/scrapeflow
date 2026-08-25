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
property was never structural).
**§4 reviewed 2026-08-10** — five corrections, two of them owner calls: **layer A validates a
single chain in data flow as well as execution order** (data-flow fan-out is wanted but deferred
post-Phase 4, and the PRD problem it leaves unfixed is now a recorded known exclusion), and **run
inputs are config bindings restricted to per-type declared fields** (today only Scrape's `url`).
Also corrected: block identifiers must be **stable across versions**, not merely immutable once
assigned; the block-state column is **named** (`pipeline_run_blocks.status`) rather than asserted;
and per-type config-schema versioning is acknowledged as the residual DSL cost. Knock-ons applied
to **§5** (run-input scalars are an exception to "inputs are references") and **§8** ("final" =
last block in execution order, pinned before fan-out can make it ambiguous).
**§5 reviewed 2026-08-10** — decision upheld and strengthened. The payload figures are now
**measured, not hedged** (256 KiB warn / 2 MiB error), which puts the *entire* BUG-003 page range
past a threshold; the self-hosted config escape hatch is named as a trap that would undo §5 and
§2c together. Two owner calls: the catalog splits into **content-producing (Scrape, Clean, LLM)
and effect (Validate, Webhook)** blocks, with effect blocks passing their input reference through
unchanged — **"one object per block" was false of two of the five types**, which §4's strict-path
data flow turned into a correctness question; and **the run's result, and the charged artifact,
are both the last *content-producing* block's output**, without which **R6's own gate pipeline**
(ending in a Webhook) has no result and charges zero. *(The second half of that call — the charged
artifact — was **superseded by the §8 review on 2026-08-17**; the result half stands.)* Second
call: the intermediate-output
retention window is **a product promise, not a free dial** — result never collected, collection
per run not per block, collected renders as *collected*, and per-block status in the app DB
outlives both Temporal retention and the window. Knock-on applied to **§8** (metering + GC rules).
**§6 reviewed 2026-08-10** — decision upheld, **its stated reason replaced.** The replay argument
was factually wrong (Temporal replays workflow *code* against recorded history, and **input
arguments are part of that history**, so a definition passed in as an argument is pinned
automatically); the described failure needs a workflow body that loads the definition from
Postgres, which this section's own determinism rule forbids. The real basis is **semantic**: a run
that executed the old shape cannot continue into a new one. Four previously unstated things now
decided: the definition travels as a **workflow input argument**; the pinned version is recorded
in **`pipeline_runs.pipeline_version_id`**; a pipeline with a run in flight **may be deleted and
the run finishes** (deleting a definition is not a back-door cancel — the Q4 split again); and
**run history holds the name** — delete with no runs frees it, delete with runs soft-deletes and
409s on reuse. **Worker-code versioning promoted from a passing mention to an explicit deferral**
(Worker Versioning is GA and Temporal's default; needs server-side enablement, so it is infra).
**§7 reviewed 2026-08-10** — the section **under-covered its hardest case**, and gained a fourth
mechanism. Mechanism 1 is now explicitly **pipelines-only**: a migrated job keeps its `job_runs`
row *by requirement* (§3 makes that table a read-model mirror; R5 forbids user-visible change), so
from migration step 2 the covering set drops to mechanism 2 alone. Into that gap:
**`_recover_stale_pending` (`scheduler.py:131`) re-publishes any `job_runs` row stale at `pending`
past 10 minutes, to NATS, with no lane filter** — so a v2-owned run whose workflow has not started
is dispatched to a v1 worker, and mechanism 2 never intervenes because no second *workflow* was
started. Hence **mechanism 4: a lane marker on `job_runs`, written in the insert transaction,
built at step 2** (✅ owner's call). Mechanism 2 also **over-claimed** — the default
`WorkflowIdReusePolicy` is `ALLOW_DUPLICATE`, which permits a new execution once the prior one
closes, so **`REJECT_DUPLICATE` must be pinned** for "once, ever". Two smaller: the
`--retention work` reassurance covered only the safe half (**unacked** messages are the risk, tied
now to §16's drain gate), and mechanism 3's **rollback ordering** is stated since §16 claims
reversibility. Carried to §9: mechanism 1 is true of rows, not messages — under option (a) v2
results land on `scrapeflow.jobs.result` where `result_consumer` is subscribed, with neither FK
set, which is BUG-005's shape one lane later.
**§8 reviewed 2026-08-17** — **the storage rule is reversed, and §5 is amended to match.**
✅ Owner's call: **the meter measures bytes on disk.** Every object a run still holds is charged,
on every lane, for as long as it is stored — replacing "only the final artifact is charged" and
withdrawing §5's clause that the result and the charged artifact are the same object. §5 keeps
*the result* (R3, permanence); §8 owns *what is charged*. The rule is simpler — it needs no notion
of finality, so fan-out, effect blocks and shared objects stop being special cases — and it makes
intermediate-output collection a **user-visible refund** rather than housekeeping. Knock-ons:
screenshots become chargeable (BUG-004's other half), and the parity argument both sections leaned
on was **factually inverted** — the job path charges the *scraped page*, not the LLM output.
That trace surfaced **two unfiled defects on live code**, neither touched by the migration:
hard delete decrements by the JSON while the HTML was what was added, so the counter is
**permanently inflated by every deleted LLM job**, and **the scraped page is never deleted at
all**. Root cause named: `_try_increment_storage`'s idempotency stamp is keyed on the **run**
when it should be keyed on the **stored object**, so a redelivery and a genuinely second artifact
are indistinguishable. ⚠️ **Left open:** the `latest/` + `history/` dual write stores 2× what the
meter counts, so "charge what is stored" is not implementable until it is decided whether the
convenience copy is chargeable (recommendation: charge one copy; v2 already drops `latest/`).
Three more owner calls: **one submission = one concurrency slot on every lane** — which makes cost
and contention disagree *in general* rather than only for crawls, and incidentally fixes
`batch.py:46-47` admitting a 100-URL batch as 1 while metering it as 100; **a pipeline run parked
on a durable timer does not hold its slot, while v1 lanes keep today's behaviour** — §3 and §15
had contradictory definitions of "active" and §15's webhook horizon only survives under one of
them; and **the PM's multi-Scrape rider is closed** — §4's single-chain data flow plus "Scrape
consumes nothing" already makes a second Scrape unsatisfiable at Save, so the rider's premise
("R1 fixes one run to one URL") was wrong even though its conclusion held, and the trigger to
reopen is **multiple roots, not fan-out**. Because that guarantee is emergent from two rules
stated pages apart, §8 now asserts it directly: **a layer-A pipeline has exactly one starting
block, and it is a Scrape block.** Three gaps recorded rather than closed: the unit is now "one
fetch **attempted**" (the meter never waits for a result, and a failed scrape already costs a run
today) with **a retry explicitly not a new unit**; **`crawl_pages` has no accounted-at marker**, so
the "counting starts at cutover, no backfill" promise has no mechanism on two of four lanes; and
**per-run collection is safe only while no object is shared *between* runs** — v1's content-hash
dedup already shares them, so Monitors will break it. §8d names two things no section owns: which
component performs v2 accounting, and what happens when a pipeline hits the storage wall at its
**last** block, after the user's LLM key has already been billed.
**§9 reviewed 2026-08-23 — the decision is REVERSED: option (b) first, the NATS bridge rejected.**
✅ Owner's call. The workers gain native Temporal activity entry points in the **first** increment;
activities never dispatch through NATS. The draft's argument — keep workers untouched so an R6
failure is unambiguously the model's fault — was sound in form and failed on four premises.
**(1)** "Rewriting three workers" overstated the change by an order of magnitude: only ~10–22% of
two files per worker is transport, and everything expensive (`blocking.py`'s 313 lines of bot-wall
detection, the Patchright stealth setup, `formatter.go`, `llm.py`, the MinIO clients) touches NATS
**not at all**. The Go worker's `processJob(ctx, job, fetcher) (string, error)` **already has the
shape a Temporal activity wants**. **(2)** About half of what option (a) preserved — `ack_wait`,
the 30s heartbeat, `max_deliver`, the nak ladder — exists *because* JetStream redelivers, so it is
**deleted, not ported**; §10's genuine carry-forwards port identically under either option, so (a)
bought nothing and kept the code alive in two places. **(3) The bridge is blocked, with a dead
service in production proving it:** `SCRAPEFLOW` is `--retention work` (verified on the live
stream), and a work-queue stream refuses a second consumer overlapping `api-result-consumer`'s
claim on `scrapeflow.jobs.result`. The coordinator attempts exactly that at
`result_handler.py:203`, and **`coordinator-result-consumer` has never existed** —
`result_handler_subscribed` appears zero times in its log while the sibling `dispatch_loop_started`
logs fine, because `main.py:82`'s `asyncio.gather(..., return_exceptions=True)` swallows the
exception. The pod is healthy with half of itself dead; **no crawl has ever run in production**, so
nothing surfaced it (BUG-005's shape — ⚠️ **not yet filed**). The remaining answer, polling for the
row `result_consumer.py` writes, puts **the Q8 component on the critical path of every pipeline
run** — the thing this ADR exists to delete. Worse, a v2 result today is **destroyed, not ignored**:
`db.get(JobRun, run_id)` misses, and the handler acks, which on a work-queue stream deletes it
(§7 predicted "neither FK set"; the real failure is a step earlier and final). **(4)** "Workers
unchanged" and "NATS must not retry" are **incompatible** — the retry lives in worker code
(`worker.go:316`, `llm-worker/worker/worker.py:128`) and in per-consumer `max_deliver`, so
neutralising it per-message is a worker change either way. The honest comparison was **a new bridge
plus unchanged workers** vs **a thin permanent adapter plus unchanged worker logic**, where the
bridge is the larger body of novel code, sits in exactly the area that produced Q5–Q8, and is
deleted at the next step regardless. The gate survives via a **required pre-gate**: run the Scrape
activity standalone and diff against a v1 run of the same URL, which is the isolation (a) claimed
and cannot provide. Costs accepted: each worker runs as **two deployments of one image** during
coexistence (mode flag, not one process serving both); three integrations rather than one bridge
(two share a language, and (a)'s "one place" was illusory per premise 4); and ⚠️ **the Playwright
container contract** — Xvfb then `exec python` as pid 1, never `xvfb-run` — must be preserved
exactly, the riskiest part of the port. Sequence: **Go http-worker → LLM → Playwright**. Knock-ons:
§16's sequence moves the worker port **from third to first**, §7's result-path carry-forward is
**moot**, §2's "not in the first increment" is reversed, and the residual retry hazard is now only
that §10's ported classifier must become **`RetryPolicy` non-retryable error types**, not its own
loop inside the activity.
**Next: §10** (the do-not-delete list — now first-increment work, since the workers port first).
§11 and §13–§17 not yet reviewed.
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
- The three scrapers become **activity workers** (Go SDK for http-worker, Python for the others).
  **In the first increment, not eventually** — see [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected), which rejected the NATS bridge that
  would otherwise have deferred this. Each runs as two deployments of one image during
  coexistence: one bound to NATS for v1, one to a Temporal task queue for v2.

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
- **`concurrent_jobs` counts distinct concurrency *groups*** among active rows, where the group is
  **the submission**: the crawl for crawl pages, the **batch** for batch items, and the row itself
  for single job runs and pipeline runs.

That split is deliberate: cost and contention are different quantities. Per-page concurrency would
put every default crawl (`max_pages` 100) permanently over the default ceiling of 5 — a meter that
makes a shipped feature unrunnable is misconfigured, not strict — and enforcing it would need
dispatch throttling inside `coordinator/`, which [§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block)
deletes.

> **Amended 2026-08-17 (§8 review), two changes.** ✅ Owner's call: **the group is the submission
> on every lane**, so a batch of N is one slot, not N. The earlier text grouped only crawl pages
> and left batch items as individual rows — which does not match `batch.py:46-47`, where admission
> already checks the ceiling **once for the whole batch** and then inserts N rows. Admitting as 1
> and metering as 100 locks a user out of a 5-slot pool with a single call. Crawls were never the
> only place cost and contention disagree; under this rule they disagree everywhere, by design.
>
> Second: **"non-terminal" is replaced by "active", and active is per-lane.** For v1 lanes it means
> exactly what `quota.py:59` means today — `pending`, `running`, `processing` — and R5 forbids
> narrowing it. For pipeline runs it excludes a run parked on a durable timer, without which
> [§15](#15-oq-11--webhook-delivery-is-a-step-the-run-waits-for)'s ≈2.6 h webhook horizon would let
> one dead receiver hold a slot for hours. The view therefore carries a lane-aware predicate on
> this column; that cost is accepted in
> [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored).

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
mechanism. [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)'s
storage rule presumes something does the charging; on the v2 lane that something must be named,
not inherited. *(Still unnamed as of the §8 review — [§8d](#8d-who-charges-and-what-happens-at-the-wall--not-yet-decided).
The rule it refers to is no longer "only the final artifact is charged": §8 charges every object
a run holds, which widens what the unnamed component is responsible for rather than narrowing
it.)*

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

**Decision: a fixed catalog of typed blocks, stored as JSON in Postgres. Every block carries an
identifier that is stable across versions of its pipeline and names its input by explicit
reference to an earlier block. Data flow in layer A is a single chain; the stored shape is a
graph, so relaxing that is a validator change rather than a data migration. Run inputs are
config bindings, not graph edges.**

- **Fixed catalog, not a general DAG schema.** R2's catalog is closed, user-authored code is a
  non-goal, and R1's save-time validation requires knowing each type's declared consumes/produces.
  A general DAG schema would defer all of that to run time.
- **JSON in Postgres, not a DSL.** A DSL needs a grammar, a parser, versioning *of the grammar*,
  and its own error messages. At five block types that is pure cost. JSON validates against a
  schema and produces field-level errors for free.
- **Explicit named input references, not implicit previous-block wiring.** PRD-016 is internally
  inconsistent without it: R1's validation clause says a block's input must be producible by
  "anything before it," while Non-goals says chains are linear. Both hold only if a block can name
  an earlier block. **Under the linearity decision below, explicit wiring buys no capability that
  implicit wiring could not express today** — it is bought for forward-compatibility (see the two
  bullets below on execution order vs data flow) and because named references need identifiers, which
  OQ-2's pinning and R3's per-block history require regardless.
- **Block identifiers are stable across versions, which is stronger than immutable.** "Immutable
  once assigned" is satisfied by an edit that regenerates every identifier — nothing changed, they
  are merely all new — and under that reading both benefits evaporate: per-block history cannot be
  correlated across versions, and user story 3 ("edit my schema and re-run") produces run history
  that looks like a different pipeline. **The property is therefore an assertion about the edit
  operation, not about the stored row: an update carries block identifiers through, a new
  identifier means a new logical step, and an identifier absent from an update is a deletion.**
  This is what makes OQ-2's pinning meaningful and what lets per-block run history reference a
  block a later edit removed.
- **Execution order and data flow are different properties, and only one of them is linear by
  necessity.** Sequential execution — one block at a time, no parallelism — is what PRD-016's
  "parallel fan-out" non-goal forbids. A *data-flow* fan-out (two LLM blocks both consuming the
  Scrape block's page, executed one after the other) violates neither parallelism nor branching.
  An earlier draft of this section used "a single chain" for both meanings and so rejected, in its
  validator rule, the very example it cited to justify explicit wiring.
- **Layer A validates a single chain in both senses. ✅ Owner's call, 2026-08-10.** Data flow is a
  path: block *n* consumes block *n−1*. Data-flow fan-out is wanted, but **deferred to post-Phase
  4** rather than smuggled in here. The stored shape stays a graph, so lifting the restriction is
  a validator change against unchanged stored definitions — which is the entire
  forward-compatibility requirement, still satisfied at zero cost.
- **The consequence, recorded as a known exclusion.** PRD-016's Problem section lists *"run two
  extractions on one fetched page"* among the things a user cannot do today. **Layer A does not
  fix it** — a second extraction still means a second Scrape, hence a second render and a second
  hit on the target site. This belongs in PRD-016's exclusion list alongside R6's four
  divergences; a problem statement the design does not answer must be visible, not implied by the
  absence of a feature.
- **Per-type config schemas are the residual of the DSL cost, and are versioned like the
  validator, not like the data.** Rejecting a DSL avoids a grammar and a parser; it does not avoid
  needing a schema per block type, a validator over it, and a story for changing it. Config
  schemas live with the block-type definitions in the workflow worker, versioned with the code.
  **The obligation this creates: the validator must remain able to validate historical config
  shapes**, because §6 pins definitions and a pinned old version keeps the config shape it was
  saved under. A block type that gains a required field therefore needs a default for already-saved
  definitions, exactly as a database column would. Adding an optional field is free.

> **✅ Settled on review (2026-08-10).** Five corrections, none cosmetic. **(1) Linearity is now
> stated in both senses** — execution order *and* data flow — where the section previously used
> one phrase for both and contradicted its own motivating example; data-flow fan-out is deferred
> post-Phase 4 and the unfixed PRD problem is recorded as a known exclusion. **(2) Run inputs are
> config bindings, not graph edges** (below), resolving a direct conflict with
> [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)'s
> "inputs are MinIO references" — a URL is not one. **(3) Block identifiers must be stable across
> versions**, not merely immutable once assigned. **(4)** The block-state column is now named
> rather than asserted. **(5)** Config-schema versioning is acknowledged as the residual DSL cost
> with the historical-validation obligation stated.

**Run inputs are config bindings, not graph edges — and only declared fields are bindable.**
A block reading an earlier block's output and a value supplied at trigger time are different
things: the first is a data-flow edge carrying a **MinIO object reference**
([§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)), the second is a
**scalar substituted into a config field**. R1 already separates them — its validation list has
one rule for "a block whose input cannot be produced by anything before it" and a second for "a
block referencing a run input the pipeline does not declare." So:

- **Scrape consumes nothing. It is a source block**, and its `url` config field is *bound* to a
  run input rather than fed by an edge. R1's "a chain that does not start with a source block" is
  read against this: the source is Scrape, not a run-input node.
- **Each block type declares which of its config fields may be bound to a run input.** Today that
  is exactly one field: Scrape's `url`, which is R1's stated minimum. **✅ Owner's call,
  2026-08-10 — narrow, not "any config field."**
- **The reason is that R1's promise is save-time validation, and a wide binding rule dissolves
  it.** Binding the LLM block's extraction schema at run time means Save cannot check the schema —
  the user learns it is malformed *after* paying for a render, standing at the LLM block. Binding
  the Webhook URL turns "at most one Webhook block per pipeline" from a save-time rule into a
  per-run one. Widening later is additive (declare one more field bindable); narrowing later
  breaks saved pipelines.

**The halt-early obligation (from the PM's OQ-10 decision) is satisfied structurally.** Monitors
(B) will need a block that ends a run before its last block *with the run still reporting
success*. Two rules make that additive rather than breaking:

- **Run outcomes stay three** — `pipeline_runs.status` ∈ `completed`, `failed`, `cancelled`. A
  halted-early run is `completed`.
- **Block state is a separate vocabulary from run outcome, and includes `skipped` from day one.**
  The column is **`pipeline_run_blocks.status`**, and its vocabulary is `pending`, `running`,
  `completed`, `failed`, `skipped`. Nothing in R2's catalog produces `skipped`, but the column
  admits it now so that B is a new block type rather than a schema change plus a backfill.
  **Naming the column matters:** an earlier draft asserted "the column admits it" without saying
  which column, and §3 introduces `pipeline_run_blocks` without enumerating its columns — between
  them, the one defence against a future backfill was a claim about a column no section defined.
  A `CHECK` constraint written from the states in use would produce exactly the migration this
  rule exists to avoid.
  **One value covers both cases on purpose:** a block skipped because an upstream gate halted the
  run (B) and a block skipped because its branch was not taken (conditionals,
  [§14](#14-oq-10-remaining-half--conditional-execution-gets-its-own-layer-a-prd-before-monitors))
  are the same fact from the run's perspective — it did not execute, and that is not a failure.
  If the two ever need distinguishing, that is a *reason* column beside the state, not a second
  state.

Overloading a single status vocabulary across two levels of a hierarchy is exactly what Q8 was.
Keeping block state and run outcome distinct is that lesson applied before the fact.

### 5. OQ-1(c) — Blocks pass references; artifacts are keyed on run identity

**Decision: block inputs and outputs are MinIO object references. Page content never enters
workflow history. The v2 artifact path is keyed on the pipeline run and block, not on a job.**

> **Knock-on from [§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)'s
> review (2026-08-10), to be carried into §5's own review.** "Inputs and outputs are references"
> is a statement about **data-flow edges between blocks**. It is not true of **run inputs**, which
> are scalars bound into a block's config (§4) — Scrape's URL is a string, not a MinIO object, and
> Scrape has no input edge at all. Run-input scalars *do* enter workflow history, which is
> harmless and intended: they are the run's arguments, they are bounded, and a run that could not
> see its own inputs could not replay. The rule to carry forward is narrower than it reads:
> **content is never a payload; arguments may be.**

Activity inputs and outputs are recorded in workflow history, which caps individual payload size
and bounds total history size. **The figures, confirmed against Temporal's documentation on
2026-08-10** (an earlier draft said only "low single-digit MB", which understated it):

| Limit | Warn | Error |
|---|---|---|
| Single payload / blob | **256 KiB** | **2 MiB** |
| gRPC message | — | 4 MiB |
| Event history, size | 10 MiB | 50 MiB |
| Event history, event count | 10,240 | 51,200 |

Set those against the BUG-003 audit, which measured genuine pages between **291 KiB and 4.1 MiB**:

- the **largest** measured page is **over twice the hard 2 MiB payload limit** — a content-passing
  model does not degrade on it, it fails outright;
- the **smallest** measured page already **exceeds the 256 KiB warn threshold**. Not the outliers —
  the whole measured range is above the line where Temporal starts complaining.

So the decision is not "content-passing fails on unusually large pages." It is that **essentially
every real page we have measured is at or past a payload threshold**, for reasons that have
nothing to do with scraping.

> ⚠️ **The escape hatch is real and taking it is the wrong move.** We self-host
> ([§2](#2-topology)), so unlike Temporal Cloud these limits *are* configurable
> (`blobSizeLimitError`, `historySizeLimitError`). Raising them is the obvious-looking fix and it
> silently undoes this section **and** [§2c](#2c-namespace-retention-is-30-days) together: content
> moves into history, and history is retained 30 days after completion. §2c's warning — "if this
> number ever needs urgent revisiting, suspect §5 first" — is describing exactly this failure,
> reached by config change rather than by code.

The subtler cost: **workflow history is retained after completion by design** — that retention is
what buys replay and resumption. Today's NATS stream is `--retention work`, so acked messages are
deleted and orchestration state is effectively free and self-cleaning. History is not. Keeping
payloads out of it is what keeps that bill proportionate, and this is **the largest new
operator-side cost in the migration**.

R2's "each block declares what it consumes and produces" is therefore read as declaring **types
of reference**, not types of payload.

**Not every block produces an object. ✅ Owner's call, 2026-08-10.** An earlier draft's "one
immutable object per block per run" was false of two of the five catalog types, and
[§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)'s
strict-path data flow makes that load-bearing rather than pedantic: block *n* consumes block
*n−1*, so every non-terminal block must emit something its successor can consume. R2's catalog
therefore splits in two:

| Kind | Blocks | Produces | Emits as its output |
|---|---|---|---|
| **Content-producing** | Scrape, Clean, LLM extract | a new object | a reference to that object |
| **Effect** | Validate, Webhook | nothing | **its own input reference, unchanged** |

- **Effect blocks are pass-through by contract, not by accident.** Validate asserts on its input
  and either continues or fails the run terminally; Webhook delivers. Neither transforms content,
  so neither writes an object. The alternative — writing a byte-identical copy so that every block
  has "its own" artifact — is storage spent to satisfy a naming convention, on every run.
- **Two blocks may therefore name one object**, which is the constraint that makes
  [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)'s
  garbage collection non-trivial: collecting "block *n*'s output" must not orphan a reference held
  by block *n+1*. Objects are collected by **run**, on the rules in §8 — never per block.

**This is what makes "the result" well defined, and R6 is why it had to be settled here.** R3 says
the final block's output is retrievable as *the* result. R6's acceptance-gate pipeline is
`scrape → LLM → webhook` — **it ends in an effect block.** Read naively, that pipeline has no
result at all, which is
[§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)'s
invisible-lane bug in a different costume. So, stated once:

> **The result of a run is the output of the last *content-producing* block in execution order.**
> An effect block that runs last does not change the answer — it delivers or asserts on the
> result; it does not replace it.

**⚠️ Amended 2026-08-17 (§8 review): this defines the result, and no longer defines what is
charged.** The original text made one object serve both purposes — *"that same object is the
artifact charged to `storage_bytes_used`"* — and that clause is **withdrawn**. §8 now charges
**every object the run currently holds**, so the result is simply the one that is never collected
and the one R3 returns; the others are charged while they exist and release their charge when
collected. The paragraph above originally carried a second argument, that the naive reading would
also charge zero storage against a job that charges the LLM output. **That comparison was factually
wrong** — the job path charges the *scraped page*, not the LLM output ([§8a](#8a-what-the-storage-rule-costs-on-the-job-path-found-in-review-2026-08-17)) —
and it is removed rather than repaired, because under the new rule the size of any single block's
output no longer determines what a run costs.

**The v2 path convention:**

```
pipelines/{pipeline_run_id}/{block_id}.{ext}   — one immutable object per content-producing block
```

- **The key is deterministic, and that is a guarantee rather than an incident.** An activity that
  fails after uploading and is retried by Temporal re-uploads to the *same* key, so a retry is
  idempotent at the storage layer. The contrast is BUG-005, where the key was derived from a value
  that was NULL for batch runs: a non-deterministic key collided across tenants, while a
  run-and-block-keyed one cannot collide at all.
- **The path carries no tenant segment, and does not need one** — `pipeline_run_id` is unique, so
  BUG-005's shared-object failure is unreachable here. But the reference stored in workflow history
  is a **bare path**, so anything that resolves one into bytes must ownership-check the run first.
  This is [§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary)'s
  "there is exactly one tenant boundary, and nothing at the engine backs it up," at the storage
  layer, where BUG-005 proved no 404 guard reaches.

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

> **✅ Settled on review (2026-08-10).** The decision stands and is **more load-bearing than the
> section claimed**. Four changes: **(1)** the payload/history figures are now measured rather than
> hedged — at a **256 KiB warn / 2 MiB error** blob limit, the BUG-003 range means the *largest*
> real page is over twice the hard limit and the *smallest* is already past the warn line, so this
> is not an large-page edge case; plus the self-hosting escape hatch is named as a trap, because
> raising the limit undoes this section and §2c together. **(2)** The catalog splits into
> content-producing and effect blocks; "one object per block" was false of Validate and Webhook,
> and §4's strict-path data flow made that a correctness question rather than a wording one.
> **(3)** "The result" is pinned to the **last content-producing block** — without which **R6's
> own acceptance-gate pipeline**, which ends in a Webhook, has no retrievable result. *(Amended
> 2026-08-17: this originally also pinned "the charged artifact" to the same block. §8 now charges
> every object a run holds, so the two claims have been separated and only the result survives
> here.)* **(4)** Deterministic keys are stated as an idempotency guarantee, and the absence of a
> tenant segment is tied back to §12's single boundary.

### 6. OQ-2 — In-flight edits: definitions are pinned, and that is a different problem from code versioning

**Decision: a run executes the definition version it started with. Pipeline definitions are
immutable versioned rows; an edit creates a new version; a run records the version it pinned in
`pipeline_runs.pipeline_version_id`.**

**The reason is semantic, not mechanical.** A run that has already executed blocks 1–3 of the old
shape cannot meaningfully continue into a *different* block 2. If the edit deleted a block the run
has already executed, there is no answer to "which block is it on"; if it changed the LLM schema,
the run produces output half-conforming to each. This is true of any engine and would be true with
no engine at all.

> **⚠️ Corrected on review (2026-08-10) — the mechanical argument an earlier draft gave was
> wrong.** It claimed replay "reconstructs in-memory state by re-reading history against the
> current definition," making a mid-run edit a determinism violation. **Temporal's replay
> re-executes the workflow function against the recorded event history, and the workflow's input
> arguments are part of that history** (confirmed against Temporal's documentation). So a
> definition passed in as a workflow argument is *automatically* pinned — replay reuses the
> recorded argument and never reads the current row. The described failure is reachable only by a
> workflow body that loads the definition from Postgres mid-run, which the determinism rule at the
> end of this section already forbids. **Leaving the wrong reason in place was the actual risk:**
> it invites a correct rebuttal — *"we pass it as an argument, so replay is safe, so we may adopt
> edits"* — against a decision whose real basis is the paragraph above, which that rebuttal does
> not touch.

**The mechanism follows from the correction: the definition is a workflow input argument.** That
is what makes pinning free rather than something we enforce. It is also consistent with
[§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)'s rule as settled —
*content is never a payload; arguments may be* — and a definition is bounded by R1's
max-blocks-per-pipeline, so it is small against the 2 MiB payload limit. The standing prohibition
is narrower and sharper than "don't adopt edits": **the workflow body must never load the
definition itself.**

**Retention, naming, and deleting a pipeline.** R1's "deleting a pipeline must not destroy the
history of runs already executed from it" means version rows are retained as long as any run
references them. Two consequences that an earlier draft left unstated, both settled here:

- **A pipeline with a run in flight may be deleted, and the run finishes. ✅ Owner's call,
  2026-08-10.** The run already pinned its version, that version is retained, and the run is its
  own record. Cancelling a run is R3's explicit, separate operation — deleting the definition is
  not a back-door cancel. This mirrors the Q4 decision on jobs, where retiring a schedule and
  cancelling a run were deliberately split rather than folded into `DELETE`.
- **The run history holds the name, not the pipeline. ✅ Owner's call, 2026-08-10 (option C).**
  Deleting a pipeline that has **no runs** deletes it outright and frees the name immediately.
  Deleting one that **has runs** soft-deletes it; the retained rows keep holding the name, and
  reusing it returns **409**. The reason to hold a name is that run history refers to it — no
  history, nothing refers to it, nothing to hold. This lands close to the `api_keys` precedent
  (*"revoked keys still hold their name — names are identifiers, not recycled"*) without
  over-applying it: a revoked key holds its name for a different reason that always applies (the
  key string may be recorded elsewhere), whereas a never-run pipeline is referenced by nothing.
  The alternative — freeing the name on every delete — makes run history ambiguous by
  construction: two pipelines named "Price watch", possibly different URLs and different schemas,
  and no way to tell from a run which one produced it.

**Two versioning problems are easy to conflate, and only one is solved above.**

- **The user's definition** (data) — solved by pinning.
- **Our workflow code** (the interpreter) — *not* solved by pinning, and still requires
  Temporal's own versioning discipline. Changing how the engine interprets a Clean block affects
  in-flight runs regardless of which definition version they pinned.

Pinning means **the cook keeps the recipe card they started with**; it says nothing about swapping
the cook mid-dish. **Worth noticing: Temporal's current answer to the second problem has the same
shape as our answer to the first** — with pinned Worker Deployment Versions an execution runs
entirely on the worker version it started on. Pin the recipe, pin the interpreter. The mechanism
is **deliberately not chosen here** (see the deferral table), but it is not an open-ended question:
**Worker Versioning is GA and is Temporal's stated default**, patching is the older approach that
leaves conditional branches to clean up later, and the pre-2025 *experimental* Worker Versioning is
already withdrawn from the server — so the choice is between the current mechanism and patching,
not among three. It needs **server-side enablement** on our self-hosted deployment
([§2](#2-topology)), which makes it an infra task rather than a code flag.

The second brings a **new failure class**: workflow code must be deterministic. No I/O, no
`datetime.now()`, no `random()`, no direct database or MinIO access inside a workflow body —
those belong in activities. This is a standing review rule, not a one-time cleanup.

**Two obligations elsewhere in this ADR are this rule seen from the other end:**

- [§10](#10-oq-6--the-do-not-delete-list)'s do-not-delete list — the LLM cold-start
  `ensure_ready()` probe and the transient/terminal storage classifier — must be ported into
  **activities**. Both do I/O; this rule is precisely what forbids them in a workflow body. The two
  sections are read together by whoever does the port, and neither previously pointed at the other.
- [§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)'s
  config-schema obligation exists *because* of the pinning decided here: a pinned old version keeps
  the config shape it was saved under, so the validator must remain able to validate historical
  shapes, and a block type that gains a required field needs a default for definitions already
  saved.

> **✅ Settled on review (2026-08-10).** The decision stands; **its stated reason did not.** The
> replay argument was factually wrong (arguments live in history, so a definition passed in is
> pinned automatically) and, being wrong, invited the opposite conclusion from anyone who knew
> Temporal — the real basis is the semantic incoherence of continuing a run into a changed shape.
> Four things previously unstated are now decided: the definition travels as a **workflow input
> argument**; the pinned version is recorded in **`pipeline_runs.pipeline_version_id`**; a pipeline
> with a run in flight **may be deleted and the run finishes**; and **run history holds the name**
> — delete with no runs frees it, delete with runs soft-deletes and 409s on reuse.

### 7. OQ-3 — One lane: disjoint identity, plus an engine-level uniqueness guarantee

**Decision: "exactly one lane" is enforced by four stacked mechanisms, none of which is a
routing flag or a convention. They do not all cover the same cases — mechanisms 1 and 4 divide
between them, and which one applies depends on whether the work is a pipeline or a migrated job.**

1. **Disjoint identity spaces — pipelines only.** A pipeline run is a `pipeline_runs` row and
   never a `job_runs` row. v1 executors read `job_runs` and NATS subjects; v2 executors read
   Temporal task queues. No object exists that *could* be picked up by both.
   ⚠️ **This covers layer A and nothing else.** A **migrated job keeps its `job_runs` row** —
   [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
   makes that table a read-model mirror of Temporal state rather than replacing it, because R5
   forbids user-visible change and the job API, SPA, admin views and quota view all read it. So
   from migration step 2 onward the same row is visible to both lanes **by requirement**, and
   mechanism 1 stops applying exactly when the problem becomes real
   ([§16](#16-the-v1v2-coexistence-contract) says the same thing from the sequence side).
2. **Workflow ID uniqueness, for flows that do migrate.** When jobs move to `JobWorkflow`, the
   workflow ID is derived from the run identifier (`job-run-{run_id}`). Temporal refuses to start
   a second execution with a workflow ID that is already **open** — the default Workflow ID
   Conflict Policy is `Fail` — so a concurrent double-start is impossible *at the engine* rather
   than prevented by a check we wrote.
   ⚠️ **Pin `WorkflowIdReusePolicy.REJECT_DUPLICATE`, or this is weaker than it reads.** The
   default reuse policy is **`ALLOW_DUPLICATE`**, which permits a *new* execution with the same ID
   once the previous one has **closed**. That yields "never two at once," not "this run executes
   once, ever" — and R5 asks for the second. A run identifier is genuinely single-use, so
   rejecting duplicates costs nothing. Note also what would silently destroy the guarantee:
   `TERMINATE_IF_RUNNING` converts a refused double-start into a kill-and-restart.
3. **`schedule_status` is the interlock for recurring work** (cutover gotcha #2). A job moved to
   a Temporal Schedule must be `paused` in v1 or it fires on both lanes. This is what Q4's
   deliberately tri-state flag is for.
4. **A lane marker on `job_runs`, from migration step 2. ✅ Owner's call, 2026-08-10.** A column
   written in the **same transaction as the row insert** (a later write leaves a window in which a
   v2 row is indistinguishable from a v1 row), and every v1 background query that dispatches work
   filters on it. This is mechanism 1's disjointness extended to the rows that cannot have it
   structurally. It is **step-2 work, not day-one work** — §16's routing rule keeps jobs on v1
   until their flow is explicitly migrated, so layer A ships without it — but it is recorded here
   because §7 is what someone will consult *at* step 2.

**Why mechanism 4 is required and not belt-and-braces.** `_recover_stale_pending`
(`core/scheduler.py:131`) selects **every** `job_runs` row with `status = 'pending'` older than
`stale_pending_threshold_minutes` (default **10**) and re-publishes it to
`scrapeflow.jobs.run.http` / `.playwright`. It carries no lane filter and cannot know a workflow
owns the row. So: a job migrated to v2 whose workflow has not yet started — worker pod down,
task-queue backlog, Temporal unreachable — sits at `pending`, and ten minutes later **v1
dispatches it to a NATS worker**. When the workflow worker returns, the same URL is scraped a
second time and an LLM stage bills the user's key twice. Mechanism 2 does not intervene: v1 never
started a workflow, it published a message, so the engine had nothing to refuse.

Two things make it worse than a corner case. It fires **precisely when v2 looks stalled**, so the
operator's model is "nothing is running" while v1 quietly ran it; and it is **silent** — nothing
records that two lanes touched one run.

> ⚠️ **A documentation trap behind this.** `phase4-backlog.md` §3 lists `_recover_stale_pending`
> under "dissolved by Temporal — do NOT fix," because it exists only to police the hand-rolled
> scheduler. That is true of the **end state** and false of the **transition**: the loop is live
> for the whole coexistence period, which is exactly when this risk exists. *"Dissolved by the
> migration"* and *"safe during the migration"* are different claims, and §3 of the backlog only
> makes the first.

**The one v1 loop that is already safe is safe by accident, which is the argument for making it
deliberate.** `advisory.py:28` matches runs on `nats_stream_seq`, which is `NULL` for anything v1
never dispatched — a lane marker in disguise. It works, but the column it depends on is dropped
when v1 retires ([§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)),
so the protection is a side effect of something on its way out.

**Ordering matters for mechanism 3, and the safe order is the counter-intuitive one:** pause in
v1 → confirm no v1 dispatch is in flight → *then* create the Temporal Schedule. The reverse order
leaves a window in which both lanes are armed. A double scrape costs a double render; a double
LLM stage bills the user twice.

**Rollback has the mirror-image ordering, and it is not symmetric by intuition.** §16 claims every
step is reversible, which means the reverse move needs the same discipline: **pause the Temporal
Schedule → confirm no v2 execution is in flight → only then set `schedule_status` back to
`active`.** Un-pausing v1 first re-arms both lanes just as surely as creating the Schedule too
early does. Written down because whoever performs it is rolling back under pressure.

**One property helps, but only for the half that was never dangerous.** The NATS stream is
`--retention work`, so a message *acked* by v1 is deleted and there is no replayable backlog a v2
executor could later re-consume. The risk is the **unacked** message — in flight, or unprocessed
on the stream at the moment of cutover — which is still deliverable to a v1 worker. Retention says
nothing about those. The check that does is already written as §16's **deletion gate** (zero
unprocessed, zero outstanding acks, verified with `nats consumer info --json`); it is a **cutover
gate too**, not only a deletion gate, and the two sections should be read together.

> **✅ Settled on review (2026-08-10).** The section under-covered its hardest case. **(1)**
> Mechanism 1 is now explicitly **pipelines-only** — a migrated job keeps its `job_runs` row by
> requirement, so at step 2 the covering set drops to mechanism 2 alone. **(2)** A **fourth
> mechanism** is added: a lane marker on `job_runs`, because `_recover_stale_pending` is a live v1
> dispatcher that re-publishes any stale `pending` row with no lane awareness — verified in code,
> 10-minute default threshold. **(3)** Mechanism 2 **over-claimed**: the default reuse policy
> allows a fresh execution once the previous one closes, so `REJECT_DUPLICATE` must be pinned for
> "once, ever" rather than "never two at once." **(4)** The `--retention work` reassurance
> addressed the safe half; the unacked-message case is now tied to §16's drain gate. **(5)** The
> rollback ordering for mechanism 3 is stated, since §16 claims reversibility at every step.

**Carried to [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)'s review — mechanism 1 is
true of rows, not of messages.** Under the draft's option (a) a v2 activity dispatched to the
existing NATS workers, so v2 work traversed v1 infrastructure by design. The return path was the
sharper half: the worker publishes to `scrapeflow.jobs.result`, where `result_consumer.py` is
subscribed, and its routing (`result_consumer.py:616`) branches on `run.job_id is not None` /
`run.batch_item_id` — a pipeline-originated result has neither. That is BUG-005's shape one lane
later.

> **✅ Resolved by the §9 reversal, 2026-08-23 — this hazard is moot.** Option (a) is rejected, so
> **v2 publishes no NATS results at all** and nothing v2-originated ever reaches
> `result_consumer.py`. The §9 review also found the failure was one step earlier and more final
> than described here: `_handle_result` resolves the run with `db.get(JobRun, run_id)`
> (`result_consumer.py:608`), so a pipeline-run id matches no row, and the handler **acks** —
> deleting the message on a work-queue stream — before the FK branch is ever reached. Mechanism
> 1's "rows, not messages" limitation stands as written; it simply no longer has a v2 message
> path to be limited against.

### 8. OQ-4 — Metering: one run is one unit, pools are shared, and storage is charged for what is stored

**Decision:**

**The unit, stated once for every lane** (PM, PRD-016 OQ-4 round 3 — ✅ owner-confirmed
2026-08-08): **one fetch of one target URL that is attempted.** Not one user action, and not one
step. So: job run = 1; batch of N = N; **crawl of N pages = N**; pipeline run = 1, because a
pipeline can only fetch once (below) and the non-Scrape blocks fetch nothing. **Admission checks
the declared ceiling; the meter charges actuals** — a crawl is pre-checked against `max_pages`
exactly as a batch is against `len(urls)`.

**"Attempted", not "produces one stored result" (corrected in review, 2026-08-17).** The earlier
wording described an outcome the meter never waits for. Every meter counts rows, and the row is
created when the work is *dispatched*, not when it succeeds — `JobRun` at submission,
`CrawlPage` at `dispatcher.py:103`. **A failed scrape already consumes a monthly run today**, and
that is the defensible behaviour: the fetch was attempted, the target was hit, the capacity was
spent. Defining the unit by its result would have made the meter unimplementable without a
second write-back on completion, for no user-visible gain.

**A retry is not a new unit.** A Temporal activity retry, a NATS redelivery and a
`_recover_stale_pending` re-publish are all the same attempted fetch. This is stated because it is
the first question the definition invites, and because getting it wrong reproduces Q5/Q6/Q7 on the
billing axis instead of the execution axis. The unit is one *logical* fetch; the retry budget is a
property of that unit, not a multiplier on it.

- **`monthly_runs_limit`: a pipeline run is one unit, regardless of block count.** Per-block
  metering makes the R6 gate pipeline cost 3 units where the identical job costs 1 — a direct
  violation of the PM's hard constraint (a), and it makes pipelines a *penalty* for expressing
  the same work differently. The arbitrage risk in the other direction is bounded structurally by
  R1's max-blocks-per-pipeline, and the genuinely expensive resource — LLM tokens — is billed to
  the user's own provider key regardless.

  **✅ The PM's multi-Scrape rider is resolved, 2026-08-17 (owner's call). A layer-A pipeline
  cannot fetch twice, and the reason is structural, not a cap.** The rider assumed "one run, one
  fetch" was a policy choice that a later feature could quietly invalidate. It is not: it follows
  from two [§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)
  rules acting together — layer A validates a **single chain in data flow**, where each block
  consumes the previous block's output, and **Scrape consumes nothing**. A Scrape block therefore
  has no valid position except first, and a second one is unsatisfiable at Save.

  ⚠️ **The earlier justification for the same conclusion was wrong and is withdrawn.** It read
  "R1 fixes one run to one URL". R1 does not: it makes the URL an *optional* run input, so nothing
  in R1 stops a pipeline from carrying two Scrape blocks with two URLs typed into their configs
  and never using a run input at all. The right answer was reached from the wrong premise, which
  is worth recording because the wrong premise is the plausible-sounding one.

  **Because the guarantee is emergent, it must be written down where the validator is built:
  a layer-A pipeline has exactly one starting block, and it is a Scrape block.** Nothing in §4
  says this in one sentence; it falls out of two rules stated pages apart. A validator implemented
  as "every block consumes the previous one, unless it declares no input" satisfies §4 as written
  and admits a second Scrape — at which point the metering rule silently under-charges. **The
  counting rule may not rest on a property no single document asserts.**

  **What would reopen this is a second starting block, not fan-out** — the rider named the wrong
  trigger. Fan-out (one output feeding several blocks, deferred post-Phase 4 by §4) still has one
  Scrape and still costs 1. Multiple *roots* — a pipeline that fetches two pages and merges them —
  is the change that makes a run cost 2, and it is not on any roadmap. The PM's preference stands
  for that day: **count executed Scrape blocks** rather than cap Scrape at one.
- **`concurrent_jobs_limit`: one shared pool, and the unit of contention is one submission.**
  The limit exists to protect worker capacity, and worker capacity is shared between lanes. A
  separate pipeline pool would let one user consume twice the capacity, which inverts the limit's
  purpose.

  **✅ Owner's call, 2026-08-17: one submission occupies one concurrency slot, on every lane.**
  A job run, a batch of any size, a crawl of any size and a pipeline run each hold exactly one.
  **Cost and contention therefore disagree by design and in general** — `monthly_runs` counts
  attempted fetches, `concurrent_jobs` counts submissions in flight. The previous text called the
  crawl case "the one place cost and contention deliberately disagree"; that was wrong on its own
  terms, because **batch already disagrees, in the opposite direction and by accident**:
  `batch.py:46-47` admits a batch by checking `concurrent_jobs` **once, as a single unit**, then
  inserts N `job_runs` rows, so a 100-URL batch is admitted as 1 and immediately meters as 100 —
  locking the user out of a 5-slot pool with one call. Making the rule uniform fixes that as a
  side effect rather than as a special case. ⚠️ **This is a live behaviour change on the batch
  path**, and the only one in this section; it is a loosening, so it cannot break an existing
  caller.

  **✅ Owner's call, 2026-08-17: a pipeline run waiting on a durable timer does not hold its slot;
  v1 lanes are unchanged.** §15 lets a Webhook block wait for real delivery on a ≈2.6 h horizon,
  and defended the resulting long-lived runs by asserting the ceiling "counts runs actively
  executing a block, not runs that exist". [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
  defines the counting view as *not yet finished*. **These are different definitions and §15's
  argument only survives under the first**, so the divergence is resolved explicitly rather than
  left for whoever writes the view.
  - **v1 (jobs, batches, crawls): unchanged.** `quota.py:59` counts `pending`, `running` and
    `processing`, and a `pending` row is doing nothing while still holding a slot. That is
    today's shipped behaviour; R5 forbids changing it, and narrowing it would be a silent
    loosening of a live limit.
  - **v2 (pipeline runs): a run parked on a durable timer is not active.** It occupies no worker,
    and charging for it would let one unreachable webhook receiver consume a user's pool for
    hours. Blocked-on-delivery is the *expected* state of a §15 run, not an anomaly.

  The cost is that the counting view is not lane-blind on this axis: it needs a per-lane predicate
  for "active". That is a real cost and it is accepted, because the alternative is either
  reopening §15 or changing v1 behaviour.
- **`storage_bytes_used`: every stored object is charged, on every lane, for as long as it is
  stored.** **✅ Owner's call, 2026-08-17 — this reverses the previous rule and part of §5.**

  The meter measures **bytes on disk**, not "the result". If an object exists in MinIO on the
  user's behalf, it is charged; when it is deleted — by the user, or by intermediate-output
  collection — the charge is released. Storing something for the user and not charging for it is
  the defect, whichever lane does it.

  **Withdrawn:** §5's clause that *the charged artifact is the last content-producing block's
  output*, and this section's "every stored **final** artifact is charged". **Retained from §5,
  and still load-bearing:** that the same output is **the run's result** — what R3 returns, what
  the SPA shows, and what is never collected. The two ideas were fused and only one of them was
  about billing:

  | | definition | used for |
  |---|---|---|
  | **the result** | last content-producing block's output (§5, unchanged) | R3 display, permanence, what survives collection |
  | **what is charged** | every object currently stored for the run (new) | `storage_bytes_used` |

  This is simpler than what it replaces — it needs no notion of finality, so it is unaffected by
  fan-out, by effect blocks producing nothing, and by two blocks naming one object. It also makes
  intermediate-output collection **a user-visible benefit rather than housekeeping**: when a
  pipeline's intermediates are collected, the user's storage number actually falls.

  Applied to the other lanes: **every crawl page is charged** (a crawl has no intermediates, so
  nothing changes there), and **screenshots are chargeable** — they are stored on the user's
  behalf today and counted by nothing, which is BUG-004's other half and an argument for fixing it
  rather than deferring it further.

  The **call sites are not lane-agnostic and must be fixed** (see
  [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)).

#### 8a. What the storage rule costs on the job path (found in review, 2026-08-17)

The rule is a reversal, so the gap between it and live behaviour has to be stated. **Today an LLM
job stores the scraped page and the extracted JSON as separate objects, charges only the page, and
on delete subtracts only the JSON.** Traced end to end:

| step | code | effect |
|---|---|---|
| scrape completes | `result_consumer.py:411` | adds **HTML** size, sets `storage_accounted_at` |
| LLM completes | `result_consumer.py:485` → `:81` | tries to add **JSON** size, **skipped** — stamp already set |
| run finalised | `result_consumer.py:500` | `result_path` repointed at the **JSON** |
| worker wrote | `llm-worker/worker/storage.py:23` | JSON to a **new** key; the HTML is still there |

The short-circuit in `_try_increment_storage` is not itself wrong — it exists to make NATS
redelivery idempotent, which is necessary. **Its granularity is wrong: it is keyed on the run when
it should be keyed on the stored object.** A redelivery of the same result and a genuinely second
artifact are indistinguishable to it. Same pattern on the batch path (`:237` scrape, `:274` LLM).

Two consequences, both defects under the new rule and neither previously filed:

- **The counter is permanently inflated by every deleted LLM job.** Hard delete enumerates
  `JobRun.result_path` (`routers/jobs.py:391`, `admin.py:336`), stats *that* object — the JSON —
  and decrements by its size, while what was added was the HTML. `decrement_storage_bytes` clamps
  at zero, so it never goes negative; it also never returns to zero. Delete every job you own and
  your usage still reads non-zero.
- **The scraped page is never deleted.** Nothing enumerates it, so it outlives the run, the job
  and the user. Same shape as BUG-004's orphaned screenshots.

Neither file is touched by the migration, so **§10's do-not-delete reasoning does not apply and
backlog §3 does not cover these** — they are pre-migration fixes on live code. Filed as
**BUG-007** (`docs/project/open-bugs.md`), which carries the full trace; this section records only
why the metering decision depends on it.

**⚠️ Unresolved by this decision: the dual write.** Every worker writes each result **twice** —
`latest/{job_id}.{ext}` and `history/{job_id}/{ts}.{ext}` (ADR-002 §8) — while `result_size`
reports one copy, so MinIO holds 2× what the meter counts, on every lane, today.
"Charge for what is stored" read literally means charging both. **Recommendation, not yet an
owner call: charge one copy.** `latest/` is a convenience alias for bytes the user is already
paying for, not a second artifact; billing twice for one result is not defensible to a user, and
the honest fix is that the *duplicate* should not be free-standing rather than that it should be
billed. The v2 artifact path already drops `latest/` (§5), so this is a v1-only discrepancy with a
known end state — but it needs a decision before the storage bullet can be called implementable,
because "charge what is stored" and "charge one copy" are not the same rule.

#### 8b. Crawl conditions, and the mechanism the cutover promise needs

Storage is the axis where crawls are most dangerous: 10,000 pages at BUG-003's measured
291 KiB–4.1 MiB is **2.8–40 GB from a single API call** against a 5 GB wall. Two conditions the
PM attaches, both architectural obligations rather than product preferences:

- **Reclaim ships with counting.** Verified independently: **nothing frees crawl artifacts
  today.** `DELETE /crawls/{id}` (`routers/crawls.py:127`) is cancel-only and removes no objects;
  the admin user-delete (`admin.py:195`) and job hard-delete both enumerate `JobRun.result_path`
  only, so **deleting a user orphans their crawl artifacts in MinIO**. Charging against a hard
  wall with no way to free space is a support incident by construction.
- **Accounting starts at cutover.** No backfill, no reconciliation of history — and deleting a
  pre-cutover artifact must not decrement, or the counter goes negative.

**The second condition has no mechanism on two of the four lanes (found in review, 2026-08-17).**
"Do not decrement for something that was never incremented" requires knowing, at delete time,
whether a given object was counted. On the job path that knowledge exists —
`job_runs.storage_accounted_at` (`models/job_runs.py:35`) is exactly this marker. **`crawl_pages`
has no equivalent, and no size column either**, and nothing yet specifies one on `pipeline_runs`.
Without a per-object marker the cutover boundary is a date comparison against row timestamps,
which is wrong for any row created before cutover and deleted after. **Each lane that starts
counting needs its own accounted-at marker, added in the same change that starts counting it.**

#### 8c. Intermediate-output collection

Failure context is retained unconditionally (the PM's stated position): the failing block, its
input reference, and its error survive garbage collection, because "see why a step failed" is
R3's purpose.

Under the new storage rule the economics of this window change direction. Previously intermediates
were free to the user and a pure operator cost, so the window traded operator storage against
debuggability. **Now intermediates are charged while they exist**, so the window also bounds what
the user pays: a long window is a larger bill, a short one collects evidence they may still want.
It remains an operator dial, but it is now a dial with a **user-visible price**, which strengthens
rather than weakens the case for treating it as a promise.

**That window is a product-visible retention promise, not a free operator dial. ✅ Owner's call,
2026-08-10 (§5 review).** R3 promises a user can see which step failed *and what it returned*; for
**successful** intermediate blocks, that second half is exactly what garbage collection deletes.
Three rules keep the promise honest:

- **The run's result is not an intermediate and is never collected by this mechanism.** It is
  the last content-producing block's output
  ([§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)) — the run's
  *result*, which since the 2026-08-17 review is no longer the same claim as *the charged
  artifact*: the user pays for this object and every other object the run still holds. It is
  removed only when the user removes the run. **Collection operates per run, not per block** — an
  effect block passes its input reference through, so two blocks can name one object and per-block
  collection would orphan a live reference.
- **A collected output renders as collected.** The API and the SPA must distinguish *never
  produced* (an effect block), *collected* (retention elapsed) and *failed*. A collected
  intermediate that surfaces as a 404, an error or an empty result reads to the user as data loss.
- **What is retained is decoupled from Temporal's retention, and outlives it.** Per-block status
  and timing live in `pipeline_run_blocks` in the **app** database, so R3's "which step failed"
  survives indefinitely regardless of both the 30-day namespace retention
  ([§2c](#2c-namespace-retention-is-30-days)) and this window. Only *content* has a retention
  window. Stated because the natural assumption — that a run stops being inspectable when its
  workflow history expires — is wrong here, and designing to it would throw away observability the
  app database already provides for free.

The number itself is still an operator dial; what is decided is that it is chosen against a stated
promise rather than picked for storage cost alone.

**⚠️ "Per run, never per block" is safe only while no object is shared *between runs* (found in
review, 2026-08-17).** The rule was derived from a within-run hazard: an effect block passes its
input through, so two blocks in one run can name one object. The same hazard exists one level up,
and v1 already has it — on a content-hash match, `result_consumer.py:385` points the new run at
the **previous run's** object instead of writing a new one. Collecting per run would then delete
an object a later run still references. Pipelines do not have this yet, only because
change-detection was deferred to Monitors; **the deferral is what makes per-run collection safe,
not the design.** When Monitors bring "skip if unchanged" onto the pipeline lane, per-run
collection breaks exactly as per-block collection breaks now, and the rule becomes *collect an
object when no run references it*. Recorded here so that arrives as a known consequence rather
than as a data-loss bug. It interacts with the storage rule too: a shared object is stored once
and must be charged once, not once per referencing run.

#### 8d. Who charges, and what happens at the wall — not yet decided

Two gaps this section does not close, both noted so they are not mistaken for settled:

- **No section names the component that performs the accounting on the v2 lane.**
  [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
  says it "must be named, not inherited"; this section says the mechanism is unchanged and points
  back at §3. On v1 it is `result_consumer.py`, which the migration deletes. The plausible answer
  is a metering activity at run completion, which has the useful property of being retryable and
  idempotent under Temporal, but it is a decision for §9's activity inventory, not an assumption.
- **Enforcement has no v2 story at all** — this section covers *counting* only. On v1, exceeding
  the wall is handled at `result_consumer.py:631`: the result is deleted and the run fails. The
  pipeline lane needs an equivalent and the ergonomics are worse, because the ceiling is reached
  at the **last** block, after the user's own LLM key has already been billed for the extraction.
  Failing the run there destroys paid work to reclaim a few KB. **Admission-time checking cannot
  substitute** — a pipeline's output size is not knowable before it runs. This is a real product
  question (fail the run, keep it and let the account go over, or refuse to start new runs while
  over) and it belongs to whoever writes the layer-A implementation PRD.

### 9. OQ-5 — Workers become Temporal activity workers directly; the NATS bridge is rejected

**Decision: the three workers gain a native Temporal activity entry point (option (b)) in the
first increment. Option (a) — activities dispatching through NATS to the unchanged workers — is
rejected. ✅ Owner's call, 2026-08-23, reversing this section's draft.**

The v1 lane is untouched by this: jobs, batches and crawls keep running on NATS until their flow
migrates ([§16](#16-the-v1v2-coexistence-contract)). What changes is that the pipeline lane never
acquires a NATS bridge at all.

**The draft chose option (a) and the reasoning inverted on inspection.** The original argument was
that R6 — reproduce today's `scrape → LLM → webhook` recipe as a pipeline — tests the *pipeline
model*, so rewriting three workers in the same increment would confound it: a failing gate could
mean the model is wrong or the rewrite is buggy. That argument is sound in form. It fails on its
premises, in four ways.

**1. "Rewriting three workers" overstates the change by an order of magnitude.** The workers split
cleanly into domain logic and transport, and only transport moves:

| file | lines | touching NATS |
|---|---|---|
| `http-worker/internal/worker/worker.go` | 411 | ~22% |
| `llm-worker/worker/main.py` | 164 | ~21% |
| `playwright-worker/worker/main.py` | 203 | ~18% |
| `playwright-worker/worker/worker.py` | 309 | ~10% |

Everything that is expensive to get right touches the transport **not at all**: `blocking.py`
(313 lines of tiered bot-wall detection, BUG-003), the Patchright/headed-Chrome stealth setup
(ADR-008), `formatter.go`, `robots.go`/`robots.py`, `llm.py`, the MinIO clients. These are plain
functions over inputs and outputs, and option (b) calls them from a different caller rather than
changing them.

The Go worker makes the point sharpest: `processJob(ctx context.Context, job *ScrapeMessage,
f *fetcher.Fetcher) (string, error)` (`worker.go:377`) **already has the shape Temporal wants** —
a Go activity is any `func(ctx, T) (R, error)`. The adapter is a few lines because the natural
shape of "do the work and return where you put it" is the same in both worlds.

**2. Roughly half of what option (a) preserves is compensation for NATS.** `ack_wait=120`, the
30-second `in_progress()` heartbeat, the `max_deliver` caps, the nak-with-backoff ladder — every
one exists because JetStream redelivers a message the worker has not acked in time. That is the Q6
incident, and the tuning was expensive. Under Temporal it is **deleted, not ported**: activity
heartbeating and the start-to-close timeout occupy that role. Preserving those settings is
preserving a dressing for a wound being removed. What genuinely carries forward is
[§10](#10-oq-6--the-do-not-delete-list)'s list — the cold-start probe, the transient/terminal
classifier, bot-wall detection — and **that list ports identically under either option.** Option
(a) does not avoid the work; it defers it while keeping the code that must be ported alive in two
places.

**3. Option (a)'s bridge does not exist, is not cheap, and one part of it is blocked.** The bridge
needs four things: dispatch (trivial — the payload already exists), **result correlation**, retry
neutralisation, and a lane marker. The second is the problem.

> **⚠️ The results subject cannot take another consumer, and there is a dead service in production
> proving it.** `SCRAPEFLOW` is `--retention work` (verified on the live stream, not from the
> manifest), subjects `scrapeflow.jobs.>`. A work-queue stream refuses a second consumer whose
> filter overlaps an existing one, and `api-result-consumer` already claims
> `scrapeflow.jobs.result` in full. The crawl coordinator attempts exactly the addition option (a)
> would need — `pull_subscribe(NATS_JOBS_RESULT_SUBJECT, durable="coordinator-result-consumer")`
> at `coordinator/result_handler.py:203` — and **that consumer has never existed on the stream**:
>
> ```
> $ nats consumer ls SCRAPEFLOW
>   api-result-consumer · go-worker · python-llm-worker · python-playwright-worker
> ```
>
> The failure is silent by construction. `result_handler_subscribed`, logged immediately after the
> subscribe, appears **zero** times in the coordinator's retained log, while `dispatch_loop_started`
> from the sibling task logs normally; `main.py:82` awaits both with
> `asyncio.gather(..., return_exceptions=True)`, which captures the exception and never re-raises.
> The pod reports itself started and healthy with half of itself dead. **No crawl has ever run in
> production** (`crawls` and `crawl_pages` are both empty), so nothing has surfaced it — the same
> shape as BUG-005: shipped, silently broken, never exercised. Filed as **BUG-008**
> (`docs/project/open-bugs.md`), and **deliberately not fixed on the NATS path** — the defect *is*
> the NATS integration, and [§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block)
> deletes the component that carries it.

So option (a) must invent a result path — a new subject, a second stream, or polling Postgres for
the row `result_consumer.py` writes. The last is the worst available answer and the most likely to
be reached for: it puts **`result_consumer.py` on the critical path of every pipeline run**, and
that component is the reason this ADR exists (Q8's hand-rolled state machine and its live feedback
loop). Adopting a durable execution engine and then routing its results through the thing it was
adopted to replace is a contradiction the section did not notice.

**And today the message would not merely be ignored — it would be destroyed.** `_handle_result`
(`result_consumer.py:608`) resolves the run with `db.get(JobRun, run_id)`. A pipeline-run id
resolves to nothing, so the handler logs *"Received result for unknown run, discarding"* and
**acks** — which on a work-queue stream deletes the message. The activity then waits out its
timeout while the scrape sits completed in MinIO. ([§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee)
predicted this one lane later as "neither FK set"; the real failure is a step earlier and final —
there is no row to route.)

**4. "Unchanged workers" and "NATS must not retry" cannot both hold.** The draft's own warning
required that, for workflow-originated work, the NATS layer must not retry — otherwise Temporal's
`RetryPolicy` and JetStream redelivery stack, which is R4's "retry lives in exactly one visible
layer" violated by the migration itself. But that retry lives **inside the workers**:
`msg.NakWithDelay` at `worker.go:316`/`:339`/`:360`, `msg.nak(delay=…)` at
`llm-worker/worker/worker.py:128`, and the same in the Playwright worker; and in **consumer
config** (`max_deliver`), which is a property of a consumer, not of a message. Neither can be
disabled per-message without either a flag the worker reads or a v2-only subject and consumer.
**Both are worker changes.** Option (a)'s defining property — workers completely untouched — does
not survive its own safety requirement.

**The gate is preserved, by a better mechanism.** The honest comparison was never *unchanged
workers* versus *rewritten workers*; it was **a brand-new bridge plus unchanged workers** versus
**a thin permanent adapter plus unchanged worker logic**. The bridge is the larger body of novel
code, it is novel in precisely the area that produced Q5, Q6, Q7 and Q8 (queue semantics, retry,
result correlation), and **all of it is deleted at the step that was always going to follow.**
Option (b) also admits a de-risking step option (a) cannot:

> **Pre-gate (required before R6): run the Scrape activity standalone against a URL and compare
> its output to a v1 job run of the same URL.** This separates "the adapter is wrong" from "the
> pipeline model is wrong" *before* the model is under test, which is the isolation option (a)
> claimed to provide. The bridge has no equivalent, because it has no simpler mode to run in.

**Costs, stated plainly.**

- **Each worker runs twice during coexistence** — one deployment bound to NATS for v1, one bound
  to a Temporal task queue for v2. **Two deployments of one image with a mode flag, not one
  process serving both**: retiring the v1 lane becomes deleting a deployment rather than unpicking
  a conditional, and the two scale and fail independently. Cost: three extra deployments for the
  duration.
- **Three integrations rather than one bridge.** Per worker: a dependency (`go.temporal.io/sdk`,
  `temporalio`), connection config (address, namespace, task queue) as env vars, an entry point,
  and a manifest. Two of the three are the same language and SDK, so this is realistically two
  distinct pieces of work and one repeat. Note the "one place" advantage of the bridge was
  illusory anyway — per point 4, retry neutralisation reaches into all three workers regardless.
- **⚠️ The Playwright worker's container contract must be preserved exactly.** Xvfb started first,
  then `exec python` as pid 1 — never `xvfb-run` as pid 1, which stays alive after the worker dies
  so k8s never restarts a dead container (ADR-008). A new entry point is a new opportunity to get
  this wrong, and the failure mode is a pod that looks healthy and consumes nothing. This is the
  single riskiest part of the port, and it is a container concern, not a Temporal one.
- **Ordering:** none of this can begin until the Temporal server is deployed — separate Postgres
  instance and `temporal-sql-tool` schema setup ([§2](#2-topology)). Equally true of option (a),
  so it does not separate them.

**Sequence.** Prove the pattern on the **Go http-worker** first: simplest, best-tested, and its
`NATS_MAX_DELIVER=3` already caps retry, so a like-for-like comparison against v1 is clean. Then
the LLM worker (it carries the cold-start and classifier logic that §10 requires be ported), then
Playwright (the container risk above). NATS and all four API loops are untouched throughout.

**What this decision dissolves elsewhere.** The stacked-retry hazard largely disappears — with no
NATS beneath the activity there is one retry layer, Temporal's. It does **not** disappear entirely:
§10's ported transient/terminal classifier must express itself as **non-retryable error types on
the `RetryPolicy`**, not as its own retry loop inside the activity, or R4 is violated again one
level down. §7's carry-forward — that under option (a) v2 results land on `scrapeflow.jobs.result`
with neither FK set — is **moot**, since v2 publishes no NATS results. §16's sequence step
"workers to activity workers" moves from third to first.

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
already left the room: explicit named wiring, block identifiers stable across versions,
graph-shaped storage with linearity enforced in the validator, and a `skipped` block state. The follow-up PRD is
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
  [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)'s
  rule: the ceiling counts runs **actively executing a block**. The collision was an artefact of
  counting the wrong thing. *(Scoped on review, 2026-08-17: this holds for the **pipeline lane**,
  which is all this section governs. §3's counting view defines active as "not yet finished", and
  **v1 keeps that definition** — `quota.py:59` counts `pending` rows that occupy no worker either,
  and R5 forbids changing it. §8 carries the per-lane split; the argument above is not a claim
  about jobs.)*
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
3. NATS workers stay alive until v1 is drained — but under the reversed
   [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)
   they do so **as a second deployment of the same image**, bound to NATS, alongside a
   Temporal-bound one. Removing v1's executors is deleting that deployment, and it happens after,
   not during, the flow migration.

**Sequence** (`temporal-full-migration.md` §9 is the detailed version; **reordered by the §9
review, 2026-08-23**): stand up Temporal → **workers gain Temporal activity entry points, Go
http-worker first** → pipelines run end-to-end on v2 (R6) → jobs onto `JobWorkflow` → batches and
crawls → scheduling and webhooks → delete `result_consumer.py` → remove NATS → lift the API's
single-replica/`Recreate` constraint. The worker port moves from third to first: with option (a)
rejected there is no bridge to carry pipelines in the meantime, so the activity workers *are* the
first increment's executors.

**The shape of coexistence is drawn in `temporal-full-migration.md` §9a**, not here: it changes at
four of the seven steps, so it is sequence material rather than a decision. Two things it shows
that this contract only states in words — the API keeps `replicas: 1` and all four loops until
step 5 (the thinning is the *last* payoff, not the first), and the three workers serve **both
lanes at once**. *(The diagram predates the §9 reversal and needs redrawing: serving both lanes is
now two deployments of one image rather than one process reading NATS for both, and the
retry-stacking hazard it illustrates is largely gone with the bridge.)*

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
- Two orchestration systems run concurrently for the whole coexistence period, but they no longer
  sit on top of each other: with the NATS bridge rejected ([§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)), each unit of work is driven
  by exactly one of them, and the retry-layering hazard is confined to §10's ported classifier
  being expressed as `RetryPolicy` non-retryable types rather than as its own loop.

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
| **Mechanism for versioning our *workflow code*** (Worker Versioning vs `patched`) | Distinct from §6's pinning of *user definitions*, which is decided. Not open-ended: **Worker Versioning is GA and Temporal's stated default**; patching is the older approach leaving branches to clean up; the pre-2025 experimental variant is already withdrawn from the server. Decide at the first workflow-code deploy that must survive in-flight runs — it needs **server-side enablement** on the self-hosted deployment ([§2](#2-topology)), so it is an infra task, not a code flag ([§6](#6-oq-2--in-flight-edits-definitions-are-pinned-and-that-is-a-different-problem-from-code-versioning)) |
| Conditional execution's design | Its own layer-A PRD, before PRD-018 ([§14](#14-oq-10-remaining-half--conditional-execution-gets-its-own-layer-a-prd-before-monitors)) |
| **Data-flow fan-out** (one block's output consumed by two later blocks — "two extractions on one fetched page") | **Wanted, deferred to post-Phase 4. Owner's call, 2026-08-10.** Layer A validates data flow as a path ([§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)). The stored shape is already a graph, so this is a validator change against unchanged definitions. **Cheaper than it looked, after the §8 review (2026-08-17):** it was thought to make §8's "final artifact" ambiguous and to force the multi-Scrape rider — neither now applies. §8 charges every stored object, so there is no "final artifact" for fan-out to make ambiguous, and the rider is closed on structural grounds with **multiple roots, not fan-out**, named as the trigger that would reopen it. A fanning graph still has one Scrape and still costs one run. Distinct from *parallel* fan-out, which stays a PRD-016 non-goal |
| Run-failure notification (R6's fourth exclusion) | The PM left it unassigned on purpose: it is either an on-failure branch or a run-level setting, and that depends on the conditional-execution decision above |
| Whether `webhook_deliveries` / `crawl_pages` survive as v1-only audit mirrors | Decide at the step that retires each flow, not now |
| Retention window for intermediate block outputs — **the number only** | Needs a real number from the first pipelines ([§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)). **No longer a free dial** (§5 review, 2026-08-10): it is chosen against a stated promise, since it bounds how long R3's "what did this step return" works. The rules around it — result never collected, collection per run not per block, collected renders as *collected* — are decided. **The trade-off changed direction in the §8 review (2026-08-17):** intermediates are now charged while they exist, so the window is no longer operator-storage vs debuggability but **the user's bill vs debuggability**, and collection is a visible refund. It also becomes a *correctness* deadline once Monitors ship: per-run collection is unsafe as soon as an object is shared between runs |
| **Whether the `latest/` copy is chargeable** ⚠️ blocks §8's storage rule | Every worker dual-writes `latest/` + `history/` (ADR-002 §8) while `result_size` reports one copy, so MinIO holds **2× what the meter counts** on every v1 lane. "Charge for what is stored" read literally charges both. Recommendation on record in [§8a](#8a-what-the-storage-rule-costs-on-the-job-path-found-in-review-2026-08-17): **charge one copy** — `latest/` is a convenience alias, not a second artifact, and billing twice for one result is not defensible. Not an owner call yet, and the storage rule is not implementable until it is. v2 already drops `latest/` ([§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)), so this is v1-only with a known end state |
| **Which component performs v2 storage accounting**, and **what happens when a pipeline hits the wall** | [§8d](#8d-who-charges-and-what-happens-at-the-wall--not-yet-decided). §3 says the accounting component "must be named, not inherited"; §8 says the mechanism is unchanged and points back at §3 — so no section names it. On v1 it is `result_consumer.py`, which the migration deletes; the plausible answer is a metering activity at run completion (retryable, idempotent), but that belongs to §9's activity inventory. Enforcement is worse than counting: the wall is hit at the **last** block, after the user's own LLM key has been billed, and admission-time checking cannot substitute because output size is not knowable in advance. Product question — fail the run, allow the overage, or refuse to start new runs while over |
| **Per-lane `storage_accounted_at` markers** | [§8b](#8b-crawl-conditions-and-the-mechanism-the-cutover-promise-needs). The PM's "counting starts at cutover, no backfill, and deleting a pre-cutover artifact must not decrement" needs a per-object record of whether it was ever counted. `job_runs` has one (`models/job_runs.py:35`); **`crawl_pages` has neither that nor a size column**, and nothing yet specifies one on `pipeline_runs`. Not deferred so much as unassigned: each lane needs its marker **in the same change that starts counting it**, or the cutover boundary degrades to a date comparison that is wrong for every row created before and deleted after |

---

## Implementation reference

- **PRD-016** — the product spec this design serves. R6 is the acceptance gate: reproduce
  `scrape → LLM → webhook` as a pipeline before designing any block outside R2's catalog.
- **`temporal-full-migration.md`** — component-by-component change inventory and the
  strangler-fig sequence.
- **`phase4-backlog.md`** §3 — bugs the migration dissolves; do not design fixes for them.
  **§1 P6 / BUG-005** — the batch identity failure that grounds [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
  and [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity).
- **`open-questions.md` Q5–Q8** — the incidents behind [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)'s
  retry-layering warning and [§10](#10-oq-6--the-do-not-delete-list)'s port list.
- **ADR-008** — the scraping behaviour that must survive the transport change untouched.
