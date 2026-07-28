# PRD-016 — ScrapeFlow Workflows: User-defined Pipelines

**Priority:** P1 — the foundation layer of Phase 4
**Source:** `docs/project/workflows-scoping.md` §4A (feature), `phase4-backlog.md` §2 (engine decision)
**Status:** Ready for Architect
**Last updated:** 2026-07-28

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
- do anything conditional ("only call the LLM if the page actually changed").

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
- **A visual pipeline builder in the SPA.** Pipelines are API-first in this PRD; the SPA
  needs only to *list* pipelines and *show* run status. Drag-and-drop authoring is later.
- **Migrating existing jobs/batches/crawls onto pipelines.** Users are not asked to move,
  and nothing auto-converts. Retiring the old lane is a separate, later decision.
- **Branching and parallel fan-out.** Chains are linear in this PRD. (Conditional execution
  is the *first* thing to add after it ships — see Open Questions.)
- **User-authored code as a block.** Blocks come from a fixed catalog. Arbitrary user code is
  a sandboxing problem, not a pipeline problem.
- **Replacing the LLM key model.** Users still bring their own provider key.

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
- Full CRUD: create, list, fetch, update, delete. Deleting a pipeline must not destroy the
  history of runs already executed from it.
- A pipeline is **validated at save time**, not at run time. Saving an invalid pipeline
  fails with a message naming the offending block and reason. At minimum: unknown block
  type, invalid config for the type, a chain that does not start with a source block, and a
  block whose input cannot be produced by anything before it.
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
| **Validate** | Asserts the extracted data satisfies a user-supplied rule | Failing validation is a **terminal** run failure with the rule that failed |
| **Webhook** | Delivers the run result to a user URL | Reuses today's delivery + SSRF re-validation behaviour, including on every attempt |

Each block declares what it consumes and produces, so R1's save-time validation is possible.
Bot-wall detection stays the **Scrape** block's responsibility and remains a terminal failure
of that block — a wall must never flow downstream as if it were content.

### R3 — Execution and run visibility

- Runs are triggered **on demand** via the API in this PRD (scheduling is Monitors).
- A run has a terminal outcome — completed, failed, or cancelled — and **per-block** status
  and timing, so a user can see which step failed. This is the single biggest observability
  gain over today's opaque `job_runs.status`.
- Output is retrievable per run, and the final block's output is retrievable as *the* result.
- A run can be **cancelled in flight**. Cancellation must stop subsequent blocks; work
  already completed is not rolled back (rollback is the Delivery layer's saga).
- A run must survive an API or worker **restart or redeploy** mid-run and continue. This is
  the property today's path lacks and the whole engine choice exists to provide.
- Runs count against the user's existing quota and storage accounting. **How** a multi-step
  run is metered is an Architect question (see OQ-4), but it must not be free.

### R4 — Failure handling

- Each block distinguishes **transient** failures (retry automatically, with backoff) from
  **terminal** ones (stop the run, report which block and why).
- **Retry must live in exactly one visible layer.** This is a hard requirement, not a
  preference: the entire Q5/Q6/Q7 cluster was retries hidden in layers nobody was looking at
  (a provider SDK's `max_retries`, JetStream redelivery, a cold-booting endpoint), and the
  compounding failure billed users for it. A block's retry policy must be the only retry
  operating on that block, and it must be inspectable.
- An LLM block must **never** be retried in a way that bills the user twice for one logical
  step. If an attempt succeeded but a later step failed, the succeeded attempt is not redone.

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

---

## Success criteria

- [ ] A user can create, list, fetch, update, and delete a pipeline; another user's pipeline
      returns 404.
- [ ] Saving a pipeline with an unknown block type, invalid block config, a chain not
      starting with a source block, or an unsatisfiable input fails with a message naming the
      block and the reason.
- [ ] Per-user limits on blocks-per-pipeline and pipelines-per-user are enforced and
      operator-configurable.
- [ ] All five R2 block types execute, and each exposes the options listed for it.
- [ ] A `scrape → LLM → webhook` pipeline produces output equivalent to the same job on the
      existing path, on the same URL with the same schema **(R6 gate)**.
- [ ] A run reports per-block status and timing; a failed run names the failing block and the
      reason.
- [ ] A run interrupted by an API/worker restart mid-execution resumes and completes; it does
      not re-execute already-completed blocks.
- [ ] A transient block failure retries automatically; a terminal one stops the run.
- [ ] An LLM block is billed once per run per block, verified across an induced retry and an
      induced restart.
- [ ] A run can be cancelled mid-flight; no subsequent block executes after cancellation.
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
versioning. *(Carried from `workflows-scoping.md` §9.2.)*

**OQ-2 — Editing a pipeline with runs in flight.** R1 allows editing a saved pipeline while a
run from a previous version is still executing. Does an in-flight run pin its definition
version, or adopt the edit? This interacts directly with the engine's workflow-versioning
rules and is the most likely source of a subtle correctness bug in this PRD.

**OQ-3 — Enforcing "exactly one lane" (R5).** What structurally prevents the same unit of
work from running on both the existing path and a pipeline? Related and already on record:
moving a recurring job to an engine schedule requires disabling it on the old lane, or it
fires on both.

**OQ-4 — Metering a multi-step run.** Today one job run is the billable unit. Is a pipeline
run one unit, or is it per-block? Storage accounting also needs a rule for intermediate
artifacts: are per-block outputs retained (debuggable, costly) or only the final one (cheap,
opaque)? PM position: users must be able to see *why* a step failed, so at minimum failure
context is retained — the retention policy for successful intermediates is an Architect call.

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
