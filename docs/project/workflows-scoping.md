# ScrapeFlow — Durable Workflows scoping

> **Consolidated Phase 4 view: [`phase4-backlog.md`](./phase4-backlog.md)** (item **WF**).

> The **origin artifact** for Phase 4. It framed the durable-workflows capability, ranked the
> options, compared the engines, and recommended a starting point. Everything it opened has since
> been decided: **[PRD-016](./phase4-prd/PRD-016-workflows-pipelines.md)** is the product spec for
> layer A, and **[ADR-009](../adr/ADR-009-workflow-engine-temporal.md)** (Accepted 2026-09-08) is
> the engine decision and the v1/v2 coexistence contract.

> **What this doc is still for, now that those exist.** Three things, and nothing else:
>
> - **§2 and §3** — the plain-English account of what a durable engine is, and the row-by-row
>   inventory of where ScrapeFlow already hand-rolls it. Neither has an owner elsewhere.
> - **§4's four nested layers** and **§6's state-ownership split** — still load-bearing. PRD-016
>   was written against this shape.
> - **§7's comparison table** — the honest record of *why* Temporal, and ADR-009 §1's raw material.
>
> For anything else — scope, sequencing, what gets deleted — **PRD-016 and ADR-009 are
> authoritative, and where they disagree with this doc, they win.**

> **Status:** redrawn **2026-09-08** against the Accepted ADR — pending owner review. The six 🔴
> markers this file carried are gone because the passages under them were rewritten, not because
> the findings were waived. **What the redraw changed, so a note written before it can be checked:**
> §1's "not a rip-out" non-goal is **reversed** (Phase 4 *is* the migration); §4A's block catalog
> no longer lists `branch`; §5's roadmap gains the **conditional-execution step between C and B**
> and drops "nothing existing is removed"; §6 now recommends **option (b)**, with option (a)
> recorded as rejected *and blocked*; §7's DBOS-first prototype path is recorded as the road not
> taken; §8's "we add; we don't rewrite" is withdrawn; §9's six open questions are **all answered**
> and now point at their answers; §10 is restated as what was actually built.

**Date:** 2026-07-14 · redrawn 2026-09-08
**Author:** @karthik
**Source:** exploratory (came across Temporal in a job description)
**Superseded by, for anything it decides:** [PRD-016](./phase4-prd/PRD-016-workflows-pipelines.md),
[ADR-009](../adr/ADR-009-workflow-engine-temporal.md)

---

## 1. Why this doc

We came across **Temporal** — a durable workflow-orchestration engine — and noticed it
solves the exact class of problem we hand-rolled across ScrapeFlow's workers, but with
built-in safety guarantees. This doc scopes a *new capability* we could build on that kind
of engine.

The instinct holds up. ScrapeFlow's orchestration is entirely hand-rolled: polling loops +
Postgres state + NATS JetStream, with idempotency guards, backoff schedules, and crash
recovery written by hand in every component. That approach has already **cost a production
incident**: `open-questions.md` **Q8** documents an overloaded `job_runs.status` state
machine in `api/app/core/result_consumer.py` that closed a feedback loop and burned ~200
LLM API calls in ~5 minutes before the worker was stopped. Q8's own analysis notes that the
clean fix "aligns with how Temporal/Airflow/Prefect model task-level vs workflow-level
state." That is the strongest possible internal signal that a durable-execution engine would
naturally *own* logic we are currently maintaining by hand.

**⚠️ The non-goal this doc opened with was reversed, and it is the single biggest change since.**
It originally read: *"this is **not** a rip-out or refactor of anything that exists… Workflows are
a new layer that coexists with it. Nothing shipped and hardened gets re-platformed."* That is still
true of **the feature** — layer A adds a lane rather than splitting one — but it is **no longer
true of the phase**. **Phase 4 *is* the full migration**: `result_consumer.py`, `scheduler.py`,
`webhook_loop.py`, `advisory.py` and the `coordinator/` service all retire, and NATS is removed at
the end state. Coexistence is real at every intermediate step and the strangler-fig sequence
(`temporal-full-migration.md` §9) is how — but the end state is a **replacement, not an
addition**.

**What exists today, for reference.** A user submits **one URL + options** and we run a
single, hard-coded pipeline:

```
scrape  →  (optional) LLM extract  →  (optional) diff vs last run  →  (optional) webhook
```

The user cannot change the steps, add steps, branch, or send output anywhere except a
single webhook. Output only ever lands in two places: MinIO (internal storage) and one
webhook URL. There is no pause/human-in-the-loop step anywhere. And there is a live
half-built gap: a **schedule on a crawl is accepted and persisted** (`schemas/crawls.py`,
`models/crawl.py`, `routers/crawls.py` all carry `schedule_cron`) but the scheduler only ever
selects `Job`, never `Crawl` (`api/app/core/scheduler.py`) — so **scheduled crawls silently never
run.** *(Line numbers dropped in the 2026-09-08 redraw: the crawls-router pointer had already
drifted by five lines, and this file is not where they are maintained.)*

---

## 2. What a durable workflow engine is (plain-English)

Stripped of the marketing (it has **nothing intrinsically to do with AI** — that's recent
paint), the idea is simple:

- You write **workflow** code that looks like an ordinary sequential function — "do this,
  then that, then wait a day, then do the other thing."
- Each side-effecting step (an HTTP call, a scrape, a DB write) is an **activity**.
- The engine guarantees the workflow **runs to completion exactly as written**, surviving
  process crashes, redeploys, and machine death — *without you writing any of the retry,
  state-persistence, or resume plumbing.*

The mechanism is **event-sourced replay**: every step's outcome is appended to a durable
history. If a worker dies mid-workflow, another worker replays the history to rebuild
in-memory state up to the last completed step, then continues. Your code reads as
straight-line; underneath it's a resumable state machine.

What you get for free — and what we currently hand-roll:

- **Automatic retry with backoff** on any activity.
- **Exactly-once effects** even under redelivery/crash (no more idempotency guards on every
  branch).
- **Durable timers** — `sleep(6 hours)` or `sleep(30 days)` that survives restarts.
- **Wait-for-signal** — pause and durably wait for an external event (e.g. a human clicking
  "approve"), for minutes or weeks.
- **Fan-out / fan-in** — run N children in parallel, wait for all.

---

## 3. Where ScrapeFlow already hand-rolls this

Every row below is code we wrote and maintain, that an engine provides as a primitive:

| Hand-rolled today | File | Engine equivalent |
|---|---|---|
| `reenqueue_stalled` — reset stuck items after a timeout on startup | `coordinator/coordinator/dispatcher.py:27` | Activity heartbeat + start-to-close timeout |
| `_recover_stale_pending` — re-publish runs stuck in `pending` | `api/app/core/scheduler.py:131` | Retry policy on the dispatch activity |
| Webhook backoff loop `BACKOFF_SECONDS=[0,30,300,1800,7200]` | `api/app/core/webhook_loop.py:31` | Activity retry policy (exponential backoff) |
| MaxDeliver advisory → mark run failed when NATS exhausts retries | `api/app/core/advisory.py` | Dead-letter / retry-exhausted handling |
| The `scrape → LLM → diff → webhook` state machine, disambiguated by `(worker_status, source, current_status)` | `api/app/core/result_consumer.py` | A linear workflow function — the branching collapses to top-to-bottom code |
| BFS crawl frontier persisted in `crawl_queue`, dispatched by a poll loop | `coordinator/` (ADR-005) | A workflow drives the frontier — but ⚠️ **it stays in Postgres**, reached through activities. ADR-009 §13b measured it: ≈5 history events per page at our 10,000-page ceiling, and ≈800 KB of visited set against a 2 MiB payload limit. The engine replaces the **poll loop**, not the table |
| Idempotency guards (`if run.status in ("completed","failed"): return`) on every handler branch | `result_consumer.py` | Exactly-once activity results — guards disappear |

**The anchor is Q8.** The overloaded status machine didn't just look fragile — it *fired* in
production. That is the empirical case that this category of code is worth handing to an
engine rather than adding one more guard.

---

## 4. Proposed feature: ScrapeFlow Workflows

We want four capabilities. They are **not competing alternatives** — they nest into a single
feature with a natural build order. Each layer is built on the one below it:

```
   MONITORS (B)  — run a pipeline every N hours, forever, pause for human approval
      │  = a PIPELINE wrapped in a durable loop + durable sleep + human-wait signals
      ▼
   PIPELINE (A)  — user-defined steps: scrape → clean → LLM → validate → deliver
      │  the "deliver" step is just one block type…
      ▼
   DELIVERY (C)  — send a result to S3 + DB + Sheet + email; roll back if one fails (saga)
```

Scheduled crawls (the §1 gap) are absorbed by **B** — scheduling long-lived things is exactly what
a monitor *is*. ⚠️ *"For free"* was the original wording and is too strong: a crawl migrates as its
own `CrawlWorkflow`, not as a block, and ADR-009 §13a found it is a **rewrite** rather than a port,
since only the dispatch half of `coordinator/` has ever executed. B closes the scheduling gap; it
does not make the crawl work free.

**Where conditional execution sits.** It is a **layer-A extension**, not a fifth nesting layer —
which is why it does not appear in the diagram above but does have a phase of its own in §5. It is
built after A ships and before B, because B's cost gate consumes its primitive.

### A — Pipelines (the framework)

> *"Scrape this product page → strip nav/ads → run the LLM to pull {price, title, rating} →
> check price is a valid number → then save to my Google Sheet **and** email me."*

Users wire **blocks** into a chain (scrape / clean / LLM / validate / deliver), instead of being
stuck with our one hard-coded recipe. This is the foundation everything else sits on: it turns
ScrapeFlow from "runs one fixed recipe" into "a platform where users build recipes."

⚠️ **`branch` was in this catalog and is not in layer A.** Conditional execution gets its own
follow-up layer-A PRD (ADR-009 §14), built after A ships and before B; PRD-016 lists branching as
an explicit non-goal. The reason is not that it is hard — it is an `if` in workflow code — but that
*what a condition is allowed to say* is constrained by replay determinism, and that question wants
its own spec.

- **Engine fit:** high — a pipeline is the textbook workflow; each block is a retryable
  activity.
- **User value:** high — the single biggest product leap.
- **Effort:** **L** (defines the whole framework + block model + a builder UI later).

### C — Delivery sinks (a rich block type)

> *"When this finishes, put the file in my S3 bucket, append a row to BigQuery, and email me
> a summary. If BigQuery fails, don't leave the S3 file orphaned."*

Today output goes only to MinIO + one webhook (verified: no S3/SES/SMTP/BigQuery/GCS code
exists anywhere). This layer adds outbound **sink blocks** and the **saga** pattern: if one
delivery fails partway, cleanly undo the ones that already succeeded.

- **Engine fit:** high — compensation/rollback (saga) is a first-class engine pattern.
- **User value:** medium-high — makes results *actionable*, not just retrievable.
- **Effort:** **M** — new block types on the Phase-1 framework; reuse existing webhook +
  SSRF machinery.

### B — Monitors (long-lived watches)

> *"Watch this competitor's pricing page every 6 hours. If it changes, tell me and **wait for
> me to approve** before doing anything else. Keep going until I stop it."*

A monitor is **one long-lived workflow** that wraps a pipeline in a durable loop. It
showcases the two things an engine makes trivial and we currently *cannot do at all*:
reliable long sleeps (survives redeploys) and **pausing to wait for a human** (there is no
pause/approve step anywhere in ScrapeFlow today).

- **Engine fit:** highest — durable timers + signals + `continue-as-new` (infinite loop
  without unbounded history) are the engine's marquee features.
- **User value:** high — "watch this for me" is a core scraping use case.
- **Effort:** **M** (given A exists) — mostly the loop + signal + approval UI.

### Gap — scheduled crawls (absorbed by B)

`crawls.schedule_cron` is already accepted and stored but never dispatched. Rather than
bolt crawl support onto the hand-rolled scheduler, a scheduled crawl becomes a monitor whose
pipeline is "crawl the site" — the gap closes as a special case of B.

---

## 5. Phased roadmap

Ordered so each layer stands on the previous one. ⚠️ **Two things changed here since the first
draft:** a **conditional-execution step** was inserted between Delivery and Monitors, and the
framing *"nothing existing is removed"* is withdrawn — see §1.

| Phase | Delivers | Notes |
|---|---|---|
| **0** | Stand up the engine *alongside* the current stack | Infra + one "hello workflow" as a proving ground. ⚠️ Not the true start — ADR-009 §16e makes the pre-migration queue (**P6 → P8 → P7 + BUG-007**) the entry condition, and the worker port lands here too, not later. |
| **1** | **Pipelines (A)** — block framework + core blocks (scrape, clean, LLM, validate) | The existing Go/Playwright/LLM workers become the *muscle* behind the scrape/LLM blocks — their **domain logic** is reused; their **transport** is rewritten in this same increment (see §6). |
| **2** | **Delivery (C)** — sink blocks (S3/DB/Sheet/email) + saga rollback | New block types on the Phase-1 framework; reuse `webhooks.py` + SSRF guard. Unblocked once A ships, and may proceed in parallel with Phase 3. |
| **3** | **Conditional execution** — branching inside a pipeline | Its own layer-A PRD (ADR-009 §14), still unnumbered. **Forced into this slot:** Monitors cannot ship without the change-detection cost gate, and the gate consumes this step's primitive. |
| **4** | **Monitors (B)** — durable loop + human approval; scheduled crawls come free | Needs A, and needs Phase 3's primitive. |

A sensible **MVP** is Phase 0 + a thin Phase 1: pipelines with 2–3 blocks (scrape → LLM → webhook)
— i.e. reproduce *today's* pipeline as a *workflow*, proving the model end-to-end before adding new
blocks. That is now formalised as PRD-016's **R6 acceptance gate**, judged on structure and
mechanics rather than byte-equality.

---

## 6. How it coexists with today's stack

The engine **orchestrates**; it does not scrape. Our existing workers keep doing the actual work.
Two integration options were posed:

- **(a) Activities call existing workers over NATS** — a workflow activity dispatches the
  same fat NATS message we send today and awaits the result. Workers 100% unchanged.
- **(b) Workers become engine activity workers directly** — their entry points and NATS
  consumers are rewritten; the scraping code is not.

**Decision: (b), in the first increment** (ADR-009 §9, 2026-08-23). Option (a) is **rejected, and
was found blocked.** Both halves matter:

- **Rejected**, because its premises failed. Only ~10–22% of each worker file touches NATS, and
  none of the expensive parts do — bot-wall detection, the Patchright stealth setup, the
  formatters, robots handling, the MinIO clients are plain functions that a different caller
  invokes unchanged. Roughly half of what (a) preserves is *compensation for NATS* (`ack_wait`, the
  in-progress heartbeat, `max_deliver` caps, the nak ladder) and is deleted rather than ported.
- **Blocked**, because the bridge needs a result path it cannot have. `SCRAPEFLOW` is
  `--retention work`, and a work-queue stream refuses a second consumer whose filter overlaps an
  existing one — `api-result-consumer` already claims `scrapeflow.jobs.result` in full. This is
  not theoretical: the crawl coordinator attempts exactly that addition, and **that consumer has
  never existed on the stream** (BUG-008). The pod reports itself healthy with half of it dead.

*Record of what this doc originally recommended, and why it is worth keeping:* **"(a) for Phase 1 —
prove the workflow layer with zero worker churn, treat (b) as an optional later decision."* The
reasoning was sound in form — don't confound a new model with a simultaneous rewrite — and it
failed on facts that were not known when it was written. It is kept because the *shape* of that
argument recurs at every cutover, and because BUG-008 was found by testing it.

**State ownership:** the engine owns *execution* state (history, timers, retries). We keep a
**thin Postgres mirror** (`workflows` = user's saved definitions; `workflow_runs` = one row
per execution, status + pointers) purely so the API/SPA can list and query workflows without
going through the engine's API on every request. This mirrors how `job_runs` backs the jobs
UI today. A workflow-builder UI in the SPA is future work, out of scope here.

---

## 7. Engine comparison (compare & recommend)

"Temporal or something similar" — the honest field, weighed against our reality (self-hosted
k3s homelab, Python-heavy services + one Go worker, single-operator maintenance):

| Engine | Model | Self-host weight on k3s | Language fit | Durable timers / signals / saga | Op cost | Learning curve | Portfolio signal |
|---|---|---|---|---|---|---|---|
| **Temporal** | Workflows + activities, event-sourced replay | **Heavy** — server + its own Postgres/Cassandra + UI | Python & Go SDKs (both first-class) | ✅ all three, best-in-class | High | Medium (determinism rules, versioning) | **Highest** — the name in the JD; industry standard |
| **Restate** | Durable functions / handlers | **Light** — single binary, embedded log | Python & Go SDKs | ✅ all three | Low-med | Low-med | Rising; strong |
| **DBOS** | Durable execution as a **library** on your existing Postgres | **Lightest** — no new server; uses our Postgres | Python (strong); TS | ✅ timers/steps; saga via code | **Lowest** | Low | Growing; "clever" signal |
| **Prefect** | Data/task orchestration (flows/tasks) | Medium — server + DB | Python-first (no Go) | Partial — retries/scheduling strong; signals/long-human-waits weaker | Medium | Low | Data-eng flavored, less "durable-exec" |
| **Windmill** | Scripts/flows platform + UI | Medium | Python/TS/Go scripts | Partial — great built-in UI/flows; less a pure durable-exec model | Medium | Low | Product-y, less systems signal |
| **Extend NATS+Postgres** (status quo) | Keep hand-rolling | **Zero new infra** | N/A | We'd rebuild each by hand (the very thing that caused Q8) | Zero infra / **high code** | N/A (we know it) | Low — "reinvented a workflow engine" |

**Decision: Temporal, outright** — taken before ADR-009 was written, and recorded in its §1. The
table above is the comparison that justifies it and the reason this section survives the redraw.

The case: first-class Python **and** Go SDKs, fitting both our service languages where every
lighter option is Python-only or Python-first; durable timers + signals + saga, which is precisely
what Monitors (B) needs and where Prefect and Windmill are only partial; and the strongest
portfolio signal, which is an explicit goal of this project. The accepted cost is operational
weight — it is the heaviest dependency this homelab runs, and ADR-009 §2 sizes it (**+1.5–2 CPU**,
landing near 50% CPU requests, with **limit** overcommit rather than requests as the real
constraint).

*The road not taken, kept because the reasoning still holds on its own terms:* the original
recommendation was **prototype Phase 0/1 on DBOS** — a library on the Postgres we already run, so
near-zero new infra — and treat a Temporal migration as a deliberate later step once the workflow
layer earned its keep. It was not taken because it optimises for the cost this project deliberately
accepts and pays a real price against the goal it does not: a second migration, and the Go worker
left out. **If operational weight ever becomes the binding constraint, this is the shape of the
fallback**, and the table above is where to restart the argument.

Extending NATS+Postgres remains explicitly **not** recommended — it means re-solving
retries/timers/idempotency by hand, which is what produced the Q8 incident.

---

## 8. Risks & costs

- **New stateful dependency + operational weight.** Temporal Server needs its own datastore
  and is non-trivial to run on a single-node k3s homelab. (DBOS sidesteps this by reusing our
  Postgres; Restate is a single binary.)
- **Determinism constraint / versioning.** Workflow code can't do I/O, call `datetime.now()`,
  or `random()` directly — those go in activities. Changing workflow code while runs are
  in-flight requires versioning discipline. Real learning curve, mostly one-time.
- **⚠️ Re-platforming risk — the mitigation this doc claimed no longer applies.** It read: *"a
  greenfield layer that leaves the existing NATS path intact… we add; we don't rewrite."*
  **Withdrawn.** Phase 4 is the full migration, so the hard-won edge cases inside
  `result_consumer.py` and the workers *are* re-opened — deliberately, and ADR-009 §10's
  do-not-delete list is the instrument: the LLM cold-start handling, the transient/terminal
  classifier, bot-wall detection, the SSRF check, the heartbeat obligation and the webhook wire
  contract are business logic that must be **ported into the activities**, not deleted with the
  plumbing that houses them. What replaces the mitigation is sequencing: strangler-fig, one flow
  at a time, with a drain gate at each cutover.
- **The first increment has no fallback.** Every other lane can be routed back to v1 if it
  misbehaves. Pipelines have no v1 implementation, so the rollback is switching the feature off —
  which is exactly the lane the acceptance gate runs on.
- **When NOT to bother:** if we only ever want today's fixed pipeline, an engine is overkill —
  the value appears specifically once we want user-defined steps, extra sinks, or long-lived
  human-in-the-loop monitors.

---

## 9. Open questions for a follow-on PRD / ADR — all six answered

This section did its job: every question it raised is now decided. Kept as the record of what the
follow-on artifacts were commissioned to answer, with each answer's home.

| # | The question | The answer | Where |
|---|---|---|---|
| 1 | **Engine final pick** — Temporal vs DBOS, possibly DBOS-now/Temporal-later | **Temporal, outright.** No prototype detour | ADR-009 §1 · §7 above |
| 2 | **How are blocks defined and stored?** Typed catalog vs general DAG; JSON vs a DSL | **Fixed typed catalog, JSON in Postgres, explicit named wiring.** Layer A is a single chain in *data flow as well as execution order*; per-type config-schema versioning is the residual DSL cost | ADR-009 §4 |
| 3 | **Per-tenant isolation** — namespaces per user/tier, or `user_id` in workflow IDs? | **Neither. One namespace, and the API's ownership check is the *only* boundary.** Identity in the workflow ID was proposed, then withdrawn — Temporal never parses the ID, so the property was never structural | ADR-009 §12 |
| 4 | **How does workflow state surface in the SPA?** Poll the mirror vs stream engine events | **Mirror activity + `pg_notify`**, preserving today's contract. Pipelines get their **own JSON channel** — `job_status` is a positional three-field string that silently drops a fourth | ADR-009 §11 |
| 5 | **Does the crawl coordinator migrate, retiring `crawl_queue` + `reenqueue_stalled`?** | **It migrates, last — but as a rewrite, and `crawl_queue` does *not* retire.** The question embedded an assumption that turned out false twice over: the frontier stays in Postgres (the history budget is ≈5 events per page at our 10,000-page ceiling), and most of `coordinator/` has never executed, so there is nothing to port | ADR-009 §13 |
| 6 | **Reuse-vs-rewrite of workers** — stay on (a), or move to (b)? | **(b), in the first increment.** See §6 | ADR-009 §9 |

⚠️ **Question 5 is the one worth re-reading.** It was framed as a scheduling question — *migrate
now or later* — and the answer changed what "migrate" means. ADR-005 called the coordinator "the
template for future multi-step coordination"; it is in fact the only lane with **no working
reference implementation to migrate from**.

---

## 10. Recommendation & next step

**Approved, and built out as follows.** ScrapeFlow Workflows is one feature in the nested layers
of §4, starting with a Phase 0 + thin Phase 1 MVP: stand up the engine alongside the current stack
and reproduce *today's* scrape→LLM→webhook pipeline as a workflow, which is now PRD-016's **R6
acceptance gate**. That unlocks Delivery (C) and Monitors (B) as additive layers.

⚠️ **One clause of the original recommendation did not survive:** *"reusing the existing workers via
integration option (a)… with near-zero risk to what's shipped."* The workers are ported to Temporal
activity workers in the **first** increment (§6), and the risk profile is not near-zero but
*differently shaped* — the pipeline lane adds a lane rather than splitting one, so double-execution
risk is ~none, and in exchange it is the one lane with no v1 fallback.

The house-style next artifacts, both now done:

1. ✅ **Done — [PRD-016](./phase4-prd/PRD-016-workflows-pipelines.md)** (2026-07-28), scoped to
   layer **A (Pipelines)** only so C and B don't move the target under the Architect.
2. ✅ **Done — [ADR-009](../adr/ADR-009-workflow-engine-temporal.md)** (drafted 2026-08-04,
   reviewed §1–§17 to 2026-09-05, **Accepted 2026-09-08**), recording the **engine decision** and the
   v1/v2 coexistence contract. It answers all **11** of PRD-016's open questions (nine when this
   line was written; PM review added two). §1 of it is where this doc's §7 comparison table ends
   up.

---

## Related

- `docs/project/temporal-full-migration.md` — **engine decided (Temporal).** Full change
  inventory for going *all-in*: what gets deleted/transformed/kept/added, the target topology,
  and the strangler-fig migration sequence.
- `docs/project/open-questions.md` **Q8** — the state-machine incident this is grounded in.
- `docs/adr/ADR-005-site-crawl-bfs-coordinator.md` — the hand-rolled multi-step precedent.
- `docs/adr/ADR-006-batch-scraping-data-model.md` — existing fan-out/fan-in prior art.
- `api/app/core/result_consumer.py`, `api/app/core/scheduler.py`,
  `api/app/core/webhook_loop.py`, `coordinator/coordinator/dispatcher.py` — the code a
  workflow engine would subsume.
