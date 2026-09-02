# PRD-016 — ScrapeFlow Workflows: User-defined Pipelines

**Priority:** P1 — the foundation layer of Phase 4
**Source:** `docs/project/workflows-scoping.md` §4A (feature), `phase4-backlog.md` §2 (engine decision)
**Status:** Ready for Architect
**Last updated:** 2026-08-08 (PM review round 3 — **OQ-4 gains a decided section**: the crawl lane
joins the quota meters, at page granularity. Still **11** open questions; OQ-4's *pipeline* metering
question remains the Architect's, but *which lanes the meter covers* is now answered.)
*Previously: 2026-08-04 (PM review round 2 — three Architect escalations decided; see the
revision note below. **OQ-10 is half-answered** — change detection is assigned, conditional
execution is not — and **OQ-4**'s PM constraint is narrowed. No OQ is fully closed.)*

> **Scope note.** "ScrapeFlow Workflows" is one feature in three nested layers:
> **Pipelines (A) → Delivery sinks (C) → Monitors (B)**. This PRD covers **A only** —
> the layer the other two are built on. C and B get their own PRDs once A ships, so the
> Architect is not designing against a moving target.

> **Revision note — PM review round 2 (2026-08-04).** Three questions raised by the Architect
> while writing ADR-009 are now decided, in place:
>
> | Question | Decision | Landed in |
> |---|---|---|
> | Does layer A get a change-detection / cost gate? | **No.** Both halves of change detection go to **Monitors (B)**, where "the previous run of this same thing" is well defined; layer A's obligation is only to not foreclose a block that halts a run early. The cost delta is an accepted, bounded, *named* divergence. | **OQ-10** (change-detection half answered; conditional execution still open), **OQ-4** (constraint narrowed), **OQ-1** (forward-compat obligation), **Non-goals**, **R6**, Success criteria |
> | More than one Webhook block per pipeline? | **No — at most one, rejected at save time** with a message naming the limit. Multi-destination delivery is what layer **C** exists to provide, with rollback; layer A must not teach a fan-out pattern C then has to replace. Silent collapse is explicitly ruled out. | **R2**, **R6**, Success criteria |
> | Cancellation model | **Ratified: no block is ever aborted mid-execution** — the Scrape exception is dropped. The user-facing half is new: cancellation is acknowledged immediately, is visible while pending, says what is still running and for how long, and the completed blocks' outputs stay retrievable. | **R3**, Success criteria |
>
> **One gap was found while deciding these and is recorded, not decided:** a pipeline that fails
> before reaching its Webhook block **tells nobody**, where the equivalent job fires `job.failed`
> from any stage. It is now R6's **fourth** known exclusion with a success criterion of its own.
> It is not assigned to a layer, because the honest answer depends on whether run-failure
> notification arrives as an on-failure branch (conditional execution, **OQ-10**) or as a
> run-level setting that is not a block at all — and that is the half of OQ-10 still open.

> **Revision note — PM review round 3 (2026-08-08) — ✅ owner-confirmed 2026-08-08.** The round-3
> decision below is ratified and carried into ADR-009 §3/§8. (Rounds 1 and 2 above are also
> settled.) The ADR *as a whole* is now **Accepted** (2026-09-08); this decision is settled within it,
> and is tracked for implementation as backlog **§1 P7**. ADR-009 §3 moves quota counting off
> hardcoded table names and onto a **view** that is the single definition of *"a run this user
> started."* That raises a product question the Architect cannot answer alone — **which lanes are
> in the view** — and it is answered in place, in **OQ-4**:
>
> | Question | Decision | Landed in |
> |---|---|---|
> | Do crawls join the view — i.e. do crawls start consuming quota? | **Yes.** A lane that can start work and is absent from the definition of "a run this user started" is a **bug, not a discount**. Crawls consume zero of all three meters today; writing the view while knowingly omitting the one shipped lane it does not cover would bake the omission into the artifact built to prevent it. | **OQ-4** (new decided section), **R5** (named exception) |
> | What counts as one run — one per crawl, or one per page? | **The unit is one fetch of one target URL that produces one stored result.** So: per **page** for `monthly_runs_limit` and `storage_bytes_used`; per **crawl** for `concurrent_jobs_limit`. Cost and contention are different quantities and are allowed to disagree — this is batch's existing shape, not a new asymmetry. | **OQ-4**, Success criteria |
>
> **This is a metering change to a shipped, live feature**, and therefore a named exception to R5's
> "no user-visible change" — scoped to accounting only, with no change to crawl mechanics, API
> shape, or results. It is **not caused by pipelines**: it is a pre-existing gap that the view
> forces us to name, and it would be correct even if Phase 4 were cancelled. Rollout, storage
> reclaim (a hard precondition), and sequencing are all settled in OQ-4.

---

## Problem

A user submits **one URL plus options**, and ScrapeFlow runs a single hard-coded recipe:

```
scrape → (optional) LLM extract → (optional) diff vs last run → (optional) webhook
```

That recipe is not a user-facing concept — it is an implementation detail the user cannot
see, reorder, extend, or branch on. Concretely, a user today **cannot**:

- put a cleaning step between the scrape and the LLM call, so they pay for tokens on nav
  bars and cookie banners;
- validate an extracted field before it is stored, so bad extractions land silently in the
  data pipeline they are feeding;
- run two extractions on one fetched page — a second extraction means a second scrape, which
  means paying for a second render and hitting the target site twice;
- control *when* a step runs. Exactly one conditional exists — an exact-byte hash of the
  scrape output is compared with the previous run's, and an identical page skips the LLM, the
  diff and the webhook (`result_consumer.py:376`). It is hard-coded, always on, invisible in
  the API and the UI, and byte-equality only: a user cannot disable it, cannot ask for "only
  if the *price* field changed," and cannot tell that it fired.

The platform ceiling is the real problem: ScrapeFlow is *a service that runs one recipe*, and
the ML/data-pipeline use case in its charter needs *a platform where users build recipes*.

There is also a standing reliability argument. Today's fixed recipe is enforced by a
hand-rolled state machine (`result_consumer.py`) that has already caused a production
incident — **Q8**, where an overloaded status value closed a feedback loop and burned ~200
billable LLM calls in five minutes. Every step added to the fixed recipe multiplies that
class of bug. Pipelines are the product reason to move orchestration onto an engine that
owns run state; Q8 is the operational one.

---

## Goals

1. A user can define a **pipeline** — an ordered chain of typed **blocks** — save it, name
   it, edit it, and run it, without ScrapeFlow code changes.
2. A pipeline run produces the same quality of result artifact and status visibility a job
   run does today: retrievable output, per-step status, and a clear terminal outcome.
3. **Today's fixed recipe is expressible as a pipeline** — scrape → LLM → webhook — and
   produces equivalent output. This is the acceptance bar for the whole layer.
4. A step that fails for a **transient** reason is retried without user involvement; a step
   that fails for a **terminal** reason stops the run and says which step failed and why.
5. Running a pipeline never double-charges the user: one run = one scrape of the target
   site, and one billable LLM call per LLM block per run, including across retries and
   redeploys.
6. Existing jobs, batches, crawls, and schedules keep working **unchanged** while pipelines
   exist alongside them.

---

## Non-goals

- **Delivery sinks (S3 / database / Sheet / email) and saga rollback** — the next layer, its
  own PRD. This PRD's only outbound block is the **webhook** that already exists, and there may
  be **at most one of it per pipeline** (R2). "Send one result to several destinations" is the
  defining capability of layer C and arrives there with rollback attached; layer A must not teach
  a fan-out pattern that C then has to replace.
- **Monitors** — scheduling, durable long sleeps, human-approval waits, and the dormant
  scheduled-crawl gap. Own PRD, built on this one.
- **A visual pipeline builder in the SPA.** Pipelines are API-first in this PRD. The
  user-facing surface is exactly two things: *list* pipelines, and *show* run status including
  R3's per-block detail. Authoring, editing, re-running, and comparing runs in the UI are all
  later.
- **Migrating existing jobs/batches/crawls onto pipelines.** Users are not asked to move,
  and nothing auto-converts. Retiring the old lane is a separate, later decision.
- **Branching and parallel fan-out.** Chains are linear in this PRD. (Conditional execution
  is the *first* thing to add after it ships — see **OQ-10**, which also records that
  Monitors depends on it.)
- **Change detection — neither the cost gate nor the reporting diff.** Now an explicit non-goal
  rather than an unassigned gap: both belong to **Monitors (B)**, because "the previous run of
  this same thing" is only well defined once a monitor supplies the identity (**OQ-10**). Layer A
  owes B one thing — not foreclosing a block that ends a run early and still reports success
  (**OQ-1**). The consequence is that a repeat run against an unchanged page costs more as a
  pipeline than as a job; that is accepted, bounded and measured, not overlooked (**OQ-4**, R6).
- **User-authored code as a block.** Blocks come from a fixed catalog. Arbitrary user code is
  a sandboxing problem, not a pipeline problem. The **Validate** block is the one place user
  input shapes execution, and it is deliberately confined to a declarative rule vocabulary
  (R2) to stay on this side of the line — an expression evaluator would cross it.
- **Replacing the LLM key model.** Users still bring their own provider key.
- **MCP tooling for pipelines.** The MCP server keeps working unchanged (R5), but no pipeline
  tools are added to it here — the HTTP API settles first and MCP is a thin wrapper written
  afterward. Said explicitly so the API is neither distorted toward LLM-callable ergonomics
  nor made awkward to wrap later.

---

## User stories

**As a data engineer** feeding an ML pipeline, I want to define *scrape → clean → LLM
extract → validate* once and run it against many URLs, so my training data arrives already
structured and already checked.

**As a cost-conscious user**, I want a cleaning step before the LLM block, so I stop paying
tokens for navigation and cookie banners on every single run.

**As a user** whose extraction schema changed, I want to edit a saved pipeline and re-run it
without re-creating the job or re-scraping pages I already have.

**As a user debugging a bad result**, I want to see *which step* failed and what it returned
— not just "job failed" on the whole run.

**As a platform operator**, I want a ceiling on steps per pipeline and on concurrent runs per
user, so one user's pipeline cannot consume all worker capacity.

**As the maintainer**, I want today's recipe reproduced as a pipeline before any new block
types are added, so the model is proven against known-good output rather than against a new
feature's expectations.

---

## Requirements

### R1 — Pipeline definition

- A pipeline has a **name** (unique per user), an ordered list of **blocks**, and belongs to
  exactly one user. Cross-tenant access returns **404**, consistent with jobs.
- Each block has a **type** from a fixed catalog and a type-specific **configuration**.
- A pipeline may declare named **run inputs** — values supplied per run rather than baked into
  a block's configuration — and blocks reference them by name. At minimum the **Scrape**
  block's URL must be expressible this way. Without run inputs one saved pipeline serves
  exactly one URL, so "run it against many URLs" (user story 1) means one pipeline per URL:
  that collides with the pipelines-per-user limit below, and turns "edit a saved pipeline"
  (user story 3) into editing every copy. Note this is **not** fan-out — one run still
  processes one URL; running many is many runs. Parallel fan-out stays a non-goal.
- Full CRUD: create, list, fetch, update, delete. Deleting a pipeline must not destroy the
  history of runs already executed from it.
- A pipeline is **validated at save time**, not at run time. Saving an invalid pipeline
  fails with a message naming the offending block and reason. At minimum: unknown block
  type, invalid config for the type, a chain that does not start with a source block, a
  block whose input cannot be produced by anything before it, and a block referencing a run
  input the pipeline does not declare.
- **Limits:** maximum blocks per pipeline and maximum saved pipelines per user, both
  operator-configurable.

### R2 — Block catalog (this PRD)

The catalog is deliberately small — enough to express today's recipe plus the two steps users
most visibly lack:

| Block | Does | Notes |
|---|---|---|
| **Scrape** | Fetches one URL and produces page content | Must expose the options a job exposes today: output format, engine (http/playwright), proxy, cookies, page actions, robots.txt toggle |
| **Clean** | Strips boilerplate (nav, ads, scripts) from content | New capability; the cost-saving step users lack |
| **LLM extract** | Runs the user's schema against content via their own key | Same provider/key model as today |
| **Validate** | Asserts that the block's **input** satisfies a user-supplied rule — so it can guard scraped content *before* an LLM call as well as check extracted output after one | Rules are **declarative only** (schema, type, presence, comparison to a constant); no expression evaluation. Failing validation is a **terminal** run failure naming the rule that failed |
| **Webhook** | Delivers the run result to a user URL | Reuses today's delivery + SSRF re-validation behaviour, including on every attempt. **Delivery is a step the run depends on, not a fire-and-forget side effect** — a terminal delivery failure fails the run. This diverges from today's path; mechanism and retry horizon are **OQ-11**. **At most one Webhook block per pipeline** — see below |

**A pipeline may contain at most one Webhook block, and a second one is rejected at save
time** under R1's validation rule, with a message that names the limit and says multi-destination
delivery is coming as its own capability. It is **not** silently accepted and collapsed into one
delivery — a user who wires two destinations and gets one, with no error, has lost data and has
no way to find out.

The reason is layer ownership, not difficulty. *"Send this one result to several places"* is the
defining sentence of **Delivery sinks (layer C)** — `workflows-scoping.md` §4C is literally "put
the file in my S3 bucket, append a row to BigQuery, and email me a summary," and the thing C adds
alongside the sinks is **saga rollback for partial failure**. Two Webhook blocks in layer A would
ship that exact fan-out *without* rollback, and R2's own rule makes the gap concrete: delivery is
a step the run depends on, so if the second webhook fails terminally the first has already fired
and cannot be undone, and the run reports `failed` having half-delivered. That is a half-saga,
taught to users, that C must then either honour or break. Capping at one keeps the capability
whole and arriving once.

The cap is a **layer-A cap, not a permanent product rule** — it lifts when C ships, with rollback
attached. Users are not being told "no"; they are being told "not from this block."

Each block declares what it consumes and produces, so R1's save-time validation is possible.
Bot-wall detection stays the **Scrape** block's responsibility and remains a terminal failure
of that block — a wall must never flow downstream as if it were content. The stakes are higher
here than on today's path: a wall that escapes Scrape lands in the **LLM** block, and the user
pays real money to extract structured data from a CAPTCHA page.

**Validate rules are declarative on purpose.** Three reasons, all pointing the same way:

1. An expression evaluator is user-authored code by another name — see the Non-goal on exactly
   that, which this block would otherwise quietly breach.
2. A durable engine recovers by **replaying** a run's history. A rule that can return a
   different answer on replay is a correctness bug that surfaces only *after* a restart. A
   declarative check is deterministic by construction.
3. Validation is **terminal** and fires after the LLM block has already billed the user, so a
   user must be able to read their own rule and predict what it will reject.

Cross-field rules (`discount < price`) are therefore out of scope in this PRD. They are
additive later — as their own decision, with its own security review, rather than acquired by
accident through a vague requirement.

### R3 — Execution and run visibility

- Runs are triggered **on demand** via the API in this PRD (scheduling is Monitors).
- A run has a terminal outcome — completed, failed, or cancelled — and **per-block** status
  and timing, so a user can see which step failed. This is the single biggest observability
  gain over today's opaque `job_runs.status`.
- Output is retrievable per run, and the final block's output is retrievable as *the* result.
- A run can be **cancelled in flight**. Cancellation takes effect at the **next block
  boundary**: the block already executing runs to completion, no subsequent block starts, and
  work already completed is not rolled back (rollback is the Delivery layer's saga).
  **No block is ever aborted mid-execution — there is no exception.** A run therefore always
  stops at a block boundary, and "where did it stop" always has one clean answer.

  The rule is absolute on purpose. An earlier draft of this PRD carved out **Scrape** — the one
  block that is long-running, holds a scarce shared resource, and has no external side effect.
  **That exception is dropped.** The reasoning is recorded here so it is not relitigated:
  - it was the only thing complicating R3's own rule, and Delivery sinks (layer C) — which are
    all side-effecting — now inherit "never abortable" with no special-casing to write;
  - the cost of not aborting is **bounded and small**: one wasted scrape, capped by the Scrape
    block's own declared time budget (R4). Against that, real mid-flight abort needs
    interruptible work, cancellation delivery, and heartbeating;
  - it is **not a regression**. Today's cancel does not stop the scrape either — the API marks
    the run cancelled and the worker's result is discarded when it arrives
    (`result_consumer.py:614`). The worker is never told anything;
  - it is a one-way door **in the safe direction**: adding abort later is a pure improvement,
    removing it later would be a visible regression.

  **Abortable blocks are a deliberate later enhancement, not an oversight.** If they are ever
  added, Scrape remains the only candidate in this catalog and the test is unchanged:
  long-running, holds a scarce shared resource, and has no external side effect. LLM extract
  fails that test (aborting does not reliably avoid the provider bill — generated tokens are
  still charged); Clean and Validate finish faster than a cancellation could reach them; Webhook
  and every layer-C sink are side-effecting, and cutting a delivery mid-flight replaces a known
  outcome with an unknown one.

- **Cancellation must be visible while it is still pending.** This is the user-facing cost of the
  rule above and it is a requirement, not a UI detail: a user who cancels during a Scrape can
  wait most of that block's budget before the run reports `cancelled`, and a Cancel button that
  appears to do nothing for a minute reads as broken. So:
  - the cancel request is **acknowledged immediately**. It must never look ignored — and it must
    never report `cancelled` before the run has actually stopped, which would be the same lie in
    the other direction;
  - until the run reaches its terminal outcome, it reports that cancellation is **in progress**,
    distinguishable from both "running normally" and "cancelled";
  - it says **what is still running** — which block — and **an upper bound on how long the wait
    can last**. R4 already requires every block's time budget to be declared, so a bound always
    exists and can be stated. A user must never be asked to wait an unknown length of time.
- **What the user was charged, and what they get for it.** A cancelled run may already have
  billed the user for an in-flight LLM block. The API response and the run detail must **say
  so** — a Cancel button that silently keeps charging reads as a bug, and is only acceptable if
  the user was told. It follows that **the outputs of blocks that completed before the
  cancellation stay retrievable**, under this section's per-run output rule: telling a user they
  were charged for a step and then withholding what they paid for is worse than not telling them
  at all. This is a deliberate improvement on today's path, which discards a cancelled run's
  worker result outright (`result_consumer.py:614`).
- A run must survive an API or worker **restart or redeploy** mid-run and continue. This is
  the property today's path lacks and the whole engine choice exists to provide.
- Runs count against the user's existing quota and storage accounting. **How** a multi-step
  run is metered is an Architect question (see OQ-4), but it must not be free.
- **Hitting the storage ceiling must be legible.** `storage_bytes_used` is a hard, enforced
  quota, and layer A ships **without** the content-hash gate that keeps repeat runs from adding
  bytes (OQ-10, R6) — so a pipeline accumulates storage faster than the job it reproduces, and a
  user can meet this wall who never approached it before. A run that fails for lack of storage
  quota must say **that** — which block, which quota, how much was needed — not a generic
  failure. Today's equivalent reports `storage_accounting_failed` (`result_consumer.py:413`),
  which tells the user nothing they can act on; the pipeline path must do better, because it is
  the path more likely to hit it.
- **A ceiling on concurrent runs per user**, operator-configurable, alongside R1's
  blocks-per-pipeline and pipelines-per-user limits. This is the limit that actually protects
  worker capacity: R1's two bound how much a user can *define*, not how much they can *start
  at once* — a user with 200 saved pipelines can trigger 200 runs in a second. **A mechanism
  already exists — `user_quotas.concurrent_jobs_limit`** — so the decision is whether pipeline
  runs count against that same pool or get their own, not whether to build a second mechanism
  beside it (OQ-4). This also interacts with **OQ-11**: if a Webhook block waits on a long
  delivery horizon, runs stay open for hours and a handful of dead receivers can consume a
  user's entire concurrency budget. Behaviour at the ceiling (reject the trigger vs queue it)
  is an Architect call, but it must be visible to the user either way.

### R4 — Failure handling

- Each block distinguishes **transient** failures (retry automatically, with backoff) from
  **terminal** ones (stop the run, report which block and why). **An unrecognised error is
  classified terminal** — the classification fails *closed*, on purpose, because the risk is
  asymmetric: a wrong "transient" guess retries against the user's own provider key and bills
  them for it, while a wrong "terminal" guess fails a single job. This is the existing workers'
  rule, already tested and in production; OQ-6 requires it be **ported, not re-derived**.
- **Retry must live in exactly one visible layer.** This is a hard requirement, not a
  preference: the entire Q5/Q6/Q7 cluster was retries hidden in layers nobody was looking at
  (a provider SDK's `max_retries`, JetStream redelivery, a cold-booting endpoint), and the
  compounding failure billed users for it. A block's retry policy must be the only retry
  operating on that block, and it must be inspectable.
- An LLM block must **never** be retried in a way that bills the user twice for one logical
  step. If an attempt succeeded but a later step failed, the succeeded attempt is not redone.
- **Every block declares a time budget, and the budgets must compose.** This is the retry rule
  above applied to *time*, and the failure mode is on record: Q6 was an outer layer being less
  patient than the work inside it — JetStream's 30s `ack_wait` under a 37s scrape, producing
  redelivery mid-job and an infinite re-scrape loop. Three properties:
  1. Each block's budget is **declared and inspectable** — the same standard R4 already sets
     for its retry policy.
  2. Budgets **compose**. A run-level ceiling shorter than what its blocks are individually
     permitted will kill a block that was comfortably inside its own budget; that is Q6 again
     under a new name. A configuration that violates this must fail at **save** time, not be
     discovered at run time.
  3. Timeout maps onto the transient/terminal split above: a single **attempt** exceeding its
     budget is *transient* and the block's own retry policy handles it; a block's **total**
     budget exhausting is *terminal* and fails the run, naming that block.

  **The LLM block's budget must accommodate cold starts.** A scale-to-zero endpoint takes
  90–110s to wake, which is why the current timeout is 180s. A reader who picks a "sensible"
  60s default silently re-breaks the exact failure OQ-6 exists to prevent. The numbers are the
  Architect's call; this constraint is not.

### R5 — Coexistence with the existing path

- Pipelines are **additive**. Jobs, batches, crawls, schedules, webhooks, the admin panel,
  and the MCP server keep working with no user-visible change — with **one named exception**:
  **crawls begin consuming quota** (OQ-4, round 3). That is an accounting change only — crawl
  mechanics, API shape and results are untouched — and it is **not caused by pipelines**. It is a
  pre-existing gap (crawls consume zero of all three meters today) that ADR-009's run-counting view
  forces us to name, and it would be correct even if Phase 4 were cancelled. It is called out here
  so it is a recorded product decision rather than a contradiction found later.
- **A unit of work executes on exactly one lane — never both.** Double execution means a
  double scrape of the target site and a double LLM bill. This is the top cutover risk on
  record and must be structurally prevented, not just avoided by convention.
- Pipelines are visible to admins the way jobs are: an admin can list any user's pipelines
  and runs, and read a run's result through an admin-scoped route.

### R6 — Proving the model (the acceptance gate)

Before any block outside R2's catalog is designed, a pipeline of **scrape → LLM → webhook**
must run end to end and produce output equivalent to the same job on the existing path, on
the same URL, with the same schema. If it does not, the model is wrong and no further layer
is built on it.

**What "equivalent" means, precisely.** Two halves: how it is judged, and what is not claimed.

**Judged on structure and mechanics, not byte-equality.** The LLM block is nondeterministic —
the same prompt against the same page can come back with a different key order, different
phrasing in a free-text field, a number formatted differently. That is normal model behaviour,
and holding the gate to byte-equality would fail it for a reason that has nothing to do with
the pipeline model, which is the only thing under test. Equivalence therefore means:

- the same blocks ran, in the expected order, with the expected per-block outcomes;
- the extracted result satisfies the **same schema**, with the same field set populated;
- the artifact is stored in the same place and retrievable by the same means;
- the webhook fired with a payload of the same **shape**, field for field.

**Not claimed: behavioural parity with today's job path**, which has four things this pipeline
does not:

- the **content-hash cost gate** (`result_consumer.py:376`), which on a byte-identical repeat
  skips the LLM entirely **and** deletes the artifact it just wrote, repointing the run at the
  previous run's stored file (`:380–393`). **Two costs, not one** — the PRD previously tracked
  only the LLM half. A repeat run of the R6 pipeline against an unchanged page therefore costs
  **one billable LLM call and at least one newly stored artifact** (more if OQ-4 retains
  intermediates) where the job costs **zero of each**. Both halves must be measured (Success
  criteria) and both are accepted here. **Owner: Monitors (B) — see OQ-10**;
- the **reporting diff** (`:460` text / `:506` JSON), which populates
  `diff_detected`/`diff_summary` in the webhook payload. **Owner: Monitors (B) — see OQ-10**;
- **webhook failure semantics.** Today `create_webhook_delivery` inserts a *pending* row and
  `webhook_loop` delivers it asynchronously with backoff — the run is already `completed` by
  then, so **an undelivered webhook never fails a job**. As a pipeline *block* it does (R2).
  Same recipe, different terminal outcome. That is **OQ-11**;
- **notification when the run fails before reaching the Webhook block.** Today *any* worker
  failure — scrape or LLM — fires `job.failed` to the user's webhook URL
  (`result_consumer.py:563–575`), because delivery is triggered by the run's outcome, not by a
  position in the recipe. In a pipeline the Webhook
  block is a *step in the chain*: if an earlier block fails terminally, the chain stops and the
  Webhook block never runs, so nobody is told. Failure visibility in this PRD is **R3's per-block
  run status, read from the API** — deliberately, because the alternative is either an
  on-failure branch (conditional execution, a non-goal here) or a **run-level** notification
  that is not a block at all. Which of those it becomes is settled when conditional execution is
  settled; **it is not layer A in this PRD** (see OQ-10).

All four are stated here so a divergence found during the comparison is read as a **known
exclusion** — not as a failed gate, and not as licence to add a diff block outside R2's catalog,
which is the exact move this gate exists to prevent. Each now names the layer that owns it, so
"nothing picked it up" cannot recur.

**Multi-destination delivery is likewise out of the comparison.** The R6 recipe has one webhook,
so R2's one-Webhook-block cap does not narrow the gate. It is noted only because a reader
reproducing "today's recipe" for a job with several consumers will reach for a second Webhook
block and must be sent to layer C rather than around the cap.

---

## Success criteria

- [ ] A user can create, list, fetch, update, and delete a pipeline; another user's pipeline
      returns 404.
- [ ] Saving a pipeline with an unknown block type, invalid block config, a chain not
      starting with a source block, or an unsatisfiable input fails with a message naming the
      block and the reason.
- [ ] Saving a pipeline with **two Webhook blocks is rejected** with a message naming the
      one-per-pipeline limit; it is never accepted and silently collapsed into a single
      delivery. A pipeline with exactly one Webhook block saves and runs normally.
- [ ] Per-user limits on blocks-per-pipeline, pipelines-per-user, and **concurrent runs per
      user** are enforced and operator-configurable; hitting the concurrency ceiling produces a
      clear response rather than a silent stall.
- [ ] All five R2 block types execute, and each exposes the options listed for it.
- [ ] A `scrape → LLM → webhook` pipeline is equivalent to the same job on the existing path,
      on the same URL with the same schema **(R6 gate)** — judged on **structure and mechanics**
      (blocks run and their outcomes, schema satisfied with the same fields populated, artifact
      stored and retrievable the same way, webhook payload of the same shape), **not** on
      byte-equality of nondeterministic LLM output.
- [ ] One saved pipeline runs against different URLs by supplying the URL as a run input, and
      changing its extraction schema is **one** edit rather than one per URL.
- [ ] The cost of an R6 pipeline run is measured against the equivalent job **on both axes**:
      run each twice against an **unchanged** page, then count (a) billable LLM calls and
      (b) **bytes added to `storage_bytes_used`**. Today's content-hash gate makes the job's
      second run cost zero on both; a pipeline without that gate costs one call and one artifact
      every run. The measured delta is **recorded before shipping** — not discovered on a user's
      invoice or at a storage wall.
- [ ] **Metering parity (hard):** for the *same* work, the R6 gate pipeline consumes no more
      quota (`monthly_runs_limit`, `concurrent_jobs_limit`, `storage_bytes_used`) than the job it
      reproduces **for having been expressed as a pipeline** — a 3-block pipeline doing one
      scrape and one LLM call must not bill 3 units where the job bills 1 (OQ-4).
- [ ] **Lane completeness (OQ-4, round 3):** the run-counting view enumerates **every** lane that
      can start work — job runs, batch items, **crawl pages**, pipeline runs — and adding a lane
      later is a change to the view, not an audit of every call site. Verified by exhausting a
      user's `monthly_runs_limit` and confirming that a **crawl** is then refused at creation,
      naming the meter and the shortfall. A lane absent from the view is a bug, not a discount.
- [ ] **Crawl metering granularity:** a crawl of N pages consumes **N** monthly runs and **one**
      concurrent slot, and each stored page counts against `storage_bytes_used`. Crawl artifacts
      are deletable and deleting them frees the bytes — verified before counting is enabled.
- [ ] **Feature-parity gap (accepted, bounded):** the repeat-run delta above is attributable
      *only* to the absent content-hash gate, and to nothing else. It is recorded against R6 as a
      known exclusion owned by Monitors (B), with the storage figure stated in absolute terms
      (bytes per run) so the size of the exposure is a number, not an adjective.
- [ ] A run reports per-block status and timing; a failed run names the failing block and the
      reason.
- [ ] A run that exhausts the user's storage quota fails with a message naming the block, the
      quota, and the shortfall — not a generic accounting error.
- [ ] A run interrupted by an API/worker restart mid-execution resumes and completes; it does
      not re-execute already-completed blocks.
- [ ] A transient block failure retries automatically; a terminal one stops the run. An
      **unrecognised** error is treated as terminal and does not retry.
- [ ] A run whose webhook receiver is unreachable produces the outcome OQ-11 settles on, and
      that outcome is recorded as a known divergence from the job path rather than found during
      the R6 comparison.
- [ ] A pipeline whose **Scrape block fails terminally** stops the run and names that block in
      the run detail, and **sends no webhook** — the equivalent job's `job.failed` delivery has
      no layer-A counterpart. Verified deliberately, as the fourth R6 known exclusion, so the
      silence is a recorded product decision and not a bug report waiting to happen.
- [ ] An LLM block is billed once per run per block, verified across an induced retry and an
      induced restart.
- [ ] A run can be cancelled mid-flight; no subsequent block executes after cancellation, and
      **no block is aborted mid-execution — including Scrape**. The executing block completes and
      the run stops at that boundary.
- [ ] The cancel request is acknowledged immediately; while the executing block finishes, the run
      reports cancellation **in progress** — distinguishable from both running and cancelled —
      and states **which block is still running** and **an upper bound on the remaining wait**.
      The run does not report `cancelled` until it has actually stopped.
- [ ] A cancelled run states **what was already charged** (e.g. a completed LLM block), and the
      outputs of blocks that completed before the cancellation are **still retrievable**.
- [ ] A single attempt exceeding its budget retries; a block exhausting its total budget fails
      the run and names the block. A configuration whose run ceiling is shorter than the sum of
      its block ceilings is rejected at save time.
- [ ] A bot-wall response fails the Scrape block terminally and never reaches a downstream
      block.
- [ ] Validation failure fails the run terminally and reports the rule that failed.
- [ ] Existing jobs, batches, crawls, schedules, and webhooks are unaffected — the existing
      API test suite passes unchanged.
- [ ] No unit of work is observed executing on both lanes.
- [ ] An admin can list any user's pipelines and runs and read a run result.

---

## Open questions for Architect

**OQ-1 — Block definition and storage.** Fixed typed catalog vs a general DAG schema; JSON in
Postgres vs a small DSL. Whatever is chosen must support R1's save-time validation and OQ-2's
versioning. **It must also not foreclose conditional execution or non-linear wiring.** Those
are non-goals *of this PRD*, not of the feature — and a strictly linear model with implicit
previous-block wiring would make adding them later a breaking change to every stored pipeline
definition rather than an additive one. Leaving room costs nothing now; not leaving it costs a
re-spec (see OQ-10).

**One forward-compatibility obligation is now concrete rather than hypothetical.** OQ-10 assigns
change detection to Monitors (B), but the half of it that layer A owns is the ability for a block
to **stop a run early and have the run still be a success**: the cost gate's whole purpose is to
skip everything downstream and finish. No block in R2's catalog does this, so nothing needs
building here — but the model must be able to express **"run ended before the last block, outcome
`completed`, these blocks were skipped"** without inventing a new terminal outcome and without a
breaking change to stored definitions. R3's terminal outcomes stay three (completed / failed /
cancelled); "skipped" is a *block* state, not a run state. If the model cannot represent that,
B's PRD turns into unplanned layer-A rework — which is exactly the failure OQ-10 was raised to
catch.

**Settle in the same breath how a block names its input:** implicitly the previous block's
output, or an explicit reference to any earlier block? R1's validation clause says "anything
before it," which implies the latter, while Non-goals says chains are linear — both hold only
if a block can reference an earlier block **by name**. This is not decorative: the Problem
section's "run two extractions on one fetched page" needs the second LLM block to consume the
**page**, not the first LLM's JSON, and is unsatisfiable under implicit previous-block wiring.
Named references need identifiers that survive an edit, which loops straight back into OQ-2.

**And what flows between blocks must be a reference, not a payload.** Activity inputs and
outputs are recorded in the engine's **workflow history**, which caps individual payload size
(low single-digit MB) and bounds total history size — confirm the current numbers against the
engine's own docs, not this PRD. Scraped pages already exceed that range: the BUG-003 audit
measured genuine pages between 291 KiB and 4.1 MiB. A block model in which page *content* flows
block to block therefore fails on large pages for a reason that has nothing to do with
scraping, and bloats retained history on every run that doesn't fail. Content stays in MinIO;
only paths move through the workflow. R2's "each block declares what it consumes and produces"
should be read as declaring **types of reference**, not types of payload.

This is also the largest new *operator-side* cost in the migration, and worth stating plainly
next to OQ-4's user-side metering: today's NATS stream is `--retention work`, so acked messages
are deleted and orchestration state is effectively free and self-cleaning. Workflow history is
**retained after completion** by design — that retention is what buys replay and resumption.
Keeping payloads out of it is what keeps the bill proportionate.
*(Carried from `workflows-scoping.md` §9.2.)*

**OQ-2 — Editing a pipeline with runs in flight.** R1 allows editing a saved pipeline while a
run from a previous version is still executing. Does an in-flight run pin its definition
version, or adopt the edit? This interacts directly with the engine's workflow-versioning
rules and is the most likely source of a subtle correctness bug in this PRD.

**OQ-3 — Enforcing "exactly one lane" (R5).** What structurally prevents the same unit of
work from running on both the existing path and a pipeline? Related and already on record:
moving a recurring job to an engine schedule requires disabling it on the old lane, or it
fires on both.

**OQ-4 — Metering a multi-step run.** Three meters exist today, all per-user **shared pools**:
`user_quotas.monthly_runs_limit`, `concurrent_jobs_limit`, and
`storage_bytes_limit`/`storage_bytes_used`. Jobs — with batches and crawls beneath them — are
their only consumer. Pipelines become a *second* consumer of the same pools, which is where
R5's "no user-visible change" comes under pressure: every pipeline run is one fewer job run
that month, for a user who did not change how they use jobs at all.

And because a pipeline is multi-step where a job is not, "one run" does not mean the same thing
on each lane. **Neither answer is free:**

- **A pipeline run = one unit.** A 5-block pipeline costs what a 1-step job costs. Pipelines
  become the cheap way to do more work, users migrate to arbitrage the quota, and
  `monthly_runs_limit` stops tracking the resource consumption it exists to bound.
- **A pipeline run = one unit per block.** The R6 gate pipeline (`scrape → LLM → webhook`)
  burns **3 units** where the identical job burns 1 — same URL, same schema, same output,
  triple the quota, purely for having been expressed differently. This is the worse of the two:
  it makes pipelines a *penalty* and contradicts R5's "additive."

Storage has the same shape. If intermediate block outputs are retained, a 5-block pipeline
stores 5 artifacts where a job stores 1, so `storage_bytes_used` inflates for identical work.
The retention question is therefore not only "is debuggability worth the disk" but "does
retention change what the user is charged."

Also settle whether the pools are **shared with jobs or separate**. Shared leaves the user one
number to understand but makes R5's claim untrue; separate keeps R5's claim but hands the user
two budgets to reason about.

PM position: users must be able to see *why* a step failed, so at minimum **failure context is
retained**. Retention of successful intermediates and the metering rule itself are Architect
calls — subject to the constraint below.

**The constraint, narrowed (PM review round 2).** It previously read: *"the R6 gate pipeline must
not cost a user more than the job it reproduces."* As written that is now false, and it was
conflating two different things:

- **(a) Metering parity — stays hard, unchanged in force.** Identical work must not consume more
  quota **for having been expressed as a pipeline**. This is the arbitrage/penalty question above
  — the "one unit per block" option that makes the R6 pipeline burn 3 units for one scrape and
  one LLM call is ruled out by this, and so is any storage rule that charges for retained
  intermediates the user did not ask for. This constraint is not the Architect's to relax.
- **(b) Feature parity — explicitly waived for layer A, with a named expiry.** The R6 pipeline
  lacks the content-hash cost gate, so a *repeat* run against an unchanged page costs one LLM
  call and one stored artifact where the job costs zero of each. That is a **missing feature**,
  not a metering defect, and OQ-10 assigns it to Monitors (B). The waiver is bounded three ways:
  it applies only to repeat runs against unchanged content; the delta must be **measured and
  recorded before shipping** (Success criteria), not estimated; and **B does not ship without the
  gate** — this is a launch requirement of B, not a wish.

**Why the waiver is safe in layer A specifically.** The gate's value is proportional to run
frequency, and R3 makes pipeline runs **on demand only** — scheduling is Monitors. The
pathological case that motivates the gate (*watch hourly, changes twice a week: ~2 stored files
per week as a job, 24 per day as a pipeline, against an enforced 5 GB `storage_bytes_used` wall*)
**is not reachable in layer A at all**, because nothing in layer A can make a pipeline recur. A
human clicking "run" is asking for a fresh result; that is the point of clicking. The exposure
appears the moment something *else* clicks run on a timer — which is the moment B exists. So the
gap opens and closes in the same layer.

---

**✅ DECIDED (PM review round 3, 2026-08-08) — which lanes the meter covers: crawls join, at page
granularity.** *(The pipeline metering rule itself stays the Architect's, under the constraint
above. This answers the prior question the view raises: which lanes it contains.)*

ADR-009 §3 answers OQ-1(a) by moving quota counting off hardcoded table names and onto a **view**
that is the single definition of *"a run this user started."* That is the right fix, and it forces
a question the Architect cannot answer alone: **which lanes are in the view.** Pipelines obviously
are. **Crawls are not — and are not in today's meters either.**

**The gap, verified against live code (2026-08-08).** `JobRun` rows are created in exactly three
places — `routers/jobs.py:207`, `routers/batch.py:105`, `core/scheduler.py:80` — and never for a
crawl. Crawl work lives in `crawl_pages`, with its own `status` and `result_path`.
`routers/crawls.py` contains **no quota check of any kind**. `increment_storage_bytes` has exactly
one call site (`result_consumer.py:85`), gated on `job_runs.storage_accounted_at`; the coordinator's
result handler sets `page.result_path` and never accounts. Net effect: **a crawl of up to
`max_pages` = 10,000 pages costs zero monthly runs, zero concurrent slots and zero counted bytes**,
from one API call. Nobody decided crawls are free — the query never looked. This is the same shape
as BUG-005: a contract that assumed every unit of work is a job.

**1. Crawls join the view.** A lane that can start work and is absent from the definition of "a run
this user started" is a **bug, not a discount**. Writing the view now while knowingly omitting the
one shipped lane it does not cover would bake the omission into the very artifact built to prevent
it — and would leave the platform's stated position as "your 500-run limit is real unless you phrase
the work as a crawl."

**2. The unit is one fetch of one target URL that produces one stored result** — not one user
action, and not one step. Applied across all four lanes: a job run is 1; a batch of N URLs is N
(already shipped); **a crawl of N pages is N**; a pipeline run is 1. A crawl page is byte-for-byte
the same unit of work as a job run — same NATS subject, same worker, same fat message, same stored
artifact, same proxy bandwidth. It differs from a batch item only in that the URL was *discovered*
rather than supplied, and **discovery is not a discount**.

**3. This does not contradict ADR-009 §8 — it supplies the reason §8 is true.** §8 prices a pipeline
run at one unit "regardless of block count," and that stays correct: R1 fixes one run to one URL,
and Clean / LLM extract / Validate / Webhook fetch nothing, so block count does not change how many
pages were fetched. R6's metering-parity criterion still passes unchanged. Stating the rule as *the
fetch is the unit* is what makes it extend to a lane the ADR did not originally cover, instead of
being a per-lane convention.
**One rider for the Architect:** if layer A permits more than one Scrape block in a chain, that run
fetches twice and costs 2. PM preference is to **count executed Scrape blocks** rather than cap
Scrape at one — the Webhook cap had a layer-ownership reason (C owns fan-out) that has no analogue
here, and capping would foreclose "scrape a page, then scrape a link found on it." Either resolution
satisfies the rule; leaving it unstated does not, because "regardless of block count" reads as a
licence for a five-Scrape pipeline to cost 1.

**4. `concurrent_jobs_limit`: a crawl is one unit, not N.** Cost and contention are different
quantities and are allowed to disagree. Three reasons, all pointing the same way:
- Per-page concurrency makes the feature unrunnable. The default `concurrent_jobs_limit` is **5**
  and the default `max_pages` is **100** — every default crawl would be permanently over-limit. A
  meter that makes a shipped feature impossible is misconfigured, not strict.
- Making it work would require dispatch throttling against a live quota read inside `coordinator/`
  — new scheduling logic in a service ADR-009 §13 deletes. It would change crawl throughput (a real
  user-visible change) and then be thrown away.
- The contention the limit exists to bound is already bounded on this lane by two other mechanisms:
  the coordinator's dispatch batch size, and the one-active-crawl-per-origin rule in
  `routers/crawls.py`. The meter is not the only thing protecting worker capacity here.

So a crawl occupies **exactly one concurrent slot for its lifetime**, from creation until terminal.
Revisit the granularity when `CrawlWorkflow` exists and per-workflow activity concurrency is a
configuration value rather than new code.

**5. Batch is the precedent, and it already has this exact shape.** `routers/batch.py:46–47`
pre-approves **monthly** runs at `batch_count=len(urls)` but checks **concurrency** once, at
`batch_count=1`. One user action, N cost units, one admission slot — which is precisely the crawl
rule. (Named honestly: batch's N `job_runs` rows *do* then count individually against the *next*
request, so a 100-item batch locks the user out until it drains. That is an inconsistency inside
batch, not a precedent to copy; if it is ever normalised it should move **toward** this rule, not
away from it.)

**6. `storage_bytes_used`: every stored crawl page is charged — and this ships in the same change,
not separately.** ADR-009 §8's "only the final artifact is charged" needs **no exception** here: a
crawl has no intermediates, every page result is a deliverable the user retrieves via
`GET /crawls/{id}/pages`. In scope because it is the axis where crawls are most dangerous —
10,000 pages at the BUG-003-measured 291 KiB–4.1 MiB range is **2.8 GB–40 GB from a single API
call**, against an enforced 5 GB default — and because shipping "crawls now cost quota" while the
largest axis stays free means announcing a **second** pricing change to the same feature later. One
change, announced once.

> **Hard precondition: crawl artifacts must become reclaimable before their bytes are counted.**
> No path frees them today. `DELETE /crawls/{id}` cancels and deletes no objects; job permanent
> delete (`routers/jobs.py:391`) and both admin delete paths (`admin.py:213`, `admin.py:336`)
> enumerate `job_runs.result_path` and never see `crawl_pages.result_path` — **so even deleting a
> user leaves that user's crawl artifacts in MinIO forever**, which is a retention problem
> independent of quota. Counting bytes against a hard, enforced wall with no user-side remedy is a
> support incident by design: the counter could only ever go up.

**7. Admission checks the declared ceiling; the meter charges what actually happened.** A crawl is
pre-checked at creation against `max_pages`, exactly as a batch is pre-checked against `len(urls)`;
pages are counted as they are created; the unused remainder of the ceiling is **not** charged
(crawls usually discover fewer pages than their ceiling). Checking the ceiling is the honest UX — a
crawl that dies at page 37 because quota ran out is worse than one that never starts. The same rule
covers a pipeline's declared Scrape-block count. A crawl rejected for quota must say so **at
creation**, naming the meter, the ceiling checked and the shortfall — the same legibility bar R3
already sets for a pipeline hitting the storage wall.

**8. Rollout — a pricing change to a shipped, live feature.**
- **Monthly runs and concurrency need no grandfathering.** Neither is a stored ledger; both recount
  live (`_count_monthly_runs`, `_count_concurrent_jobs`). The meters can only ever see crawls
  created *after* the change, so no user can wake up retroactively over-limit. There is nothing to
  forgive, and the "do nothing" answer here is also the correct one.
- **Storage is the only cumulative counter and the only real hazard. Do not backfill.** Accounting
  **starts at cutover; history is not reconciled** — pre-cutover crawl artifacts stay uncounted, and
  once the delete path exists, deleting one must **not** decrement (it was never incremented). The
  counter is already approximate — BUG-004's screenshots are uncounted, and a failed stat silently
  counts 0 (`minio_stat_failed`) — so no exactness is being surrendered that we currently claim.
- **Measure before enabling.** A read-only audit: for each user, what their last 90 days of crawls
  *would have* cost under this rule. That converts "could someone land over-limit" from speculation
  into a number, and it is one query.
- **If the audit finds anyone affected, raise that user's `user_quotas` row — do not move the
  global default.** Per-user limits already exist and are nullable-with-fallback, so a bump is one
  row and zero code. Raising `default_quota_monthly_runs` to absorb crawls would silently loosen the
  limit for every user who never crawls, which is a worse change than the one being made.
- **Accept and announce.** No feature flag, no dual-counting period, no grandfather list. That
  machinery exists for a large paying tenant base; here the user set is known and countable, and the
  audit says in advance exactly who is affected. If it finds nobody — the likely outcome, and the
  same "latent in prod" state BUG-005 found for batch — ship it plainly, with a changelog entry
  naming the date accounting starts.

**9. What this blocks.** The **decision** blocks ADR-009 §3 and §8 and is needed now, before the
view is written; retrofitting a lane into "the single definition of a run" afterwards is exactly the
audit-every-call-site failure the view exists to prevent. The **implementation** does not block the
migration and must not be sequenced behind it: it ships on today's code, in `phase4-backlog.md` §1,
**after P6/BUG-005** — which it should follow, because BUG-005 re-keys the v1 artifact path and
touches the same accounting surface. It is legitimately §1 work by the backlog's own selection rule:
`core/quota.py` and `routers/crawls.py` **survive** the migration, and only the storage-accounting
call site sits inside `coordinator/`, which does not. After the migration, `CrawlWorkflow` inherits
the rule unchanged.

**No new PRD.** This is a metering-policy answer to OQ-4 on an existing shipped feature, with no new
user-facing capability. It belongs in the document under review, not in a PRD-019 that would restate
OQ-4 under a new number — duplicated tracking docs have already caused drift twice on this project.

---

**Carry into ADR-009 (Architect — this PRD does not edit the ADR).**

**§3 — after the `storage_bytes_used` paragraph, which needs correcting.** The ADR currently says
`storage_bytes_used` "needs no change… it is already lane-agnostic. Only the two COUNT-based meters
had the defect." The *mechanism* is lane-agnostic; the **call sites are not** — there is exactly one
(`result_consumer.py:85`), gated on a `job_runs` column, so the crawl lane never increments it. All
three meters have the defect, in two different ways.

> **Which lanes the view contains: four, not two** — `job_runs` on the job path, `job_runs` on the
> batch path, **`crawl_pages`**, and `pipeline_runs`. Crawls consume no quota today
> (`routers/crawls.py` has no quota check and crawl work never creates a `JobRun`), which is the
> same omission this view exists to end, one lane earlier. **A lane that can start work and is
> absent from the view is a bug, not a discount** (PM, PRD-016 OQ-4, round 3).
>
> The view is **one row per countable unit**, and the two COUNT meters aggregate it differently:
> `monthly_runs` counts **rows** (a batch of N counts N; a crawl of N pages counts N);
> `concurrent_jobs` counts **distinct concurrency groups** among non-terminal rows, where the group
> is the **crawl** for crawl pages and the row itself on every other lane — so one crawl occupies
> one slot however many pages are in flight.

**§8 — metering.**

> **The unit is one fetch of one target URL that produces one stored result** — not one user action,
> and not one step. Job run = 1; batch of N = N (shipped); **crawl of N pages = N**; pipeline run =
> 1, because R1 fixes one run to one URL and the non-Scrape blocks fetch nothing. "Regardless of
> block count" is true *because* block count does not change how many pages were fetched.
> **Rider:** if layer A permits more than one Scrape block in a chain, that run fetches twice and
> costs 2 — PM preference is to count executed Scrape blocks rather than cap Scrape at one.
>
> **Admission checks the declared ceiling; the meter charges what actually happened.** A crawl is
> pre-checked against `max_pages` at creation, as a batch is against `len(urls)`; pages are counted
> as created and the unused remainder is not charged. Same rule for a pipeline's declared Scrape
> count.
>
> **`concurrent_jobs_limit`: a crawl is one unit, not N.** Cost and contention are different
> quantities and may disagree. Per-page concurrency would put every default crawl (`max_pages` 100)
> permanently over the default ceiling of 5, and satisfying it would need dispatch throttling inside
> the service §13 deletes. Crawl-lane contention is already bounded by the coordinator's dispatch
> batch size and the one-active-crawl-per-origin rule. Revisit when `CrawlWorkflow` makes throttling
> a config value rather than new code.
>
> **`storage_bytes_used`: every stored crawl page is charged.** "Only the final artifact is charged"
> needs no exception — a crawl has no intermediates; every page result is a deliverable.
> **Precondition:** crawl artifacts must first become reclaimable (no path frees them today, and the
> job/admin delete paths enumerate `job_runs.result_path` only — even user deletion orphans them).
> **Accounting starts at cutover; history is not reconciled** — pre-cutover artifacts stay uncounted
> and deleting one must not decrement.

---

**OQ-5 — Reusing existing workers.** The scoping doc recommends activities dispatching to the
existing workers over the current queue for the first phase, with a later move to workers as
native activity workers. Confirm this holds for R6's proving pipeline, and say what would
trigger the move.

**OQ-6 — Business logic that must be ported, not deleted.** Two pieces of hard-won behaviour
live inside plumbing the migration removes, and both were production incidents. They are
**requirements of the blocks that replace them**, not artifacts of the old transport:
- the LLM cold-start handling (warm-up probe + extended timeout) — an engine has no idea a
  scale-to-zero endpoint is cold and would simply retry a timing-out step, re-billing the
  user;
- the transient-vs-terminal classification for storage-write faults, in all three workers —
  this is R4's classification, already worked out and tested.

**OQ-7 — Run state in the SPA.** Poll a Postgres mirror vs stream engine events; can the
existing notify → WebSocket pattern be reused for per-block status?

**OQ-8 — Per-tenant isolation.** How are one user's runs isolated from another's in the
engine — namespace per user/tier, or user identity encoded in the run identifier?

**OQ-9 — Does the crawl coordinator migrate?** Out of scope here, but it is the obvious
second candidate and the answer shapes the block model if a Crawl block is ever wanted.

**OQ-10 — Conditional execution and change detection: which layer owns them?**
**⚙️ PARTIALLY RESOLVED (PM, review round 2): change detection is decided — both halves go to
Monitors (B). Conditional execution stays open and is the Architect's to sequence.** The original
framing is kept below because it is what the decision answers.

Neither is in this PRD, and — as the layers were originally scoped — neither is picked up by
what follows. Delivery (C) adds outbound blocks; Monitors (B) wraps a pipeline in a durable
loop. Both are
built *on* the pipeline model; neither extends what a pipeline can **express**. Two facts make
this a decision rather than an omission:

- `workflows-scoping.md` §4A lists **branch** in layer A's *own* block catalog, and §3 names
  "the user cannot … branch" as a symptom of the problem. This PRD deferred it; nothing picked
  it up.
- §4B's motivating example — *"watch this page every 6 hours; **if it changes**, tell me and
  wait for my approval"* — needs a diff signal **and** a conditional to be expressible at all.
  So Monitors appears to depend on capabilities deferred here, which would surface during B's
  PRD as unplanned layer-A work.

Sub-questions:
- **Change detection is two mechanisms, not one — where does each land?** Today's path has
  both, in different positions for different reasons, and they should be settled separately:
  - the **cost gate** (`result_consumer.py:376`) — an exact-byte hash compared against the
    previous run, evaluated *before* the LLM, whose entire purpose is to **skip downstream
    work**. This is conditional execution wearing a different hat, which is why it cannot be
    cleanly separated from the branching question above.
  - the **reporting diff** (`:460` text / `:506` JSON) — runs *after* the final step, sets
    `diff_detected`/`diff_summary`, and gives the webhook something to say. Purely
    descriptive; it skips nothing.

  Both need "the previous run of this same thing," which is a Monitor concept and argues for
  B. But the cost gate must be able to halt a chain mid-run, which is a layer-A capability.
  They are unlikely to land in the same layer.

  **✅ PM decision — both halves go to Monitors (B). Layer A gets no change-detection block.**

  The two halves land in the same layer after all, and the deciding argument is not effort — it
  is that **"the previous run of this same thing" is not definable in layer A.** A job is one
  URL, so its previous run is unambiguous. A *pipeline* takes **run inputs** (R1): one saved
  pipeline run against 50 URLs has 50 independent histories, and a gate in layer A would have to
  invent a per-input-tuple identity concept that layer A has no other reason to want, and that
  the Architect would have to guess at. Under a Monitor that ambiguity does not exist — the
  monitor instance *is* the identity, and its previous iteration is the obvious comparand. The
  same argument settles the reporting diff, which needs the identical comparand.

  Three consequences, all recorded elsewhere in this PRD so they are not lost:
  - **layer A's only obligation is to not foreclose it** — a block that halts a run early with a
    `completed` outcome must be expressible later without a breaking change (**OQ-1**);
  - **the cost delta is accepted, bounded, measured, and named** — one LLM call and at least one
    stored artifact per repeat run against unchanged content, waived under **OQ-4(b)**, recorded
    as a known exclusion under **R6**, and quantified in Success criteria;
  - **B does not ship without the gate.** It is a launch requirement of that PRD, not a backlog
    item — the layer that makes pipelines recur is the layer that makes the missing gate hurt.

  What this does **not** decide: whether the gate's comparand is an exact-byte hash (today's
  mechanism) or something a user can point at a field. That is B's PRD to write, and today's
  byte-equality-only behaviour is on record in the Problem section as a limitation, not a target.

- **Still open — does conditional execution need a follow-up layer-A PRD before B can be
  specced, or does B absorb it?** This is the half of OQ-10 that remains, and the change-detection
  decision above sharpens rather than removes it: B now owns a capability (**halt the run when
  nothing changed**) that consumes a layer-A primitive B cannot build for itself. Answering this
  is what makes OQ-1's forward-compatibility constraint concrete — it says what the block model
  must leave room for, **and by when**. The PM position is only that the *answer* must exist
  before B's PRD is written; which way it goes, and whether it arrives as a PRD-016 follow-up or
  inside B, is an Architect sequencing call.

**OQ-11 — Webhook delivery: a step, or a side effect?** Today delivery is **decoupled** — the
run is marked `completed`, a pending row is written, and `webhook_loop` retries it
independently with backoff for as long as it takes. An undelivered webhook never fails a job.
As a pipeline *block*, R4's terminal-failure rule says it does. PM position: **delivery is a
step the run depends on** — a user who puts a Webhook block in their chain expects it to have
happened, and layer C's saga rollback only makes sense if delivery failure is a run-level fact.
The mechanism is the open part, and the options are not equivalent:

- **(a) Fail the run; retries bounded by the block's time budget (R4).** Simplest and
  consistent with layer C — but it **loses a capability**: today a receiver that is down for
  two hours still eventually gets its delivery. A run-bounded retry gives up long before that.
- **(b) The block succeeds once the delivery is durably queued**, and the existing loop
  delivers it. Preserves today's behaviour exactly — but "the block succeeded" then means
  *queued*, not *delivered*, which is the kind of quiet lie R3's per-block status exists to
  eliminate.
- **(c) The block waits for real delivery on its own long horizon.** Durable timers make
  hours cheap, and this is precisely what the engine is good at. It keeps both the honesty of
  (a) and the reach of today's loop — but a run then stays open for hours, which collides with
  R3's **concurrent-runs-per-user ceiling**: a handful of dead receivers could consume a user's
  entire concurrency budget.

Whichever is chosen, the divergence from today's path must be recorded against R6, not
discovered during the comparison.

**One complication is now removed:** R2 caps a pipeline at **one** Webhook block, so option (c)'s
long-horizon wait can tie up at most one delivery per run. A user cannot build a chain of five
webhook blocks each waiting hours on a dead receiver. That does not settle OQ-11, but it bounds
the concurrency interaction flagged in R3.

---

## Related

- `docs/project/workflows-scoping.md` — feature scoping and engine comparison
- `docs/project/temporal-full-migration.md` — full change inventory and migration sequence
- `docs/project/phase4-backlog.md` §2 — engine decision, rollout, cutover gotchas; §3 — bugs
  the migration dissolves (**do not design fixes for these**)
- `docs/project/open-questions.md` **Q8** — the incident grounding the engine decision;
  **Q5** — the cold-start handling OQ-6 requires porting
- ADR-005 (crawl coordinator), ADR-006 (batch data model) — existing multi-step prior art
- **ADR-009** (to be written) — the engine decision and coexistence contract
