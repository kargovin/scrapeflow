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
| [ADR-002](ADR-002-phase2-worker-contract.md) | Phase 2 Worker Contract | **Accepted** | 2026-04-02 | ADR-001 (§2, §3, §8) | — |
| [ADR-003](ADR-003-job-run-split.md) | Job/Run Data Model Split | **Accepted** | 2026-04-09 | — | — |
| [ADR-004](ADR-004-phase3-fat-message-schema.md) | Phase 3 Fat Message Schema v2 | **Accepted** | 2026-04-15 | — | — |
| [ADR-005](ADR-005-site-crawl-bfs-coordinator.md) | Site Crawl BFS Coordinator | **Accepted** | 2026-04-15 | — | — |
| [ADR-006](ADR-006-batch-scraping-data-model.md) | Batch Scraping Data Model | **Accepted** | 2026-04-15 | — | — |
| [ADR-007](ADR-007-job-secrets-storage.md) | Job Secrets Storage | **Accepted** | 2026-04-15 | — | — |
| [ADR-008](ADR-008-playwright-antibot-hardening.md) | Playwright Worker Anti-Bot Hardening | **Accepted** | 2026-07-03 | — | — |
| [ADR-009](ADR-009-workflow-engine-temporal.md) | Workflow Engine — Temporal + v1/v2 Coexistence Contract | **Draft** | 2026-08-04 | — | — |

**ADR-009 is drafted and its section-by-section review is IN PROGRESS — it is not yet a decision.**
**§1–§12 have been reviewed** (several reversed in the process); §13–§17 have not.
The ADR's own **Review log** (top of the file) is authoritative for which sections are settled —
prefer it over any summary, including this one. It records the Phase 4
engine decision (Temporal), answers all **11** of
PRD-016's open questions, and defines the contract under which the NATS path (**v1**) and the
Temporal path (**v2**) run side by side. Its inputs were
**[PRD-016](../project/phase4-prd/PRD-016-workflows-pipelines.md)**, `workflows-scoping.md` §7
(the engine comparison), `temporal-full-migration.md` (change inventory + strangler-fig sequence),
`open-questions.md` **Q8** (the incident grounding the decision), and `open-bugs.md` **BUG-005**
(the batch identity failure that grounds its run-identity and artifact-path decisions).

Three things in it that are easy to miss and expensive to rediscover:

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
- **§5 partially departs from ADR-002 §8** (the MinIO path convention) for the v2 lane only.

It will eventually **supersede parts of ADR-001/002/004** — the NATS subjects, fat-message schema,
and worker result contract are all deleted at the migration's end state. Those rows stay empty
until then, per ADR-009 §17: the contracts remain authoritative for as long as v1 serves traffic,
and the supersession notices are added when each v1 component is actually deleted.

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
