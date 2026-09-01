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

- ## 📝 START HERE (2026-09-05) — **ADR-009's section-by-section review is FINISHED. Still Draft — promoting it to Accepted is your call.**

  ### The closing blocks (Consequences · Deliberately not decided here) — reviewed 2026-09-05; **fourteen corrections, no decision changed**

  These two blocks are the summary a reader reaches for **instead of** the 3,700 lines. Neither had
  ever been reviewed in its own right — both had only been **amended as knock-ons** when a section
  review happened to touch them. So what was stale is exactly what no review reached in passing.

  ⚠️ **That is [16a]'s failure mode aimed at the summary**: a later review changes a section,
  records the change there, and does not carry it back. Every stale item below has that cause —
  D1 from §13, D2 from §15, D3 from §13, C2 from §2b, C4 and C7 from §16.

  #### Consequences — what changed

  | | finding | correction |
  |---|---|---|
  | 🔴 | **C1** *"The Q8 class of bug **cannot occur**, because the engine owns the state machine"* | Contradicted twice by this ADR. **§11a keeps two writers** on the run-status column deliberately, with a **silent** failure mode — a cancelled run flips back to `completed`, work done and charged. And **16c** found a *second* overloaded flag, `schedule_status`, created by an instruction in this ADR. ✅ Narrowed: the engine owns *its* transitions; it does not own `pipeline_runs.status` |
  | | **C3** *"The ack_wait/redelivery class (Q6) disappears with NATS"* | The **mechanism** goes; the **shape** does not. **15c**: four nested timeouts where the smallest silently wins, and a reasonable *"no pipeline run may exceed one hour"* set in another file — which §15 calls *"Q6's exact shape"*. ✅ Split mechanism from shape |
  | 🔴 | **C2** *"A per-workflow timeline in the Temporal Web UI **replaces** log-spelunking"* | **§2b does not expose the Web UI** — `kubectl port-forward` only, because it terminates and cancels workflows. ⚠️ **The identical overclaim was already caught and withdrawn from §15's first bullet on review**; it survived here only because this block was never reviewed. ✅ Qualified to operators with cluster access |
  | 🔴 | **C4** *"each unit of work is driven by **exactly one** of them"* | **16b** found the window where it is false: an unacked NATS message consumed by a v1 worker *after* the flow is routed to v2 — scraped twice, user's LLM key re-billed, and the v1 result **overwrites a live v2 run**. None of §7's four mechanisms reaches it. ✅ Narrowed to **migrated** flows; the flip is what the drain gate buys |
  | | **C5** *"Two SDKs… a small duplication in activity-worker setup"* | Understates §9's accepted price: **two deployments of one image per worker** through coexistence, **three integrations not one bridge**, any cross-cutting rule written twice without drifting (BUG-010's SSRF check is filed separately for this), and ⚠️ **the Playwright container contract as the riskiest single item in the port** |
  | | **C6** — missing from *Paid* entirely | **§11a's two writers.** The clearest permanent cost the review produced: correctness rests on a rule an implementer must remember, and forgetting it fails silently. ✅ Added |
  | 🔴 | **C7** *"the **deletion gate**"* | **16b renamed and rescoped it** — a drain gate firing at **two** points, cutover and deletion. The old name, in the block that summarises the plan |
  | | **C8** — one risk named, three produced | ✅ Added: **16d** (pipelines have no v1 fallback, so **R6 runs on a lane whose rollback is switching the feature off**) and **13a** (crawls have **no reference implementation**, so the diff-against-a-v1-run pre-gate *cannot be made to exist*, and crawls migrate **last**, when the habit of having it is most established) |

  #### Deliberately not decided — what changed

  | | row | correction |
  |---|---|---|
  | 🔴 | **D1** crawl frontier — *"visited-set + `continue-as-new` **vs** child-workflow-per-page… decide against measurements"* | **Every clause wrong.** 13b: `continue-as-new` is **mandatory in both**, so the either/or was already false; the measurements **already existed**; the visited set **cannot ride in workflow state** (≈800 KB vs a 2 MiB limit); ✅ **the frontier stays in Postgres**. As written it invited reopening a settled decision. Only **the table's shape** is open |
  | 🔴 | **D2** run-failure notification — *"left unassigned on purpose"* | **15f assigned it** to the conditional-execution PRD, noting §14 had enumerated that PRD's obligations one session earlier without it. Sharpest form now recorded: `webhook_events: ["job.failed"]` has **no expressible layer-A equivalent at all** |
  | 🔴 | **D3** *"whether `webhook_deliveries` / **`crawl_pages`** survive as v1-only audit mirrors"* | **13c settled `crawl_pages`: required, on the v2 lane** — P7's per-page metering unit, §8d's ledger FK target, and **the artifact's own name**. The opposite of a v1 audit mirror. ✅ Removed from the row; `webhook_deliveries` stays, genuinely open |
  | | **D4** conditional execution — *"before PRD-018"* | Loses **14a's whole finding** — written and built are different dates (written before PRD-018, possibly during A's build; **built after A ships, before B is built**) — and **14d's un-numbered PRD is invisible** in the one table where an un-numbered deliverable should be visible |
  | | **D5** two named open items missing from the table of open items | ✅ Added: **§7's scheduled-quota waiting room** (*"left as a named open item rather than decided here"* — no Temporal Schedule overlap policy reproduces today's waiting behaviour; `SKIP` loses the firing, which R5 forbids) and **13d's sitemap origin restriction** (*"still open, and needed before the crawl step is built"*) |
  | | **D6** namespace-per-tier — *"revisit under noisy-neighbour pressure"* | §12's reversal leaves the API's ownership check as the **only** tenant boundary, with nothing at the engine — so namespaces are the only engine-level isolation available. ✅ Trigger widened to **isolation as well as contention** |

  *(One additive, not a defect: the workflow-code-versioning row is accurate, but its trigger —
  "the first deploy that must survive in-flight runs" — didn't say when that arrives. **§15's ≈2.6 h
  webhook horizon means it lands in layer A**, not only with Monitors. Added.)*

  ### The review is finished — what that does and does not mean

  ✅ **§1–§17 and both closing blocks are reviewed.** ⚠️ **The document is still `Draft`.** Promoting
  it to **Accepted** is a separate owner decision and has **not** been taken — the status block now
  says so explicitly, so nobody reads "review complete" as "safe to implement against". Individual
  sections *are* settled; the **Review log** at the top of the ADR remains authoritative for which.

  ### Session close (2026-09-05)

  **Docs-only session — no code changed.**

  | file | what |
  |---|---|
  | `docs/adr/ADR-009-…` | both closing blocks rewritten (64 → 145 lines) with C1–C8 and D1–D6 applied inline; **two rows added** to the deferral table, **one row split** (`crawl_pages` out); status block rewritten — review complete, still Draft, Accept is a separate decision; review-log entry; date line |
  | `docs/adr/README.md` | review status → **COMPLETE, still not a decision**; closing blocks flagged as having been the most stale |
  | `docs/project/phase4-backlog.md` | header entry; ADR row → **SECTION REVIEW COMPLETE**, closing-block summary appended |
  | `CLAUDE.md` | bullet header → review complete / still Draft; closing-block summary appended |
  | this handoff | closing-blocks block; §17 demoted to superseded |

  ⚠️ **Knock-on from the previous session, fixed here:** one `ADR-002 §8` reference was **wrapped
  across a line break** (`ADR-002\n§8`) and survived the §17 pass, which was line-based. Corrected
  in `21fadd2`. If you ever re-run that kind of sweep, match across newlines.

  ⚠️ **Git state.** Deployed code is still `b110591`; `5c7fbdf` (the crawl page status-filter fix)
  is still **committed on `develop`, not deployed, not on `main`**. Everything on top is docs.
  ✅ **The owner's call of 2026-08-28 stands: `main` is deliberately NOT fast-forwarded** — pushing
  it starts a push to the prod server, so a fast-forward is a **release**, not a tidy-up. Wait to be
  asked. *(Re-check ahead/behind against the remote before quoting numbers — last `git fetch` was
  2026-07-13.)*

  **What is blocking: nothing.** The review is done. Outstanding, in rough order:

  1. **🔷 Owner decision — promote ADR-009 to `Accepted`?** Nothing in the document blocks it. Until
     it happens, the Draft rule holds: do not implement against it, do not cite it as settled.
  2. **`temporal-full-migration.md` gets its one-pass redraw** — this is the work the owner deferred
     *until the review closed*, and the review has now closed. It contradicts the ADR in five known
     places: its seven numbered steps are the stale numbering 16a replaced, its step 3 is the worker
     port (now second and named), the line-314 diagram assumes the **rejected** NATS bridge, the
     339–351 retry discussion describes a hazard that mostly no longer exists, and its crawl step
     assumes `crawl_queue` retires. 🔴 markers are in place so nobody implements from it meanwhile.
  3. **The conditional-execution PRD needs a number and a backlog row** (14d), and it owes **four**
     things: the Validate-precedent brief and the replay constraint (14c), the halt-early block B
     cannot build for itself (§4), and run-level failure notification (15f). ⚠️ The number is an
     **owner decision, deliberately left open** — creation order gives PRD-019, which sorts *after*
     the PRD-018 it must precede.
  4. **PRD-016 owes four things for one PM pass**: §4's known exclusion; two more R6 divergences
     from §10; two passages still reasoning from §8's reversed storage rule (`PRD-016:697`, `:802`);
     and §14a's sequencing fact.
  5. **Four admin meters need lane scoping** — three webhook (15e) plus `active_recurring_jobs`
     (16c). Not bugs today; migration work, recorded so they are not discovered by a dashboard
     reporting a number that is well-formed and wrong.
  6. **Two open items now visible in the ADR's own deferral table** (D5) — §7's scheduled-quota
     waiting room, and 13d's sitemap origin-restriction question, the latter needed **before the
     crawl step is built**.
  7. **The pre-migration queue is the entry condition for any build work** (16e):
     **P6 → P8 → P7 + BUG-007**, then engine up. `phase4-backlog.md` §1 is its source of truth.

  ### Superseded: START HERE (2026-09-04) — §17 review


  ### §17 (relationship to the earlier ADRs) — reviewed 2026-09-04; the deferral is upheld, and **six of the facts stated around it were wrong**

  §17 was the last numbered section, 12 lines, and mostly a *deferral*: ADR-009 will supersede
  parts of the earlier contracts, but **not yet** — they stay authoritative while v1 serves
  traffic, because a `Superseded` header reads as *do not implement against this* to the person
  maintaining the Go worker next month.

  **That call stands, unchanged.** It is now stated as **this ADR's own call** rather than
  attributed to the index. The index defines *how* a supersession notice is written (status header
  + inline `⚠` markers); it says nothing about *when*, because no previous supersession here
  replaced a contract that stayed live afterwards. §17 said *"per the ADR index's own rule"*, and
  the index's ADR-009 paragraph said *"per ADR-009 §17"* — **each attributed the rule to the
  other, and neither stated it.**

  §17 was 12 lines; it is now 220.

  #### 🔴 1. The ADR-001 entry lists the sections **ADR-002** superseded, and misses all four live ones → **17a**

  §17 said ADR-009 will supersede ADR-001 *"§2 subjects, §3 schemas, §8 MinIO paths."* Those are,
  word for word, ADR-001's own header notice from **2026-04-02**:

  > ADR-002 supersedes §2 (Subjects), §3 (Message Schemas), and §8 (MinIO Path Convention)…
  > Sections §4, §5, §6, §7 remain authoritative.

  So the entry was **a copy of that notice, not an assessment of what this ADR does** — and it
  fails in both directions at once. It points at a document that gave those decisions away four
  months before ADR-009 was drafted (if the migration deletes the subject names, it deletes
  **ADR-002 §2**), and it names none of the four sections that are still live:

  | ADR-001 | what ADR-009 does |
  |---|---|
  | §5 Ack Timing | deleted — on §10's correctly-dissolved list |
  | §6 Retry Policy | deleted — ⚠️ and already false today, see finding 5 |
  | §7 Cancellation | **contradicted by name** — §7 says *no cancellation signal is sent to NATS or the worker*; §15d has the API signal the workflow |
  | §4 Worker Responsibilities | **split** ↓ |

  ```
  ADR-001 §4

    Retry logic                    │ NATS JetStream (via MaxDeliver)   ← deleted
    Update job status in Postgres  │ API (result consumer)             ← rewritten by §11
    Fetch / write MinIO / publish                                      ← unchanged

    "Worker dependencies: NATS + MinIO only. No database access."      ← SURVIVES, permanently
  ```

  ⚠️ **That last line is the most load-bearing sentence in the ADR set for this migration**, and
  this ADR leans on it three times: **§9** keeps it under the activity-worker port, **§8d**
  enforces it *structurally* through task-queue routing rather than convention, and **16b's
  unfixed residual risk exists because of it** — a v1 worker cannot read the lane marker, because
  it cannot read the database at all. A blanket "ADR-001 is superseded" stamp asserts the opposite
  of the rule three sections depend on.

  ✅ **Owner's call: the list is rebuilt per section, and §4 gets a *partial* notice that names the
  surviving rule.**

  #### 🔴 2. `ADR-002 §8` is not a section that exists → **17b**

  The §5 departure (v2 keys artifacts on the run, drops `latest/`) is real and correctly reasoned.
  Its **address is not**: ADR-002 has six sections and its MinIO Path Convention is **§4**. §8 was
  *ADR-001's* number for that decision, carried along when ownership moved in 2026-04-02.

  ```
  ADR-001:164   "⚠ Superseded by ADR-002 §4."     ← right, written 2026-04-02
  ADR-003:65    "See ADR-002 §4 …"                ← right
  … and 16 occurrences of "ADR-002 §8" across SIX files
  ```

  Including `open-bugs.md` (×3) — **the document P6 will be implemented from**. This is 16a's
  failure one document layer up: an address that silently moved, still quoted as if it resolved.

  ✅ **Owner's call: corrected to §4 in every live document** (the ADR, the index, `CLAUDE.md`,
  `open-bugs.md`, `phase4-backlog.md`). ⚠️ **The five occurrences inside this handoff's archived
  session blocks are deliberately left as written** — they are a record of what past sessions
  said, and rewriting them would be editing history rather than fixing an address.

  #### 🔴 3. The trigger names an event two of the documents never reach → **17c**

  *"The supersession notices are added when the corresponding v1 component is deleted."* Against
  §16's now-named steps:

  | document | when it stops being true | a component deletion? |
  |---|---|---|
  | ADR-001 §5 / §6 | per flow, finishing at **NATS removal** | spread across cutovers |
  | **ADR-001 §4's light-worker rule** | **never** | ❌ **no event exists** — and a stamp would assert the opposite of what §9 and §8d rely on |
  | ADR-002 | subjects + pull consumer at NATS removal; result-consumer clause at consumer deletion; MinIO convention when the v1 lane retires | ❌ three different steps |
  | ADR-004 | when the last flow stops publishing — **batch and crawl cutover** | ❌ it belongs to the **stream**, and dies *before* either deletion step |

  §16 had just finished converting addresses into names because the numbers moved once already.
  §17 reintroduced an unnamed one twelve lines later.

  ✅ **Owner's call: every row of the new scope table names a §16 step**, and the cell is left
  empty where a contract survives.

  #### 4. "For as long as v1 serves traffic" is one global switch on a per-flow migration → **17d**

  After the **job cutover**, ADR-002 is authoritative for batch and crawl and **not** for jobs.
  All of §16 is per-flow; this deferral was all-or-nothing. Same lane-blindness as 15e's webhook
  meters and 16c's recurring-jobs tile — in prose rather than SQL, so it fails by being *read*
  wrongly rather than by returning a wrong number. ✅ **Authority is per flow**; same repair as 17c.

  #### 🔴 5. Two contracts §17 protects as authoritative are already false of live code → **17e**

  §17's stated reason for deferring is that a notice *"would mislead anyone maintaining the live
  system."* Two of the contracts it protects **already mislead that person**:

  ```
  ADR-001 §6   "No application-level retry loop is needed in the worker."
  ADR-002 §6   "NATS-managed retries │ MaxDeliver controls retry count;
                no application-level retry loop"      ← re-affirmed as Unchanged
  ```

  Every worker has had one since Q5 / UF-003:

  | worker | where |
  |---|---|
  | LLM | `llm-worker/worker/worker.py:107` `num_delivered`, cap at `:122`, `msg.nak(delay=…)` at `:128` |
  | Playwright | `playwright-worker/worker/worker.py:259`, `:281` |
  | Go http | `http-worker/internal/worker/worker.go:308` `retryDelay(…)` → `NakWithDelay` `:316` |

  The retry **decision** and the backoff ladder are in worker code; `max_deliver` is only the
  backstop. Predates ADR-009 and is not caused by it — but §17 is the section asserting these
  documents are currently authoritative, so it is the section that has to qualify the claim.

  ✅ **Owner's call: recorded here, not fixed on the v1 documents** — ADRs are immutable once
  accepted and are not edited to match drifted code (the index's first rule). The correction rides
  with the supersession notice at NATS removal. ⚠️ It also **reframes §10**: the ported classifier
  is not moving retry *into* the workers, it is moving a retry decision that **already lives
  there** onto a different engine. *The classifier decides; Temporal retries* is continuity, not a
  new hazard.

  #### 🔴 6. ADR-005 and ADR-006 appear nowhere in ADR-009 → **17f**

  Zero occurrences in 3,300 lines, while the ADR decides the fate of both lanes they describe.

  **ADR-005 (crawl BFS coordinator)** is a textbook partial supersession whose four sections **§13
  has already decided** — including two it **upholds by name**:

  | ADR-005 | §13's verdict |
  |---|---|
  | §1 dedicated coordinator process | **deleted** → `CrawlWorkflow` |
  | §2 Postgres `crawl_queue` | ✅ **upheld** — the retire clause was withdrawn |
  | §3 `crawls` / `crawl_pages` | ✅ **upheld** — `crawl_pages` is *required* |
  | §4 crawl NATS subjects | superseded |

  Leaving that unrecorded means the next reader re-derives a **withdrawn** clause — which is
  exactly what 13b found people assume.

  **ADR-006 (batch data model)** loses only §3 (result-consumer routing). Its data model is cited
  as **correct** in BUG-005's root cause — *`job_id` is NULL for batch runs (correct, per
  ADR-006)* — and P6 changes the artifact path, not the tables.

  The heading *"Relationship to ADR-001/002/004"* was itself the defect: a reader concludes 003,
  005, 006, 007 and 008 are unaffected, and for 005 that is wrong.

  ✅ **Owner's call: both brought into scope; the section is renamed *Relationship to the earlier
  ADRs* so the list is not fixed in the heading; and ADR-003 / ADR-007 / ADR-008 are stated as
  unaffected rather than left to inference.**

  ### Session close (2026-09-04)

  **Docs-only session — no code changed.**

  | file | what |
  |---|---|
  | `docs/adr/ADR-009-…` | §17 rewritten with 17a–17f (12 → 220 lines); **section renamed** *Relationship to the earlier ADRs* (anchor changed, the one inbound link updated); a **scope table naming a §16 step per row**; `ADR-002 §8` → **§4** at the three live citations; review log entry; date line |
  | `docs/adr/README.md` | progress → §1–§17 all reviewed; the supersession paragraph rewritten — five ADRs in scope, per-flow authority, named steps, and the four easy-to-get-wrong facts (ADR-001 §2/§3/§8 are ADR-002's; §4's light-worker rule survives; ADR-005's two upheld sections; ADR-006's data model is correct); `§8` → `§4` |
  | `docs/project/phase4-backlog.md` | header entry; ADR row → **section review COMPLETE**, §17 summary appended; `§8` → `§4` (×2) |
  | `CLAUDE.md` | §17 summary appended to the ADR-009 bullet; review progress → closing blocks only; `§8` → `§4` |
  | `docs/project/open-bugs.md` | `ADR-002 §8` → `§4` (×3) — **BUG-005's writeup, which P6 is implemented from** |
  | this handoff | §17 block; §16 demoted to superseded |

  ✅ **Committed as `87d2396`.** Working tree clean apart from an untracked `tmp/architecture.md`,
  which predates this session (May) and was deliberately left alone.

  ⚠️ **Knock-on outside §17:** the `ADR-002 §8` → `§4` correction touched **four files beyond the
  ADR**. Anyone holding an earlier note that the MinIO path convention is "ADR-002 §8" should read
  it as **§4**; §8 is ADR-001's superseded version of the same decision.

  ⚠️ **Git state.** Deployed code is still `b110591`; `5c7fbdf` (the crawl page status-filter fix)
  is still **committed on `develop`, not deployed, not on `main`**. Everything on top is docs.
  ✅ **The owner's call of 2026-08-28 stands: `main` is deliberately NOT fast-forwarded** — pushing
  it starts a push to the prod server, so a fast-forward is a **release**, not a tidy-up. Do not do
  it at session end; wait to be asked. *(Re-check the ahead/behind counts against the remote before
  quoting them — the last `git fetch` was 2026-07-13, and two consecutive handoffs carried stale
  numbers.)*

  **What is blocking: nothing.** The section review is finished. Outstanding:

  1. **The closing blocks close the review** — **Consequences** and **Deliberately not decided
     here**. ⚠️ They are the two blocks most likely to be stale, because they were written
     **before** the reviews that reversed §8, §9 and §12 and amended §4, §5, §7, §2d and §16.
     Things to check against the sections rather than re-read in isolation: the Deliberately-not-
     decided table still lists the **crawl frontier model** as open when 13b decided the frontier
     stays in Postgres and `continue-as-new` is mandatory either way (only the table's *shape* is
     open); **run-failure notification** is listed as unassigned when 15f assigned it to the
     conditional-execution PRD; and the Consequences' *"two orchestration systems… no longer sit on
     top of each other"* bullet should be checked against 16b's unacked-message hazard.
  2. **The conditional-execution PRD needs a number and a backlog row** (§14d), and it owes
     **four** things: the Validate-precedent brief and the replay constraint (14c), the halt-early
     block B cannot build for itself (§4), and run-level failure notification (15f). ⚠️ The number
     is an **owner decision, deliberately left open** — creation order gives PRD-019, which sorts
     *after* the PRD-018 it must precede.
  3. **PRD-016 owes four things for one PM pass** (unchanged): §4's known exclusion; two more R6
     divergences from §10; two passages still reasoning from §8's reversed storage rule
     (`PRD-016:697`, `:802`); and §14a's sequencing fact.
  4. **Four admin meters need lane scoping** — three webhook (15e) plus `active_recurring_jobs`
     (16c). Not bugs today; migration work, recorded so they are not discovered by a dashboard
     reporting a number that is well-formed and wrong.
  5. **`temporal-full-migration.md` still contradicts the ADR** — its seven numbered steps are the
     stale numbering 16a replaced, step 3 is the worker port (now second and named), the line-314
     diagram assumes the rejected NATS bridge, the 339–351 retry discussion describes a hazard that
     mostly no longer exists, and its crawl step assumes `crawl_queue` retires. ✅ Owner's call
     stands: **redraw in ONE pass after the review closes** — which is now one step away. 🔴
     markers are in place so nobody implements from it meanwhile.
  6. **One open item from §13**, needed before the crawl step is built: whether sitemap entries are
     **origin-restricted** like extracted links.
  7. **A §17 obligation for implementation time, not now:** each supersession notice is pinned to a
     named §16 step, so **whoever executes that step writes the notice** — including the *partial*
     one on ADR-001 §4 that must name the surviving light-worker rule rather than stamping the
     section.

  ### Superseded: START HERE (2026-09-03) — §16 review

  ### §16 (the v1/v2 coexistence contract) — reviewed 2026-09-03; the decision is upheld, but five of its **instructions** were stale, and three of those were **addresses that had silently moved**

  The plan stands exactly as drafted. Temporal comes up **beside** NATS; **pipelines are v2-only
  from the day they exist** (there is no v1 pipeline, so layer A **adds** a lane rather than
  splitting one); jobs, batches and crawls stay on v1 until their flow is explicitly migrated;
  deletion is last and gated. Nothing about that changed.

  What did not survive is the part of §16 that tells someone **what to do** — the sequence, the
  gate and two of the three cutover obligations. §16 was 56 lines; it is now 277.

  #### 🔴 1. The sequence was renumbered and the references into it were not → **16a**

  §16 said, at line 2985:

  ```
  "…become a real problem only at migration step 2, when jobs move to JobWorkflow"
  ```

  and then, twenty lines later, listed a sequence in which **step 2 was the worker port** and jobs
  were **step 4**. Both sentences are in the same section, one screen apart.

  The cause is mechanical, not careless: **the §9 review (2026-08-23) reordered the sequence and
  updated the list without updating the references into it.** And "step 2" is not prose — three
  places use it as an *address*:

  | who | what they mean | where "step 2" pointed after the reorder |
  |---|---|---|
  | **§7**, five times | build the lane marker on `job_runs` — **a schema change** | the **worker port**, which has no `job_runs` rows to mark |
  | **§2d** | "both orchestrators running at steps 2–3" | worker port + pipeline lane |
  | `temporal-full-migration.md` §9 | its own 7-item list, **never renumbered** | its step 2 is still the **rejected** NATS bridge |

  ⚠️ **Both failure directions are silent.** Build the marker "at step 2" and you build it during
  the worker port, where it is inert and untestable. Read §16's list instead and you conclude step
  2 is done, then reach the job cutover believing the marker was handled. Neither produces an error.

  ✅ **Owner's call: the sequence is NAMED, not numbered.**

  ```
  engine up → worker port → pipeline lane → job cutover → batch and crawl cutover
            → schedule and webhook cutover → consumer deletion → NATS removal → API thinning
  ```

  Names do not renumber when a step is inserted, moved or split — and this sequence has already
  been reordered once by a review and will be again. §7 and §2d corrected in place;
  `temporal-full-migration.md` inherits the names in its post-review redraw. Also caught while
  counting: the section said the shape *"changes at four of the **seven** steps"* while listing
  **nine** — the seven was the migration doc's list, quoted from before the reorder.

  #### 🔴 2. The drain gate is a **cutover** gate, and §16 described only the deletion half → **16b**

  This is not two sections disagreeing. It is one section that was never amended. **§7 already
  says it**, in these words:

  > The check that does is already written as §16's **deletion gate** … it is a **cutover gate
  > too**, not only a deletion gate, and the two sections should be read together.

  §16 said *"A v1 component is **deleted** when its flow is fully drained and its NATS consumers
  report zero unprocessed and zero outstanding acks."* One firing point, at the very end. So the
  document an implementer works from carried the deletion-only version while the section that
  **depends** on the gate asserted §16 said something it did not.

  What the gate is actually for — `--retention work` deletes a message once **acked**, so there is
  no replayable backlog. The risk is the message not yet acked when a flow is routed to v2:

  ```
  10:00:00  user submits a job → API writes job_runs row → publishes to NATS
  10:00:01  the message sits on the stream, unacked
  10:00:02  ✂  the job flow is routed to v2
  10:00:03  Temporal starts JobWorkflow for that run
  10:00:05  the v1 NATS worker — still alive, per obligation 3 — consumes the message
            it was already handed, and scrapes

            → the target site is scraped twice
            → the user's own LLM key is billed twice
            → the v1 result returns to result_consumer.py, which resolves the run by id
              and OVERWRITES the state of the run Temporal is still executing
  ```

  🔴 **None of §7's four mechanisms reaches this**, and each one looks like it should:

  | mechanism | why it misses |
  |---|---|
  | 1 · disjoint identity | operates on **rows**; this is a message already in flight |
  | 2 · workflow-ID uniqueness | v1 started no workflow — it consumed a message |
  | 3 · `schedule_status` interlock | one-off submission, not a schedule |
  | 4 · lane marker on `job_runs` | ⚠️ **workers hold no DB access at all** (ADR-001's light-worker rule, still true under §9) — a v1 worker cannot read the marker and cannot know it has been sidelined |

  Mechanism 4 stops the *dispatcher* re-publishing. Nothing stops a message already handed out.

  ✅ **Owner's call: the gate fires at every flow cutover as well as at deletion**, written into
  the gate paragraph rather than left as a cross-reference. ⚠️ **Residual recorded, not solved:**
  the worse half is not the wasted scrape, it is the **write** — §11a's precedence rule guards a
  *cancellation* written by the API, and `result_consumer.py:613` guards `cancelled` specifically;
  neither refuses a stale v1 result for a run the other lane now owns.

  #### 🔴 3. Obligation 2 borrows a user-facing switch, and one live meter reads it → **16c**

  "Pause the recurring job in v1 first" means writing `schedule_status = 'paused'`. That order is
  right (a missed firing beats a doubled one). What no section noticed is **what that flag is**:

  ```
  schemas/jobs.py:114   JobPatch.schedule_status        ← the user can write it
  routers/jobs.py:465   generic setattr loop            ← no lane awareness, no guard
  admin.py:494          COUNT(*) WHERE schedule_status = 'active'
  schemas/admin.py:61   active_recurring_jobs
  UsageStats.tsx:67     <Stat label="Recurring jobs" …> ← rendered today
  ```

  **Consequence 1 — a live meter reports the migration as a decline.** As recurring jobs move to
  Temporal Schedules the tile counts down toward **0** and the "next scheduled run" figure goes
  null, while every one of those jobs fires normally on v2. Nothing errors. ⚠️ This is the
  **fourth** instance of one defect in this ADR — BUG-005 (batch invisible because `job_id` is
  NULL), P7 (crawls invisible because every meter reads `job_runs`), 15e (webhook success blind to
  the pipeline lane) — and **the first caused by an instruction in the ADR** rather than found in
  existing code.

  **Consequence 2 — the interlock is user-reversible in one request.**

  ```
  you   :  schedule_status = paused    +  create the Temporal Schedule
  user  :  PATCH {"schedule_status": "active"}     ← legal, owner-scoped, 200 OK
  now   :  v1 fires it  AND  v2 fires it
           → the double scrape and double LLM bill R5 requires be STRUCTURALLY prevented
  ```

  Obligation 1 gets structural treatment. Obligation 2 rests on a switch the user owns. *(One
  accidental mitigation: `schedule_status` is absent from `JobResponse`, so the user cannot read
  it back and has nothing prompting them to flip it.)*

  ✅ **Owner's call: both recorded as obligations of the schedule and webhook cutover; neither
  fixed on the v1 path.** The meter fix is **naming, not features** (the 15e precedent). The
  deeper point: `schedule_status` is doing two unrelated jobs — *the user's intent* and *which
  engine owns this schedule* — which is the overloading Q8 came from, and the same shape as
  `nats_stream_seq` being "a lane marker in disguise".

  ⚠️ **Dormant is not safe.** There are no scheduled jobs in production, so the tile reads 0
  before and after and the defect is invisible for exactly 13d's reason — the feature is dormant,
  not correct. **Monitors (layer B) is entirely about recurrence**, so the population this breaks
  arrives with it.

  #### 4. "Every step is reversible" is false for the increment that ships first → **16d**

  §16's safety net was *"a misbehaving flow falls back to the v1 path until fixed."* Its own
  opening paragraph says pipelines have **no v1 implementation**.

  ```
  Worker port          broken? → delete the Temporal-bound deployment;
                                 the NATS-bound one keeps serving        ✅ v1 fallback
  Job cutover          broken? → route jobs back to the v1 path          ✅ v1 fallback
  Pipeline lane (R6)   broken? → there is no v1 pipeline.
                                 Fallback = switch the feature off       ❌
  ```

  The section recorded the **favourable** half of "adds a lane" (nothing to double-execute, so the
  top cutover risk is near zero at the first increment) and then made a blanket promise the *same
  fact* contradicts:

  | | double-execution risk | rollback target |
  |---|---|---|
  | **adding** a lane (pipelines) | ~none | ~none |
  | **moving** a lane (jobs, batches, crawls) | high | the v1 path |

  ✅ **Owner's call: narrowed, not dropped** — reversibility is a property of **migrated** flows.
  Consequence worth carrying: **R6 runs on a lane with no fallback**, which makes §9's standalone
  pre-gate (run the Scrape activity alone and diff it against a v1 run of the same URL) a
  **requirement rather than a nicety** — and it is 13a's crawl exposure arriving one lane earlier
  than that section expected.

  #### 5. The sequence starts after work this ADR moved in front of it → **16e**

  The sequence opens at "stand up Temporal". Three items sit before it and are on no list here:

  | | what | why the pipeline lane needs it |
  |---|---|---|
  | **P6** | BUG-005 — batch broken on all three paths | not a dependency; a live silent defect the queue puts first |
  | **P8** | the shared per-object storage ledger (§8d) | **a dependency** — the v2 charging activity writes ledger rows; without the table a pipeline run stores objects and charges nothing |
  | **P7** | crawls join the run-counting view | same table, and the view is what lets *any* new lane be counted |

  Without the counting view, pipeline runs consume none of the three meters **by construction** —
  which is P7's own bug, reproduced on a brand-new lane by a plan written to prevent it.

  The cause is dating: the sequence was last touched by the §9 review on **2026-08-23**, and §8d
  moved the ledger to pre-migration on **2026-08-25** — two days later, recording the knock-on as
  *"filed as P8"* without carrying it back into the contract that states the order of work.

  ✅ **Owner's call: the pre-migration queue is the sequence's entry condition** —
  **P6 → P8 → P7 + BUG-007**, then engine up. Not restated item by item;
  `phase4-backlog.md` §1 stays the source of truth for its contents.

  ### Owner's note on scale (2026-09-03)

  Raised in session and worth not re-deriving: the platform currently serves **one user, few jobs,
  and no scheduled jobs at all**. That changes the *urgency ordering* of these findings and
  **nothing about whether they are recorded** — §16 is the section this migration plan is judged
  by, and it is written for a system with users.

  | finding | scale-dependent? |
  |---|---|
  | 16a step names · 16d reversibility · 16e entry condition | **No** — wrong at any volume, and 16e blocks the first increment |
  | 16b drain gate | **No in kind, yes in cost** — one unacked message is enough; low volume makes the *fix* nearly free (the queue is empty most of the time), not the risk absent |
  | 16c meter + interlock | **Yes today, no soon** — dormant because nothing is scheduled, and Monitors is entirely about recurrence |

  ### Session close (2026-09-03)

  **Docs-only session — no code changed.**

  | file | what |
  |---|---|
  | `docs/adr/ADR-009-…` | §16 rewritten with 16a–16e (56 → 277 lines); **the sequence is now a named table, not a numbered list**; **§7's five "step 2" references and §2d's "steps 2–3" renamed**; review log entry; date line |
  | `docs/project/phase4-backlog.md` | header entry; ADR row → §1–§16 reviewed, §16 summary appended |
  | `docs/project/temporal-full-migration.md` | **three new 🔴 markers** in the banner — its step *numbers* are stale as addresses, its sequence does not begin where work begins (the pre-migration queue is the entry condition), and its crawl step still assumes `crawl_queue` retires |
  | `docs/adr/README.md` | index progress note was stale at "§1–§13 reviewed" — now §1–§16 |
  | `CLAUDE.md` | §16 summary appended to the ADR-009 bullet; review progress → §17 |
  | this handoff | §16 block; §15 demoted to superseded |

  ✅ **Committed as `400cda7`.** Working tree clean apart from an untracked `tmp/architecture.md`,
  which predates this session (May) and was deliberately left alone.

  ⚠️ **Note the knock-on edits outside §16** — this session changed **§7 and §2d**. Anyone holding
  an earlier session's note that mechanism 4 is "step-2 work" should read it as **job-cutover
  work**; the numbering it referred to no longer exists.

  **All 141 ADR internal links re-verified — none broken.** Slug rule that works: lowercase → drop
  everything that is not **alphanumeric, underscore, space or hyphen** → replace **each** space
  with a hyphen. **No whitespace collapsing** (which is why ` — ` becomes `--`), and **keep
  underscores**.

  ⚠️ **Git state.** Deployed code is still `b110591`; `5c7fbdf` (the crawl page status-filter fix)
  is still **committed on `develop`, not deployed, not on `main`**. Everything else on top is
  docs. ✅ **The owner's call of 2026-08-28 stands: `main` is deliberately NOT fast-forwarded** —
  pushing it starts a push to the prod server, so a fast-forward is a **release**, not a tidy-up.
  Do not do it at session end; wait to be asked.

  **What is blocking: nothing blocks §17.** Outstanding:

  1. **§17 is next** — Relationship to ADR-001/002/004 — then the closing **Consequences** and
     **Deliberately not decided here** blocks, which close the review. ⚠️ §17 is short and is
     mostly a *deferral* (those ADRs stay authoritative while v1 serves traffic; supersession
     notices are added when each v1 component is deleted), so the thing to check is whether the
     deletion trigger it names still matches §16's renamed steps.
  2. **The conditional-execution PRD needs a number and a backlog row** (§14d), and it owes
     **four** things: the Validate-precedent brief and the replay constraint (14c), the halt-early
     block B cannot build for itself (§4), and run-level failure notification (15f). ⚠️ The number
     is an **owner decision, deliberately left open** — creation order gives PRD-019, which sorts
     *after* the PRD-018 it must precede.
  3. **PRD-016 owes four things for one PM pass** (unchanged): §4's known exclusion; two more R6
     divergences from §10; two passages still reasoning from §8's reversed storage rule
     (`PRD-016:697`, `:802`); and §14a's sequencing fact.
  4. **Three admin webhook meters need scoping** (15e) — and **now a fourth**, `active_recurring_jobs`
     (16c). Neither set is a bug today; both are migration work, recorded so they are not
     discovered by a dashboard reporting a number that is well-formed and wrong.
  5. **`temporal-full-migration.md` still contradicts the ADR** — and §16 adds to the list: its
     step 3 is the worker port (now second and named), the line-314 diagram assumes the rejected
     NATS bridge, the 339–351 retry discussion describes a hazard that mostly no longer exists,
     its crawl step assumes `crawl_queue` retires, and **its seven numbered steps are now the
     stale numbering 16a replaced.** ✅ Owner's call stands: **redraw in ONE pass after the review
     closes.** 🔴 markers are in place so nobody implements from it meanwhile — **three more were
     added this session** (stale step numbers, the missing entry condition, and the crawl step).
  6. **One open item from §13**, needed before the crawl step is built: whether sitemap entries are
     **origin-restricted** like extracted links.

  ### Superseded: START HERE (2026-09-02) — §15 review

  ### §15 (webhook delivery) — reviewed 2026-09-02; the decision holds, but **two of its supporting statements could not be built as written**

  Option (c) stands unchanged: the Webhook block **waits for real delivery** on its own ≈2.6 hour
  durable horizon, and there is **no `webhook_deliveries` row on the v2 lane**. Both rejections
  hold, and one was verified at the database — a pipeline delivery row is refused by **two `CHECK`
  constraints plus a `run_id` foreign key into `job_runs`**, so option (b) was never the cheap
  reuse it looked like.

  ✅ **The ≈2.6 h figure was verified against production**, not just the repo:
  `WEBHOOK_MAX_ATTEMPTS: "5"` in the infra repo's `app/api.yaml` matches `settings.py`. Checked
  deliberately, because the §10 review found `llm_request_timeout_seconds` overridden in
  production (60 in code, 180 deployed) and the repo default therefore misleading. **That trap
  does not repeat here.**

  Six findings. §15 was 47 lines; it is now 342.

  #### 🔴 1. The argument that answers the PM's only objection has no column to read → **15a**

  §15's third bullet is what makes option (c) survivable: *a run asleep on a timer doesn't hold a
  concurrency slot.* §8 made that an owner's call on 2026-08-17. **But it has to execute as a SQL
  predicate**, because the concurrency check runs in the API when a run starts. So: what does that
  query read?

  ```
  run A — inside a 4-minute LLM call
    pipeline_run_blocks  [Scrape ✅][Clean ✅][LLM  running][Webhook pending]

  run B — 40 minutes into a 2.6-hour webhook backoff
    pipeline_run_blocks  [Scrape ✅][Clean ✅][LLM ✅][Webhook  running]
  ```

  **Indistinguishable.** One holds a worker and a paid API key; the other is a timer. And the
  vocabulary cannot separate them: §4 fixed block state at
  `pending`/`running`/`completed`/`failed`/`skipped` — no waiting value — and run outcome at
  `completed`/`failed`/`cancelled`, three *terminal* values with no in-flight value enumerated at
  all. So the objection arrives anyway: **five runs parked on a dead receiver lock a user out of
  five slots for two and a half hours.**

  🔴 **The owner's question is what settles it.** The tempting predicate is *"a running Webhook
  block doesn't count"* — special-case the one type that waits. That is **wrong, not merely
  fragile**, and layer C is where it breaks: its sinks deliver to **the user's own S3, the user's
  own database, the user's own mail server** — infrastructure exactly as unavailable as a webhook
  receiver, wanting exactly the same long horizon.

  ```
  run A   [ S3 sink  running ]   parked 40 min, holding nothing  →  counts
  run B   [ Webhook  running ]   parked 40 min, holding nothing  →  does not count
  ```

  Same situation, opposite answer, no explanation a user could be given — and it fails in the
  worse direction, with the sink locking the user out of their own quota. **The distinction is not
  which block it is; it is whether the run holds worker capacity at this instant** — which flips
  back and forth *inside* one block (holding during each 10s POST, holding nothing across each
  backoff sleep).

  ✅ **Owner's call, both halves:**

  | | |
  |---|---|
  | **which blocks may wait** | §5's existing **content-producing / effect** split — no block type is ever enumerated, so a new layer-C sink inherits the behaviour by being an effect block |
  | **how it is recorded** | **`waiting` is pre-admitted to `pipeline_run_blocks.status` now**, for the reason §4 pre-admitted `skipped`: Monitors will park a run on a sleep belonging to **no block at all**, which the category rule cannot cover. A value today is a line in a `CHECK`; a value later is a migration plus a backfill |

  The counting view's v2 arm becomes **"a run with at least one block in `running`."** §4 and §8
  amended to match.

  ⚠️ **Third missing value found in that one vocabulary in three review sessions** — `skipped`
  (pre-admitted by §4), *"succeeded then undone"* (§14, saga rollback), `waiting` (here). The last
  two share a cause: the vocabulary describes a block's **progress**, while both callers need what
  the run is **doing with resources**.

  ⚠️ One accepted interaction: whoever writes `waiting` writes it at every timer boundary, and
  §11c decided **a failed mirror write fails the run** — so a delivery that would have succeeded
  can fail because a status write failed mid-backoff. Accepted, not special-cased.

  #### 🔴 2. "Reproduces today's behaviour exactly" contradicts §15's own first bullet → **15b**

  The first bullet says the delivery loop **is the activity's retry policy**. The second says the
  horizon reproduces today's reach **exactly**. Both cannot be true, because today's backoff is an
  explicit list and **not a curve**:

  ```
  today          30s  →  300s  →  1800s  →  7200s
  ratio                 ×10      ×6        ×4        ← not geometric
  ```

  A Temporal retry policy takes four numbers — first interval, multiplier, ceiling, maximum
  attempts. **There is no way to hand it a list.** Best fit, and what should be configured:

  ```
  initial 30s · ×10 · ceiling 7200s · maximum_attempts 5

                attempt 1   attempt 2   attempt 3   attempt 4   attempt 5
  today            0s         30s        5m 30s     35m 30s     2h 35m
  retry policy     0s         30s        5m 30s     55m 30s     2h 55m
                   same       same       same       ✗ +20m      ✗ +20m
  ```

  Same number of POSTs, ~13% longer horizon, attempts 4–5 displaced. The exact alternative —
  explicit durable sleeps around a **non-retrying** activity — **rebuilds the delivery loop in the
  workflow**, which is the thing this section says it is not doing.

  ✅ **Owner's call: keep the retry policy, drop "exactly", record the drift** — so an unqualified
  claim is not quoted back during the R6 comparison as a failed gate.

  #### 3. The horizon lives in one of four nested timeouts, set in three files → **15c**

  Today there is one number (`timeout=10.0`) plus a loop we wrote. Temporal replaces the loop with
  configuration, and **the smallest of four nested limits silently wins**:

  | | measures | value | set in |
  |---|---|---|---|
  | 1 · POST timeout | one HTTP request | **10s** | the activity's own code |
  | 2 · `start_to_close` | **one attempt** | **~20s** | the workflow, at the call site |
  | 3 · `schedule_to_close` | **all attempts + all the sleeping between them** | **≥ 2.6 h** ← *the horizon* | the workflow, one line later |
  | 4 · the run's R4 time budget | the whole pipeline run | **> 2.6 h + every other block** | pipeline / operator config |

  Two silent failures, both named in the section now. **The natural one:** you want 2.6 hours, so
  you set it on `start_to_close` — the timeout everyone reaches for — and a single POST to a
  receiver that never answers is allowed to hang for 2.6 hours, one attempt instead of five.
  **The Q6 one:** somebody sets *"no pipeline run may exceed one hour"* in a different file, for a
  different reason, knowing nothing about webhooks; four attempts happen, the reach becomes ~56
  minutes, and nothing errors. That is Q6's exact shape — NATS `ack_wait` at 30s under a 60s+ LLM
  call, where the symptom pointed nowhere near the setting. R4 already requires budgets to
  compose; **this is the first concrete place they must.**

  #### 🔴 4. Cancel now takes up to 2.6 hours, and a cancelled run still delivers → **15d**

  Two decisions collide, neither written knowing about the other: the PM's rule that
  **cancellation never aborts a block mid-execution** (priced when blocks lasted minutes), and
  §15 introducing **the first block measured in hours**.

  ```
  14:00  Webhook block starts; receiver down; retries begin
  14:05  user clicks Cancel → API writes cancelled, page greys out instantly   (§11a)
                            → the workflow is not told
  14:35  attempt 4  ← POSTing for a run cancelled thirty minutes ago
  16:35  attempt 5 succeeds → the customer's system is told the run finished
  ```

  Impossible today, and not by luck: the delivery row is created **when the run completes**, so a
  cancelled run has no delivery. Note §11a's precedence rule protects the **status column** — it
  says nothing about the outbound HTTP request, which is what the user's customer sees.

  ✅ **Owner's call: the API sends a cancel to the workflow *in addition to* writing the row** —
  row first, signal best-effort, so §11a's instant-UI property is untouched and a Temporal outage
  degrades gracefully. Temporal interrupts durable sleeps natively at await points, so this is
  nearly free **once the workflow is told**. The backoff-boundary re-check is the fallback, not
  the primary: alone it only turns 2.6 hours into 2 hours, because the last backoff step is
  7200s. **Not webhook-specific** — Monitors' multi-day sleeps are the identical problem.

  #### 🔴 5. "No delivery table on v2" removes live capability and blinds three meters → **15e**

  The section treats `webhook_deliveries` as a *record*. It is also the backing store of live API
  surface:

  ```
  GET  /admin/webhooks/deliveries              list, filter by status   (admin.py:367)
  POST /admin/webhooks/deliveries/{id}/retry   attempts = 0, re-queue   (admin.py:387)
  ```

  ✅ **Owner's call: both become job-lane-only, deliberately and permanently.** The reasoning is
  recorded so it does not read as an omission: manual retry mattered **because failure was
  invisible** — today a delivery exhausts at 3 a.m. into a table nobody watches. Under (c) the
  **run itself fails**, in the user's own run list, and they can re-trigger it. Visibility moves
  from a hidden admin table to the person who cares. The endpoints are **not deleted** — they keep
  serving the job lane — so the accurate statement is *"job lane only, by design"*, which stops a
  later reader filing the gap as a bug and closing it by widening the table.

  ⚠️ **What may not simply be left.** Three meters read that table with **no lane filter**:

  ```
  webhook_deliveries_pending          ← rendered on the admin Usage page today
  webhook_deliveries_exhausted
  webhook_delivery_success_rate_7d
  ```

  After pipelines ship the dashboard reports **webhook success rate 100%** while every pipeline
  delivery fails. Nothing errors; the number is well-formed and wrong. **That is this project's
  recurring defect** — a meter keyed on a table a new lane doesn't write to. It is BUG-005 (batch
  invisible because `job_id` is NULL), it is P7 (crawls invisible because every meter reads
  `job_runs`), and it is why §3 moved run counting onto a view. Declining to build pipeline
  delivery stats is fine; leaving a lying meter is not, and **the fix is naming, not features**.

  Also withdrawn from the first bullet: *"and the Web UI shows it"* — §2b does not expose it. To
  be fair to the section, per-attempt **detail** is parity (today's row also keeps only `attempts`
  and `last_error`); the real losses are **retention** (a row lives until its parent is deleted;
  Temporal history is 30 days per §2c) and **reach**.

  #### 🔴 6. The failure-notification obligation has no home → **15f**

  Today **any** failure notifies — dead scrape, bad LLM key, anything
  (`result_consumer.py:563–575`) — because delivery is triggered by the run's *outcome*, not by a
  position in the recipe. In a pipeline the Webhook block is a step in a chain: an earlier
  terminal failure stops the chain, the block never runs, **and nobody is told.**

  PRD-016 records this and passes it on — settled *"when conditional execution is settled"*. Then
  §14, reviewed **one session earlier**, created that PRD and enumerated exactly what it owes —
  the Validate precedent, the replay constraint, the halt-early block — **and failure notification
  is not on the list.** Handed to a document whose obligations were written down a week later
  without it. ✅ Now added.

  The sharpest form is a configuration, not a behaviour. `webhook_events` is a live user-facing
  setting validated against `{job.completed, job.failed, crawl.completed, batch.completed}`, so a
  user may save:

  ```json
  { "webhook_events": ["job.failed"] }        "only tell me when it breaks"
  ```

  **That job has no expressible pipeline equivalent in layer A at all.** Not a divergence in
  outcome — an entire configuration that cannot be migrated.

  #### Rider: the SSRF refusal

  §15 keeps SSRF re-validation **per attempt** (DNS rebinding is why), which is right and
  unchanged by the transport. Two things added: it must be raised **non-retryable** — this is
  §10's second non-retryable obligation, and it lands here, because a retryable SSRF refusal
  becomes ≈2.6 hours of re-resolving a hostname an attacker is actively rebinding — and its
  **consequence is larger on this lane**: today it marks the delivery `exhausted` and never
  touches the job's outcome; under (c) **the run fails**, because a URL saved weeks ago resolves
  somewhere new today. Inside the recorded R6 divergence, but the one failure a user cannot fix by
  bringing their server back up.

  ### Session close (2026-09-02)

  **Docs-only session — no code changed.**

  | file | what |
  |---|---|
  | `docs/adr/ADR-009-…` | §15 rewritten with 15a–15f (47 → 342 lines); **§4 gains `waiting` in the block vocabulary**; **§8's v2 concurrency rule amended** to name the column it reads; two stale vocabulary enumerations marked as of-their-date (the §14 review-log entry, and 15a's own narrative); review log entry; date line |
  | `docs/project/phase4-backlog.md` | header entry; ADR row → §1–§15 reviewed, §15 summary appended |
  | `CLAUDE.md` | §15 summary appended to the ADR-009 bullet; review progress → §16–§17 |
  | this handoff | §15 block; §14 demoted to superseded |

  ✅ **Committed as `9b2df55`** — one commit covering **two** sessions, because the §14 session
  (2026-09-01) closed without committing and its edits sit in the same files. That commit
  therefore also carries **`docs/project/workflows-scoping.md`** (banner amended, 🔴 markers on
  §4A, §5 and §6), which is §14's work, not §15's. Nothing in either session is code.
  Working tree is clean apart from an untracked `tmp/architecture.md`, which predates both
  sessions (May) and was deliberately left alone.

  ⚠️ **Note the two knock-on edits inside the ADR** — this session changed **§4 and §8**, not only
  §15. §4's block-state vocabulary is now six values (`waiting` added), and §8's v2 concurrency
  bullet no longer asserts a rule with no column behind it. Anyone reading §4 or §8 from an
  earlier session's notes has a stale vocabulary.

  **All 121 ADR internal links re-verified — none broken.** The slug rule that works: lowercase →
  drop everything that is not **alphanumeric, underscore, space or hyphen** → replace **each**
  space with a hyphen. **No whitespace collapsing** (which is why ` — ` becomes `--`), and
  **keep underscores** (a checker that drops them reports `#13c-crawl_pages-…` as broken when it
  is fine).

  ⚠️ **Git state — the counts carried by the last two handoffs were stale; corrected here.**
  Deployed code is still `b110591`, and `5c7fbdf` (the crawl page status-filter fix) is still
  **committed on `develop`, not deployed, not on `main`** — those two facts are unchanged. But the
  local `origin/develop` ref stands at **`205acd4`**, so `develop` is **1 ahead of it**
  (`d6f9ce6`), not the "8 ahead" the last two sessions recorded; `main` is **26 behind**.
  *(Last `git fetch` was 2026-07-13, so that remote ref reflects pushes rather than a recent
  fetch — re-check against the remote before relying on it.)* ✅ **The owner's call of 2026-08-28
  stands: `main` is deliberately NOT fast-forwarded** — pushing it starts a push to the prod
  server, so a fast-forward is a **release**, not a tidy-up. Do not do it at session end; wait to
  be asked.

  **What is blocking: nothing blocks §16.** Outstanding:

  1. **The conditional-execution PRD needs a number and a backlog row** (§14d) — and it now owes
     **four** things, not three: the Validate-precedent brief and the replay constraint (14c), the
     halt-early block B cannot build for itself (§4), and **NEW — run-level failure notification**
     (15f), which PRD-016 assigned to it and §14 enumerated without. ⚠️ The number is still an
     **owner decision, deliberately left open** — creation order gives PRD-019, which sorts *after*
     the PRD-018 it must precede.
  2. **PRD-016 owes four things for one PM pass** (unchanged from last session): §4's known
     exclusion; two more R6 divergences from §10; two passages still reasoning from §8's reversed
     storage rule (`PRD-016:697`, `:802`); and §14a's sequencing fact, which does not belong in
     that PRD's Non-goals.
  3. **Three admin webhook meters need scoping** (15e) — `webhook_deliveries_pending`,
     `webhook_deliveries_exhausted`, `webhook_delivery_success_rate_7d`. **Not a bug today** and
     not filed as one: they are correct until the pipeline lane exists. It is migration work, and
     it is recorded in §15e so it is not discovered by a dashboard reporting 100%.
  4. **`temporal-full-migration.md` still contradicts the ADR** — step 3 is the worker port (now
     step 1), the line-314 diagram assumes the rejected NATS bridge, the 339–351 retry discussion
     describes a hazard that mostly no longer exists, and its crawl step assumes `crawl_queue`
     retires. ✅ Owner's call stands: **redraw in ONE pass after the review closes.** 🔴 markers
     are in place so nobody implements from it meanwhile.
  5. **One open item from §13**, needed before the crawl step is built but not before §16–§17:
     whether sitemap entries are **origin-restricted** like extracted links.
  6. **§16 is next** (the v1/v2 coexistence contract) — `ADR-009` line ~2972, running to §17
     (Relationship to ADR-001/002/004) and then the closing **Consequences** /
     **Deliberately not decided here** blocks. Two things already point *into* §16 and should be
     checked against it rather than rediscovered: **§9's reversal moved the worker port from third
     to first in the sequence**, and **§7's mechanism 4** (a lane marker on `job_runs`, written in
     the insert transaction) is owed at **migration step 2**. §16 is also where the drain gate
     lives, which §7 established is a **cutover** gate too, because `--retention work` only covers
     acked messages.

  ### Superseded: START HERE (2026-09-01) — §14 review

  ### §14 (conditional execution) — reviewed 2026-09-01; the decision holds, but the section stated a **sequencing** decision in a notation that could not carry one

  The call is upheld exactly as drafted: conditional execution gets **its own follow-up layer-A
  PRD, written before PRD-018 (Monitors), not absorbed into B.** The argument for it is sound and
  is the whole point of the section — *if B absorbs it, the reviewer of B's PRD is redesigning
  layer A's block model under a heading that does not say so.* The work does not disappear; it
  gets renamed and hidden.

  Four gaps, none of them in the decision itself. §14 was 26 lines; it is now 206.

  #### 🔴 1. The order is two orders → **14a**

  The section's ordering line was:

  ```
  A ships  →  C (Delivery) in parallel  →  conditional-execution PRD  →  B
  ```

  That chain mixes a **shipped product**, a **document** and a **layer** in one sequence of
  arrows — and a sequencing decision has exactly one job, which is telling *written* apart from
  *built*. It could not answer "when do we actually build this?", which is the first question
  anyone reading it asks.

  | | when |
  |---|---|
  | conditional PRD **written** | before PRD-018 is written; **may be written during A's build** — writing it early is what tests whether the four forward-compatibility choices actually suffice |
  | conditional execution **built** | **after layer A ships.** This was only ever stated in PRD-016's Non-goals ("the first thing to add after it ships") — a sequencing fact in a list of exclusions |
  | …and **before B is built** | forced, not chosen: the PM made the cost gate a **launch requirement** of Monitors, and the gate consumes this primitive |
  | PRD-017 (C) | independent of the chain — but see finding 2 for what "independent" was *not* checked against |

  ✅ **The defence of "after, not alongside" is now stated rather than assumed.** The cost gate's
  purpose is to skip the LLM call and the webhook when the page is byte-identical to last time.
  That saves nothing unless the same pipeline runs against the same URL repeatedly — and **layer A
  has no scheduling**; recurrence, durable sleeps and timers are all Monitors. So building it into
  layer A means building for a consumer that does not exist yet. It is the same fact that makes
  the accepted repeat-run cost delta (one LLM call + one stored artifact) near zero in layer A and
  painful in B, which is why B is where the PM made it a launch requirement.

  #### 🔴 2. "C only adds block types" covered half of layer C → **14b**

  The section waved Delivery through to run in parallel because it *"adds block types without
  extending what a pipeline can express."* Layer C is **two** deliverables:

  | part of C | a block type? |
  |---|---|
  | sink blocks — S3, database, Sheet, email | ✅ yes |
  | **saga rollback** — one delivery fails, so the ones that already succeeded are undone | ❌ no — nobody authors a rollback block |

  ✅ **The sink half now *verified* rather than assumed**, and worth recording: §5 split the
  catalog into content-producing and **effect** blocks, and effect blocks pass their input
  reference through unchanged. So "one result, several destinations" is a **chain** of effect
  blocks under §4's single-chain data flow — **C needs none of the data-flow fan-out §4 deferred
  past Phase 4.** That is the real content of the parallelism claim, and it holds.

  🔴 **The rollback half was not examined at all.** It is not a block; it is a **run shape** — the
  run turning around and executing work the user never wrote down:

  ```
  layer A today     Scrape → Clean → LLM → Webhook → done          (forward only)

  layer C           Scrape → Clean → LLM → S3 ✅ → BigQuery ✅ → Email ❌
                                              undo BigQuery ← undo S3
  ```

  Which lands on layer A as a concrete, checkable question: **what status does the S3 block have
  after it has been compensated?** §4 fixed the vocabulary as `pending` / `running` / `completed`
  / `failed` / `skipped`. It is **not `completed`** (the object is gone), **not `failed`** (it
  worked — BigQuery failed), **not `skipped`** (it ran). There is no value for *"succeeded, then
  undone."*

  ⚠️ **This is precisely the trap §4 spent effort avoiding, aimed at the nearer layer.** `skipped`
  was admitted on day one though nothing in today's catalog produces it, expressly *"so that B is
  a new block type rather than a schema change plus a backfill."* The identical exposure for C
  went unnoticed because the sentence clearing C only looked at the sinks — **and C is the layer
  cleared to start soonest.** ✅ Recorded as an obligation on **PRD-017: settle how a compensated
  block is recorded before C starts building, not during.** Until then, "C may proceed in
  parallel" is a claim about the sinks only.

  #### 🔴 3. The cheap part is the wiring; the hard part went unmentioned → **14c**

  The four things that make the follow-up PRD additive — named input references, identifiers
  stable across versions, graph storage behind a linear validator, and `skipped` — share a
  property: **every one is about how blocks are connected.** None says anything about the question
  a conditional exists to ask, which is **"if *what*?"**

  And that runs straight into a hard rule: PRD-016 excludes **user-authored code as a block**, and
  says in as many words that *"an expression evaluator would cross the line."* A condition is, on
  its face, an expression. So the follow-up PRD's first and hardest job — let a user say *"if the
  price changed"* without handing them a programming language — gets **nothing** from the four
  choices the "cost is low" claim rests on.

  ✅ **The Validate block is the precedent, and belongs in the follow-up PRD's brief rather than
  being rediscovered.** PRD-016 already fought and won this: Validate rules are a fixed
  declarative vocabulary, for three reasons — the expression-evaluator non-goal, the fact that
  validation is terminal and fires after the user has been billed, and **replay determinism**.

  ⚠️ **Determinism binds a conditional harder than it binds Validate**, because branching is an
  `if` in the **workflow body** — which is exactly the code Temporal replays:

  ```
  16:58  run starts; condition "hour < 17" is TRUE  → LLM block runs, user is billed
  17:03  workflow worker pod restarts (a routine deploy)
  17:03  Temporal replays the history; the same condition now evaluates FALSE
         → recorded history and re-executed code disagree; the run is corrupt
         → nothing was wrong at 16:58, and nothing logs a cause at 17:03
  ```

  Recorded as **not** open, so it is not re-litigated: the **monitor** supplies the gate's
  comparand, and §4 already settled that **widening the bindable-field list is additive** (declare
  one more field bindable); only narrowing breaks saved pipelines.

  #### 4. The PRD this section creates has no name → **14d**

  §14's own argument is that absorbing the work into B *"renames it and hides it inside a PRD
  nobody will read as a layer-A change."* Measured against the doc set:

  | | PRD-017 (C) | PRD-018 (B) | the conditional PRD |
  |---|---|---|---|
  | has a number | ✅ | ✅ | ❌ |
  | has a `phase4-backlog.md` §2 row | ✅ | ✅ | ❌ — one line of prose in the artifact chain |
  | records what it **owes** | ✅ multi-sink fan-out with rollback | ✅ does not ship without the cost gate | ❌ nothing |

  The one deliverable this section *creates*, and the only one that **blocks** another PRD, is the
  only one with no number, no row and no obligations. **A nameless PRD is only marginally more
  visible than an absorbed one** — which is the entire property §14 exists to buy.

  ⚠️ **Left open deliberately: the number.** Creation order gives **PRD-019**, which reads as
  *after* PRD-018 in every index while being required *before* it. That is a real choice, not
  bookkeeping — **owner's to make**, and the backlog row goes in with it.

  ### ✅ Fixed this session: `workflows-scoping.md` was stale in three places and its own banner endorsed two of them

  Found while checking §14's ordering claims. The banner flagged §7 and §1 and declared *"§4 and
  §6 still load-bearing"* — which was half right in a way that mattered:

  | passage | still said | actually decided |
  |---|---|---|
  | §4A, layer A's block catalog | `scrape / clean / LLM / validate / `**`branch`**` / deliver` | branch is **not** in layer A — §14 is why |
  | §5, phased roadmap | Phase 1 = A · Phase 2 = C · Phase 3 = B | no row for the **conditional step between C and B** |
  | 🔴 §6, worker integration | *"**Recommendation: (a) for Phase 1**"* | ⚠️ **reversed 2026-08-23** by §9 — option (a) is rejected **and blocked** (a work-queue stream refuses the second consumer it needs, proven by a dead service in prod, BUG-008) |

  The third is the one that mattered: a **live recommendation to build the design that was
  rejected**, five days old and unmarked — same class as the known staleness in
  `temporal-full-migration.md`. Banner amended, 🔴 markers added inline at all three, original
  text kept as the record of what was recommended and why. §6's *state-ownership* half is
  untouched and still load-bearing.

  ### Session close (2026-09-01)

  **Docs-only session — no code changed.** Nothing committed yet; `git status` shows the four
  files below modified.

  | file | what |
  |---|---|
  | `docs/adr/ADR-009-…` | §14 rewritten with 14a–14d (26 → 206 lines); review log entry; date line |
  | `docs/project/workflows-scoping.md` | banner amended + 🔴 markers on §4A, §5, §6 |
  | `CLAUDE.md` | §14 summary appended to the ADR-009 bullet; review progress → §15–§17 |
  | this handoff | §14 block; §13 demoted to superseded |

  **All 112 ADR internal links re-verified — none broken.** ⚠️ **The slug rule recorded in the
  last two sessions is itself slightly wrong, and it produces a false positive.** GitHub keeps
  **underscores**, so a checker written as *"drop non-alphanumerics except spaces and hyphens"*
  reports `#13c-crawl_pages-is-required-not-a-ui-convenience` as broken when it is fine. The
  correct rule: lowercase → drop everything that is not **alphanumeric, underscore, space or
  hyphen** → replace **each** space with a hyphen (**still no whitespace collapsing** — that part
  of the earlier note stands, and is why ` — ` becomes `--`).

  ⚠️ **Git state unchanged from the last session.** Deployed code is still `b110591`; `5c7fbdf`
  (the crawl page status-filter fix) is **committed on `develop`, not deployed, not on `main`**.
  `develop` is **8 ahead of `origin/develop`**, `main` is **23 behind**. ✅ **The owner's call of
  2026-08-28 stands: `main` is deliberately NOT fast-forwarded** — pushing it starts a push to the
  prod server, so a fast-forward is a **release**, not a tidy-up. The next prod deploy should carry
  Phase 4 work with `5c7fbdf` riding along. Do not fast-forward at session end; wait to be asked.

  **What is blocking: nothing blocks §15.** Outstanding:

  1. **The conditional-execution PRD needs a number and a backlog row** (§14d). ⚠️ The number is
     an **owner decision, deliberately left open** — creation order gives PRD-019, which sorts
     *after* PRD-018 everywhere while being required before it. The row carries three commitments:
     the Validate-precedent brief and the replay constraint (14c), and the halt-early block B
     cannot build for itself (§4).
  2. **PRD-016 now owes four things, all for one PM pass** (the Architect does not edit the PM's
     doc, which is why they accumulate): (a) **§4's known exclusion** — "two extractions on one
     fetched page" is *not* fixed by layer A, owed since 2026-08-10; (b) **two more R6 divergences
     from §10** — a pipeline webhook payload has no `job_id`, and no `diff_detected`/`diff_summary`
     until Monitors; (c) **two passages still reasoning from §8's reversed storage rule**
     (`PRD-016:697`, `:802`) — low urgency, the conclusion survives and only the reasoning is
     stale; **(d) NEW — §14a moves a sequencing fact out of PRD-016's Non-goals**, where "the first
     thing to add after it ships" did not belong.
  3. **`temporal-full-migration.md` still contradicts the ADR** — step 3 is the worker port (now
     step 1), the line-314 diagram assumes the rejected NATS bridge, the 339–351 retry discussion
     describes a hazard that mostly no longer exists, and its crawl step is written as a port that
     assumes `crawl_queue` retires (both now wrong, per §13). ✅ Owner's call stands: **redraw in
     ONE pass after the review closes.** 🔴 markers are in place so nobody implements from it
     meanwhile.
  4. **One open item from §13**, needed before the crawl step is built but not before §15–§17:
     whether sitemap entries are **origin-restricted** like extracted links.
  5. **§15 is next** (OQ-11 — webhook delivery is a step the run waits for), then §16–§17.

  ### Superseded: START HERE (2026-08-28) — §13 review

  ### §13 (the crawl coordinator) — reviewed 2026-08-28; the decision holds, but it described a **port** of code that has mostly **never executed**

  Every factual claim the section makes is true — the aiohttp import, the httpx sibling, the
  missing lockfile, and "a crawl is unbounded fan-out, not a linear step". So the decision stands:
  the coordinator becomes a `CrawlWorkflow`, the service is deleted, it happens **after** jobs and
  batches, and a Crawl block is **not** added to the pipeline catalog.

  Four owner calls on what it did not say, one of which **withdraws a clause**.

  #### 🔴 1. It is a rewrite, not a port — and BUG-008 is wider than "one consumer is missing" → **13a**

  Traced through: because nothing reads results, **only the dispatch half of `coordinator/` has
  ever executed.**

  | file | has run | never run |
  |---|---|---|
  | `dispatcher.py` | dispatch loop, stalled-item recovery | — |
  | `result_handler.py` | `check_completion`, `enqueue_crawl_webhook` (called from `dispatcher.py:156-158`) | `result_handler_loop`, `_process_crawl_result`, `_enqueue_url`, `_fetch_minio_bytes` |
  | `link_extractor.py` | — | all of it |
  | `sitemap.py` | — | all of it |

  So **a crawl in production has never got past dispatching its seed page.** Queue items never
  leave `dispatched`, so `check_completion` never fires either. `crawls`/`crawl_pages` being empty
  is **what this looks like**, not evidence the feature is unused. The unit tests do not cover it
  and never claimed to — they mock the DB and MinIO and call `_process_crawl_result` directly,
  never `result_handler_loop`, which is where the defect lives.

  ✅ **Owner's call: treat it as a rewrite; still last in the phase.** ⚠️ **The consequence worth
  carrying:** §9's pre-gate — *run the new implementation and diff it against a v1 run of the same
  URL* — **does not exist for crawls and cannot be made to exist.** There is no v1 crawl result.
  Every other lane migrates with a reference implementation; this one migrates when the least v1
  machinery is left to build one from, so **the compensating gate has to be built, not borrowed**,
  and that belongs in the crawl step's own plan. Only two pieces are genuine ports — link
  extraction (moves intact) and sitemap discovery (which 13d modifies).

  #### 🔴 2. `crawl_queue` does NOT retire — the clause is withdrawn → **13b**

  The draft deferred the frontier model (visited-set-in-workflow-state + `continue-as-new` vs
  child-workflow-per-page) on the grounds that history size is the binding constraint and should be
  measured. The constraint is right; **the deferral was too wide.** §5 already measured the limits
  and the API already publishes the ceiling (`schemas/crawls.py:13`, `le=10000`):

  ```
  Temporal history ceiling      51,200 events
  max_pages ceiling             10,000 pages
  ⇒ budget per page             ≈5 events
  cheapest possible page         3 events   (Scheduled / Started / Completed)
                                            …before workflow-task overhead, timers, signals

  visited set, 10,000 × ~80 B   ≈800 KB
  §5 payload warn                256 KiB
  §5 payload hard limit          2 MiB
  ```

  Two conclusions need no new measurement: **`continue-as-new` is mandatory in *both* candidate
  designs** — so it never distinguished them and the either/or was already false — and **the
  visited set cannot ride in a workflow argument.**

  ✅ **Owner's call: the frontier and visited set stay in Postgres.** Temporal owns control flow,
  durability and retry. Three consequences: the workflow reaches the frontier **through
  activities** (§6's determinism rule forbids a workflow body reading the DB); the dedup mechanism
  to preserve is an **index** — `idx_crawl_queue_url UNIQUE (crawl_id, url)` plus
  `on_conflict_do_nothing` — not a separate "seen" collection; and what stays genuinely open is
  only the **table's shape**, not the location.

  #### 🔴 3. `crawl_pages` is required, not "may be kept for the UI" → **13c**

  That sentence is untouched original text from `eb78146` (2026-08-04) and predates everything that
  now leans on the table:

  | date | decision | what it needs |
  |---|---|---|
  | 2026-08-08 | **P7** — crawls metered **per page** | a per-page row to meter |
  | 2026-08-17 | **§8 reversal** — every stored object is charged | a producer for each charged object |
  | 2026-08-25 | **§8d** — one shared ledger, producer by nullable FK | the FK target for a crawl artifact |

  Plus one older than the ADR: **the artifact's name is the page row's id** —`dispatcher.py:120`
  puts `crawl_page_id` into the message's `job_id` field so the ADR-002 §8 path convention
  resolves. And P7's reclaim half needs to enumerate a crawl's objects; `crawl_pages.result_path`
  is the only enumeration there is.

  ✅ **Owner's call: `crawl_pages` survives.** Note its **missing size column is now correct**
  rather than a gap — §8d put bytes on the ledger row and gave no lane its own marker.
  ⚠️ **Deferred past Phase 4, product not architecture:** how a crawl is *presented* in the
  dashboard. It is not a job and does not fit the job list; likely its own page. Recorded so the
  table's survival is not mistaken for the UI question being answered.

  #### 🔴 4. The httpx swap is the smaller half of what is wrong in that file → **13d**, and **BUG-010**

  The platform checks a URL **once, at the front door.** `validate_no_ssrf` runs exactly twice,
  both inside the creation request, on the seed and webhook URLs (`routers/crawls.py:34-36`). After
  that the coordinator validates nothing, and **no worker validates anything** — no SSRF check,
  IP-range test or `getaddrinfo` call exists in `http-worker/`, `playwright-worker/` or
  `llm-worker/`.

  Two routes discover URLs mid-crawl and **only one is guarded**: extracted links survive by
  accident (`link_extractor.py:33` restricts to the seed origin), while **sitemap entries get
  nothing** — `sitemap.py:39` takes them verbatim from the target's `robots.txt`, `:45` fetches
  them, and `result_handler.py:183` enqueues them with no origin filter.

  ```
  user submits   https://evil.example/           → SSRF-checked, public, allowed
  evil.example/robots.txt says
      Sitemap: http://169.254.169.254/latest/meta-data/...
  coordinator fetches it                          → unchecked   sitemap.py:45
  coordinator enqueues it                         → unchecked   result_handler.py:183
  a worker scrapes it, uploads the body           → unchecked   no worker validates
  user reads it via GET /crawls/{id}/pages        → the response comes back out
  ```

  The last line makes it a **read** primitive rather than a blind fetch. ⚠️ **It has never fired
  for exactly one reason: sitemap discovery is only reachable from `_process_crawl_result`, which
  has never run.** This is the inverse of the usual latent-bug note — the code is not latent
  because nobody hit the input, it is latent **because the component is dead**, and reviving it is
  precisely what this section proposes.

  ✅ **Owner's call: every URL entering the frontier is SSRF-checked at admission** — the one point
  seed, extracted link and sitemap entry all converge on. A rejected URL is **skipped and the crawl
  continues**; it is not a crawl failure, because the user did not choose that URL and cannot fix
  it. The refusal is **terminal and never retried**, matching §10's webhook-SSRF rule — retrying
  means re-resolving a hostname an attacker is actively rebinding.

  ⚠️ **Still open, needed before the crawl step is built:** are sitemap entries also restricted to
  the seed's origin, the way extracted links are? The SSRF check stops the internal-address case;
  it does not stop a `robots.txt` pointing the crawl at an unrelated **public** site, which is a
  quota and attribution question rather than a security one. Legitimate sitemaps do occasionally
  cross subdomains, so this is a genuine trade-off, not a free tightening.

  ✅ **A worker-side check was raised by the owner and is the correct point-of-use position** — the
  worker opens the socket, and a check there covers every lane including ones not yet built. It is
  **not** a substitute for the admission check (a worker cannot tell *"the user typed a bad URL"*,
  which should 400 at creation, from *"a crawl discovered one"*, which should skip a page and
  continue). **Filed as separate cross-lane work, not the crawl migration's to carry**, because it
  is larger than it looks: two implementations in two languages that must not drift (the risk
  `playwright-worker/worker/robots.py`'s *"mirrors the Go worker's internal/robots package"* already
  carries); DNS rebinding that a naive check narrows but does not close, needing
  resolve-once-then-connect-to-that-IP in both languages; and a new terminal failure class that
  must be wired into each worker's classifier at the same time, or Temporal retries a policy
  refusal for the full horizon.

  ### 🔴 Filed against live code: BUG-010 — mid-crawl URLs are never SSRF-checked

  Both halves above, in `open-bugs.md` → **BUG-010** and `phase4-backlog.md` **§4**. Part 1 (the
  admission check) is decided and ships with the crawl migration; part 2 (worker-side) is
  recommended and unscheduled. **It does not jump the pre-Phase 4 queue**, which is unchanged:
  **P6/BUG-005 → P8 (ledger) → P7 + BUG-007 together.**

  ### Also corrected, and one live fix shipped

  - **BUG-006 undercounted itself.** Filed as "Dependabot scans 3 of 6 manifests"; the real figure
    is **7 manifests, 3 scanned** (`api/uv.lock`, `http-worker/go.sum`, `frontend/package-lock.json`).
    The unscanned set is **four, not three** — **`mcp/` was missing from its own list**, and it is
    the LLM-callable public surface. The undercount has the same shape as the bug: a manifest is
    invisible to the count for exactly the reason it is invisible to the scanner. Corrected in
    `open-bugs.md` and backlog §4.
  - **✅ Fixed on live code (`5c7fbdf`): the crawl page status filter accepted a value nothing
    writes.** `routers/crawls.py` allowed `{pending, processing, completed, failed}` while the
    coordinator writes `{pending, running, completed, failed}` — so `?status=running` returned
    **422** and `?status=processing` always returned **empty**. Allowlist lifted to `_PAGE_STATUSES`
    next to a comment naming the writer. 2 tests; **251 passing**. Safe to fix despite backlog §3
    because §13c keeps `crawl_pages` and its read route.

  ### Session close (2026-08-28)

  **Not a docs-only session** — the first code change since `b110591`.

  | commit | what |
  |---|---|
  | `5c7fbdf` | **code** — crawl page status filter fix + 2 tests (full suite **251 passing**) |
  | `e4a19fc` | §13 rewritten with 13a–13d; review log + date; **BUG-010** filed; **BUG-006** scope corrected (7 manifests, 3 scanned — `mcp/` was missing); backlog, ADR index, `CLAUDE.md` and this handoff brought level |

  ⚠️ **Git state changed shape this session.** Every session since 2026-07-28 could say "code on
  `develop` and `main` is level and deployed at `b110591`, docs on top." **That is no longer true:**
  `5c7fbdf` is a code commit sitting on `develop`, **not deployed and not on `main`**. Current
  state — `develop` is **8 ahead of `origin/develop`**, `main` is **23 behind `develop`**, deployed
  code is still `b110591`.

  ✅ **Owner's call 2026-08-28: `main` is deliberately NOT fast-forwarded, and the crawl fix is
  held back.** Pushing `main` starts a push to the prod server, so a fast-forward is a **release**,
  not a bookkeeping step. The owner does not want a one-line status-filter fix deploying on its
  own; **the next prod deploy should carry Phase 4 work**, with `5c7fbdf` riding along. So do
  **not** fast-forward `main` as a tidy-up at the end of a session — wait to be asked.

  All **105** ADR internal links re-verified after the rewrite — none broken. The checker was
  written **without whitespace collapsing**, per the trap the last two sessions hit: GitHub
  lowercases, drops non-alphanumerics except spaces and hyphens, then replaces **each** space with
  a hyphen — so ` — ` becomes `--`, and a checker that collapses runs calls every `§N. OQ-x — Title`
  anchor broken.

  **What is blocking: nothing blocks §14.** Outstanding:

  1. **`temporal-full-migration.md` still contradicts the ADR** — step 3 is the worker port (now
     step 1), the line-314 diagram assumes the rejected NATS bridge, and the 339–351 retry
     discussion describes a hazard that mostly no longer exists. ✅ Owner's call stands: **redraw in
     ONE pass after the review closes.** 🔴 markers are in place so nobody implements from it
     meanwhile. ⚠️ **§13 adds to the redraw list:** that doc's crawl step is written as a port and
     assumes `crawl_queue` retires — both now wrong.
  2. **PRD-016 owes three things, all for one PM pass** (the Architect does not edit the PM's doc,
     which is why they accumulate): (a) **§4's known exclusion** — "two extractions on one fetched
     page" is *not* fixed by layer A, owed since 2026-08-10; (b) **two more R6 divergences from
     §10** — a pipeline webhook payload has no `job_id`, and no `diff_detected`/`diff_summary`
     until Monitors; (c) **two passages still reasoning from §8's reversed storage rule**
     (`PRD-016:697`, `:802`) — ⚠️ low urgency, the conclusion survives and only the reasoning is
     stale.
  3. **One open item from §13 itself**, needed before the crawl step is built but not before
     §14–§17: whether sitemap entries are **origin-restricted** like extracted links.
  4. **§14 is next** (conditional execution → its own layer-A PRD), then §15–§17.

  ### Superseded: START HERE (2026-08-26, later session) — §11 review

  ### §11 (run state to the SPA) — reviewed 2026-08-26; **the first section with no factual error**

  Every claim the section made was checked against live code and **all of them hold**:

  | Claim | Verdict |
  |---|---|
  | `job_status`'s payload is a positional 3-field string that cannot be widened | ✅ — and widening fails **silently**: `job_notifier.py:51` catches the `ValueError`, logs "malformed", **drops the update** |
  | `batch_status` already demonstrates the JSON pattern | ✅ |
  | The mirror row is written anyway, so the notify is nearly free | ✅ |
  | The Temporal Web UI is write-capable and not a dashboard | ✅ (§2b) |

  So the decision stands as drafted — run state stays on **mirror row + `pg_notify`**, a
  status-mirror activity replaces `result_consumer.py`, engine events are **not** streamed to the
  browser, and pipelines get **their own JSON channel** rather than overloading `job_status`.
  **All four findings are things the section did not say**, plus one live bug it inherits.

  #### 🔴 1. The section named one writer. The contract it preserves has four. → **11a**

  `job_status` is emitted from **four** files, and two are request handlers, not the background loop:
  `result_consumer.py:579`, `quota.py:260`, **`routers/jobs.py:419`** and **`routers/admin.py:354`**
  — the last two both cancellation, written and notified *inside the request*.

  ✅ **Owner's call: keep two writers, exactly as today.** Routing cancellation through the workflow
  would make Cancel *look broken*: the PM's rule is that a block is **never aborted mid-execution**,
  so a run cancelled four minutes into an LLM block does not reach its next mirror point for four
  minutes, where today the page greys out instantly. R5 forbids that regression.

  ⚠️ **The accepted cost, and the reason it is now written down in three places rather than left to
  the implementer: two writers on one status column fail SILENTLY.** The failure is a user
  cancelling a run, watching it cancel, then watching it flip back to `completed` — with the work
  done and charged. The single-writer alternative fails *loudly and harmlessly* (a sluggish button),
  which is the better failure shape; it lost only because matching today's behaviour exactly is a
  stated requirement.

  > **The precedence rule: a cancellation written by the API wins. A mirror write must never move a
  > run out of a terminal state it did not itself set.**

  **It already exists on the job path and must be re-established, not invented:**
  `result_consumer.py:613` checks `run.status == "cancelled"` **before anything else**, discards the
  worker's result and re-notifies. The v2 mirror activity needs the same guard against
  `pipeline_runs.status`. Now recorded in ADR §11a, in `CLAUDE.md`'s key-decisions table (its own
  row — this is the "make sure we remember" item), and here.

  #### 🔴 2. The socket gives up after 5 minutes; §15 deliberately creates runs silent for 2.6 hours. → **11b**

  `jobs.py:745` waits for the next transition with a **300-second timeout**, then sends
  `{"type":"timeout"}` and closes. Never mattered — a job run takes ~40s. It matters now:
  §15 gives a Webhook block a delivery horizon of **≈2.6 hours** (`BACKOFF_SECONDS`
  `[0, 30, 300, 1800, 7200]`), during which the status does not change. Monitors extend that to
  **days**.

  The frontend has no recovery: `JobDetail.tsx:81` is `ws.onclose = () => setWsLive(false)`, with
  **no reconnect and no polling fallback**, and the react-query cache is invalidated only by a
  *terminal* WebSocket message — which never arrives. So a pipeline parked at its webhook block goes
  stale at **6m30s** and stays stale for the remaining two and a half hours; the eventual completion
  notify fires into an empty room, because no subscriber is holding the queue.

  ✅ **Owner's call: the client reconnects on any close that did not carry a terminal message. The
  300-second timeout stays exactly as it is.** A server keep-alive was **rejected as the primary
  fix**, on three grounds: it covers only **one** cause of a dead socket (not Traefik's idle cut, a
  rolling deploy, or a closed laptop lid); it would make **BUG-009 invisible** — the browser would
  receive heartbeats forever while receiving no status changes, where today's `timeout` at least
  signals *something*; and it **cannot self-heal**, whereas a reconnect re-reads the row
  (`jobs.py:734-739`) and **repairs every update missed while disconnected**.

  Keeping the timeout is deliberate: alongside reconnect it stops being a defect and becomes **a
  five-minute self-healing re-read**, a slow safety net under the fast push, at no server cost.
  Two constraints: reconnect must **back off**, and must **honour close code 4029** —
  `subscribe_job` refuses past `ws_max_connections_per_user`, so a loop that ignores it spins
  against a wall. This is a small, accepted dent in the "zero frontend change" property.

  #### 🔴 3. A failed mirror write. → **11c**

  It is the only activity in a pipeline whose failure has **no effect on the work** — the scrape
  still scraped, the LLM key was still billed — which is exactly why it invites a "best-effort,
  don't fail the run" treatment. That is wrong here, because two decisions compose: §11 says don't
  stream engine events (the app row is what the user sees) and **§2b** says the Web UI is not
  exposed at all. Together, **the mirror row is the only window into a run that exists** — a run
  whose mirror write failed completes, charges storage, fires its webhook, and shows as stuck at
  `running` forever, indistinguishable from a hung run to anyone without cluster access.

  ✅ **Owner's call: the run fails.** Under §3's decision that the app table is the read model, a run
  whose state cannot be read is not a successful run.

  #### 🔴 4. The payload has an 8000-byte ceiling, and overflowing it destroys the status write. → **11d**

  `pg_notify` caps a payload at **8000 bytes** (documented Postgres limit; not measured — the local
  Postgres container is down). Unreachable today (two UUIDs and a word; five integers and a URL) —
  **this section's own JSON decision is what brings it into range.**

  The failure lands in the worst place. The notify runs **inside the transaction that writes the
  row**, which is what makes it trustworthy. So an oversized payload does not merely lose the
  notification: **the transaction rolls back and the block's status was never written.** Realistic
  trigger: a failed LLM block's error string, which is whatever the provider returned and can run to
  kilobytes of JSON.

  ✅ **Owner's call: identifiers and status values only — never error text, never content, never
  anything user-supplied and unbounded.** The browser fetches detail over HTTP. This is §5's
  references-not-payloads rule one layer down.

  ✅ **Second rule, same call: absolute state, never deltas.** Temporal runs activities
  **at least once**, so a worker that crashes after committing but before reporting success runs the
  mirror activity again. A duplicated `status = completed` is harmless; a duplicated *"add one to
  the completed count"* drifts. `batch_status` already sends absolute totals
  (`result_consumer.py:345`) — **correct, but by accident**, and a per-block progress payload is
  exactly where someone reaches for an increment.

  #### 5. Scope note → **11e**

  "Zero frontend change" is true **of the job path**. The pipeline lane still needs a channel, a
  third listener on the `JobNotifier` connection, a third subscriber map and `subscribe_*` method,
  a WebSocket route, and a page. Not a decision — recorded so the section is not read as cheaper
  than it is.

  ### 🔴 Filed against live code: BUG-009 — `JobNotifier` never reconnects

  `JobNotifier` opens **one** dedicated asyncpg connection at API startup (`main.py:54` →
  `job_notifier.py:36`), registers `job_status` and `batch_status` on it, and holds it for the
  process lifetime. **There is no termination handler and no reconnect path** — `start()` is called
  once, `stop()` closes it at shutdown, and nothing observes it dying in between.

  So if that connection drops — a Postgres restart, a minor-version upgrade, a failover, an idle cut
  — both subscriptions are gone for good and **every WebSocket in that API process goes deaf**:
  the socket stays open, `pg_notify` keeps firing correctly with nobody listening, **and nothing
  logs it.** Watchers sit until the 300s timeout; the page then shows a stale status until manually
  refreshed. Masked today by frequent API restarts and 40-second runs.

  ⚠️ **Not dissolved by Temporal** — §11 keeps this component and adds a **second channel** to the
  same single connection, and §15/Monitors stretch watcher lifetimes from 40 seconds to hours and
  days, which is when a blip actually gets a chance to happen.

  ⚠️ **And §11b's client reconnect masks it without fixing it** — that repairs *the browser's view*
  while the server-side listener stays dead, so the process degrades from push to a permanent
  5-minute poll with **no operator signal**. §11b rejected a server keep-alive partly *because* it
  would hide this bug; fixing only the client arrives at the same place by another road. The fix
  needs all three: detect the drop, re-register **every** channel on a backoff, and **log loudly**.

  Filed in `open-bugs.md` → **BUG-009**, and in `phase4-backlog.md` **§4** (survives Temporal, not
  blocking it) alongside BUG-004 and BUG-006. **It does not jump the pre-Phase 4 queue**, which is
  unchanged: **P6/BUG-005 → P8 (ledger) → P7 + BUG-007 together.**

  ### Session close (2026-08-26, later session)

  **Docs-only session — no code changed.** Files touched: `docs/adr/ADR-009-…` (§11 rewritten with
  11a–11e; review log entry; date line), `docs/project/open-bugs.md` (BUG-009 writeup),
  `docs/project/phase4-backlog.md` (BUG-009 → §4; ADR row updated; header date), `CLAUDE.md` (§11
  summary + **a new key-decisions row for the two-writer precedence rule**), and this handoff.

  All **22** ADR internal anchors re-verified after the rewrite — none broken. ⚠️ **The em-dash slug
  trap bit again**, exactly as the last session predicted: a first checker reported **14** broken
  anchors, all false. GitHub's rule is *lowercase → drop non-alphanumerics except spaces and hyphens
  → replace each space with a hyphen*, and it does **not collapse runs of whitespace** — so ` — `
  becomes `--`, and any checker that collapses whitespace calls every `§N. OQ-x — Title` anchor
  broken. The last handoff recorded this; the checker was still written wrong. **If you write one
  again: do not collapse.**

  **What is blocking: nothing blocks §13.** Outstanding, unchanged from the previous session except
  where noted:

  1. **`temporal-full-migration.md` still contradicts the ADR** — its step 3 is the worker port (now
     step 1), the line-314 diagram assumes the rejected NATS bridge, and the 339–351 retry
     discussion describes a hazard that mostly no longer exists. ✅ Owner's call stands: **redraw in
     ONE pass after the review closes.** 🔴 markers are in place so nobody implements from it
     meanwhile. §10's review added §4 of that doc to the redraw list ("pure logic reused verbatim").
     **§11 adds nothing to it** — that doc does not cover the SPA path.
  2. **PRD-016 owes three things, all for one PM pass** (the Architect does not edit the PM's doc,
     which is why they accumulate): (a) **§4's known exclusion** — "two extractions on one fetched
     page" is *not* fixed by layer A, owed since 2026-08-10; (b) **two more R6 divergences from
     §10** — a pipeline webhook payload has no `job_id`, and no `diff_detected`/`diff_summary` until
     Monitors; (c) **two passages still reasoning from §8's reversed storage rule** (`PRD-016:697`,
     `:802`) — ⚠️ low urgency, the conclusion survives and only the reasoning is stale.
  3. **§13 is next** (the crawl coordinator migrates last, and a crawl is not a block) — it already
     carries a rider added by the §10 review: **sitemap discovery must port to `httpx`, not
     aiohttp**, the one do-not-delete item that is a *modification* rather than a copy, so a
     faithful port is the failure mode. Then §14–§17.

  ### Superseded: START HERE (2026-08-26) — §10 review

  ### §10 (the do-not-delete list) — reviewed 2026-08-26; the porting *mechanism* was not implementable

  All findings accepted by the owner; two relocated on the owner's call. This section had the
  unusual property that **the decision was right and the instruction for carrying it out was
  impossible** — so nothing was reversed, but the section was substantially rewritten.

  #### 🔴 1. "The classifier becomes `RetryPolicy` non-retryable error types" is WITHDRAWN

  Stated in **four** places (review log, §9, §10's table, Consequences); corrected in all four.
  It cannot work, for three independent reasons — any one fatal:

  - **Direction.** Temporal offers only a **denylist**: `non_retryable_error_types` means *retry
    everything except these named types*. The classifier is an **allowlist** — retry only these,
    everything else terminal. There is no `retryable_error_types`, and a denylist cannot express
    an allowlist over an open set. The table cell asserted **"non-retryable error types"** and
    **"fail-closed default preserved"** simultaneously; those are mutually exclusive.
  - **Dynamism.** `classify()` reads attributes, not just types: an `APIStatusError` is transient
    at **429** and terminal at **418**; an `S3Error` is transient on `SlowDown` and terminal on
    `NoSuchBucket`. A list of type *names* cannot see inside the exception.
  - **Structure (Go).** `classify` returns terminal for everything not wrapped in `*uploadError`
    (`errors.go:71-77`), because in Go a dead **target site** and a dead **MinIO** raise the *same*
    type — the only disambiguator is which step raised it (`worker.go:393`). "Which step" is not a
    type name.

  **Replaced by: the classifier decides; Temporal retries.** The activity catches, calls
  `classify()`, and re-raises terminal verdicts as `ApplicationError(non_retryable=True)` (Go:
  `NewNonRetryableApplicationError`). This is **not** the in-activity retry loop §9 warned against
  — no second backoff, no second attempt counter, no second ceiling — so R4 still holds.

  #### 🔴 2. The timeout number was wrong, and production's value is in another repo

  Row 1 said *"180s request timeout."* Two budgets were conflated:

  | | setting | value |
  |---|---|---|
  | warm-up budget | `llm_warmup_max_wait_seconds` | **180s** |
  | request timeout | `llm_request_timeout_seconds` | **60s** in `config.py`; **180s in production**, set as `LLM_REQUEST_TIMEOUT_SECONDS` in the infra repo's `llm-worker.yaml` |

  Verified against git history: `llm_request_timeout_seconds` has **never** been 180 in code.
  An implementer sizing start-to-close from the file §10 points at computes **240s** against a
  real requirement of **≈360s** — short by two minutes, and failing **only on cold starts**, the
  one case the row exists for. Fixed in the ADR and in `CLAUDE.md`.

  #### 🔴 3. The section's risk model was miscalibrated — now split in two

  §10 opened *"These live inside code the migration removes."* **False for three of five items.**
  Verified: `llm.py`, both `errors.py`, `errors.go` and `blocking.py` contain **zero** NATS calls
  (two comment references between them). Nothing points a delete at them.

  - **Group A — at risk of deletion.** SSRF check (`webhook_loop.py`), content-hash (inside
    `result_consumer.py`). Instruction: *rescue before the file goes.*
  - **Group B — at risk of silent semantic change.** Cold-start handling, the three classifiers,
    bot-wall detection. They port by being left alone; the danger is they arrive intact and are
    **overruled by the new retry owner** — i.e. finding 1. Instruction: *port untouched, then
    re-establish the guarantee the old layer provided.*

  #### 4. Non-retryable is now an explicit three-part obligation

  1. **Classifier terminals** — the mechanism above.
  2. **SSRF failures.** Today an SSRF block marks the delivery `exhausted` **immediately** and
     does **not** increment `attempts` (`webhook_loop.py:99-110`). §15 makes the activity's retry
     policy the delivery loop, so a naive port turns "instantly dead" into **≈2.6 hours of
     re-resolving a hostname an attacker is actively rebinding**.
  3. **Bot walls and robots disallows.** ⚠️ **These never raise today.** `detect_block()` *returns*
     a verdict and the worker publishes `failed` (`playwright-worker/worker/worker.py:205`; robots
     at `:61-64`) — so **the classifier has never seen a block**, and §10's rows 2 and 3 meet for
     the first time *in the port*. Returning a failure must become raising one, marked
     non-retryable, or Temporal re-scrapes the same wall from the same IP every attempt.

  #### 5. Four items added to the list

  - **The heartbeat obligation.** `ensure_ready`'s docstring carries a caller obligation
    (*"caller must ensure the heartbeat is running: this loop can outlast `ack_wait`"*). §9 says
    heartbeats are "deleted, not ported" — true of the **mechanism**, false of the **duty**, which
    re-homes onto `activity.heartbeat()` + `heartbeat_timeout`. Both ways of forgetting fail: set a
    timeout and never heartbeat → the activity fails on **every** cold start; set none → a dead
    worker's job hangs for the full start-to-close.
  - **The webhook wire contract** — HMAC-SHA256; header `X-ScrapeFlow-Signature: sha256=<hex>`;
    success = `status_code < 300`; 10s per attempt; and the quirk that with no secret
    `secret_bytes = b""`, so **the header is always sent**. Lives in a deleted file. **The most
    externally visible thing in the migration** — change the header name or the threshold and every
    customer integration breaks silently, with **no failing test in this repo**.
  - **The webhook payload schema** (`webhooks.py:41-49`). Two fields have no v2 source: a pipeline
    run has **no `job_id`**, and `diff_detected`/`diff_summary` are homeless until Monitors. Both
    now recorded as R6 divergences rather than left to be discovered.
  - **A correctly-dissolved list**, so it is not re-derived: the terminal-status idempotency guard
    (`result_consumer.py:541`), schedule-drift prevention (`croniter(cron, next_run_at)`,
    `scheduler.py:88`), and `ack_wait`/`max_deliver`/the nak ladder.

  #### 6. Two precision fixes

  - **`diff.py` and the content-hash are not equally at risk, and bundling them hid which.**
    `diff.py` is its own module — its only functional caller is `result_consumer.py`
    (`webhooks.py` imports just the `DiffResult` type), so deleting the caller leaves it orphaned
    but **intact**. The content-hash is `_compute_content_hash` at `result_consumer.py:49-56` plus
    the dedup branch at `:375-392`, both **inside** the deleted file. Only the second is at risk of
    the "accidental loss" the note warns about.
  - **"Pure logic reused verbatim" is false of the dedup branch.** On a hash match it **deletes the
    new `history/` object** and **repoints `result_path` at the previous run's object** — the
    cross-run object sharing §8 already recorded as what breaks per-run GC. Porting it as pure
    logic carries the code and leaves the hazard behind.
  - Also: **"the Scrape activity" → "the Playwright scrape activity"**, since bot-wall detection
    exists on one lane only (the Go worker has just `fetcher.go:72`'s non-2xx check, so a `200`
    wall passes through). Not reopening BUG-003 — but which engine the Scrape block routes to now
    decides whether detection runs at all.

  ### Two findings relocated on the owner's call

  **→ §7: a scheduled job blocked by quota *waits*; it is not skipped.** `_dispatch_due_jobs`
  checks both meters and, on a breach, logs and `continue`s **without advancing `next_run_at`**
  (`scheduler.py:65-78`) — so the row stays due and the 60s poll retries it until a slot frees.
  A waiting room, and a concurrency breach clears in minutes. **No Temporal Schedule overlap
  policy reproduces this:** `SKIP` discards the firing permanently (an R5-forbidden user-visible
  regression), `BUFFER_ONE` holds one and drops the rest — and all five react to *a previous
  execution still running*, whereas this gate reacts to *the account's meters*, which a Schedule
  cannot read. §3 makes the meters **count** every lane; it never said what a Schedule does when
  one says no. Recorded as a **named open item, not decided** — the admission check likely belongs
  in the workflow's first step (park on a durable timer and re-check, composing with §8's
  timer-parked-holds-no-slot rule), but it pairs with §8's headroom buffer, and that pairing is
  unreviewed: a **storage** breach does not clear on its own, so parking forever is right for
  concurrency and wrong for storage.

  **→ §13: sitemap discovery must port to `httpx`. This is a change, not a copy.**
  `coordinator/coordinator/sitemap.py:11` fetches robots.txt and sitemap XML with **aiohttp**,
  from **user-supplied target sites**; `playwright-worker/worker/robots.py:10` — the direct sibling
  — uses httpx, as every other untrusted-target fetch does. This is BUG-006's reachable copy, in
  the one service Dependabot has never scanned. **Do not close BUG-006 as dissolved:** the service
  is deleted but sitemap discovery *ports into a `CrawlWorkflow` activity* and carries the exposure
  unless the port changes the client. It was surviving **only** as a `CLAUDE.md` paragraph — not
  where someone doing the crawl migration would look. ⚠️ The only do-not-delete item that must be
  **modified** rather than copied, which makes **a faithful port the failure mode**.

  ### Session close (2026-08-26)

  **Docs-only session — no code changed.** Two commits on `develop`:

  | commit | what |
  |---|---|
  | `4d27475` | §10 reviewed — porting mechanism corrected in all four places; list split into Group A (at risk of deletion) / Group B (at risk of silent semantic change); non-retryable made explicit for SSRF + bot walls; heartbeat obligation, webhook wire contract, webhook payload schema and the correctly-dissolved list added; `diff.py` separated from the content-hash; §7 and §13 gained the relocated findings |
  | `a40b89b` | handoff + `CLAUDE.md` brought level with the review; BUG-006's port requirement recorded in `CLAUDE.md` alongside its §13 home |
  | `1770b70` | **doc consistency sweep** — five more files + 31 broken links. See below |

  **The consistency sweep found two *reversed* decisions still being asserted as live guidance**,
  neither of them from this session's review — they had been stale since 2026-08-23 and were
  missed at the time:

  | file | what was stale |
  |---|---|
  | `docs/adr/README.md` | Told readers §9 "warns that option (a) recreates the Q5/Q6/Q7 failure mode… NATS-side retry must be neutralised." **That is the rejected design.** Also still described the ADR as "drafted and awaiting review" with no indication ten sections are settled |
  | `docs/project/phase4-backlog.md` §2 | Same withdrawn §9 claim, **plus** *"only the final artifact is charged"* — reversed by the §8 review on 2026-08-17 — and "review in progress (§2 resolved 2026-08-08)" |
  | `docs/project/temporal-full-migration.md` | Caveat block rewritten and an inline 🔴 marker added above the option-(a) paragraph at ~line 358. **The doc is still not redrawn** (owner's call: one pass after the review closes) — the markers exist so nobody implements from it meanwhile |
  | `docs/project/open-bugs.md` + backlog §4 | BUG-006's httpx switch was recorded as a *recommendation* "worth adding to §10 when reviewed". It is now a **decision in §13**; both rows updated, and the `sitemap.py` path corrected to `coordinator/coordinator/sitemap.py:11` |

  **Also fixed in the same sweep: 31 broken cross-document links**, all pre-existing — the Phase 3
  PRDs moved to `docs/archive/phase3/prd/` and the referring docs (`docs/process/product-manager.md`,
  `docs/archive/phase3/phase3-engineering-spec.md`) still pointed at `../project/phase3-prd/`; plus
  ADR-003's link to the Phase 2 spec. Every relative link in `docs/`, `CLAUDE.md` and this handoff
  now resolves, and all **89** ADR-009 internal anchors do too.

  ⚠️ **The lesson worth keeping:** both reversals were correctly recorded in the ADR and in
  `CLAUDE.md`, and *neither* propagated to the two documents a new reader is most likely to open
  first — the ADR index and the backlog that `CLAUDE.md` calls the single source of truth. **When a
  section is reversed, the summaries elsewhere are the thing that goes stale, not the ADR.**

  All **89** ADR internal anchors re-verified after the §10 rewrite — none broken. (A first pass
  reported ~70 broken; that was a bug in the checker, not the document — GitHub's slug rule
  collapses runs of whitespace but leaves the **double hyphen** where an em-dash was stripped from
  between two spaces, so every `§N. OQ-x — Title` anchor looked wrong. Noted because the next
  person to write such a checker will hit it too.)

  **⚠️ Correction to the last handoff's git note:** it said `develop` was 10 ahead of
  `origin/develop` and nothing was pushed. That was true when written; `develop` has since been
  pushed. Current state: **`develop` is 1 ahead of `origin/develop`** (this session's commits) and
  **`main` is 17 behind `develop`**, not fast-forwarded. Code on both is level at `b110591` — every
  commit since is docs. Deliberate, as in the last four sessions; push and fast-forward when you
  want the docs live.

  **What is blocking: nothing blocks §11.** Outstanding, unchanged from the last session except
  where noted:

  1. **`temporal-full-migration.md` still contradicts the ADR** — step 3 is the worker port (now
     step 1), the line-314 diagram assumes the rejected bridge, and the 339–351 retry discussion
     describes a hazard that mostly no longer exists. ✅ Owner's call stands: **redraw in ONE pass
     after the review closes.** ⚠️ **§10's review adds to the redraw list:** §4 of that doc is the
     source of the "pure logic reused verbatim" phrasing now corrected in the ADR.
  2. **PRD-016 owes three things, all for one PM pass.** The Architect does not edit the PM's doc,
     which is why these accumulate rather than get fixed in place.
     (a) **§4's known exclusion** — that "two extractions on one fetched page" is *not* fixed by
     layer A. Owed since 2026-08-10.
     (b) **Two more R6 divergences from §10** — a pipeline webhook payload has no `job_id`, and no
     `diff_detected`/`diff_summary` until Monitors.
     (c) **Two passages still reason from §8's reversed storage rule** (`PRD-016:697` and `:802`,
     found in the 2026-08-26 doc sweep). Both cite *"only the final artifact is charged"* and argue
     that crawls need **no exception** to it. ⚠️ **Low urgency — the conclusion survives, only the
     reasoning is stale:** under §8's reversal (*every stored object is charged*) there was never a
     restriction to make an exception to, so "every stored crawl page is charged" is now true by
     default rather than by argument. Reword when the PM next opens the file; nothing downstream
     is wrong.
  3. **§11 is next** (run state to the SPA: mirror activity + `pg_notify`), then §13–§17.

  ### Superseded: START HERE (2026-08-25) — §8's blockers closed

  ### §8's two blockers CLOSED (2026-08-25) — not a section review; four owner calls

  §8 and §8d had been left carrying named open items that made the storage rule **un-implementable**
  and BUG-007 **unfixable**. All are now closed. Closing them **re-sequenced the pre-Phase 4 queue**,
  so this is not just bookkeeping.

  **1. ✅ The `latest/` copy is NOT chargeable.** The rule, stated plainly:

  > **Every `history/` object is charged, once. `latest/` is never charged, and is deleted with the
  > artifact it mirrors.**

  `latest/` is **kept as-is** — v2 already drops it, so the 2× discrepancy is v1-only with a known
  end date and removing it early buys nothing. This unblocks §8's storage rule and BUG-007.

  **🔴 Settling it exposed a fourth symptom nobody had counted: an LLM job leaves FOUR objects, not
  two.** The scrape writes the job's own format and the LLM **always** writes `.json`, so
  `latest/{job}.{fmt}` and `latest/{job}.json` are **different keys** and neither overwrites the
  other. Hard delete derives **one** filename from `job.output_format` (`routers/jobs.py:395`), so
  it removes whichever `latest/` matches the declared format and **orphans the other** — and *which*
  survives depends on the format (an `output_format=json` job has only three objects, because the
  LLM's write lands on the scrape's key). The assumption underneath is **one artifact in one format
  per job**: the same per-run granularity error as the counting stamp, expressed in the deletion
  path. Added to BUG-007.

  **2. ✅ §8d's accountant is named: an activity in the WORKFLOW worker, never the scraper workers.**
  ADR-001's *light worker — no DB access* rule holds, and the apparent conflict was a naming
  collision: after the migration "worker" means two things. The **scraper workers** (Go http,
  Playwright, LLM) run hostile pages through a real browser and stay DB-free; the **workflow
  worker** is the new pod holding orchestration that leaves the API — `result_consumer.py`'s direct
  successor, which has DB access by definition because orchestration *is* database work.
  **The boundary is enforced by task-queue routing, not convention:** a scraper pod is never offered
  accounting work because it is not listening on that queue.

  A **periodic batch sweep was considered and rejected as the primary counter**, on three grounds:
  it is a **new hand-rolled loop** of exactly the class this ADR exists to delete (Q8 was that loop
  failing); a **stale counter breaks the admission buffer** in call 4, which can only be as live as
  the number it reads; and it **cannot attribute bytes** without either a full-bucket join that
  grows forever (MinIO paths carry **no tenant segment**) or moving tenant identity into the worker
  pods — **verified: `user_id` appears in ZERO files across all four worker services today.**
  The sweep is **retained as auditor**: reconciliation (drift alarm) and §8c collection.

  **3. ✅ The record is ONE shared per-object ledger, not per-lane — and the meter is lane-blind.**
  Per-run columns cannot work at all: `job_runs` has one `result_path` and one
  `storage_accounted_at` while an LLM job has two chargeable objects — **that IS BUG-007** — and a
  pipeline run has one object per content-producing block, a count unknown at schema-design time.
  So a child table is forced; the only real choice was one table or one per lane.

  > **The design rule that makes the shared table safe: the meter reads `user_id` and `bytes`.
  > Nothing else.** `user_id` is on every lane already, never null, never needs widening — so the
  > meter is **not lane-aware at all** and a future lane cannot be forgotten out of it. The producer
  > link is recorded as nullable FKs but used **only** by delete and collection.

  Per-lane tables were rejected because their failure is **silent** — a missing `UNION` arm returns
  a well-formed, too-small number, **which is literally P7** — while a shared table's failure is a
  **loud rejected insert**. ⚠️ The precedent cuts both ways and is named in the ADR:
  **`webhook_deliveries` is already a shared table whose closed `num_nonnulls(job_id, batch_id,
  crawl_id) = 1` structurally rejects the pipeline lane**, so the `CHECK` must be designed to be
  widened.

  **Knock-ons applied, not merely noted:** **§8b's per-lane accounted-at markers are WITHDRAWN** —
  no lane gets its own, the ledger row *is* the marker, so "counting starts at cutover, no backfill"
  holds by construction rather than by a date comparison; **§3 is partly superseded for storage** —
  the counting view stays right for `monthly_runs`/`concurrent_jobs` (runs genuinely live in
  per-lane tables) but bytes all land in one owner-keyed table, so **do not build a storage arm of
  the view**; and **BUG-004's screenshots need no separate mechanism** — a stored object is a ledger
  row, so it is charged and deletable by the same path.

  **4. ✅ At the wall: the run FINISHES and is charged; a headroom buffer refuses to START new runs.**
  v1's delete-the-result-and-fail (`result_consumer.py:631`) is the wrong shape for a pipeline,
  because the ceiling is hit at the **last** block *after* the user's own LLM key was billed —
  failing there destroys paid work to reclaim a few KB. Admission-time checking cannot substitute:
  output size is not knowable before the run. ⚠️ **The buffer holds only while the largest single
  admitted run is smaller than it** — comfortable for jobs and pipelines, **false for crawls**,
  where one submission is up to `max_pages` fetches (2.8–40 GB at BUG-003's measured range).
  **Crawls need the ceiling checked per page as they go**; that belongs to P7's implementation. The
  buffer's *number* stays an operator dial.

  ### 🔴 The sequencing change — P8, and it is NOT Temporal-era work

  The ledger **reads** as v2 design because that is where it was derived. It is not. **BUG-007
  cannot be fixed without per-object accounting**, and **P7 needs per-page counting plus reclaim,
  which is the same table.** Build it once, before both, and v2 inherits it; build it after, and P7
  invents a crawl-shaped marker the ledger then replaces — **the exact per-lane mistake this whole
  bug family is made of, committed on purpose.**

  **Filed as P8. The pre-Phase 4 queue is now: P6/BUG-005 → P8 (ledger) → P7 + BUG-007 together.**

  ### Also corrected — a live trap in backlog §3

  Owed since 2026-08-10 and never made, sitting in the table `CLAUDE.md` tells every session to read
  before fixing an orchestration bug. The **BUG-001 row said `_recover_stale_pending` is "dissolved
  by Temporal — do NOT fix."** True of the **log spam**, false of the **loop**: `scheduler.py:131`
  re-publishes every `job_runs` row stale at `pending` past 10 minutes straight to NATS **with no
  lane filter**, and §3 keeps `job_runs` as a read-model mirror for migrated jobs — so a v2-owned
  run whose workflow has not started is dispatched to a **v1 worker**, and §7's mechanism 2 never
  intervenes because v1 started no *workflow*, it published a *message*. It fires **precisely when
  v2 looks stalled**. The row now says do-not-fix covers the log spam only, and points at §7's
  **mechanism 4** (lane marker on `job_runs`) as real work owed at **migration step 2**.

  ### Session close (2026-08-25)

  **Docs-only session — no code changed.** Two commits on `develop`:

  | commit | what |
  |---|---|
  | `2caaddc` | §8's two blockers closed — `latest/` not chargeable; §8d settled (workflow-worker activity + shared ledger + wall policy); §8b markers withdrawn; §3 partly superseded for storage; **P8 filed**; BUG-007 unblocked |
  | `7e6d9ec` | backlog §3 — BUG-001's do-not-fix corrected to cover the log spam, not the lane-blind loop |

  All **21** ADR internal anchors re-verified after the §8d retitle. Three rows retired from the
  ADR's "Deliberately not decided here" table (they are decided); one added for what genuinely
  remains — the buffer's number and per-page crawl checks.

  **⚠️ `develop` is 10 ahead of `origin/develop` and `main` is behind it — nothing pushed, nothing
  fast-forwarded.** Deliberate, as in the last three sessions; push when you want the docs live.

  **What is blocking: nothing blocks §10.** What remains outstanding:

  1. **`temporal-full-migration.md` still contradicts the ADR** — step 3 is the worker port (now
     step 1), the line-314 diagram assumes the rejected bridge, and the 339–351 retry discussion
     describes a hazard that mostly no longer exists. ✅ **Owner's call: redraw in ONE pass after the
     review closes**, not incrementally — later sections may move things again. Not blocking, but it
     would confidently tell an implementer to build the thing §9 rejected.
  2. **PRD-016 owes §4's known exclusion** — that "two extractions on one fetched page" is *not*
     fixed by layer A. Owed since 2026-08-10; deliberately not done because the Architect does not
     edit the PM's doc. Needs a PM pass.
  3. **§10 is next, and it is first-increment work** — the workers port first, so the do-not-delete
     list ports with them: `ensure_ready()` + the 180s timeout, the transient/terminal classifier on
     all three workers, bot-wall detection, SSRF re-validation. §9's constraint on it: **the ported
     classifier becomes `RetryPolicy` non-retryable error types, not its own retry loop inside the
     activity** — otherwise R4's "retry in exactly one visible layer" is violated one level down
     from where it was fixed.

  ### Superseded: START HERE (2026-08-23) — §9 review

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
| — | **§1 queue: 3 open — P6/BUG-005 → P8 → P7 + BUG-007** | ⚠️ **This table is a snapshot and goes stale first — `phase4-backlog.md` §1 is the source of truth.** P8 (the shared per-object storage ledger) was inserted between P6 and P7 on 2026-08-25. **Next: resume the ADR-009 section review at §14.** §1–§13 are done — see the ADR's Review log, which is authoritative |
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
