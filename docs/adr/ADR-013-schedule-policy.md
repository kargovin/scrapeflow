# ADR-013: Schedule Policy — `SKIP` on Overlap, and a Per-User Cap on Scheduled Jobs

**Status:** **Draft.** The overlap decision is the owner's, taken 2026-10-13 (*"go with SKIP"*); the per-user
cap's existence is the owner's, same day (recorded in ADR-010 §1's rider). Its **default value**, whether
**paused** schedules count, and the **catch-up window** are recommendations in this text, not decisions —
see §5. Written by the Tech Lead; awaits the owner's review before promotion.
**Date:** 2026-10-13
**Deciders:** @karthik
**Resolves:** implementation backlog **F.0** (gate before F.1 creates the first Temporal Schedule; tracked as
**X.3**), and the open rider in **ADR-010 §1** (*"the Schedule overlap policy is still undecided"*).
Not a supersession — ADR-009 and ADR-010 chose nothing here.

---

## 1. The problem

At the schedule-and-webhook cutover (Group F) each recurring job becomes a **Temporal Schedule** that starts
one workflow per cron tick. Every Schedule carries three policies, and every one of them has a default we
would otherwise inherit without choosing:

| policy | SDK default (`temporalio` `SchedulePolicy`, verified 2026-10-13) | question |
|---|---|---|
| `overlap` | `SKIP` | a tick fires while this job's previous workflow is still open — what happens to the tick? |
| `catchup_window` | 365 days | the Temporal server was down across one or more ticks — are they run late on recovery? |
| `pause_on_failure` | `False` | a run fails — does the Schedule stop firing? |

"Still open" includes a workflow **parked** on `concurrent_jobs` (ADR-010 §1): it is waiting, but to the
Schedule it is running.

The overlap policy is per Schedule, so it cannot bound anything **across** a user's jobs — N scheduled jobs
blocked on concurrency are N parked workflows. That is what the per-user cap is for.

### What v1 does today, for comparison

- **Overlap: nothing stops it.** `_dispatch_due_jobs` never looks for the job's own active run, so an hourly
  job whose run takes 70 minutes has two runs at once — two concurrency slots, and results (and webhooks)
  that can land out of order. Effectively `ALLOW_ALL`.
- **Catch-up: every missed tick, one per minute.** `next_run_at` advances from its *stored* value
  (`croniter(cron, job.next_run_at).get_next()`), so after a 3-hour API outage an hourly job dispatches
  three overdue runs on three consecutive 60 s loops.
- **Failure: never pauses.**
- **Cap: none.** A user may create any number of scheduled jobs; the only bound is the 5-minute minimum
  interval per job (`schedule_min_interval_minutes`).

## 2. Decision

1. **`overlap=ScheduleOverlapPolicy.SKIP`**, set **explicitly** on every Schedule we create — not inherited
   from the SDK default, which is the same value today but is not our contract. A tick that fires while the
   job's previous workflow is open is dropped. Temporal counts each drop in
   `ScheduleInfo.num_actions_skipped_overlap`.
2. **A per-user cap on active scheduled jobs** (owner, 2026-10-13), enforced by the API wherever a job becomes
   an active schedule: create with `schedule_cron`, a PATCH that sets `schedule_cron`, and a PATCH that sets
   `schedule_status` back to `active`. Over the cap → **429** with the same `quota_exceeded` body shape the
   other meters use (`quota_type: "scheduled_jobs"`). The check is a count followed by a write, so it runs
   under **BUG-023's** per-user lock. Limit stored like the others: a nullable `user_quotas` column, falling
   back to a `settings` default.
3. **`pause_on_failure=False`**, explicit. A run that fails — a dead site, a bad LLM key, a storage breach
   (ADR-010 §1) — does not stop the job's future ticks, as on v1. A paused Schedule nobody un-pauses is a
   silent stop, and nothing in the product would surface it.
4. **`catchup_window`: set explicitly** — value per §5.

### What `SKIP` does in the two cases that matter

```
Parked (concurrency full 10:00–10:12, job every 5 min)
  10:00 tick → parks ───────── 10:12 slot frees → runs
  10:05, 10:10 ticks → dropped
  10:15 tick → runs normally                        → one run when the slot frees

Overrun (job every 5 min, each run takes 7 min)
  10:00 run ───────── 10:07
  10:05 tick → dropped
  10:10 run ───────── 10:17
  10:15 tick → dropped                              → settles at one run per 10 min
```

The parked case loses nothing: the parked workflow is the run, and it executes when the slot frees. The
overrun case drops a tick, and the job runs at the pace it can actually sustain.

## 3. Alternatives

- **`BUFFER_ONE`** — one tick queued behind the open workflow, later ones dropped. In the parked case it adds
  a second run right after the first, fetching a near-identical page for another monthly unit and another
  slot. In the overrun case it delivers the missed tick late instead of dropping it — but a job slower than
  its own interval then runs back to back forever, permanently holding one of the user's concurrency slots,
  and nothing tells the user their job cannot keep its schedule. Its only gain is latency after an overrun,
  bounded by one interval. Rejected.
- **`BUFFER_ALL`** — every tick queued. Reproduces the pile-up ADR-010's amendment removed. Rejected.
- **`ALLOW_ALL`** — v1's behaviour. Two executions of one job at once, results that can finish out of order,
  and a parked run that does not stop the next tick from parking beside it. Rejected; ordering also matters
  more once Monitors compare each run against the previous one.
- **`CANCEL_OTHER` / `TERMINATE_OTHER`** — the new tick kills the open one. A parked run would be cancelled by
  every tick and never execute; an overrunning run would never finish. Rejected.
- **No cap; rely on the overlap policy.** It bounds each Schedule to one open workflow, not the number of
  Schedules. Rejected by the owner.
- **A cap on parked runs instead of on scheduled jobs.** With `SKIP` each Schedule holds at most one open
  workflow, so the scheduled-jobs cap already bounds parked runs. A second cap would be a second number to
  explain for no additional bound. Not taken.

## 4. Consequences

- **A user-visible change from v1, deliberately.** An overrunning job no longer overlaps itself; a tick is
  dropped instead. A job never holds two slots, and its runs finish in order.
- **Dropped ticks are countable, not yet shown.** `num_actions_skipped_overlap` is cumulative per Schedule and
  readable through `describe()`. Showing it on the job page (*"3 runs skipped — this job takes longer than its
  interval"*) is the obvious use; it is **not** in F.1's scope unless added.
- **A new 429** on create, schedule PATCH and resume — only requests that involve a schedule can see it. ⚠️ **Resume is the easy one to miss**: a paused job re-armed past the cap must be
  refused like a new one.
- **The cap is lane-blind on purpose.** Today only jobs have schedules (the scheduled-crawl path is dormant);
  Monitors (PRD-018) will add pipeline schedules. The cap counts *every* schedule a user owns, so the lane
  that adds one widens the count in the same change — the project's recurring defect is a meter keyed on a
  table a new lane does not write (BUG-005, P7).
- **Code that relies on these:** F.1 (Schedule create / pause / delete) passes all three policies; F.1's
  admission activity and the cap check share BUG-023's lock.

## 5. Open — the owner's call before promotion

| item | recommendation | why |
|---|---|---|
| **Default cap** (`settings.default_quota_scheduled_jobs`) | **20**, overridable per user | Bounds parked workflows per user to 20. It is not the meter that limits cost — at the 5-minute floor a single job makes 288 runs a day against a 500-run monthly default, so `monthly_runs` binds first; the cap exists to bound Temporal state, so it can be generous. A product number, not an engineering one |
| **Do paused schedules count?** | **No** — only `schedule_status = 'active'` | A paused Schedule fires nothing and parks nothing, so it costs nothing the cap protects. Counting it would make users delete schedules they mean to resume. The resume check above is what keeps this safe |
| **`catchup_window`** | **Pin the 365-day default explicitly** | With `SKIP`, a recovered server's missed ticks should collapse to **one** late run: the first starts a workflow, the rest find it open and are dropped. So a long window means *"after any outage, the job runs once, late"*, and a short one means *"a daily or weekly job's tick during an outage is lost until its next tick"*. One late run is the better outcome, and it is less than v1 (which replays every missed tick). ⚠️ **The collapse is reasoned from the policy definitions, not tested** — verify at F.1 on compose (a 1-minute Schedule, stop `temporal` for 5 minutes, count executions on restart). ⚠️ *Changed from the Tech Lead's earlier suggestion of a short window (2026-10-13), which did not account for `SKIP` already bounding the catch-up* |
