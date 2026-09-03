# ScrapeFlow — ADR Index

Architecture Decision Records for the ScrapeFlow platform.

An ADR captures a significant architectural decision: what was decided, why, what alternatives were considered, and what the consequences are. Once accepted, an ADR is immutable — if a decision changes, a new ADR is written that supersedes the old one. The old ADR is updated with a supersession notice but never edited to match the new decision.

---

## Status Definitions

| Status | Meaning |
|--------|---------|
| **Draft** | Written but not yet reviewed. **Not a decision** — do not implement against it, and do not cite it as settled in another document |
| **Proposed** | Reviewed and under discussion — not yet implemented |
| **Accepted** | Decision is final and has been (or is being) implemented |
| **Partially Superseded** | Some sections replaced by a later ADR; see supersession notice for which sections remain authoritative |
| **Superseded** | Fully replaced by a later ADR; kept for historical context only |
| **Deprecated** | Decision was reversed; the described approach is no longer in use |

---

## ADR Registry

| ADR | Title | Status | Date | Supersedes | Superseded by |
|-----|-------|--------|------|------------|---------------|
| [ADR-001](ADR-001-worker-job-contract.md) | Worker Job Contract | **Partially Superseded** | 2026-03-25 | — | ADR-002 (§2 Subjects, §3 Schemas, §8 MinIO paths) |
| [ADR-002](ADR-002-phase2-worker-contract.md) | Phase 2 Worker Contract | **Partially Superseded** | 2026-04-02 | ADR-001 (§2, §3, §8) | ADR-011 (§4 MinIO paths) |
| [ADR-003](ADR-003-job-run-split.md) | Job/Run Data Model Split | **Accepted** | 2026-04-09 | — | — |
| [ADR-004](ADR-004-phase3-fat-message-schema.md) | Phase 3 Fat Message Schema v2 | **Accepted** | 2026-04-15 | — | — |
| [ADR-005](ADR-005-site-crawl-bfs-coordinator.md) | Site Crawl BFS Coordinator | **Accepted** | 2026-04-15 | — | — |
| [ADR-006](ADR-006-batch-scraping-data-model.md) | Batch Scraping Data Model | **Accepted** | 2026-04-15 | — | — |
| [ADR-007](ADR-007-job-secrets-storage.md) | Job Secrets Storage | **Accepted** | 2026-04-15 | — | — |
| [ADR-008](ADR-008-playwright-antibot-hardening.md) | Playwright Worker Anti-Bot Hardening | **Accepted** | 2026-07-03 | — | — |
| [ADR-009](ADR-009-workflow-engine-temporal.md) | Workflow Engine — Temporal + v1/v2 Coexistence Contract | **Accepted** | 2026-08-04 | — | — |
| [ADR-010](ADR-010-crawl-admission-and-scheduled-quota.md) | Scheduled-run Quota Admission, and Sitemap Origin Scope | **Draft** | 2026-09-08 | — | — |
| [ADR-011](ADR-011-artifact-identity-and-paths.md) | Artifact Identity — Run-keyed Paths, Stage-named Objects, and the Removal of `latest/` | **Accepted** | 2026-09-03 | ADR-002 §4 | — |

**ADR-009 is Accepted as of 2026-09-08.** Its section-by-section review completed 2026-09-05 —
**§1–§17 and both closing blocks**, several reversed in the process — and the owner took the
promotion decision on 2026-09-08. The Date column above is its **drafting** date (2026-08-04).
⚠️ **The closing blocks were the last to be reviewed and were the most stale**,
because they had only ever been amended as knock-ons — fourteen corrections, including a promised
Web UI dashboard §2b does not expose, a deferral row describing a crawl-frontier question §13
closed, and **two explicitly-named open items missing from the table of open items**.
The ADR's own **Review status** block (top of the file) is authoritative for which sections are
settled — prefer it over any summary, including this one. It carries three tables worth knowing
about: the per-section **review log**, a list of everything **reversed or withdrawn** during the
review, and the sections **amended as a knock-on** by a later section's review (which is how a
"already reviewed" section still goes stale). It records the Phase 4
engine decision (Temporal), answers all **11** of
PRD-016's open questions, and defines the contract under which the NATS path (**v1**) and the
Temporal path (**v2**) run side by side. Its inputs were
**[PRD-016](../project/phase4-prd/PRD-016-workflows-pipelines.md)**, `workflows-scoping.md` §7
(the engine comparison), `temporal-full-migration.md` (change inventory + strangler-fig sequence),
`open-questions.md` **Q8** (the incident grounding the decision), and `open-bugs.md` **BUG-005**
(the batch identity failure that grounds its run-identity and artifact-path decisions).

Four things in it that are easy to miss and expensive to rediscover:

- **§9 was REVERSED on 2026-08-23 — the NATS bridge (option (a)) is rejected**, and the three
  workers become Temporal activity workers in the **first** increment. ⚠️ *This entry previously
  said option (a) merely "recreates the Q5/Q6/Q7 failure mode" and that NATS-side retry must be
  neutralised. That described the rejected design and is withdrawn:* with no NATS beneath the
  activity there is one retry layer, Temporal's. The bridge was also found to be **blocked** — a
  work-queue stream refuses a second consumer overlapping `api-result-consumer`'s claim, proven by
  a dead service in production (**BUG-008**).
- **§10's carry-forwards are the most likely accidental loss in the migration.** `diff.py` and the
  content-hash are **relocated, not deleted, and not yet re-homed** — the PM assigned change
  detection to Monitors (B), which is unwritten. ⚠️ **They are not equally at risk** (§10 review,
  2026-08-26): `diff.py` is its own module and survives its caller's deletion intact, while the
  content-hash is seven lines *inside* `result_consumer.py`. And the dedup branch is **not** "pure
  logic" — it deletes the new object and repoints `result_path` at the **previous run's** object,
  which is the cross-run sharing §8 recorded as breaking per-run GC.
- **§10's transient/terminal classifier does NOT become a `RetryPolicy` field.** Withdrawn on
  review: Temporal offers only a denylist of error type *names*, while the classifier is a
  fail-closed allowlist that also reads exception attributes and, in Go, keys on which step raised
  the error. The rule is **the classifier decides; Temporal retries** — terminal verdicts raised as
  non-retryable application errors.
- **§11 keeps TWO writers on the run-status column, and the precedence rule is load-bearing.**
  Status reaches the browser as a row write plus `pg_notify` in the same transaction, and **both**
  the background consumer and the API's own request handlers write and notify — `routers/jobs.py`
  and `routers/admin.py` do it for cancellation, inside the request. That is preserved into v2 on
  purpose (routing cancellation through the workflow makes the button appear dead for minutes,
  since a block is never aborted mid-execution). ⚠️ **The failure mode is silent:** forget the rule
  and a cancelled run flips back to `completed` with the work done and charged. **The rule — a
  cancellation written by the API wins, and a mirror write never moves a run out of a terminal
  state it did not itself set — already exists on the job path and must be re-established, not
  invented, for pipelines.** Same section: notify payloads carry **identifiers and status only**
  (`pg_notify` caps at 8000 bytes and shares the row's transaction, so an oversized payload rolls
  the status write back) and **absolute state, never deltas** (activities are at-least-once).
- **§13 (reviewed 2026-08-28) — the crawl migration is a REWRITE, not a port, and `crawl_queue`
  does NOT retire.** Two things there are easy to carry forward wrongly. First, BUG-008 is wider
  than "one consumer is missing": because nothing reads crawl results, link extraction and sitemap
  discovery **have never run either**, so a crawl in production has never got past dispatching its
  seed page — and ⚠️ **§9's diff-against-a-v1-run pre-gate therefore does not exist for crawls and
  cannot be made to exist.** Second, the draft's clause that the frontier table retires is
  **withdrawn**: 51,200 history events over a 10,000-page ceiling is ≈5 per page against 3 for the
  cheapest possible activity, and a 10,000-URL visited set is ≈800 KB against §5's 2 MiB payload
  limit — so `continue-as-new` is mandatory in **both** candidate designs and **the frontier stays
  in Postgres**, reached through activities. Also settled: **`crawl_pages` is required** (it is
  P7's metering unit, §8d's ledger producer link, and the artifact's name), and **every URL
  entering the frontier is SSRF-checked** — the `httpx` swap this section already required is the
  smaller half of what is wrong in `sitemap.py` (**BUG-010**).
- **§5 partially departs from ADR-002 §4** (the MinIO path convention) for the v2 lane only.
  ⚠️ **That gap closed on 2026-09-03**: ADR-011 supersedes ADR-002 §4 and re-keys the **v1** lane on
  the producing row with stage-named objects, which is the shape §5 already gave v2 (run + block).
  §5 is no longer an exception to the live convention — it is the same convention, one lane ahead.

It will eventually **supersede parts of ADR-001, ADR-002, ADR-004, ADR-005 and ADR-006** — the
NATS subjects, the fat-message schema, the worker result contract, the crawl coordinator process
and the batch result-consumer routing are all deleted at the migration's end state. Those
`Superseded by` rows stay empty until then, per **ADR-009 §17**: a contract keeps its authority
for **the flows still served by v1** (authority is per flow, not one global switch — after the job
cutover ADR-002 is authoritative for batch and crawl and not for jobs), and each notice is added at
a **named step of §16's sequence**, not at a vague "component deletion" — ADR-004 belongs to the
stream rather than to any component and dies at the *batch and crawl cutover*, before anything is
deleted. Four things there are easy to get wrong and are settled in that section's table:

- **ADR-001 §2/§3/§8 are not ADR-009's to supersede** — ADR-002 took them on 2026-04-02. What
  ADR-009 replaces is ADR-001 **§4–§7**, and **§4 only in part**: its retry and status-update rows
  go, while ⚠️ **"Worker dependencies: NATS + MinIO only. No database access." survives the whole
  migration** and is relied on by §9, §8d and 16b. A section-level "superseded" stamp on §4 would
  assert the opposite.
- **ADR-005 is partially superseded, and two of its four sections are UPHELD BY NAME** — §13 keeps
  the Postgres `crawl_queue` frontier (the draft's clause that it retires was withdrawn) and makes
  `crawl_pages` **required**. Only the dedicated coordinator process and the crawl NATS subjects go.
- **ADR-006 keeps its data model** — `batches`/`batch_items` and the nullable `job_id` are cited as
  *correct* in BUG-005's root cause; only its result-consumer routing section is superseded.
- ⚠️ **ADR-001 §6 and ADR-002 §6 are already false of live code** — both state there is no
  application-level retry loop in the worker, and all three workers have had one since Q5/UF-003.
  Recorded in §17e as a known divergence; ADRs are not edited to match drifted code, so the
  correction rides with the supersession notice.

**ADR-003, ADR-007 and ADR-008 are unaffected** and stay fully Accepted — ADR-008's scraping
behaviour is explicitly the thing the transport change must leave untouched.

---

## ADR-011 — accepted 2026-09-03

**ADR-011 is Accepted as of 2026-09-03**, drafted and promoted the same day, and it is the **first
supersession in this index that has actually landed** — every other one is still pending a named
step of ADR-009 §16's sequence. It takes **ADR-002 §4 only**; ADR-002 keeps its subjects and
schemas. Four things in it are easy to get wrong:

- **The key is not `run_id`.** That is the obvious candidate, and §1 rejects it by name: the crawl
  coordinator fabricates a `uuid4()` `run_id` for a lane that creates no `job_runs` row, so keying
  on it names objects after a row that does not exist. The key is **the producing row** —
  `job_runs.id` for job and batch, `crawl_pages.id` for crawl — carried in a lane-neutral
  `artifact_id` the dispatcher supplies and the worker never interprets.
- **The stage segment is load-bearing, not cosmetic.** A flat `history/{artifact_id}.{ext}` silently
  reintroduces the collision the ADR exists to remove, because `llm-worker` hardcodes `ext="json"`:
  an `output_format=json` job's extraction would overwrite its own scraped page.
- **§6 has a precondition that is not in the fix.** There are **ten publish sites building raw
  dicts and zero producer-side schemas**, so the cross-service contract test is meaningless until
  the API has one typed message model — a hand-written test payload is the same guess the worker
  fixtures already make.
- **It ships whole or not at all.** Fixing the schemas without the paths leaves the collisions;
  fixing the paths without the schemas leaves the drops.

It unblocks **P6 (BUG-005)** and covers **all three lanes** — the crawl lane's inclusion was
confirmed by name at promotion, despite BUG-008 meaning nothing reads crawl results on v1.

⚠️ **`latest/` is removed, which implies a one-time sweep of existing objects** (§4). That is a
production data deletion and is not itself a decision the ADR makes about *when* — it lands as part
of P6.

**Two cross-record effects, both easy to miss because neither ADR-011 nor the other record reads as
wrong on its own:**

- ⚠️ **ADR-011 displaces one clause of ADR-009 §8d, and only one.** §8d's owner call of 2026-08-25
  settled *two* things: **charge one copy** (`latest/` is never charged) — **upheld, unchanged** —
  and **`latest/` is kept as-is** because "removing it early costs work for a convenience that
  expires on its own" — **reversed**, because P6 rewrites the path convention anyway, so keeping it
  means carrying a dead concept through P8's ledger as an *uncharged mirror* class and then deleting
  it. **Consequence: the 2× storage discrepancy §8d accepted as v1-only ends at P6, not at the v2
  cutover.** Both ADRs are Accepted and immutable, so the displacement is declared in ADR-011's
  header rather than edited into ADR-009.
- ⚠️ **[ADR-003](ADR-003-job-run-split.md) describes `result_path` in the old format**
  (`history/{job_id}/{unix_timestamp}.{ext}`, pointing at ADR-002 §4). ADR-011 does **not** supersede
  it: what ADR-003 decides — that `result_path` holds the immutable per-run path and is what the diff
  algorithm retrieves — is still exactly true, and is what makes the convention change need no
  backfill. Only the illustrative path *shape* has drifted. Recorded here rather than edited in, on
  the §17e principle: **ADRs are not edited to match a decision made after them.**

---

## What belongs in an ADR vs `ARCHITECTURE_DECISIONS.md`

**Use an ADR when the decision:**
- Defines a contract between two or more services (especially cross-language)
- Is a schema decision that is hard or impossible to reverse
- Will be referenced by engineers implementing separate components who may never read the full codebase

**Use `ARCHITECTURE_DECISIONS.md` when the decision:**
- Is an implementation choice within a single service
- Is reversible with a normal code change (no migration, no protocol bump)
- Is primarily interesting for context, not as a binding implementation contract

---

## How to write a new ADR

1. Copy the structure of an existing ADR (Status, Date, Deciders, Context, Decisions, Consequences)
2. Number sequentially: `ADR-NNN-short-title.md`
3. Add a row to this index
4. If it supersedes an existing ADR: update the superseded ADR's status header and add inline `> ⚠ Superseded by ADR-NNN` notices at the specific sections that changed
5. Reference the ADR from `CLAUDE.md` if it defines a contract engineers will need during implementation
