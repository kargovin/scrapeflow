# ScrapeFlow — Durable Workflows scoping

> **Consolidated Phase 4 view: [`phase4-backlog.md`](./phase4-backlog.md)** (item **WF**).

> Exploratory scoping for a new capability layer: user-defined, durable, multi-step
> **Workflows**, powered by a workflow-orchestration engine (Temporal or similar).
> This doc frames the feature, ranks the options, compares engines, and recommends a
> starting point. It does **not** commit to a build — it is meant to start the discussion
> with something concrete, and can later spawn a PRD + ADR.

> **⚠️ STATUS UPDATE — 2026-07-28. The exploratory framing below is settled; two parts of
> this doc are superseded, and one is load-bearing.**
>
> - **Superseded — §7's recommendation.** "Prototype Phase 0/1 on DBOS, treat Temporal as a
>   later step" is **not** what was decided. The engine is **Temporal**, chosen for portfolio
>   value + first-class Python *and* Go SDKs. The comparison table itself is still the honest
>   record of *why*, and feeds **ADR-009** — but do not act on the DBOS-first suggestion.
> - **Superseded — §1's "not a rip-out" non-goal.** True of the *feature*, no longer true of
>   the *phase*. Phase 4 **is** the full migration: `result_consumer` / `scheduler` /
>   `webhook_loop` / `advisory` / `coordinator` and NATS all retire at the end state. The
>   strangler-fig sequence in `temporal-full-migration.md` §9 is how, and coexistence is still
>   real at every intermediate step — but the end state is a replacement, not an addition.
> - **Still load-bearing — §4 and §6.** The three nested layers (Pipelines → Delivery →
>   Monitors) and the state-ownership split (engine owns execution state, thin Postgres mirror
>   backs the UI) are the shape **PRD-016** was written against.
> - **§10's next step is done:** PRD-016 exists at
>   `phase4-prd/PRD-016-workflows-pipelines.md`, scoped to layer **A** only. ADR-009 is next.

**Status:** Draft — for discussion (see status update above)
**Date:** 2026-07-14
**Author:** @karthik
**Source:** exploratory (came across Temporal in a job description)

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

**Non-goal (important):** this is **not** a rip-out or refactor of anything that exists. The
current NATS-based job path stays exactly as it is. Workflows are a **new layer that
coexists** with it. Nothing shipped and hardened gets re-platformed.

**What exists today, for reference.** A user submits **one URL + options** and we run a
single, hard-coded pipeline:

```
scrape  →  (optional) LLM extract  →  (optional) diff vs last run  →  (optional) webhook
```

The user cannot change the steps, add steps, branch, or send output anywhere except a
single webhook. Output only ever lands in two places: MinIO (internal storage) and one
webhook URL. There is no pause/human-in-the-loop step anywhere. And there is a live
half-built gap: a **schedule on a crawl is accepted and persisted** (`schemas/crawls.py`,
`models/crawl.py:32`, `routers/crawls.py:70`) but the scheduler only ever queries `Job`,
never `Crawl` (`api/app/core/scheduler.py:54`) — so **scheduled crawls silently never run.**

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
| BFS crawl frontier persisted in `crawl_queue`, dispatched by a poll loop | `coordinator/` (ADR-005) | Workflow state *is* the durable frontier |
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

Scheduled crawls (the §1 gap) fall out for free once **B** exists — scheduling long-lived
things is exactly what a monitor *is*.

### A — Pipelines (the framework)

> *"Scrape this product page → strip nav/ads → run the LLM to pull {price, title, rating} →
> check price is a valid number → then save to my Google Sheet **and** email me."*

Users wire **blocks** into a chain (scrape / clean / LLM / validate / branch / deliver),
instead of being stuck with our one hard-coded recipe. This is the foundation everything
else sits on: it turns ScrapeFlow from "runs one fixed recipe" into "a platform where users
build recipes."

- **Engine fit:** high — a pipeline is the textbook workflow; each block is a retryable
  activity; branching is an `if` in workflow code.
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

Ordered so each phase stands on the previous one, and so **nothing existing is removed**:

| Phase | Delivers | Notes |
|---|---|---|
| **0** | Stand up the engine *alongside* the current stack | New workflows run on the engine; the NATS job path is untouched. Infra + one "hello workflow" as a proving ground. |
| **1** | **Pipelines (A)** — block framework + core blocks (scrape, clean, LLM, validate) | Existing Go/Playwright/LLM workers become the *muscle* behind the scrape/LLM blocks — reused, not rewritten (see §6). |
| **2** | **Delivery (C)** — sink blocks (S3/DB/Sheet/email) + saga rollback | New block types on the Phase-1 framework; reuse `webhooks.py` + SSRF guard. |
| **3** | **Monitors (B)** — durable loop + human approval; scheduled crawls come free | Needs A to exist first. |

A sensible **MVP** is Phase 0 + a thin Phase 1: pipelines with 2–3 blocks (scrape → LLM →
webhook) reusing existing workers — i.e. reproduce *today's* pipeline as a *workflow*, proving
the model end-to-end before adding new blocks.

---

## 6. How it coexists with today's stack

The engine **orchestrates**; it does not scrape. Our existing thin workers keep doing the
actual work. Two integration options:

- **(a) Activities call existing workers over NATS** — a workflow activity dispatches the
  same fat NATS message we send today and awaits the result. **Workers are 100% unchanged.**
  Minimal risk; honors "don't rip anything out."
- **(b) Workers become engine activity workers directly** — cleaner long-term, but rewrites
  the worker entry points and their NATS consumers.

**Recommendation: (a) for Phase 1.** It lets us prove the workflow layer with zero worker
churn. Migrating to (b) — or migrating the crawl coordinator onto the engine — becomes an
optional later decision, not a prerequisite.

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

> **⚠️ The recommendation in the next two paragraphs is SUPERSEDED — see the status update at
> the top.** The engine decision is **Temporal**, made outright; the DBOS-first prototype path
> was not taken. The table above stands as the comparison that justifies the choice and is the
> raw material for **ADR-009**.

**Recommendation:** if the goals include a strong **learning/portfolio outcome** *and* we
accept the operational weight, **Temporal** — it's the name in the JD, has first-class Python
**and** Go SDKs (fits both our service languages), and its durable-timer + signal + saga
support is exactly what Monitors (B) needs. If we want the **lightest path to the same
guarantees on this homelab**, **DBOS** is the pragmatic pick: it's a library on the Postgres
we already run — near-zero new infra — and would still let us delete the Q8-class code.

A reasonable both-worlds plan: **prototype Phase 0/1 on DBOS** (cheap to stand up, proves the
model), and treat a **Temporal migration** as a deliberate later step once the workflow layer
earns its keep. Extending NATS+Postgres is explicitly **not** recommended — it means
re-solving retries/timers/idempotency by hand, which is what produced the Q8 incident.

---

## 8. Risks & costs

- **New stateful dependency + operational weight.** Temporal Server needs its own datastore
  and is non-trivial to run on a single-node k3s homelab. (DBOS sidesteps this by reusing our
  Postgres; Restate is a single binary.)
- **Determinism constraint / versioning.** Workflow code can't do I/O, call `datetime.now()`,
  or `random()` directly — those go in activities. Changing workflow code while runs are
  in-flight requires versioning discipline. Real learning curve, mostly one-time.
- **Re-platforming risk — mitigated by design.** Because this is a **greenfield layer** that
  leaves the existing NATS path intact, we are not re-opening the hard-won edge cases already
  absorbed by `result_consumer.py`. We add; we don't rewrite.
- **When NOT to bother:** if we only ever want today's fixed pipeline, an engine is overkill —
  the value appears specifically once we want user-defined steps, extra sinks, or long-lived
  human-in-the-loop monitors.

---

## 9. Open questions for a follow-on PRD / ADR

1. **Engine final pick** — Temporal (portfolio + power) vs DBOS (lightest on this homelab)?
   Possibly DBOS-now / Temporal-later.
2. **How are blocks defined and stored?** A fixed catalog of typed blocks vs a general DAG
   schema; JSON in Postgres vs a small DSL.
3. **Per-tenant isolation** — Temporal namespaces per user/tier, or one namespace keyed by
   `user_id` in workflow IDs?
4. **How does workflow state surface in the SPA?** Poll the Postgres mirror vs stream engine
   events; reuse the existing `pg_notify` → WebSocket pattern?
5. **Does the crawl coordinator eventually migrate onto the engine** (retiring `crawl_queue`
   + `reenqueue_stalled`), or stay as-is? ADR-005 calls it "the template for future multi-step
   coordination" — a natural second candidate, but not a Phase-1 dependency.
6. **Reuse-vs-rewrite of workers** — stay on integration option (a) indefinitely, or plan a
   move to (b)?

---

## 10. Recommendation & next step

Build **ScrapeFlow Workflows** as one feature in the four nested layers, starting with a
**Phase 0 + thin Phase 1 MVP**: stand up the engine alongside the current stack and reproduce
*today's* scrape→LLM→webhook pipeline as a *workflow*, reusing the existing workers via
integration option (a). That proves the model end-to-end with near-zero risk to what's
shipped, and unlocks Delivery (C) and Monitors (B) as additive layers.

If this direction is approved, the house-style next artifacts are:

1. ✅ **Done — [PRD-016](./phase4-prd/PRD-016-workflows-pipelines.md)** (2026-07-28), scoped to
   layer **A (Pipelines)** only so C and B don't move the target under the Architect.
2. ⏭ **Next — ADR-009**, recording the **engine decision** and the v1/v2 coexistence contract.
   It also answers PRD-016's nine open questions.

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
