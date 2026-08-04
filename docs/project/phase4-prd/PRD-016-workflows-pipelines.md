# PRD-016 — ScrapeFlow Workflows: User-defined Pipelines

**Priority:** P1 — the foundation layer of Phase 4
**Source:** `docs/project/workflows-scoping.md` §4A (feature), `phase4-backlog.md` §2 (engine decision)
**Status:** Ready for Architect
**Last updated:** 2026-08-04

> **Scope note.** "ScrapeFlow Workflows" is one feature in three nested layers:
> **Pipelines (A) → Delivery sinks (C) → Monitors (B)**. This PRD covers **A only** —
> the layer the other two are built on. C and B get their own PRDs once A ships, so the
> Architect is not designing against a moving target.

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
  own PRD. This PRD's only outbound block is the **webhook** that already exists.
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
  Monitors appears to depend on it.)
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
| **Webhook** | Delivers the run result to a user URL | Reuses today's delivery + SSRF re-validation behaviour, including on every attempt. **Delivery is a step the run depends on, not a fire-and-forget side effect** — a terminal delivery failure fails the run. This diverges from today's path; mechanism and retry horizon are **OQ-11** |

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
- A run can be **cancelled in flight**. Cancellation stops *subsequent* blocks; work already
  completed is not rolled back (rollback is the Delivery layer's saga). **The block already
  executing runs to completion**, so a run always stops at a block boundary and "where did it
  stop" always has one clean answer. The sole exception is a block that is long-running, holds
  a scarce shared resource, **and** has no external side effect — such a block may be aborted
  mid-execution. Today that is Scrape alone:

  | Block | On cancel |
  |---|---|
  | **Scrape** | may be **aborted** mid-execution — longest block, holds a browser, aborting costs the user nothing |
  | **Clean**, **Validate** | complete — they finish faster than a cancellation can be delivered, so an abort path would be machinery with nothing to do |
  | **LLM extract** | completes — aborting does **not** reliably avoid the provider bill (tokens already generated are still charged), so the machinery buys a partial benefit at full cost |
  | **Webhook** | completes; **never** aborted — cutting a delivery mid-flight replaces a known outcome with an unknown one |

  Written as a rule rather than a list because Delivery sinks (layer C) are all side-effecting:
  the rule already tells that PRD's author they are never abortable.
- Because a cancelled run may still have billed the user for an in-flight LLM block, the API
  response and the run detail must **say so**. A Cancel button that silently keeps charging
  reads as a bug; it is only acceptable if the user was told.
- A run must survive an API or worker **restart or redeploy** mid-run and continue. This is
  the property today's path lacks and the whole engine choice exists to provide.
- Runs count against the user's existing quota and storage accounting. **How** a multi-step
  run is metered is an Architect question (see OQ-4), but it must not be free.
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
  and the MCP server keep working with no user-visible change.
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

**Not claimed: behavioural parity with today's job path**, which has three things this pipeline
does not:

- the **content-hash cost gate** (`result_consumer.py:376`), which skips the LLM entirely when
  a page is byte-identical to the previous run;
- the **reporting diff** (`:460` text / `:506` JSON), which populates
  `diff_detected`/`diff_summary` in the webhook payload;
- **webhook failure semantics.** Today `create_webhook_delivery` inserts a *pending* row and
  `webhook_loop` delivers it asynchronously with backoff — the run is already `completed` by
  then, so **an undelivered webhook never fails a job**. As a pipeline *block* it does (R2).
  Same recipe, different terminal outcome. That is **OQ-11**.

The first two are OQ-10. All three are stated here so a divergence found during the comparison
is read as a **known exclusion** — not as a failed gate, and not as licence to add a diff block
outside R2's catalog, which is the exact move this gate exists to prevent.

---

## Success criteria

- [ ] A user can create, list, fetch, update, and delete a pipeline; another user's pipeline
      returns 404.
- [ ] Saving a pipeline with an unknown block type, invalid block config, a chain not
      starting with a source block, or an unsatisfiable input fails with a message naming the
      block and the reason.
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
- [ ] The cost of an R6 pipeline run is measured against the equivalent job: run each twice
      against an **unchanged** page and count billable LLM calls. Today's content-hash gate
      makes the job's second run cost zero; a pipeline without that gate costs one every run.
      Any increase is **recorded before shipping**, not discovered on a user's invoice.
- [ ] The R6 gate pipeline consumes no more **quota** (`monthly_runs_limit`,
      `concurrent_jobs_limit`, `storage_bytes_used`) than the job it reproduces — same work must
      not cost more for having been expressed as a pipeline (OQ-4).
- [ ] A run reports per-block status and timing; a failed run names the failing block and the
      reason.
- [ ] A run interrupted by an API/worker restart mid-execution resumes and completes; it does
      not re-execute already-completed blocks.
- [ ] A transient block failure retries automatically; a terminal one stops the run. An
      **unrecognised** error is treated as terminal and does not retry.
- [ ] A run whose webhook receiver is unreachable produces the outcome OQ-11 settles on, and
      that outcome is recorded as a known divergence from the job path rather than found during
      the R6 comparison.
- [ ] An LLM block is billed once per run per block, verified across an induced retry and an
      induced restart.
- [ ] A run can be cancelled mid-flight; no subsequent block executes after cancellation. A
      cancellation arriving during a Scrape block aborts it; one arriving during any other
      block lets it complete, and the response states what was still charged.
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
calls — subject to one constraint that is not: **the R6 gate pipeline must not cost a user more
than the job it reproduces.**

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

**OQ-10 — Conditional execution and change detection: which layer owns them?** Neither is in
this PRD, and — as the layers are currently scoped — neither is picked up by what follows.
Delivery (C) adds outbound blocks; Monitors (B) wraps a pipeline in a durable loop. Both are
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
- **Does conditional execution need a follow-up layer-A PRD before B can be specced, or does
  B absorb it?** Answering this is what makes OQ-1's forward-compatibility constraint concrete
  — it says what the block model must leave room for, and by when.

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
