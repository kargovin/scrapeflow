# ADR-017: An Expired Approval on a Monitor Check Sends a Monitor Notice

**Status:** **Accepted** 2026-11-24 (owner) — the decision is the owner's (2026-11-24). Immutable from here: a change
is a new, superseding ADR.
**Date:** 2026-11-24
**Supersedes:** ADR-016 §8's last bullet and §9c's first bullet, **for the expiry of a monitor check's approval
only**. Everything else in ADR-016 stands.

## 1. The problem

ADR-016 §8 says approval messages are not notices, and §9c sends all of them (request, reminder, `expired`,
`superseded`) to the Approval block's destination, "none to the monitor's". Release N (2026-11-23) built that.

PRD-018 says otherwise for expiry: R5 — *"Expiry → `completed`, labelled 'approval expired', plus a monitor notice
(R6)"*; its acceptance list — *"No answer → 'approval expired' at the deadline, a notice is sent"*; and Q4's answer
rests on it — *"R6's notice already tells the owner"*. The ADR followed the "one thread, never duplicated" line of
review round 10 and dropped the notice. At the release the owner chose the PRD: **an expired approval sends a
notice.**

Why it matters: an expiry means nobody looked. The `pipeline.approval_expired` message goes to the same place the
unanswered request went, so the one channel that was missed is the one told. The monitor's notice destination is
where the owner hears that the monitor needs attention (paused itself, checks skipped, gave up waiting).

## 2. Decision

**When a monitor check ends `completed` with `stop_reason = approval_expired`, the monitor records a
`monitor_events` row of the new kind `approval_expired` and, when the monitor names a notice destination, queues a
`monitor.notice` for it, exactly as ADR-016 §8 does for its other notice kinds.**

- **Unchanged:** the block's destination still gets `pipeline.approval_expired` (§9c). Both are sent; they are
  different messages to (possibly) different places. PRD-018's *"approval messages go to the Approval block's
  destination only, never also to the notice destination"* still holds: the notice is a monitor notice
  (`monitor.notice`, `notice: "approval_expired"`), not a copy of the approval message.
- **Only expiry.** Rejected (an answer) and superseded (a later check took over) send no notice. A pipeline run
  with no monitor sends none — it has no monitor.
- **A point event, not a situation.** The other kinds describe something ongoing (skipping, paused) and stay open
  until it ends. An expiry happens once: its row is written closed (`ended_at = first_at`) and closes no other open
  situation. One row and one notice per expired check: `detail` carries the check's `run_id`, its deadline and the
  run's SPA link (`PUBLIC_BASE_URL`).
- **Who writes it:** the writer of the check's ending — `monitor_check_ended`, in the same transaction, under the
  lock order §8 fixes (the user first, then the run). Idempotent under a retried writer (one row per run).
- **Schema:** the `monitor_events.kind` CHECK widens by `approval_expired` (a migration); `NOTICE_KINDS` gains it.
  Payload, delivery, signing, SSRF, retries: §8 unchanged.
- **No workflow change.** The notice is written by an activity the workflow already calls; no patch, no new
  history shape.

## 3. Consequences

- An owner with a notice destination hears about every expired approval of every monitor, in one place, besides
  the message on the block's own destination.
- Expired checks stay out of the baseline (ADR-016 §4a); the next check that still differs asks again, and if it
  also expires, a second notice is sent. That is one notice per deadline, the cadence Q4 accepted.
- ADR-016 is Accepted and immutable: its §8 and §9c text is unchanged, and this record names the clauses it
  displaces.
