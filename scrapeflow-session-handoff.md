# ScrapeFlow Session Handoff

You are a **coding mentor** helping the user build ScrapeFlow, a self-hosted multi-tenant web scraping platform (Apify clone). Read @CLAUDE.md for the full architecture.

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
| ADRs (ADR-001 through ADR-008) | `docs/adr/` |
| Operational reference (commands, devops, usages) | `docs/project/` |
| Multi-persona process starter prompts | `docs/process/` |
| Phase 3 engineering spec (historical) | `docs/archive/phase3/phase3-engineering-spec.md` |
| Phase 3 ordered backlog (historical) | `docs/archive/phase3/PHASE3_BACKLOG.md` |
| Phase 3 deferred items → Phase 4 candidates | `docs/archive/phase3/PHASE3_DEFERRED.md` |
| Progress tracker (historical) | `docs/archive/PROGRESS.md` |
| Phase 2 spec (historical) | `docs/archive/phase2/phase2-engineering-spec-v3.md` |
| Phase 2 production readiness review | `docs/archive/phase2/production-review.md` |
| Phase 3 production readiness review | `docs/archive/phase3/production-review.md` |
| Idempotency audit (NATS redelivery) | `docs/archive/phase3/idempotency-checks.md` — 7 findings; all fixed |
| Service failure & recovery audit | `docs/archive/phase3/service-failure-recovery.md` — all findings fixed |

---

## Commands

**Tests** (must run inside Docker — `uv` manages the venv inside the container):
```bash
# from ./docker
docker compose exec api uv run pytest tests/ -v
docker compose exec api uv run pytest tests/test_jobs.py -v
```

**MCP server tests** (built and run as a standalone Docker image — not in docker-compose):
```bash
# from repo root
docker build -t scrapeflow-mcp mcp/
docker run --rm -e SCRAPEFLOW_API_KEY=test-key scrapeflow-mcp python -m pytest tests/ -v
```

**Migrations** (Alembic auto-run is enabled in `main.py` — runs on API startup):
```bash
# from ./docker
docker compose exec api uv run alembic upgrade head
docker compose exec api uv run alembic current
docker compose exec api uv run alembic revision --autogenerate -m "migration_3_N_description"
```

---

## Current state

- ## 📝 START HERE (2026-08-23) — **ADR-009 review is UNDERWAY. Next section: §10 (the do-not-delete list).**

  ### §9 (worker integration) — reviewed 2026-08-23, **DECISION REVERSED: option (b) first**

  ✅ **Owner's call: the three workers gain native Temporal activity entry points in the *first*
  increment. The NATS bridge (option (a)) is rejected.** v1 is untouched — jobs, batches and crawls
  keep running on NATS until their flow migrates. What changes is that the pipeline lane never
  acquires a bridge at all.

  The draft's argument was: keep the workers untouched, so if the acceptance test (rebuild
  `scrape → LLM → webhook` as a pipeline) fails, it is unambiguously the pipeline model's fault and
  not a worker rewrite's. Sound in form. **It failed on four premises.**

  1. **"Rewriting three workers" overstated the change by an order of magnitude.** Measured: only
     **~10–22% of two files per worker** is queue plumbing. Everything expensive touches NATS
     **not at all** — `blocking.py` (313 lines of bot-wall detection), the Patchright/headed-Chrome
     stealth setup, `formatter.go`, `robots.*`, `llm.py`, the MinIO clients. And the Go worker's
     `processJob(ctx, job, fetcher) (string, error)` (`worker.go:377`) **already has the exact
     shape a Temporal activity wants** — `func(ctx, T) (R, error)` — so the adapter is a few lines.
  2. **~Half of what option (a) "preserved" is compensation for NATS.** `ack_wait=120`, the 30s
     `in_progress()` heartbeat, `max_deliver`, the nak backoff ladder — all exist *because*
     JetStream redelivers unacked messages (the Q6 incident). Under Temporal they are **deleted,
     not ported**; activity heartbeat + start-to-close timeout take that role. The genuine
     carry-forwards (§10's cold-start probe, transient/terminal classifier, bot-wall detection)
     **port identically under either option** — so (a) bought nothing there and kept must-port code
     alive in two places.
  3. **🔴 The bridge is BLOCKED, and there is a dead service in production proving it.** The
     `SCRAPEFLOW` stream is `--retention work` (**verified against the live stream, not the
     manifest**), and a work-queue stream **refuses a second consumer whose filter overlaps an
     existing one** — `api-result-consumer` already claims `scrapeflow.jobs.result` in full, which
     is exactly what a result-awaiting activity would need. The crawl coordinator attempts that
     addition at `result_handler.py:203`, and **`coordinator-result-consumer` has never existed on
     the stream**. It is silent by construction: `result_handler_subscribed` appears **zero** times
     in the coordinator's retained log while the sibling `dispatch_loop_started` logs normally,
     because `main.py:82` awaits both tasks with `asyncio.gather(..., return_exceptions=True)`,
     which captures the exception and never re-raises. **The pod reports healthy with half of
     itself dead.** Nothing has surfaced it because **no crawl has ever run in production** —
     `crawls` and `crawl_pages` are both empty. Same shape as BUG-005. ⚠️ **Not yet filed as a
     bug — it should be.**
     The remaining option-(a) answer (poll Postgres for the row `result_consumer.py` writes) puts
     **the Q8 component on the critical path of every pipeline run** — the component this whole ADR
     exists to delete. And today a v2 result would be **destroyed, not merely ignored**:
     `_handle_result` resolves the run via `db.get(JobRun, run_id)` (`result_consumer.py:608`), a
     pipeline-run id matches nothing, and the handler **acks** — which on a work-queue stream
     deletes the message. (§7 predicted this as "neither FK set"; the real failure is a step
     earlier and final.)
  4. **"Workers unchanged" and "NATS must not retry" cannot both hold.** The draft's own safety
     requirement was that NATS must not retry workflow-originated work, or Temporal's `RetryPolicy`
     and JetStream redelivery stack — R4 violated by the migration itself. But that retry lives
     **inside the workers** (`worker.go:316`/`:339`/`:360`, `llm-worker/worker/worker.py:128`) and
     in **per-consumer** `max_deliver`. Neither is switchable per message without a flag the worker
     reads or a v2-only subject and consumer — **both worker changes.** Option (a)'s defining
     property does not survive its own requirement.

  **The honest comparison** was never *unchanged workers* vs *rewritten workers*. It was **a
  brand-new bridge + unchanged workers** vs **a thin permanent adapter + unchanged worker logic** —
  where the bridge is the *larger* body of novel code, sits in exactly the area that produced
  Q5/Q6/Q7/Q8 (queue semantics, retry, result correlation), and **is deleted at the step that was
  always going to follow.**

  **The gate is preserved by a better mechanism — and this is a requirement, not a nicety:**
  **before R6, run the Scrape activity standalone against a URL and diff its output against a v1
  job run of the same URL.** That separates "the adapter is wrong" from "the model is wrong"
  *before* the model is under test. Option (a) has no equivalent — it has no simpler mode to run in.

  **Costs accepted:**
  - **Each worker runs twice during coexistence** — one deployment bound to NATS (v1), one to a
    Temporal task queue (v2). **Two deployments of one image with a mode flag, never one process
    serving both**: retiring v1 becomes *deleting a deployment* rather than unpicking a conditional.
  - **Three integrations rather than one bridge** (dependency + connection config + entry point +
    manifest, per worker). Two are the same language and SDK, so realistically two distinct pieces
    of work and one repeat. (a)'s "one place" advantage was illusory anyway — per premise 4, retry
    neutralisation reaches into all three workers regardless.
  - ⚠️ **The Playwright container contract must be preserved exactly** — Xvfb first, then
    `exec python` as pid 1, **never `xvfb-run` as pid 1**, which stays alive after the worker dies
    so k8s never restarts a dead container (ADR-008). **The riskiest part of the port**, and it is a
    container concern rather than a Temporal one.
  - **Ordering:** nothing starts until the Temporal server is deployed (separate Postgres +
    `temporal-sql-tool`, §2). Equally true of option (a), so it does not separate them.

  **Sequence: Go http-worker → LLM worker → Playwright worker.** The Go worker first because it is
  simplest, best-tested, and its `NATS_MAX_DELIVER=3` already caps retry, so the v1/v2 comparison is
  clean. LLM second because it carries §10's cold-start and classifier logic. Playwright last
  because of the container risk. NATS and all four API loops untouched throughout.

  **Knock-ons applied:** §16's sequence moves the worker port **from third to first** (there is no
  bridge to carry pipelines in the meantime, so the activity workers *are* the first increment's
  executors); §7's result-path carry-forward is **moot** (v2 publishes no NATS results); §2's "not
  in the first increment" is reversed; and the residual retry hazard is now only that §10's ported
  classifier must be expressed as **`RetryPolicy` non-retryable error types**, not as its own loop
  inside the activity.

  **Carried into the next session:**
  1. ✅ **Filed as BUG-008**, with the owner's disposition: **not fixed on the NATS path** — it is
     in `phase4-backlog.md` §3 (dissolved by Temporal). The defect *is* the NATS integration, and
     the component that carries it is deleted by the migration. ⚠️ **The deferral has a condition:**
     crawls migrate **last**, so this stays broken for the whole migration — acceptable only while
     usage is zero (it is: `crawls` and `crawl_pages` are empty). If crawls are ever offered to
     users before that step, reject `POST /crawls` rather than repair the consumer.
  2. **§10 is next, and it is now first-increment work** rather than later — the workers port
     first, so the do-not-delete list ports with them.
  3. **The §8 blocker still stands:** is the `latest/` copy chargeable? Storage metering is not
     implementable until that is ruled on, and BUG-007 cannot be fixed without it.

  ### Session close (2026-08-23)

  **Docs-only session — no code changed.** Three commits on `develop`:

  | commit | what |
  |---|---|
  | `2849da3` | §8 metering reviewed — storage rule reversed; BUG-007 filed |
  | `72432b2` | §9 reviewed — **reversed to option (b)**; workers port to Temporal first |
  | `9f37992` | BUG-008 filed with a will-not-fix disposition; backlog §3 updated |

  `develop` is **8 ahead of `origin/develop`** and `main` is behind it — **nothing pushed, nothing
  fast-forwarded.** That is deliberate, not an oversight; push when you want the docs live.

  **Two decisions this session, both reversals of drafted text, both owner calls:**

  1. **Storage is charged for what is stored** (§8) — bytes on disk, every object a run holds, on
     every lane. Replaced "only the final artifact is charged" and withdrew §5's clause that the
     result and the charged artifact are one object.
  2. **Workers port to Temporal first; the NATS bridge is rejected** (§9) — the draft's
     "keep workers untouched" argument failed on four premises, most decisively that the bridge's
     result path is **impossible** on a work-queue stream, with BUG-008 as live proof.

  **What is genuinely blocking, in order:**

  1. ⚠️ **Is the `latest/` copy chargeable?** Every worker dual-writes `latest/` + `history/` while
     the meter counts one copy, so MinIO holds 2× what is counted. **§8's storage rule is not
     implementable until this is ruled on, and BUG-007 cannot be fixed without it.**
     Recommendation on record: charge one copy. v2 already drops `latest/`.
  2. **`temporal-full-migration.md` now contradicts the ADR.** Its step 3 is the worker port
     (now step 1), its diagram at line 314 assumes the bridge, and the retry discussion at 339–351
     describes a hazard that mostly no longer exists. Needs redrawing, not just renumbering.
  3. **§8d's two unowned items** — which component performs v2 storage accounting, and what happens
     when a pipeline hits the storage wall at its last block after the user's LLM key has already
     been billed.

  **Next section is §10 (the do-not-delete list), and its priority went up.** Under the old plan
  the workers ported third, so §10 was later work. Now they port **first**, so the do-not-delete
  list ports with them in the first increment: `ensure_ready()` + the 180s timeout, the
  transient/terminal classifier on all three workers, bot-wall detection, and SSRF re-validation.
  §9 added one constraint §10 must now satisfy: **the ported classifier becomes `RetryPolicy`
  non-retryable error types, not its own retry loop inside the activity** — otherwise R4's
  "retry in exactly one visible layer" is violated one level down from where it was fixed.

  ### Superseded: START HERE (2026-08-17) — §8 review

  **§8 reviewed 2026-08-17.** (§4–§7 were reviewed 2026-08-10; their notes are below and still
  accurate except where §8 supersedes them.) The ADR's Review log remains authoritative.

  ### §8 (metering) — reviewed 2026-08-17, **storage rule REVERSED**; §5, §3 and §15 amended

  Four owner calls, and the first one changes a rule §5 had already settled.

  - **🔴 Storage is charged for what is stored — bytes on disk, not "the result".** Every object a
    run still holds is charged, on every lane, for as long as it is stored. **Withdraws §5's
    clause that the result and the charged artifact are the same object**; §5 keeps *the result*
    (R3 display, permanence, never collected), §8 owns *what is charged*. Simpler: no notion of
    finality, so fan-out, effect blocks and shared objects stop being special cases. Turns
    intermediate-output GC into a **user-visible refund** rather than housekeeping.
  - **🔴 The parity argument in both §5 and §8 was factually backwards.** Both said the R6
    pipeline would charge zero while "the job it reproduces charges the LLM output". **The job
    path charges the *scraped page*.** Traced: scrape completes → adds HTML, stamps
    `storage_accounted_at` (`result_consumer.py:411`); LLM completes → tries to add JSON, **skipped
    by the stamp** (`:485`→`:81`); `result_path` repointed at the JSON (`:500`); the LLM worker
    wrote to a **new** key so the HTML is still there (`llm-worker/worker/storage.py:23`).
  - **🔴 Two unfiled defects fall out, neither touched by the migration** (so backlog §3 does not
    cover them — these are pre-migration fixes on live code, and they need filing in
    `open-bugs.md`):
    1. **The counter is permanently inflated by every deleted LLM job.** Hard delete enumerates
       `JobRun.result_path` (`routers/jobs.py:391`, `admin.py:336`), stats the **JSON** and
       decrements by that, while what was added was the **HTML**. Clamps at zero, never balances —
       delete every job you own and usage still reads non-zero.
    2. **The scraped page is never deleted.** Nothing enumerates it; it outlives the run, the job
       and the user. Same shape as BUG-004's orphaned screenshots.
    **Root cause named:** `_try_increment_storage`'s idempotency stamp is keyed on the **run** when
    it should be keyed on the **stored object** — a NATS redelivery and a genuinely second artifact
    are indistinguishable to it. The mechanism isn't wrong, its granularity is. Same on the batch
    path (`:237` scrape, `:274` LLM).
  - **⚠️ Open, and it blocks implementing the storage rule: the `latest/` + `history/` dual write.**
    Every worker writes each result twice (ADR-002 §8) while `result_size` reports one copy, so
    MinIO holds **2× what the meter counts** on every v1 lane. "Charge what is stored" read
    literally charges both. **Recommendation on record, not yet an owner call: charge one copy** —
    `latest/` is a convenience alias, not a second artifact. v2 already drops `latest/`, so this is
    v1-only with a known end state.
  - **✅ One submission = one concurrency slot, every lane.** Cost and contention therefore
    disagree **in general**, not only for crawls — the old text called crawls "the one place" they
    disagree, but **batch already disagreed in the opposite direction, by accident**:
    `batch.py:46-47` admits a batch by checking the ceiling **once**, then inserts N rows, so 100
    URLs are admitted as 1 and meter as 100, locking a user out of a 5-slot pool with one call.
    Live behaviour change on the batch path; it is a loosening, so it cannot break a caller.
  - **✅ A pipeline run parked on a durable timer does not hold its slot; v1 lanes unchanged.**
    §3 defined active as *not yet finished*, §15 as *actively executing a block* — **contradictory,
    and §15's ≈2.6 h webhook horizon only survives under the second.** Resolved per-lane: v1 keeps
    `quota.py:59`'s `pending`/`running`/`processing` (R5 forbids narrowing a live limit), pipelines
    exclude timer-parked runs. Cost: the counting view needs a lane-aware predicate. Accepted.
  - **✅ The multi-Scrape rider is CLOSED, and its premise was wrong.** The rider rested on *"R1
    fixes one run to one URL"* — R1 does **not**: the URL is an *optional* run input, so nothing in
    R1 stops two Scrape blocks with URLs typed into their configs. The conclusion still holds, for
    a **structural** reason: §4 validates a single chain **in data flow** and **Scrape consumes
    nothing**, so a Scrape block has no valid position except first and a second is unsatisfiable
    at Save. **The reopening trigger is multiple roots, not fan-out** — the rider named the wrong
    one, and this makes fan-out cheaper to ship than the deferral table claimed.
    **🔴 Because the guarantee is emergent from two rules stated pages apart, §8 now asserts it
    directly: a layer-A pipeline has exactly one starting block, and it is a Scrape block.** A
    validator written as *"each block consumes the previous one, unless it declares no input"*
    satisfies §4 as written and admits a second Scrape — silently under-charging.

  Three gaps recorded rather than closed:

  - **The unit is "one fetch attempted"**, not "…that produces one stored result". The meter counts
    rows created at **dispatch** (`JobRun` at submission, `CrawlPage` at `dispatcher.py:103`) and
    never waits for an outcome — **a failed scrape already costs a monthly run today**. Also
    stated, because it is the next question the definition invites: **a retry is not a new unit.**
  - **`crawl_pages` has no accounted-at marker** (and no size column), and nothing specifies one on
    `pipeline_runs`. The PM's "counting starts at cutover, no backfill, don't decrement for
    pre-cutover artifacts" needs a per-object record of whether it was counted — `job_runs` has
    one, two of four lanes don't. Each lane needs its marker **in the same change that starts
    counting it**.
  - **Per-run GC is safe only while no object is shared *between* runs.** v1 already shares them —
    on a content-hash match `result_consumer.py:385` points the new run at the **previous run's**
    object. Pipelines are safe only because change-detection was deferred to Monitors; when
    Monitors ship, per-run collection breaks exactly as per-block collection breaks now, and the
    rule becomes *collect when no run references it*.

  **§8d names two things no section owns:** which component performs v2 accounting (§3 says it
  "must be named, not inherited"; §8 pointed back at §3 — on v1 it is `result_consumer.py`, which
  the migration deletes), and **what happens when a pipeline hits the storage wall**. The second is
  a real product question: the wall is hit at the **last** block, *after* the user's own LLM key
  has been billed, and admission-time checking cannot substitute because output size isn't knowable
  in advance. Fail the run, allow the overage, or refuse to start new runs while over.

  ### Session close (2026-08-17)

  **Docs-only session — no code changed.** Four files on `develop`: `docs/adr/ADR-009-…`
  (+412/−82), `docs/project/open-bugs.md` (**BUG-007** filed), `scrapeflow-session-handoff.md`,
  `CLAUDE.md`. All ADR internal anchors re-verified after the §8 retitle. Older handoff entries
  about the multi-Scrape rider were marked **superseded** rather than rewritten.

  **What carries into the next session, in priority order:**

  1. **⚠️ One open decision blocks implementing §8's storage rule: is the `latest/` copy
     chargeable?** The dual write (ADR-002 §8) means MinIO holds 2× what the meter counts on every
     v1 lane, so "charge for what is stored" is undefined until this is ruled on. Recommendation on
     record: **charge one copy**. It is in the ADR's deferred table marked as blocking, and
     **BUG-007 cannot be fixed without it** — fixing the bug means deciding what one result's
     storage *is*.
  2. **Next section is §9 (worker reuse).** It opens carrying a finding from §7: under option (a),
     v2 results land on `scrapeflow.jobs.result` where `result_consumer` is subscribed with
     **neither FK set** — BUG-005's shape one lane later. §9 as drafted warns only about stacked
     *retry*, not about this.
  3. **BUG-007 should be sequenced with P7 (crawl quota), not fixed separately.** Same accounting
     surface, same family of mistake, and P7 already sits after P6/BUG-005.
  4. **§8d's two unowned items** — which component does v2 accounting, and what happens at the
     storage wall — need homes. The first belongs to §9's activity inventory; the second is a
     product question for the layer-A implementation PRD.

  ### §7 (one lane) — reviewed 2026-08-10, **under-covered its hardest case**; gained a 4th mechanism

  - **🔴 Mechanism 1 (disjoint identity) covers pipelines only — and stops applying exactly where
    the problem starts.** A **migrated job keeps its `job_runs` row *by requirement***: §3 makes
    that table a read-model mirror of Temporal state rather than replacing it, because R5 forbids
    user-visible change and the job API, SPA, admin views and quota view all read it. So from
    migration **step 2** the same row is visible to both lanes by design. §16 says this from the
    sequence side (*"a real problem only at migration step 2"*) — §7 didn't say it from the
    mechanism side, so three mechanisms read as covering a case only one of them touches.
  - **🔴 The gap is not theoretical — `_recover_stale_pending` walks into it.**
    `core/scheduler.py:131` selects **every** `job_runs` row with `status='pending'` older than
    `stale_pending_threshold_minutes` (**default 10**) and re-publishes it to
    `scrapeflow.jobs.run.http`/`.playwright`. **No lane filter; it cannot know a workflow owns the
    row.** So a v2-migrated run whose workflow hasn't started (worker pod down, task-queue backlog,
    Temporal unreachable) gets dispatched to a v1 worker ten minutes later — then the workflow
    worker returns and scrapes the same URL again. **Mechanism 2 never intervenes: v1 started no
    workflow, it published a message.** It fires *precisely when v2 looks stalled*, and it is
    **silent**.
    ⚠️ **Documentation trap:** backlog §3 lists this loop under "dissolved by Temporal — do NOT
    fix." True of the **end state**, false of the **transition** — it is live for the whole
    coexistence period, which is when the risk exists.
  - **✅ Owner's call — mechanism 4: a lane marker on `job_runs`**, written in the **same
    transaction as the insert** (a later write leaves a window where a v2 row looks like a v1 row),
    with every v1 dispatching query filtering on it. **Step-2 work, not day-one** — §16's routing
    rule keeps jobs on v1 until their flow is migrated, so layer A ships without it. Recorded in §7
    because that is what someone will read *at* step 2.
    Supporting evidence: **`advisory.py:28` is already safe, but by accident** — it matches on
    `nats_stream_seq`, `NULL` for anything v1 never dispatched. A lane marker in disguise, on a
    column §3 drops when v1 retires.
  - **🔴 Mechanism 2 over-claimed.** *"Temporal refuses a second execution with a workflow ID
    already running"* is correct (default **Conflict Policy = `Fail`**). *"Double-start becomes
    impossible at the engine"* is not: the default **`WorkflowIdReusePolicy` is `ALLOW_DUPLICATE`**,
    which permits a fresh execution once the prior one **closed**. That is "never two at once", not
    R5's "once, ever". **Pin `REJECT_DUPLICATE`** on `job-run-{run_id}` and `pipeline-run-{id}`; a
    run identifier is single-use, so it costs nothing. Also named: **`TERMINATE_IF_RUNNING` would
    silently destroy the guarantee** by converting a refused double-start into a kill-and-restart.
  - **🟠 The `--retention work` reassurance covered the half that was never dangerous.** Acked
    messages are deleted — fine. The risk is the **unacked** message, in flight or unprocessed at
    cutover, still deliverable to a v1 worker. The check already exists as §16's **deletion gate**
    (zero unprocessed / zero outstanding acks via `nats consumer info --json`) — it is a **cutover
    gate too**, and §7 now points at it.
  - **🟠 Rollback ordering stated.** §16 claims every step is reversible, so the reverse move needs
    the same discipline as the forward one: **pause the Temporal Schedule → confirm no v2 execution
    in flight → only then set `schedule_status` back to `active`.** Un-pausing v1 first re-arms
    both lanes exactly as creating the Schedule too early does.
  - **🟡 Carried to §9's review:** mechanism 1 is true of **rows, not messages**. Under option (a) a
    v2 activity dispatches into NATS, and the result returns on `scrapeflow.jobs.result` where
    `result_consumer` is subscribed — its routing (`result_consumer.py:616`) branches on
    `run.job_id` / `run.batch_item_id`, and a pipeline-originated result has **neither**. BUG-005's
    shape, one lane later. §9 warns about stacked *retry*; it needs one about the *result* path.

  ### §6 (in-flight edits / pinning) — reviewed 2026-08-10, decision upheld, **its reason replaced**

  - **🔴 The central technical argument was wrong, and wrong in a way that invited the opposite
    conclusion.** §6 claimed replay "reconstructs in-memory state by re-reading history against the
    current definition," making a mid-run edit a determinism violation. **Verified against
    Temporal's docs: replay re-executes the workflow *code* against recorded event history, and the
    workflow's input arguments are part of that history.** So a definition passed in as an argument
    is pinned *automatically* — replay never reads the current row. The described failure requires
    a workflow body that loads the definition from Postgres, which §6's own determinism rule (three
    paragraphs later) already forbids. The danger was that anyone who knows Temporal could
    correctly reply *"we pass it as an argument, so replay is safe, therefore we can adopt edits"*
    — against a decision whose real basis the rebuttal doesn't touch. **The real reason is
    semantic:** a run that already executed the old shape cannot coherently continue into a new one
    (it ran a Clean block that v2 deleted). True of any engine, or none.
  - **The mechanism is now stated: the definition travels as a workflow input argument.** That's
    what makes pinning free rather than enforced, and it's consistent with §5's settled rule
    (*content is never a payload; arguments may be*) — a definition is bounded by R1's
    max-blocks-per-pipeline, so it's small against the 2 MiB limit. The standing prohibition is
    sharper than "don't adopt edits": **the workflow body must never load the definition itself.**
  - **"A run records the version it pinned" named no column.** Same shape as §4's `skipped`
    problem. Now **`pipeline_runs.pipeline_version_id`**.
  - **✅ Owner's call: deleting a pipeline with a run in flight lets the run finish.** It already
    pinned its version, that version is retained, and the run is its own record. Cancelling is R3's
    separate explicit operation — deleting a definition is not a back-door cancel. Same reasoning
    as the Q4 split on jobs.
  - **✅ Owner's call (option C): the run *history* holds the name, not the pipeline.** Delete a
    pipeline with **no runs** → deleted outright, name free immediately. Delete one **with runs** →
    soft-deleted, holds its name, reuse returns **409**. Principle: the reason to hold a name is
    that history refers to it; no history, nothing to hold. Close to the `api_keys` precedent
    without over-applying it (a revoked key holds its name for a reason that always applies — the
    key string may be logged elsewhere). Freeing the name on every delete would make run history
    ambiguous by construction: two "Price watch" pipelines, different URLs and schemas, no way to
    tell which produced a given run.
  - **🟡 Worker-code versioning promoted from a passing mention to an explicit deferral row.**
    §6 correctly separates *user definition* versioning (solved by pinning) from *our workflow
    code* versioning (not solved by it) — the recipe card vs the cook — but named "patched APIs /
    Worker Versioning" without choosing. Checked: **Worker Versioning is GA and Temporal's stated
    default**; patching is the older approach leaving branches to clean up; the pre-2025
    *experimental* variant is already withdrawn from the server. So it's a two-way choice, not
    three. **Note the symmetry worth keeping:** pinned Worker Deployment Versions run an execution
    entirely on the version it started on — the same answer as §6's, applied to the interpreter.
    Needs **server-side enablement** on our self-hosted deployment, so it's an infra task.
  - **Two cross-references added.** §10's do-not-delete list (LLM `ensure_ready()`, the
    transient/terminal classifier) must land in **activities**, and §6's determinism rule is
    exactly what forbids them in a workflow body — the two sections are read together by whoever
    does the port and neither pointed at the other. And §4's config-schema obligation exists
    *because* §6 pins.

  ### §5 (references, not payloads) — reviewed 2026-08-10, decision upheld and strengthened

  - **✅ The payload numbers are now measured, not hedged.** The section said "low single-digit MB
    — confirm against Temporal's docs." Confirmed: **256 KiB warn / 2 MiB error** per payload;
    history 10 MiB warn / **50 MiB error**, 10,240 / 51,200 events. Against BUG-003's measured
    291 KiB–4.1 MiB that means the **largest real page is over twice the hard limit** and the
    **smallest is already past the warn line**. This is not a big-page edge case — the whole
    measured range is over a threshold, which makes §5 considerably more load-bearing than it read.
  - **⚠️ Named a trap the section didn't have: we self-host, so these limits ARE configurable**
    (unlike Cloud). Raising `blobSizeLimitError` is the obvious-looking fix and it undoes **§5 and
    §2c together** — content into history, history retained 30 days. §2c's "suspect §5 first"
    warning is reachable by config change, not just by code.
  - **🔴 "One immutable object per block per run" was false of two of the five block types.**
    Validate asserts on its input; Webhook delivers. Neither produces content. §4's strict-path
    data flow (block *n* consumes *n−1*) turned that from pedantry into correctness: every
    non-terminal block must emit something consumable. **✅ Owner's call: the catalog splits into
    content-producing (Scrape, Clean, LLM) and effect (Validate, Webhook) blocks; effect blocks
    pass their input reference through unchanged and write nothing.** Consequence to carry: two
    blocks can name one object, so **GC is per run, never per block**.
  - **🔴 R6's own acceptance gate had no definable result.** R6's pipeline is
    `scrape → LLM → webhook` — it **ends in an effect block**. Under R3 ("the final block's output
    is *the* result") plus §8's freshly pinned "final = last block in execution order", that
    pipeline returns nothing and charges **zero** storage, while the job it reproduces charges the
    LLM output. Metering break in the *opposite* direction from the one the PM guarded against, and
    structurally identical to §3's invisible-lane bug. **✅ Resolved: the result — and the charged
    artifact — is the last *content-producing* block's output.** Worth noting this was invisible
    until yesterday's §8 pin made "final" explicit.
  - **🟠 The GC window was listed as a free operator dial; it isn't.** R3 promises "which step
    failed *and what it returned*" — GC deletes exactly that second half for successful
    intermediates. **✅ Owner's call: it's a product-visible retention promise.** Three rules now
    stated: the run's **result is never collected** (it's charged storage the user owns), collection
    is **per run**, and a collected output must render **as collected** — never a 404, an error or
    an empty result. Plus the decoupling nobody had written down: **per-block status/timing live in
    `pipeline_run_blocks` in the app DB**, so "which step failed" outlives both the 30-day namespace
    retention and this window. Only *content* has a retention window.
  - **Two smaller ones:** deterministic keys are now stated as an **idempotency guarantee** (a
    retried activity re-uploads to the same key — the direct contrast with BUG-005's NULL-derived
    colliding key), and the **absence of a tenant segment** is tied back to §12's single boundary:
    the reference in history is a bare path, so whatever resolves it into bytes must ownership-check
    the run first.

  ### §4 (block model) — reviewed 2026-08-10, five corrections, two owner calls
  The section's two core choices (fixed typed catalog; explicit named wiring) survived. What
  changed was whether the section as written actually delivered them.

  - **🔴 §4 rejected its own motivating example.** It justified explicit wiring with *"run two
    extractions on one fetched page"* — Scrape feeding **two** LLM blocks — and then stated a
    validator rule that "rejects anything that is not a single chain," which rejects exactly that.
    Root cause: **"linear" was doing duty for two different properties.** *Execution order* linear
    (one block at a time, no parallelism) is PRD-016's non-goal; *data flow* linear (block *n*
    consumes *n−1*) is a stronger claim nobody had made deliberately.
    **✅ Owner's call: linear in both senses for layer A. Data-flow fan-out is wanted but deferred
    post-Phase 4** — added to the ADR's "Deliberately not decided here" table. Consequence recorded
    honestly: **explicit wiring now buys no capability that implicit wiring couldn't**, and is kept
    purely for forward-compatibility plus the identifiers OQ-2/R3 need anyway. And **layer A does
    not fix the PRD Problem section's "two extractions on one page"** — logged in the ADR as a known
    exclusion; **PRD-016 should absorb it** next PM pass (not done — Architect doesn't edit the PM's
    doc).
  - **🔴 "Input" meant two incompatible things in one sentence.** A block reading an earlier
    block's output is a data-flow edge carrying a **MinIO reference** (§5); a run input is a
    **scalar bound into a config field**. §5 says inputs are *always* references — and Scrape's URL
    is a string, so §4 and §5 contradicted each other. **Resolved: run inputs are config bindings,
    not graph edges; Scrape consumes nothing and is a source block.**
    **✅ Owner's call: narrow binding** — each block type declares which config fields are bindable;
    today exactly one, Scrape's `url`. Wide binding (any field) would dissolve R1's whole promise:
    an LLM schema supplied at run time can't be checked at Save, so the user finds out it's
    malformed *after* paying for a render. Widening later is additive; narrowing breaks saved
    pipelines.
  - **Block IDs: "immutable once assigned" was the wrong property.** It's satisfied by an edit that
    regenerates every ID — nothing changed, they're all just new — which breaks both things it was
    supposed to buy. Corrected to **stable across versions**, stated as a rule about the *edit
    operation*: an update carries IDs through; an omitted ID is a deletion.
  - **`skipped` was asserted about a column no section defined.** §3 introduces
    `pipeline_run_blocks` without columns; §4 said "the column admits it." Now named:
    **`pipeline_run_blocks.status` ∈ `pending`/`running`/`completed`/`failed`/`skipped`** — else a
    `CHECK` written from states-in-use produces the exact migration+backfill the rule exists to
    prevent.
  - **Config-schema versioning is the DSL's grammar cost arriving by the back door.** §4 rejects a
    DSL partly to avoid "versioning of the grammar," but a typed catalog still needs a schema per
    block type and a story for changing it. Now stated, with the obligation it creates: **§6 pins
    definitions, so the validator must stay able to validate historical config shapes** — a new
    required field needs a default for already-saved definitions.

  **Knock-ons applied:** **§5** gained a note that run-input scalars are the exception to
  "inputs are references" (*content is never a payload; arguments may be*), to be carried into §5's
  own review. **§8** now pins **"final artifact" = last block in execution order**, not "an output
  nothing consumes" — the two coincide only while data flow is a path, and diverge the moment
  fan-out ships.

  **§8's multi-Scrape rider is still open** and unaffected: one Scrape feeding two LLMs still
  fetches once, so "one run = one unit" survives. Two *Scrape* blocks remains the open question.
  → **CLOSED 2026-08-17 in the §8 review** (see START HERE): two Scrape blocks are unsatisfiable at
  Save, because §4 validates a single chain in **data flow** and Scrape consumes nothing. The
  reopening trigger is **multiple roots, not fan-out**.

  ### Session close (2026-08-10)

  **Docs-only session — no code changed.** Three files, committed on `develop`: `CLAUDE.md`,
  `scrapeflow-session-handoff.md`, `docs/adr/ADR-009-…`. **`develop` and `main` remain level for
  code at `b110591`**; docs commits sit on top of `develop`.

  **Four sections reviewed (§4, §5, §6, §7).** The ADR is **still Draft** — sections being settled
  individually does not make it Accepted, and nothing may be implemented against it yet.

  **What carries into the next session, in priority order:**

  1. **Next section is §8 (metering)** — it does *not* open clean. It has been amended twice as a
     knock-on already (from §4: "final" pinned to execution order; from §5: re-pinned to the last
     *content-producing* block, plus the three GC rules), and it still carries the **open
     multi-Scrape rider** from the PM. Three things to check before reading it as drafted.
     → **DONE 2026-08-17.** All three resolved, and the storage rule was reversed outright — the
     "final artifact" pinning both earlier knock-ons installed is **withdrawn**. See START HERE.
  2. **§9 opens carrying a finding from §7** — under option (a), v2 results land on
     `scrapeflow.jobs.result` where `result_consumer` is subscribed with **neither FK set**
     (BUG-005's shape, one lane later). §9 currently warns only about stacked *retry*.
  3. **§5's own review closed, but §5 carries a note into §4's territory** — already applied; no
     action.
  4. **Two things owed to other documents, not done** (deliberately — the Architect does not edit
     the PM's doc): **PRD-016 should absorb §4's known exclusion** ("two extractions on one fetched
     page" is *not* fixed by layer A), and **backlog §3 should be corrected** — it files
     `_recover_stale_pending` as "dissolved by Temporal — do NOT fix", which is true of the end
     state and false of the transition, where §7's mechanism 4 now depends on it.

  **Two implementation obligations recorded this session that are easy to lose**, both of them
  step-2 migration work rather than layer-A work: the **lane marker on `job_runs`** (§7 mechanism
  4) and **`WorkflowIdReusePolicy.REJECT_DUPLICATE`** on both workflow-ID formats (§7 mechanism 2).
  Neither is needed to ship pipelines; both are needed before a job runs on Temporal.

  <details><summary>Prior START HERE (2026-08-08) — §2/§3 reviewed, §12 reversed</summary>

  ## 📝 (2026-08-08) — **ADR-009 review UNDERWAY. Next section: §4 (block model).**

  The ADR is being reviewed **section by section**, not read straight through, and decisions are
  landing **in the document under review**. **The ADR's own `Review log` (status block, top of
  `ADR-009-workflow-engine-temporal.md`) is authoritative** for what is settled — read it first
  rather than trusting this summary.

  **State: §1 (settled pre-ADR), §2, §3 done. §12 reversed as a knock-on. §4–§11, §13–§17 not yet
  reviewed.** The document as a whole is **still Draft** — individual sections being settled does
  **not** make it Accepted, and nothing may be implemented against it yet.

  ### What §2 gained (four sub-decisions, all new)
  - **2a — Temporal persistence is a SEPARATE Postgres instance.** A separate *database* on the app
    instance buys no isolation (shared connections/buffers/disk). Two further reasons: the Temporal
    DB holds **in-flight execution state** so its restore posture differs, and its schema is owned
    by **`temporal-sql-tool`** on Temporal's release cadence — **a second migration mechanism**
    alongside Alembic, which nothing in the deploy flow currently runs. **Temporal wants two DBs
    inside that instance** (`temporal` + `temporal_visibility`) even on standard visibility.
  - **2b — the Web UI is NOT exposed.** `kubectl port-forward` only. The governing fact: the UI
    **terminates, cancels, signals and resets** workflows — a *write-capable control plane*, not a
    dashboard, so the mlflow `basicAuth` precedent on this cluster does not transfer. OSS Temporal
    UI ships with no auth of its own. Deferred alternatives (basicAuth vs forwardAuth, with the
    Clerk **cookie-vs-bearer** wrinkle spelled out) are tracked in the ADR's "Deliberately not
    decided here" table, post-Phase 4.
  - **2c — namespace retention = 30 days.** Low-stakes on purpose: it has **no correctness role**
    (§3 forbids answering user-facing questions from Temporal), it is changeable after creation, and
    volume is tiny. ⚠️ **It is cheap *only because* §5's references-not-payloads holds** — if an
    activity ever returns page *content*, a run goes ~50 KB → ~4 MB and retention becomes binding
    overnight. **If this number ever needs urgent revisiting, suspect §5 first.**
  - **2d — capacity.** Node is 8 CPU / 32 GiB at **28% CPU / 11% memory requests**; Temporal adds
    ~+1.5–2 CPU, landing near 50%. **That figure IS the coexistence peak** (the baseline already
    includes NATS + coordinator + all workers), so nothing later in the sequence is heavier.
    ⚠️ **CPU *limits* are the real risk — already 162% overcommitted.** A headed render plus a
    Temporal history burst throttles the history service and **surfaces as workflow task timeouts
    that look exactly like a workflow bug.**

  ### What §3's review found (verified against live code, and it changed the section)
  - **🔴 Crawls consume ZERO of all three quota meters — filed as P7, decision owner-confirmed.**
    `JobRun(...)` is constructed in exactly three places (`routers/jobs.py:207`,
    `routers/batch.py:105`, `core/scheduler.py:80`) and **never for a crawl**; crawl work lives in
    `crawl_pages`. `routers/crawls.py` has **no quota check at all**. So the ADR's "a new table
    would be invisible by construction" was describing something **already true, of crawls** —
    a live gap, not migration prep.
  - **The `storage_bytes` exemption was wrong.** The ADR said it "needs no change… already
    lane-agnostic." It isn't: `increment_storage_bytes` has **one** call site
    (`result_consumer.py:85`) gated on `run.storage_accounted_at`, a **`job_runs` column**. **All
    three meters are blind to crawls, not two.** The exemption was dangerous because it told an
    implementer storage needed no thought — and pipeline artifacts written by an *activity* would
    be uncounted by the identical mechanism.
  - **A fourth thing, found while checking the above: nothing ever frees crawl artifacts.**
    `DELETE /crawls/{id}` is cancel-only; admin user-delete and job hard-delete both enumerate
    `JobRun.result_path` only. **Deleting a user orphans their crawl artifacts in MinIO.**
  - **The view fixes *which rows*, not *when*.** Both meters recount, so two concurrent creates can
    each read 499/500 and both proceed. Pre-existing; named in the ADR so nobody assumes the view
    closed it. Converting to stored counters is deliberately **not** done.

  ### P7 — the crawl-metering decision (PM, PRD-016 OQ-4 round 3 · ✅ owner-confirmed 2026-08-08)
  Crawls join the run-counting view. **The unit is one fetch of one target URL producing one stored
  result** — a crawl page is identical work to a job run, differing only in that the URL was
  *discovered* rather than supplied, and **discovery is not a discount**. That framing is also what
  keeps §8's "one run = one unit regardless of block count" true rather than contradicted.
  - **Per page** for `monthly_runs` and `storage_bytes`; **per crawl** for `concurrent_jobs`
    (the one axis where cost and contention deliberately disagree — per-page concurrency would put
    every default crawl, `max_pages` 100, permanently over the default ceiling of 5, and enforcing
    it would need throttling inside `coordinator/`, which §13 deletes).
  - **Reclaim ships with counting** — charging against a hard wall with no way to free space is a
    support incident by construction. Scale: 10,000 pages at BUG-003's measured 291 KiB–4.1 MiB is
    **2.8–40 GB from one API call** against a 5 GB default.
  - **Accounting starts at cutover** — no backfill, and deleting a pre-cutover artifact must not
    decrement or the counter goes negative.
  - **⚠️ Rider on §8, still to rule on:** "one unit regardless of block count" holds only while a
    pipeline fetches once. If layer A ever permits two Scrape blocks, that run fetches twice and
    must cost 2. PM prefers **counting executed Scrape blocks** over capping Scrape at one.
    → **RULED 2026-08-17: closed on structural grounds.** Layer A cannot fetch twice, so nothing
    needs counting. The PM's preference stands for the day multiple roots ship.
  - **Sequence: after P6/BUG-005**, which re-keys the v1 artifact path and touches the same
    accounting surface. Not a §3 do-not-fix — `core/quota.py` and `routers/crawls.py` **survive**
    the migration; only the storage-accounting call site is in `coordinator/`.

  ### §12 reversed — read this before touching tenant isolation
  The ADR claimed tenant identity was encoded in the workflow ID, giving "a second, structural
  property." **Withdrawn.** The IDs stay `pipeline-run-{id}` / `job-run-{run_id}`, user-free.
  **Temporal never parses a workflow ID** — it is an opaque string — so an embedded `user_id`
  protects nothing unless something reads it back, which is *a check we wrote*, exactly what §7's
  uniqueness mechanism is valuable for **not** being. It also contradicted §3 (never ask Temporal a
  user-scoped question) and duplicated a guarantee the API's ownership check already provides.
  **The consequence to carry: there is exactly ONE tenant boundary — the API's ownership check —
  and nothing at the engine backs it up.** Any future path reaching Temporal without first loading
  and ownership-checking the app row is a tenant-isolation bug with no second line of defence.

  ### Two open items carried forward
  - **§8's multi-Scrape rider** (above) — unresolved, needs an owner call when §8 is revisited.
    → **Resolved 2026-08-17.**
  - **The coexistence topology diagram** is now drawn (`temporal-full-migration.md` **§9a**, two
    ASCII diagrams: today-v1 and the peak). Placement was deliberate — the shape changes at four of
    the seven steps, so it is *sequence* material, and the ADR holds decisions only. §16 points at
    it rather than duplicating it.

  **Docs-only session — no code changed.** Six files modified — since **committed** as `59ae835`
  and `5f43863`: `CLAUDE.md`, `scrapeflow-session-handoff.md`, `docs/adr/ADR-009-…`,
  `docs/project/phase4-backlog.md`, `docs/project/phase4-prd/PRD-016-…`,
  `docs/project/temporal-full-migration.md`.
  Also fixed in passing: **`phase4-backlog.md` claimed ADR-009 was "written + Accepted"** while the
  ADR, the ADR index, `CLAUDE.md` and this handoff all said Draft — the one doc designated single
  source of truth was the only one asserting a decision that had not been made.
  **`develop` and `main` remain level and deployed** (code at `b110591`).

  <details><summary>Prior START HERE (2026-08-04, later session) — ADR-009 drafted</summary>

  ## 📝 (2026-08-04, later session) — **ADR-009 is DRAFTED and needs your review**
  **The next action is a human review of
  [`docs/adr/ADR-009-workflow-engine-temporal.md`](docs/adr/ADR-009-workflow-engine-temporal.md)**
  — 605 lines, status **Draft**, *not* Accepted. It records the Temporal decision, answers **all
  11** of PRD-016's open questions, and defines the v1/v2 coexistence contract. Nothing in it is
  settled; do not implement against it or cite it as a decision elsewhere until the status changes.

  **Filed 2026-08-05 — BUG-006: Dependabot scans 3 of 6 dependency manifests.** No
  `.github/dependabot.yml`, so only `api/uv.lock`, `frontend/package-lock.json` and
  `http-worker/go.mod` are covered; **`coordinator/`, `llm-worker/` and `playwright-worker/` have
  no lockfile and have never been scanned**, so the real advisory count is unknown. Surfaced by a
  live aiohttp high (`CVE-2026-69244`, OOB heap read in the C response parser) whose **visible
  alert is the unreachable copy** — for `api` and the two workers aiohttp only parses MinIO
  responses — while the **reachable** one, `coordinator/sitemap.py` fetching robots.txt and
  sitemap XML from *user-supplied target sites*, sits in a service nothing scans. ⚠️ **Do not
  close it as dissolved by the migration:** `coordinator/` is deleted, but sitemap discovery
  *ports into a `CrawlWorkflow` activity* and carries the exposure unless the port uses **httpx**,
  as every other untrusted-target fetch already does. **Deferred behind BUG-005 and Temporal**
  (owner's call). Alert count 2026-08-05: **51 open — 2 high, 34 medium, 15 low.**

  **Three findings from the ADR session that are not in the ADR and matter on their own:**

  - **BUG-005 — batch is broken on all three execution paths, silently.** Found while verifying
    PRD-016's claims against live code, not during triage. **(A)** playwright batch: workers
    reject `job_id: null` as malformed and **ack+drop**, so items hang at `pending` forever and
    stale-pending recovery can't reach them. **(B)** http batch: **Go unmarshals `null` into
    `string` as `""` with no error** (reproduced), so every item writes `latest/.html` and
    `history//{ts}.{ext}` — items overwrite each other inside one batch, and across users the same
    object is served to two tenants, breaking isolation *at the storage layer* where no 404 guard
    reaches. **(C)** batch + LLM: same drop at the LLM stage → stuck `processing`, batch counters
    never reach `total`, `batch.completed` never fires. One root cause: **`job_id` is NULL for
    batch runs (correct, per ADR-006) while both message schemas and the ADR-002 §8 path
    convention assume it is not.** `api/tests/test_batch.py:451` **asserts the broken value is
    correct**, which is why every suite is green. Filed as **P6** in the backlog; fix is three
    parts that must ship together (writeup in `open-bugs.md`).
  - **The quota meters cannot see a new lane.** `_count_monthly_runs` and `_count_concurrent_jobs`
    both *recount* rather than store, and both hardcode `FROM job_runs`. A `pipeline_runs` table is
    invisible to them **by construction** — a user out of monthly job runs could trigger unlimited
    pipeline runs. This is why ADR-009 §3 moves counting onto a view. `storage_bytes_used` is fine
    (it's a counter incremented by whoever writes bytes, already lane-agnostic).
  - **`webhook_deliveries` rejects a pipeline row at the database level** —
    `num_nonnulls(job_id, batch_id, crawl_id) = 1` and `num_nonnulls(run_id, crawl_id) = 1`, with
    `run_id` an FK into `job_runs`. So OQ-11 option (b) was never the free reuse it read as.

  **PM review round 2 happened in the same session** (a PM agent, decisions recorded *in* PRD-016
  per the doc-under-review rule; 446 → 721 lines). Three Architect escalations settled: **no
  change-detection / cost gate in layer A** — both halves go to Monitors, because *"the previous
  run of this same thing"* is undefinable once R1 run inputs exist, and **B cannot ship without the
  gate**; **at most one Webhook block per pipeline**, rejected at save time, because two would ship
  layer C's fan-out *without* its rollback; **cancellation never aborts a block mid-execution** —
  the Scrape exception is dropped. It also found a **fourth** R6 divergence: a pipeline that fails
  before its Webhook block **tells nobody**, where a job fires `job.failed` from any stage. That
  one is deliberately **unassigned** — it depends on the open half of OQ-10.

  **Two warnings in the ADR worth not rediscovering the hard way:**
  - **§9 — integration option (a) recreates Q5/Q6/Q7.** Dispatching to the existing NATS workers
    from an activity puts **two retry layers on the same work**. NATS-side retry must be
    neutralised for workflow-originated messages.
  - **§10 — `diff.py` + content-hash are relocated, not deleted, and not yet re-homed.** The
    migration inventory sends them to a "diff/dedup activity"; the PM has since assigned change
    detection to Monitors, which is unwritten. They must outlive `result_consumer.py` and wait.

  **After the ADR is Accepted, the next artifact is the conditional-execution PRD** (layer A),
  which ADR-009 §14 places *before* PRD-018 (Monitors). PRD-017 (Delivery sinks) is unblocked and
  can proceed in parallel — it adds block types without extending what a pipeline can express.

  **Docs-only session — no code changed.** `develop` and `main` are still level at `b110591` for
  code; docs commits sit on top of `develop`.

  <details><summary>Prior START HERE (2026-08-04, earlier session) — PRD-016 PM review round 1</summary>

  ## ✅ (2026-08-04) — PRD-016 PM review **complete**; next is **ADR-009 (Architect)**
  PRD-016 was read section by section and revised in place. This was **not a copy-edit pass** —
  several claims were verified against live code and found wrong, and four capabilities were
  missing outright. The doc went **270 → 446 lines**; open questions **9 → 11**.

  **Start the next session as the Architect**, on `docs/project/phase4-prd/PRD-016-workflows-pipelines.md`.
  Two sequencing facts to not re-derive:
  - **Answer OQ-1 first.** The block model is upstream of nearly everything — OQ-2 needs the
    identifiers it defines, OQ-10's conditionals need the shape it picks.
  - **OQ-11 is on R6's critical path.** You cannot run the acceptance gate without a Webhook
    block, and you cannot build one without deciding what a failed delivery does to a run.
    It reads like a side question. It isn't.

  **What the review found (verified against code, not taken on trust):**
  - **Today's recipe is `scrape → [content-hash gate] → LLM → diff → webhook`.** The diff runs
    **after** the LLM, on the final artifact — JSON diff if the LLM ran (`result_consumer.py:506`),
    text diff if not (`:460`). There are **two distinct change-detection mechanisms**, not one:
    the **cost gate** (`:376`, exact-byte hash, *before* the LLM, skips everything downstream)
    and the **reporting diff** (after, purely descriptive). They serve different purposes and
    almost certainly don't belong in the same layer.
  - **The Problem section was factually wrong.** It claimed users "cannot do anything conditional
    — *only call the LLM if the page actually changed*." The content-hash gate does **exactly
    that**, today. Rewritten: the one conditional that exists is hard-coded, always-on, invisible
    in API and UI, and byte-equality only. Same complaint, now unfalsifiable by someone reading
    the code.
  - **Conditionals and change detection are homeless → OQ-10.** `workflows-scoping.md` §4A lists
    **branch** in layer A's own catalog; §4B's Monitors example (*"if it changes, tell me"*) needs
    both a diff signal and a conditional. Neither Delivery (C) nor Monitors (B) extends what a
    pipeline can *express*, so **B depends on capabilities this PRD defers.** OQ-1 gained a
    forward-compat constraint so the deferral stays reversible.
  - **Run inputs were missing entirely → R1.** The URL lived in the Scrape block's config, so one
    saved pipeline served one URL. That breaks user stories 1 *and* 3, collides with the
    pipelines-per-user limit, and makes a pipeline a **downgrade from a job** on that axis.
  - **Webhook-as-a-block silently changes failure semantics → OQ-11.** Today
    `create_webhook_delivery` writes a *pending* row and `webhook_loop` delivers async — the run
    is already `completed`, so **an undelivered webhook never fails a job**. As a block it does.
    Three options, none clean (fail the run / succeed-on-queued / wait on a long horizon), and
    the third collides with the concurrency ceiling.
  - **Quota is a shared pool → OQ-4 rewritten.** `user_quotas` has `monthly_runs_limit`,
    `concurrent_jobs_limit`, `storage_bytes_limit`. Jobs are its only consumer today; pipelines
    become a second one, which is where R5's "no user-visible change" comes under pressure.
    One-unit invites arbitrage; per-block makes the R6 pipeline cost **3×** the job it reproduces.
    **PM constraint added (not an Architect call): the R6 gate pipeline must not cost more than
    the job it reproduces.** Note `concurrent_jobs_limit` **already exists** — the question is
    share-or-split, not whether to build one.
  - **Blocks must pass references, not payloads → OQ-1.** Activity I/O lands in **workflow
    history**, which caps payload size and is **retained after completion** by design. Real pages
    run 291 KiB–4.1 MiB (BUG-003 audit), so a content-passing model fails on big pages for
    reasons unrelated to scraping. Contrast worth keeping: today's NATS stream is
    `--retention work`, so orchestration state is deleted on ack — free and self-cleaning.
    History is not. **This is the largest new operator-side cost in the migration.**

  **Decisions made in the doc (PM calls, don't relitigate):** Validate rules are **declarative
  only** (an evaluator is user-code by another name; a durable engine *replays*, so a
  nondeterministic rule is a correctness bug that only shows after a restart; validation is
  terminal and fires *after* the LLM billed). Validate asserts on the block's **input**, so it can
  guard content *before* an LLM call. Cancellation lets the **in-flight block finish** — runs stop
  at a block boundary — with **Scrape** the sole abortable block; written as a *rule* (long +
  scarce resource + no side effect) rather than a list, so layer C inherits "sinks are never
  abortable" for free. R6 equivalence is judged on **structure and mechanics, not byte-equality**
  (the LLM block is nondeterministic). MCP tooling for pipelines is a **non-goal**; the SPA surface
  is pinned to list + run status.

  **Also added:** R4 time budgets (declared, **composing** — a run ceiling shorter than the sum of
  block ceilings *is Q6 again* — and attempt-timeout=transient vs total-budget=terminal, with the
  LLM cold-start floor named); R4's **fail-closed** rule (unknown error → terminal) which was
  carried only by reference in OQ-6; R3's **concurrent-runs-per-user** ceiling, which the platform
  operator user story asked for and no requirement delivered.

  **Process rule worth keeping:** review findings land **in the document under review**, never in a
  parallel notes file. A review note *about* a PRD is a second copy of its open questions — the
  same drift this handoff already fixed once by deleting the duplicated triage tables.

  <details><summary>Prior START HERE (2026-07-28) — pre-Phase-4 queue closed + PRD-016 first draft</summary>

  ## ✅ (2026-07-28) — queue empty + PRD-016 written; next is **ADR-009 (Architect)**
  **P5 (Q1–Q4 close-out) is DONE, and with it the entire §1 pre-migration queue** — nothing
  blocks the Temporal migration. **PRD-016 (Workflows: Pipelines) is written and ready for the
  Architect:** `docs/project/phase4-prd/PRD-016-workflows-pipelines.md`. The next artifact is
  **engine ADR-009** (Temporal decision + v1/v2 coexistence contract), which is also where
  PRD-016's open questions get answered. See `docs/project/phase4-backlog.md` §2.

  **PRD-016 scope call:** it covers **layer A (Pipelines) only** — Delivery sinks (C) and
  Monitors (B) get their own PRDs, so the Architect isn't designing against a moving target.
  Three things in it worth not re-deriving:
  - **R6 is the acceptance gate.** Reproduce *today's* `scrape → LLM → webhook` recipe as a
    pipeline with equivalent output **before** designing any new block type. If the model can't
    express what already works, it's wrong.
  - **R4 makes "retry lives in exactly one visible layer" a hard requirement**, not a
    preference — that's the Q5/Q6/Q7 cluster's whole lesson written into the spec.
  - **OQ-6 is the do-not-delete list.** LLM cold-start handling (`ensure_ready()` + 180s
    timeout) and the transient/terminal storage-fault classifier are **block requirements**,
    not NATS artifacts. They read as plumbing and will be deleted with it if nobody says so.

  The two OQs most likely to produce a subtle correctness bug: **OQ-2** (editing a pipeline
  while a run from the previous version is in flight — pin or adopt?) and **OQ-3**
  (what *structurally* prevents a unit of work running on both lanes).

  **Tags** (annotated; the older `v1.0.0`/`v2.0.0` are lightweight):
  | Tag | Commit | Marks |
  |---|---|---|
  | `v3.0.0` | `d9e1edb` (2026-05-13) | End of Phase 3 — last commit before the post-Phase-3 change log opens |
  | `prephase4` | `1965953` (2026-07-28) | Pre-Phase 4 queue closed, immediately before PRD-016. Its message records what the system *is* at that point (NATS + the five hand-rolled loops) — the thing the migration replaces |

  **Doc sweep done in the same pass** (2026-07-28) — several docs still asserted stale state:
  - `CLAUDE.md` — the pre-Phase-4 queue was still listed item-by-item with two entries marked
    "unpushed" (long since deployed). Compressed to a closed-summary + pointer, since it loads
    every session; the two **must-port carry-forwards** are what remain in full.
  - `open-bugs.md` — **BUG-001 was still "Open"**; now closed as do-not-fix (§3 dissolves it:
    `_recover_stale_pending` exists only to police the hand-rolled scheduler). BUG-004 stays
    genuinely open.
  - `usage-findings.md` — UF-003 3a still said "Go worker still open" (fixed in `fbce01f`).
  - `workflows-scoping.md` — **actively contradicted the decision**: §7 recommended prototyping
    on DBOS first, and §1's "not a rip-out" non-goal is no longer true of the *phase*. Both
    marked superseded in place; §4/§6 (the three layers, state-ownership split) still stand and
    are what PRD-016 was written against.
  - `docs/process/architect.md` — added the ADR-009 hand-off, and corrected "the coordinator is
    the template for future multi-step coordination" (`coordinator/` is on the deletion list).
  - `docs/process/product-manager.md` — Phase 4 addendum (PRDs now in `phase4-prd/`, engine
    choice is not a PM question, read backlog §3 before speccing anything).
  - `docs/adr/README.md`, `docs/README.md`, root `README.md` — ADR-009 row + inputs, a Phase 4
    section (the index had none — the backlog and PRDs were unreachable from it), release tags.

  **What P5 actually found (it was not pure bookkeeping).** All four were verified against live
  code rather than taken on trust, and **two had landed on a different option than the one
  originally recommended** — so the doc was actively misleading before this pass:
  - **Q2** shipped as Option **C (Postgres `BEFORE UPDATE` trigger)**, not the recommended B. The
    question asked "do some paths bypass ORM assignment?" — they do: the scheduler and cancel
    route write via `db.execute(update(...))`, which silently skips SQLAlchemy `onupdate`. General
    lesson: **ORM `onupdate` is a convention; a trigger is an invariant.**
  - **Q4** shipped as Option **B**, not the recommended A. Disable is its own operation
    (`PATCH /jobs/{id}` `{"schedule_status":"paused"}`); `DELETE` keeps soft-cancel, with Option C
    available as `?permanent=true`. The flag is deliberately **tri-state** (`NULL` = not a
    scheduled job at all), which a bare `is_active` boolean could not express. **This matters for
    the migration:** `schedule_status` is the switch cutover gotcha #2 depends on — a job moved to
    a Temporal Schedule must be paused in v1 or it fires on both lanes.
  - Q1 (Option A — `uq_api_keys_user_name` + `IntegrityError`→409) and Q3 (`webhook_url` → `Text`)
    shipped exactly as written.

  **Q8 was closed in the same pass as do-not-fix**, so no question in `open-questions.md` is left
  without a STATUS block. Its Option B refactor targets `result_consumer.py`, which the migration
  deletes; the `5cb8c7f` source guards stay as the live defence until then. **The Q8 incident is
  the empirical grounding for ADR-009** — carry the argument, not the code.

  `open-questions.md` now opens with an outcome table and the rule that **where a STATUS block
  contradicts the discussion beneath it, the STATUS block wins** (Q2, Q4, and Q7 all diverge; Q7's
  advice outright reverses).

  **Still open, deliberately deferred (both in backlog §4, neither blocking):** **BUG-004**
  (screenshots orphaned on every path — latent; facet 1 is a *product call*, not a bug fix) and
  the **47 medium/low Dependabot alerts** (aiohttp ×21 + dompurify ×17 dominate, both transitive).

  **✅ `develop` and `main` are level and deployed** (code at `b110591`; docs commits on top).

  <details><summary>Prior START HERE (2026-07-28) — P4/BUG-002 detail, now closed</summary>

  **P4 (BUG-002 — Dependabot critical + highs) is DONE and DEPLOYED.** Went **8 crit / 13 high →
  0 crit / 0 high**. Three commits, one per ecosystem, all on `main` and deployed; **login
  smoke-tested on the deploy 2026-07-28** (the clerk 5→6 gate — see below).

  | Commit | What |
  |--------|------|
  | `b9c8a1a` | Go http-worker: `golang.org/x/crypto` 0.23→0.52 — clears **11 alerts (all 8 crit + 3 high)**, all SSH-only (not reachable; http-worker runs no SSH). Forced `go` directive → 1.25 (x/crypto v0.52 requires it); Dockerfile builder `golang:1.22`→`1.25`. Also fixed a latent wrong import in `worker_test.go` (behind `//go:build integration`, so it never built in CI). |
  | `e8726bf` | API: python-multipart 0.0.22→0.0.32, cryptography 46→48, starlette 1.0→1.3.1, pyjwt 2.12→2.13, Mako 1.3.10→1.3.12 — **8 high**. **clerk-backend-api 5.0.6→6.0.1 was REQUIRED, not scope creep:** clerk 5.x hard-pins `cryptography<47`, and every cryptography <48.0.1 is vulnerable, so the crypto fix is unreachable on clerk 5. Verified clerk 6's API surface vs our code (`is_signed_in` is now a *property* not a field, but works; `authenticate_request`/`AuthenticateRequestOptions`/`users.get()`/`email_addresses` all intact) — no code change. `uv.lock` diff is large because the committed lock was **stale** (missing croniter/xxhash/hiredis/pre-commit); regenerating reconciled it. 249 API tests green. |
  | `b110591` | Frontend: js-cookie 3.0.5→3.0.7, postcss 8.5.14→8.5.24, vite 8.0.12→8.1.5 — **3 high**. js-cookie is pinned exactly by `@clerk/shared`, so forced via an npm `overrides` entry. postcss/vite are dev/build-time + Windows-only, not reachable in prod (Linux, pre-built static) — bumped for completeness. Prod build passes, bundle unchanged (~94 KB gzip). |

  **Remaining Dependabot = 47 medium/low, deliberately out of BUG-002 scope.** Dominated by two
  noisy transitive deps: **aiohttp ×21** (fix 3.14.1) and **dompurify ×17** (fix 3.4.12). Also
  x/net ×1 (go, direct, fix 0.55.0), react-router ×3 + react-router-dom ×1 (npm), and
  Pygments/idna/pydantic-settings/pytest ×1 each (pip). These are a future clean-up, not urgent.

  **Consumer-recreate note:** none needed for BUG-002 (no `ConsumerConfig` change). The pre-existing
  playwright `2432be7` recreate backstop (from P3b) still applies whenever those changes are
  reconciled — non-urgent (per-worker `num_delivered` cap prevents looping).

  **✅ `develop` and `main` are level at `b110591` and deployed.**

  <details><summary>Prior START HERE (2026-07-24) — P3b/UF-003 detail, now closed</summary>

  **P3b (UF-003 — inconsistent MinIO write-path failure handling) is DONE — all four parts.**
  In order:
  | Commit | What |
  |--------|------|
  | `98b25ec` | UF-001 — `/health/deps` endpoint (MinIO check split out of the `/health/ready` probe) |
  | `d5709dd` | docs — filed UF-003 as P3b |
  | `2432be7` | UF-003 3a — **playwright worker** naks transient MinIO faults instead of acking |
  | `6ad95e3` | UF-003 3a — **LLM worker** aiohttp-unreachable gap (retried MinIO 5xx but not MinIO *down*) |
  | `bbc18d7` | docs — mid-P3b handoff (superseded by this block) |
  | `fbce01f` | UF-003 3a — **Go worker** naks transient MinIO faults (new `errors.go` + 16 tests) |
  | `7c339a2` | UF-003 3b — `result_consumer` log lines (`minio_stat_failed` + `content_hash_failed`) |

  **What the Go worker fix did (`fbce01f`) — the one non-obvious part worth keeping:**
  `handleMessage` used to publish `failed` + `Ack` on every `processJob` error. Now a transient
  MinIO write fault is `Nak`ed with backoff (5s→60s) up to the consumer's `NATS_MAX_DELIVER` (3,
  **already set** on the Go consumer — unlike the Python workers, which had it `-1`), publishing
  terminal `failed` only on the last attempt. **Go-specific divergence from the Python port:** in
  Python a connection error can only come from MinIO, so type-based classification is safe; in Go
  both the `net/http` fetcher and `minio-go` use the net stack, so a dead *site* and a dead *MinIO*
  both raise `*net.OpError`/`*url.Error` — a bare `net.Error` is ambiguous. So transient-eligibility
  is scoped to the **upload step** via a typed `*uploadError` wrapper (`processJob` wraps the
  `Upload()` error); only inside that does `classifyMinIO` apply net.Error→transient /
  `minio.ErrorResponse.Code`→5xx. `TestClassify_NonUploadErrorsAreTerminal` pins this: the same
  connection-refused error is transient from the upload but terminal from the fetcher.

  **Design decisions (settled, don't relitigate):** navigation/fetch failures against a *dead site*
  stay **terminal** — a re-scrape costs a headed-Chrome render / proxy bandwidth, and a dead site is
  its own answer. Only *infra* (MinIO) faults are transient. Fail-closed default (unknown →
  terminal). The transient/terminal S3 split is **domain knowledge** that ports into the Temporal
  activity `RetryPolicy` (backlog §3) — do not let it be deleted with the NATS plumbing.

  **Test counts now:** 249 API · **155** playwright-worker · **90** llm-worker · 14 MCP ·
  http-worker Go tests green (`go test ./...`, non-integration).

  **Consumer-recreate note (deploy-time, non-urgent):** the playwright (`2432be7`) `max_deliver`
  change needs the out-of-band consumer recreate to take on the **live** durable. The Go worker's
  `max_deliver` is **already 3** on its consumer, so no recreate is needed for it. Either way each
  worker's own `num_delivered` cap prevents looping, so recreates are a **non-urgent backstop** — and
  moot until these commits are pushed + deployed (they aren't). Verify NATS consumer state only via
  `nats consumer info **--json**` (the table omits `Max Deliver` when `-1`), never the worker's
  `subscribed` log line (prints config, not the live consumer).

  </details>

  </details>

  </details>

  </details>

  </details>

  </details>
- Phase 1 + Phase 2 + Phase 3 complete and production-verified at `scrapeflow.govindappa.com`
- **Auth on production Clerk instance** as of 2026-07-03 (was dev instance). See "Clerk production cutover" below.
- **In Phase 4. Scope is now decided: Phase 4 *is* the Temporal durable-workflows migration.** All Phase 4 items live in one place — **`docs/project/phase4-backlog.md`** — split into Pre-Phase 4 / the migration / **dissolved by Temporal (do NOT fix)** / survives-Temporal. Read that first; it supersedes the triage tables that used to be inlined in this handoff. Shipped Phase 4 work so far: **admin result viewer + user-email surfacing**, the **user-facing job dashboard**, and the **Playwright anti-bot hardening (ADR-008)**.
- **✅ Q6 is CLOSED — code and production.** Playwright (`67ba983`) and LLM worker (`6fb5b9c`); the live `python-llm-worker` consumer was recreated on 2026-07-21 and verified at `Ack Wait: 2m0s` (was `30.00s`). The reusable recreate procedure is in the Q6 status block in `open-questions.md`.
- **✅ Q5 is CLOSED — code and production** (options A + B + C). A live via `df44f95`; B + C shipped as `e1fde0d` → pushed/ff-merged as `fbcf254` on 2026-07-22, image deployed, and the `python-llm-worker` consumer recreated. Verified on the live consumer: `ack_wait 2m0s`, `max_deliver: 3` (was `-1`). **Q7 is closed with it** — the Q5 option-B nak retry *is* the worker-level retry Q7 asked for.
- **249** API tests passing (deterministic — first-run clean); **155** playwright-worker tests passing (was 70); **90** llm-worker tests passing (was 29); 14 MCP tests passing.
  - playwright-worker tests aren't wired into a compose service either. Same mount trick, but the
    `docker-playwright-worker` image on disk is **stale** (predates the credentials feature — no
    `cryptography`, so `test_main.py` fails to import). Use a newer tag:
    `docker run --rm -v "$PWD/playwright-worker/worker:/app/worker:ro" -v "$PWD/playwright-worker/tests:/app/tests:ro" -w /app --entrypoint python scrapeflow-playwright:ackfix -m pytest tests/ -q -p no:cacheprovider`
  - llm-worker tests aren't wired into a compose service (the image is production-only and doesn't COPY `tests/`). Run them by mounting the source over the built image:
    `docker run --rm -v "$PWD/llm-worker:/app" -w /app docker-llm-worker python -m pytest -q`
- Alembic auto-migration enabled in `api/app/main.py`

### Post-Phase-3 changes (since handoff, 2026-05-13 → 2026-07-03)

| Commit | Change |
|--------|--------|
| `5cb8c7f` | **Incident fix** — LLM dispatch loop. The regular + batch result handlers had un-source-guarded `if worker_status == "running"` branches; the LLM stage's `running` clobbered `run.status` from `processing` back to `running`, so the next `completed` re-matched the scrape-completed branch and re-dispatched to `scrapeflow.jobs.llm` — a tight feedback loop that burned ~200 LLM API calls in ~5 min before the worker was stopped. Fix: `if source == "scrape":` guards on the `running` transition in both `_handle_job_result` and `_handle_batch_result`. Root cause + permanent fix tracked as **Q8** (status-value overloading — needs a total state machine, not more guards). |
| `63b2dfc` | playwright-worker: split proxy URL userinfo into `server`/`username`/`password` for `browser.new_context(proxy=…)` (Playwright rejects credentials embedded in the URL). |
| `4b2d1d6`, `d9e1edb` | Added top-level `README.md`; replaced ASCII architecture diagram with Mermaid; updated `docs/project/COMMANDS.md`. |
| `70ce8bc` | **Admin result viewer + user email** (first Phase 4 feature). Backend: `user_email` added to `JobResponse` (admin jobs query joins `User`); new `GET /admin/jobs/{id}/result` (admin-scoped, no owner check) via shared `load_completed_result()` helper extracted from `get_job_result`. Frontend: read-only Monaco viewer on Job Detail (Source/Preview toggle — markdown via react-markdown, HTML via sandboxed iframe, JSON pretty-printed), User email row, and a User column + user filter on the Jobs list. Monaco is bundled locally (config in `frontend/src/lib/monaco.ts`) and lazy-loaded so the main admin bundle stays ~93 KB gzip. New deps: `@monaco-editor/react`, `monaco-editor`, `react-markdown`, `remark-gfm`. 4 new admin tests → 243. |
| `d8a6ce1` | **CI fix** for `70ce8bc`. The frontend's committed `package.json` uses **vite `^8`** (a working-tree bump that rode along in `70ce8bc`), but `@vitejs/plugin-react@4.7.0` only peers vite ≤7 — so the API Docker build's `npm install` failed with ERESOLVE. Bumped `@vitejs/plugin-react` → `^6.0.3` (peers vite `^8`), so the tree resolves with **no `--legacy-peer-deps` needed** locally or in CI. Build output/bundle sizes unchanged. |
| `d0a64b5` | **Docs** — handoff + CLAUDE.md updated for the admin result viewer; Phase 4 triage docs (`open-questions.md`, `open-bugs.md`, `usage-findings.md`) added. |
| _(user-dashboard commit)_ | **User-facing job dashboard + admin/user nav cross-link** (frontend-only; no backend/API/schema change — the owner-scoped `/jobs*` routes already existed). New user routes `/app/dashboard/jobs` + `/app/dashboard/jobs/:jobId` (nav item added; `/app/dashboard` now lands on Jobs). The admin `Jobs`, `JobDetail`, and `ResultViewer` are **shared, not duplicated** — each takes a `mode: 'admin' \| 'user'` prop that swaps the API base (`/admin/jobs` ↔ `/jobs`), the route/link base, and the result endpoint. Admin-only bits (User column, user filter which calls `/admin/users`, User detail row) render only in `mode='admin'`; the user Job Detail gets a **soft Cancel** button (`DELETE /jobs/{id}`) in place of the admin permanent-delete Danger Zone. `AdminJob` type renamed to `Job` (back-compat alias kept). Admin-detection extracted from `RequireAdmin` into a shared `lib/useIsAdmin.ts` hook (same `['admin-check']` cache key — no extra request); `Layout` gained a `variant` prop that renders the cross-link ("← My dashboard" in admin; "Admin panel →" in user, only if the user is an admin). **Layout bug fixed**: shell `min-h-screen` → `h-screen` so the sidebar footer (cross-link + Sign out) no longer falls below the fold on tall pages (Jobs/Stats) — `main` scrolls internally instead of the whole page. Bundle unchanged (~93 KB gzip main; Monaco stays a lazy chunk). No new tests (backend untouched; 243 API tests still green). |
| `92df7ea` | **Playwright anti-bot hardening (ADR-008 + `docs/guides/anti-bot-hardening.md`).** Scrapes were blocked despite residential proxies; BrowserScan diagnosed 3 fingerprint fails (`navigator.webdriver`, `HeadlessChrome` UA, CDP `Runtime.enable` leak). Fix: swap Playwright → **Patchright**; run **real Google Chrome** (`channel="chrome"`) **truly headed under Xvfb** (only mode with a clean UA — even `--headless=new` leaks `HeadlessChrome`); `--disable-blink-features=AutomationControlled` (clears `webdriver`; Patchright alone didn't) + `--no-sandbox`/`--disable-dev-shm-usage`; `new_context(no_viewport=True)`; no UA spoofing. All env-tunable in `worker/config.py`. k8s: `patchright install chrome` in image; playwright-worker Deployment resources bumped (infra repo `538fba5`). 70 playwright-worker tests. **Verified in prod: BrowserScan now `Normal`, 0 Robot / 18 checks.** |
| `4257183` | **Entrypoint fix** — first stealth deploy looked healthy (pod 1/1, 0 restarts) but ran nothing. `xvfb-run` as pid 1 masked worker crashes (container never exited → k8s never restarted), a cold-start race killed Chrome before Xvfb was ready, and `PYTHONUNBUFFERED` unset hid the logs. Replaced with `playwright-worker/entrypoint.sh` (start Xvfb → wait for its socket → `exec python` as pid 1) + `PYTHONUNBUFFERED=1` + pre-create `/tmp/.X11-unix 1777`. Now: python is pid 1, logs flow, crashes surface as CrashLoopBackOff. |
| `67ba983` | **NATS `ack_wait` + heartbeat (Q6 — now CONFIRMED & FIXED for playwright worker).** A headed-Chrome scrape (~37s) exceeded the pull consumer's default 30s `ack_wait`, so NATS redelivered mid-scrape; the late `ack()` was a no-op and, with `max_deliver=-1`, the job **looped forever** (re-scrape + re-upload every ~20s). Live incident mitigated by purging the subject + raising the live consumer to `ack_wait=120` (via `add_consumer` — this nats-py has no `update_consumer`). Permanent fix: `pull_subscribe(config=ConsumerConfig(ack_wait=120))` + `msg.in_progress()` heartbeat every 30s (covers jobs longer than `ack_wait`). **Caveat: JetStream won't apply a new `ack_wait` to an existing durable consumer — must update/recreate out-of-band.** |
| `ba8fb8a`, `9cddce0` | **Docs** — recorded the anti-bot/entrypoint/Q6 fixes; scoped the Temporal migration (`workflows-scoping.md`, `temporal-full-migration.md`); flagged the LLM-worker Q6 audit + Dependabot. |
| `35fb89f` | **Phase 4 backlog consolidated** into `docs/project/phase4-backlog.md` — Phase 4 items had accreted across seven docs. Structured by the Temporal decision, including a **§3 "dissolved by Temporal — do NOT fix"** table recording *which deleted code* removes each bug so they don't get re-raised. Pointers added from each source doc. |
| `df44f95` (infra) | **Q5 option A — LLM request timeout 60 → 180s** (env-only change in `govindappa-k8s-config`, `llm-worker.yaml`). Modal's scale-to-zero endpoint cold-starts in **90–110s**, which exceeded the 60s httpx read timeout. **This was urgent, not cosmetic:** the Q6 fix's `max_retries=0` pin removed an *accidental* cold-start mitigation — the SDK default of `max_retries=2` meant attempt 2 or 3 landed after Modal had booted, so cold starts silently succeeded. Pinned to 0, every cold start became a hard failure. The two commits are safe together and **unsafe apart**. Note `ack_wait` stays 120s: the 30s heartbeat resets the ack timer during the call, so a 180s request is never redelivered. |
| _(operational)_ | **Q6 consumer recreate — done.** Live `python-llm-worker` durable went `Ack Wait: 30.00s` → **`2m0s`**. Procedure (reusable, recorded in `open-questions.md`): deploy image → confirm pod → `nats consumer rm` → `kubectl rollout restart` → verify with `nats consumer info`. **Delete before restart** (the worker only subscribes at startup, so deleting under a running pod leaves it holding a dead subscription); **never scale to 0** (Flux reconciles `replicas: 1` back up). Safe because the stream is `--retention work` — acked messages are deleted, so a fresh consumer cannot replay completed jobs. |
| `e1fde0d` (shipped as `fbcf254`) | **Q5 options B + C.** **B:** new `llm-worker/worker/errors.py` classifies exceptions transient vs terminal; `handle_message` now `nak`s transient failures with exponential backoff (5/10/20s, capped) instead of acking everything. Critically it publishes **no** `failed` on a retry — the API's terminal-status guard (`result_consumer.py:125`) would lock the run failed and then discard the retry's `completed`. The worker caps attempts itself via `metadata.num_delivered` and publishes a real terminal `failed` on the last one, so a run can't dangle in `processing`. Classification **fails closed** (unknown → terminal): a wrong "transient" guess retries against the user's own API key. **C:** `llm.ensure_ready()` polls the OpenAI-compatible `/models` endpoint (spec'd; `/health` isn't) with a short per-probe timeout + long overall budget, so the real call runs against a warm endpoint. Only for `openai_compatible` **with** a `base_url`; 60s process-local warm cache; any HTTP response (incl. 401/404) counts as awake. Also resolves **Q7**. 29 → **87** tests. **Error strings changed format** — they now carry the exception type, because several httpx/provider errors stringify to `""` and showed as blank in the UI. |
| `8168760` | **BUG-003 — bot-wall detection (minimum tier).** A bot wall returns **HTTP 200 with valid HTML**, so `page.goto()` succeeded, nothing threw, and the interstitial flowed straight to `publish_result(status="completed")` — the user got a CAPTCHA page as their result, and its hash became a **dedup baseline** silently suppressing future change detection. **Prod audit: 6 of 15 completed runs (40%) were walls**, across three vendors — Amazon in-house (5.4 KiB "Continue shopping"), Akamai/`errors.edgesuite.net` on myntra (411 B), PerimeterX "Robot or human?" on walmart ×3 (464 B); genuine pages ran 291 KiB–4.1 MiB. All six `engine=playwright` (the Go worker's `fetcher.go:72` non-2xx check already covers hard walls), so Playwright only. New `playwright-worker/worker/blocking.py`: **Tier 1** vendor challenge harnesses (Akamai, Cloudflare, PerimeterX, DataDome, Imperva, Sucuri, Kasada, Amazon) decisive alone at any size; **Tier 2** generic challenge language gated to **< 20 KB** (the false-positive guard — those phrases are legitimate content at full page size); **Tier 3** structural integrity deliberately unimplemented. Patterns adapted from **Crawl4AI** (Apache-2.0 + custom attribution clause → `README.md`); **Amazon is our own addition**, their list has none. Worker keeps the `page.goto()` `Response` (was discarded), detects **after `final_url`, before `format_output`** (markdown strips every HTML signal), publishes `failed` + `error="blocked:<vendor>"`, logs `block_detected` with vendor/tier/signals. **Two signal corrections vs the original writeup:** `final_url` is *not* a tell (Amazon serves the wall *at* the requested URL; `/errors/validateCaptcha` is only a form action), and body size is the crispest separator (3 orders of magnitude). **Tests caught a real false positive** — the empty-body rule fired on a genuine tiny 404; now gated on status 200. **Prod: 6 poisoned `content_hash` baselines nulled + verified** (statuses left `completed` — rewriting history would misrepresent what the system did). 61 new tests → **131** playwright-worker tests. **✅ Deployed + verified in prod 2026-07-22** — image `main-1784742943-8168760c…`; the deployed classifier was run inside the pod against real MinIO artifacts: Amazon → `blocked:amazon`, Myntra → `blocked:akamai`, while CNN (4.1 MB), Times of India (319 KB) and **browserscan.net/bot-detection (450 KB)** all correctly passed. No consumer recreate was needed (no `ConsumerConfig` change). Also filed **BUG-004**. |
| `6fb5b9c` | **LLM worker Q6 fix.** Same bare `pull_subscribe` as playwright, but worse: `llm_request_timeout_seconds` (60) is **2× the 30s default `ack_wait`**, so redelivery fired on ordinary slow calls, and each redelivery re-bills the **user's own** provider key, unbounded (`max_deliver` unset → `-1`). **The playwright numbers did not transfer:** both SDKs default to `max_retries=2`, so one `call_llm()` could make 3 attempts each with a fresh httpx read timeout ≈ **210s** — well past a 120s `ack_wait`. Fix: `ConsumerConfig(ack_wait=120)` + `in_progress()` heartbeat every 30s + **`max_retries` pinned** (`llm_max_retries`, default `0`) on both SDK clients. Here **`ack_wait` is the orphan-recovery window, not a job-duration budget** — the heartbeat is what covers long calls. 29 llm-worker tests. **✅ Prod consumer recreated 2026-07-21 (`ack_wait`) and again 2026-07-22 (`max_deliver`).** |
| `98b25ec` ⚠️ **unpushed** | **UF-001 — `/health/deps` (P3 closed).** `/health/ready` checked DB/Redis/NATS but not MinIO, so it reported `200 ok` while every job silently failed to store output. The obvious fix (add MinIO to the readiness set) would have been wrong: `/health/ready` is the k8s readinessProbe on a **single-replica** API, so a MinIO blip would 503 the whole API (`/jobs`, auth, admin panel) — a partial outage escalated to a total one. **Split the two questions instead:** `/health/ready` stays serving-deps-only (unchanged, still the probe); new **`GET /health/deps`** adds MinIO (`bucket_exists` + 3s `asyncio.wait_for`), 503s when degraded, nothing routes on it. Per-dep checks factored into shared helpers. No infra change (`api.yaml` still probes `/health/ready`). Curl recipes in `COMMANDS.md`. 6 tests → **249**. |
| `2432be7` ⚠️ **unpushed** | **UF-003 3a — playwright worker naks transient MinIO faults.** The general `except` acked on **every** exception, so a momentary MinIO outage on the result upload permanently failed a job whose expensive headed-Chrome render had already succeeded (the ack preempts JetStream redelivery). Same ack-on-failure mode as Q5, only ever fixed on the LLM worker. New `playwright-worker/worker/errors.py` (ported from the LLM worker): `classify()` → transient/terminal, `retry_delay()` backoff, `describe()`. `worker.py`'s except now `nak`s transient faults with backoff (5/10/20s) up to `playwright_max_delivery_attempts` (3), publishing terminal `failed` only on the last attempt; `max_deliver` added to the consumer config as a backstop. **Correction vs a naive copy:** "MinIO down" (connection refused) raises `aiohttp.ClientConnectionError`, **not** an `S3Error`, so the LLM worker's `_TRANSIENT_S3_CODES`-only match would have missed the literal down case — the classifier adds `aiohttp.ClientConnectionError`/`ServerTimeoutError`. Navigation failures against a dead site stay **terminal** by design. Error strings now carry the exception type (`describe()`). 24 tests → **155**. **Live consumer keeps `max_deliver=-1` until recreated out-of-band — non-urgent (the worker's `num_delivered` cap prevents looping), and moot until deployed.** |
| `6ad95e3` ⚠️ **unpushed** | **UF-003 3a — LLM worker aiohttp gap.** The playwright port surfaced that the LLM classifier matched MinIO faults **only** via `S3Error.code` — which fires when MinIO returns a 5xx (overload) but **not** when MinIO is *unreachable* (pod down → `aiohttp.ClientConnectionError`, no `.code`), so the literal "MinIO down" case fell through to TERMINAL and failed permanently. Added `aiohttp.ClientConnectionError`/`ServerTimeoutError` to `_TRANSIENT_TYPES` (mirrors playwright) + declared `aiohttp` in `pyproject`. 3 tests → **90**. |
| `fbce01f` ⚠️ **unpushed** | **UF-003 3a — Go http-worker naks transient MinIO faults.** `handleMessage` published `failed` + `Ack` on **every** `processJob` error, so a momentary MinIO write fault permanently failed a job whose fetch + format had already succeeded — same ack-on-failure mode as playwright/LLM, latent on the Go worker. New `http-worker/internal/worker/errors.go`: `classify()` (transient/terminal), `classifyMinIO()`, `retryDelay()` (5s→60s cap). `processJob` wraps the `Upload()` error in a typed `*uploadError`; `handleMessage` naks transient faults with backoff up to the consumer's `NATS_MAX_DELIVER` (3 — **already set** on the Go consumer, unlike the Python workers' `-1`), publishing terminal `failed` only on the last attempt (no `failed` on a retry, or the API terminal-status guard would discard the retry's `completed`). **Go-specific correction vs a naive copy:** a `net.Error` in Go is ambiguous — both the `net/http` fetcher and `minio-go` use the net stack, so a dead *site* and a dead *MinIO* both raise `*net.OpError`/`*url.Error`. Transient-eligibility is therefore scoped to the upload step via the `*uploadError` wrapper; a fetcher net error stays terminal. `classifyMinIO` splits unreachable (`net.Error`, no code) from 5xx (`minio.ErrorResponse.Code`). New `errors_test.go` — 16 subtests incl. the scoping guard. `go test ./...` green; `go vet`/`gofmt` clean. **No consumer recreate needed** (max_deliver already 3; no `ConsumerConfig` change). |
| `7c339a2` ⚠️ **unpushed** | **UF-003 3b — API log lines for swallowed MinIO errors.** `stat_minio_size` (`api/app/core/storage.py`) and `_compute_content_hash` (`api/app/core/result_consumer.py`) both caught `Exception` and returned a fallback (0 / None) with **no log**. The stat one is money-adjacent: a silent stat→0 permanently under-counts the user's storage quota. Added one `logger.warning` each (`minio_stat_failed`, `content_hash_failed`); best-effort by design so control flow is unchanged. Kept to one line each — `result_consumer.py` is deleted by the Temporal migration. `ruff`/`ruff-format` clean. **Closes UF-003 (P3b).** |
| `9a73b70` | **docs (BUG-003 note).** Recorded the cross-worker bot-wall **error-string divergence** as a later-phase cleanup, not a bug: the Go worker reports a wall as `non-2xx: 403` (hard block, caught by the status guard at `fetcher.go:72`), while playwright reports `blocked:akamai` (soft-block 200, caught by body regex). The server serves each worker a *different* response because they present differently on the wire — the same reason BUG-003 only existed on playwright. Deferred to the Temporal activity layer / post-Phase-4 block-handling tiers (gated on UF-002), where a shared `blocked:<vendor>` taxonomy has a natural home. |
| `b9c8a1a` | **BUG-002 (1/3) — Go http-worker `golang.org/x/crypto` 0.23.0 → 0.52.0.** Clears **11 Dependabot alerts (all 8 critical + 3 high)** — every one SSH-related (`PublicKeyCallback` bypass, agent key forwarding, FIDO/U2F, KEX DoS), **not reachable** (http-worker runs no SSH client/server) but transitive and zero-risk. x/crypto v0.52 requires **go 1.25**, so `go mod tidy` bumped the `go` directive; bumped the Dockerfile builder `golang:1.22`→`1.25` to match. Also fixed a **pre-existing wrong import path** in `worker_test.go` (`scrapeflow/worker/internal` → `scrapeflow/http-worker/internal`) that only compiled before because the file is behind `//go:build integration` and never built in CI. `go build`/`vet`/`test` green. |
| `e8726bf` | **BUG-002 (2/3) — API python deps, 8 high.** python-multipart 0.0.22→0.0.32 (direct; floor raised ≥0.0.30), cryptography 46.0.5→48.0.1 (direct; floor ≥48.0.1), starlette 1.0.0→1.3.1, pyjwt 2.12.1→2.13.0, Mako 1.3.10→1.3.12 (transitive). **clerk-backend-api 5.0.6→6.0.1 was mandatory, not scope creep** — clerk 5.x pins `cryptography<47` and every cryptography <48.0.1 is vulnerable, so the crypto CVE is unfixable on clerk 5. Verified clerk 6's surface against our code (`jwt.py`/`dependencies.py`/`user_sync.py`): `Clerk(bearer_auth=)`, `authenticate_request`, `AuthenticateRequestOptions`, `RequestState.is_signed_in` (now a **property**, not a dataclass field, but works) / `.payload` / `.reason`, `users.get().email_addresses[0].email_address` — all intact, **no code change**. The `uv.lock` diff is large because the committed lock was **stale** (missing declared croniter/xxhash/hiredis + dev pre-commit); regenerating reconciled it. **249 API tests green** on the rebuilt image; login smoke-tested on deploy. |
| `b110591` | **BUG-002 (3/3) — frontend, 3 high.** js-cookie 3.0.5→3.0.7 (runtime; prototype-hijack cookie injection) — pinned **exactly** by `@clerk/shared`, so forced via an npm `overrides` entry (3.0.5→3.0.7 is an API-compatible patch). postcss 8.5.14→8.5.24 and vite 8.0.12→8.1.5 (dev/build-time; both **Windows-/dev-only**, not reachable in our Linux prod serving pre-built static). Prod build passes (`tsc -b && vite build`), main bundle unchanged (~94 KB gzip), Monaco still a lazy chunk, `@vitejs/plugin-react` 6 peer holds against vite 8.1.5. |

### Clerk production cutover (2026-07-03)

Moved auth from the Clerk **dev** instance to a **production** instance. No app code changed — Clerk is derived entirely from keys/config (backend `jwt.py` gets the instance from the secret key; frontend `pk` is a build-time env). Work was dashboard + Cloudflare + cluster secret only.

- **DNS**: production Frontend API `clerk.scrapeflow.govindappa.com` + account portal + mail CNAMEs added **manually in Cloudflare, grey-cloud (DNS-only)**. Not via ExternalDNS — its `sources` are `ingress`/`service` only, so it neither creates these nor prunes them (they carry no `txtOwnerId` TXT registry record, so `policy: sync` ignores them).
- **OAuth**: production needs **own** Google (and GitHub, if used) OAuth credentials — Clerk's shared demo app is dev-only. Symptom when missing: Google `Error 400: invalid_request / Missing required parameter: client_id`. Google client created, creds pasted into Clerk → login works.
- **Keys** (easy to mix up):
  - backend **`sk_live`** → k8s secret `scrapeflow-app-secrets/clerk-secret-key` (patched via the README "Rotating the Clerk secret" flow + `rollout restart`)
  - frontend **`pk_live`** → GH Actions secret `VITE_CLERK_PUBLISHABLE_KEY`, baked into the api image at build (CI rebuilds api on `api/**`/`frontend/**` changes — the `a0c905b` `index.html` title tweak was the trigger for that rebuild).
  - **Incident during cutover**: `pk_live` was accidentally pasted into the `clerk-secret-key` slot → backend couldn't auth to Clerk's Backend API to load JWKS → `TokenVerificationErrorReason.JWK_FAILED_TO_LOAD` (HTTP 401 in the SPA). Fixed by patching the real `sk_live`. Verified: pod env is `sk_live`, `api.clerk.com/v1/jwks` + Frontend-API `/.well-known/jwks.json` both 200, no verification errors in logs.
- **Fresh start**: prod **Postgres app tables truncated + MinIO `scrapeflow-results` emptied** on 2026-07-03 (schema + `alembic_version 8f4b6eb47abb` preserved; bucket kept). Rationale: a prod Clerk instance issues **new `sub` (user) IDs**, so old `users` rows keyed on the dev `clerk_id` would be orphaned — clean slate instead. Fernet keys (`llm-key-encryption-key`, `credentials-encryption-key`) were **not** rotated (would orphan encrypted-at-rest data).
- **Loose end**: GitHub OAuth custom credentials only need setup if GitHub sign-in is offered (Google done).

### Phase 3 — complete (archived)

All 28 steps + the full production-review findings (all `[x]` done, with `[?] 40` and `[?] 43`
deferred to Phase 4) are recorded in `docs/archive/phase3/production-review.md` and the Phase 3
backlogs under `docs/archive/phase3/`. The still-open deferred items are echoed in the Phase 4
"Deferred from Phase 3" table below.

### Phase 4 entry point

**Start at `docs/project/phase4-backlog.md`** — the single source of truth for Phase 4 scope. When returning:

1. Read **`docs/project/phase4-backlog.md`** first. It is an index; each item points to its source doc for full context/options/recommendation.
2. **Check §3 ("Dissolved by Temporal — do NOT fix") before writing any bug fix.** The migration deletes the code containing those bugs, so fixing them is wasted work. This is the most common way to waste a session here.
3. Only then open the source docs for depth: `docs/project/open-questions.md` (Q5–Q8 options + recommendations), `open-bugs.md`, `usage-findings.md`, `PHASE3_DEFERRED.md`.
4. Decide whether to run the full PM → Architect → Tech Lead → Engineer process (see `docs/process/`) or a lighter spec approach.

**Pre-Phase 4 queue** (§1 of the backlog), in cost-of-delay order — the first two get *worse* the longer they sit, the rest are flat-cost:

| | Item | State |
|---|---|---|
| 1 | Q6 — LLM worker `ack_wait` | ✅ **done, verified in prod** (`6fb5b9c` + consumer recreate) |
| 1b | Q5 — cold starts + transient retry | ✅ **done, verified in prod** (`fbcf254` + consumer recreate; `max_deliver: 3`) |
| 2 | BUG-003 — bot walls stored as `completed` (minimum fix) | ✅ **done, verified in prod** (`8168760`; image deployed, classifier verified against real MinIO artifacts; 6 poisoned baselines nulled) |
| 3 | UF-001 — MinIO missing from `/health/ready` | ✅ **done, deployed** (`98b25ec`; shipped as the `/health/deps` split, not a probe change) |
| 3b | UF-003 — inconsistent MinIO write-path failure handling | ✅ **done, deployed.** playwright 3a `2432be7`, LLM aiohttp gap `6ad95e3`, Go worker 3a `fbce01f`, 3b log lines `7c339a2`. All four parts closed 2026-07-24. |
| 4 | BUG-002 — Dependabot critical + highs only | ✅ **done, deployed 2026-07-28** (`b9c8a1a` Go x/crypto + `e8726bf` Python incl. forced clerk 5→6 + `b110591` frontend). **8 crit / 13 high → 0 crit / 0 high**; login smoke-tested on deploy. 47 medium/low left, out of scope. |
| 5 | Q1–Q4 close-out | ✅ **done 2026-07-28.** Verified against live code, not taken on trust — **Q2 landed as Option C (Postgres trigger), Q4 as Option B (`PATCH schedule_status`)**, both differing from the doc's recommendation. **Q8 closed alongside as do-not-fix**, so `open-questions.md` has no entry without a STATUS block |
| 6 | **BUG-005 — batch broken on all three execution paths** | 🔴 **OPEN, filed 2026-08-04.** `job_id` is NULL for batch runs (correct per ADR-006) while both message schemas and the ADR-002 §8 artifact-path convention assume it is not. Playwright batch drops every message and hangs at `pending`; http batch collides all items onto `latest/.html` + `history//{ts}.{ext}` (cross-tenant); batch + LLM hangs at `processing`. Fixed pre-migration on the **Q6 precedent**. `open-bugs.md` → BUG-005 |
| 7 | **P7 — crawls consume zero of all three quota meters** | 🔴 **OPEN, filed 2026-08-08** (decision ✅ owner-confirmed; implementation not started). Every meter is keyed on `job_runs`; a crawl never creates one, and `routers/crawls.py` has no quota check at all. A 10,000-page crawl costs **zero** runs, **zero** concurrent slots and **zero** counted bytes — 2.8–40 GB of artifacts against a 5 GB default. Nothing frees crawl artifacts either: deleting a user orphans them in MinIO. Per page for runs + storage, per crawl for concurrency; reclaim ships with counting; accounting starts at cutover. **Sequence after P6.** `phase4-backlog.md` §1 P7 · PRD-016 OQ-4 round 3 |
| — | **§1 queue: 2 open (P6, P7)** | **Next: resume the ADR-009 section review at §8** (metering — already amended twice as a knock-on, and carrying the open multi-Scrape rider). §1–§7 + §12 are done — see the ADR's Review log, which is authoritative |
| — | BUG-004 — screenshots orphaned on every path | **new, filed 2026-07-22.** Worker uploads screenshot PNGs + publishes `screenshot_paths`; the API consumer never reads the field — never persisted, surfaced, quota-counted or deleted. Latent (`screenshots/` empty in prod). Parked in backlog §4; facet 1 is a **product call**, not a bug fix |

---

### Phase 4 direction — Temporal Workflows migration (intended)

The strategic direction for Phase 4 (exploratory — not yet a committed build). Two design docs
capture it in full: `docs/project/workflows-scoping.md` (feature + engine comparison) and
`docs/project/temporal-full-migration.md` (complete change inventory + migration sequence).

**Engine decision: Temporal.** Chosen over DBOS/Restate/Prefect/Windmill for portfolio value +
first-class Python *and* Go SDKs (fits both our service languages). Grounded in the **Q8**
incident: our orchestration is hand-rolled polling-loops + Postgres state, and the overloaded
`job_runs.status` machine already caused a live feedback-loop incident — exactly the class of
code a durable-execution engine owns natively.

**The feature — "ScrapeFlow Workflows"** — one feature in nested layers, natural build order:

- **Pipelines (A)** — user-defined multi-step chains (scrape → clean → LLM → validate → deliver),
  replacing today's single hard-coded `scrape → LLM → diff → webhook`.
- **Delivery sinks (C)** — a rich block type on A: deliver to S3 / DB / Sheet / email with saga
  rollback (today output is only MinIO + one webhook).
- **Monitors (B)** — a pipeline wrapped in a durable loop: long sleeps, human-approval waits,
  scheduling. Absorbs the **live scheduled-crawl gap** (`crawls.schedule_cron` is accepted +
  persisted but the scheduler only ever dispatches `Job`, never `Crawl` — so scheduled crawls
  silently never run today).

**How it lands (v1/v2 coexistence, no big-bang).** Temporal comes up *alongside* the NATS stack.
It's **one product, two orchestration engines, a routing switch, and a drain period** — same
Postgres/MinIO/auth underneath. New work is routed (flag / per-tenant canary) to **v2 (Temporal)**
while **v1 (NATS + `result_consumer`)** keeps serving in-flight work; cut v1 per-flow once v2 is
proven. Reversible at every step (flip the flag back). Cutover gotchas to handle *at migration
time*, not defer: (1) a job must run on **exactly one lane** — never both (double-scrape / double
LLM-bill risk); (2) when moving a recurring job to a Temporal Schedule, **disable it in v1**
(`schedule_status`) or it fires on both; (3) keep the NATS workers alive (integration option **a**)
until v1 is drained — worker cutover to Temporal activities (option **b**) is what removes v1's
executors.

**Complete end-state (deep adoption).** Retire `result_consumer.py`, `scheduler.py`,
`webhook_loop.py`, `advisory.py`, and the `coordinator/` service; **remove NATS** entirely; workers
become Temporal activity workers. New infra: **Temporal Server + its own Postgres + Web UI**, plus a
**workflow-worker pod**. Orchestration logic leaves the API pod (into the workflow worker) — the API
becomes thin and **horizontally scalable**, removing the current single-replica / `Recreate`
constraint (see "Deferred from Phase 3" → NATS pull consumers). Q8 dissolves; the ack_wait/Q6 class
of bug disappears with NATS.

**Next artifacts if pursued:** a PRD (PM template) for the Workflows feature, then an engine **ADR**
(next number after ADR-008 → ADR-009) recording the Temporal decision + the coexistence contract.

---

### Phase 4 triage — moved

The triage tables that used to live here (Q1–Q8, BUG-001→003, UF-001/002, deferred-from-Phase-3)
were **duplicated** into `docs/project/phase4-backlog.md` and have been removed from this handoff
to stop the two copies drifting apart.

**They are not lost** — every item is in the backlog, now sorted by whether the Temporal migration
dissolves it. Two things the flat triage list could not express, which are worth knowing before you
open it:

- **Roughly half the triage list is now "do not fix."** Q6, Q7, Q8, BUG-001, the NATS pull-consumer
  item, the crawl-webhook bypass and the scheduled-crawl gap are all deleted outright by the
  migration. The old list gave no way to see that, so each of them read as work.
- **Q5 only *half* dissolves — and as of 2026-07-21 the surviving half is real code.** The
  ack-on-failure behaviour goes away with NATS, but the cold-start handling
  (`llm_request_timeout_seconds=180` + `llm.ensure_ready()`) is *business* logic — Temporal has
  no idea a scale-to-zero endpoint is cold and would just retry a timing-out activity.
  **Port `ensure_ready()` into the LLM activity**; don't let it be deleted with the NATS
  plumbing around it.

The **LLM-worker cluster (Q5/Q6/Q7)** framing holds, and it played out exactly that way — all three
resolved together in one session:

- **Q6** ✅ fixed on both workers **and** closed in production.
- **Q5** ✅ A, B and C all live in production as of 2026-07-22 (`fbcf254` + consumer recreate).
- **Q7** ✅ resolved *by* the Q5 option-B fix — the NATS-level nak retry replaces the SDK retry the
  Q6 pin removed. The status note at the top of Q7 in `open-questions.md` is now settled, and its
  advice **reverses**: do **not** restore `llm_max_retries=2`. With NATS-level retry in place an
  SDK retry multiplies *underneath* it (3 × 3 = 9 billable calls). Retry stays in one visible layer.

The through-line worth carrying into Phase 4: **every bug in this cluster was a retry hidden in a
layer nobody was looking at** — the SDK's `max_retries`, JetStream's redelivery, Modal's cold boot.
Temporal's value here is making retry declarative and visible in one place; the cost is that
anything left retrying underneath it multiplies silently.
