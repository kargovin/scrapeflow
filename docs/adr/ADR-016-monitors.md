# ADR-016: Monitors — Schedules, the Baseline, the Change Gate, Approval

**Status:** **Draft** — written 2026-11-18 by the Architect persona; owner review in progress (round 11). Implement nothing against it
until the owner's review accepts it.
**Date:** 2026-11-18
> **Review round 1 (owner, 2026-11-18): §1 The problem, §2 The data model.** Two corrections accepted. (1) §2d
> check 6: the refusal is on the blocks' *need* plus the allowance, and the check's ceiling is capped at the
> maximum; as drafted, a version declaring the full 86,400 s could never be monitored. (2) §2c: a cron change on an
> active monitor that the engine refuses is put back with a `503`, as for jobs. **`notify_on_overlap` moves to *not
> yet*** (the history shows every overlap skip; the opt-in notice can come later). **§2c gains what the SPA and
> admin screens need**: the run response's new columns, a diff read, a monitor filter on the run list, the
> monitors using a pipeline, a jobs/monitors split of the schedule count, admin monitor reads, and
> `monitors_paused_itself`. Owner asked where "check" comes from: PRD-018 D1 and review round 5 — a check is a
> pipeline run a monitor started; a firing becomes a check only if admission lets it run. Pipeline-lane admin
> metrics, missing today, go to the backlog (§4), not this ADR.
>
> **Review round 2 (owner, 2026-11-18): §3 The firing.** Agreed, with one gap closed: admission is a **third
> writer** of a failed run (storage full, refused firing), outside ADR-014 §8a's two, so it calls
> `queue_failure_alerts` and `monitor_check_ended` in its insert transaction — otherwise *the run breaks* stays
> silent for exactly those failures. §7a's writer table and §19's trap updated.
>
> **Review round 3 (owner, 2026-11-18): §4 The baseline.** Agreed, with `baseline_outputs` keyed by the content
> block that produced each object (the block feeding the gate for the page diff, the LLM block for the extraction
> diff), present only while its `collected_at` is empty. Owner asked whether a check waiting for approval blocks a
> new one: no, by design (PRD-018 R1, D10, Q3 = supersede) — at most one waits, it holds no slot, and only
> *running* checks exclude each other; it is also why the newest baseline is chosen by start time.
>
> **Review round 4 (owner, 2026-11-18): §5 The change gate.** Agreed, with two corrections. (1) §5d: an old LLM
> worker does not read every page as changed — its `rules` field is a closed union, so `changed` fails to decode
> and the check fails; ADR-014's "unknown fields are ignored" covers fields, not a new rule kind. Workers first
> still stands. (2) §5b: the stored config is a full dump with defaults, so "absent" is read from the submitted
> JSON.
>
> **Review round 5 (owner, 2026-11-18): §6 What changed, carried values, the allowance.** Agreed, with one
> correction and one note. (1) `core/diff.py` is not caller-less: `core/webhooks.py` imports `DiffResult` for
> `create_webhook_delivery`'s unused `diff` parameter, so §14's cleanup removes the `compute_*` functions, the
> parameter and the type together. (2) A raw-HTML page diff reads poorly (minified HTML is a few long lines); the
> docs point at R3's gate-after-Clean. "Step" reworded to "activity" where it meant one: Diff and Capture are
> activities no block owns, run on the `content` task queue the LLM worker polls.
>
> **Review round 6 (owner, 2026-11-18): §7 Self-pause and the monitor's history.** Agreed, with three fixes. (1) The
> counter moves only in the call that ends the check: the mirror and admission are retried, and a bump per retry
> would pause a monitor after fewer than five real failures. (2) Every `monitor_events` write is under `lock_user`
> (the pipelines-switched-off skip had been drawn before it). (3) Pausing closes any open situation, so a skip
> after resuming sends one fresh notice. Owner asked what a notice is: a message about the monitor itself (paused
> itself, monthly runs used up, gave up waiting), always in the SPA, also sent to the notice destination once one
> exists; not a run's failure alert, not an approval message.
>
> **Review round 7 (owner, 2026-11-18): §8 Notices.** Agreed, with two fixes. (1) `webhook_delivery.pending()` gains
> a `monitor_event_id` filter, or a notice's best-effort start finds no row and waits for the sweep. (2) Lock order,
> a knock-on of round 6: `mirror_run_status` writes `paused_itself` under `lock_user` but locks the run row first,
> the reverse of §11's deletes; for a monitor check it takes the user lock first. A notice still retrying towards a
> destination the user has since deleted fails with ADR-015 §2c's final "destination deleted" — accepted.
>
> **Review round 8 (owner, 2026-11-18): §9 The Approval block.** Agreed (walked through again in plain words with
> an example: Priya's price push). Approval's catalog `Timing` covers only its two DB activities; the wait is
> excluded (§9d). **G8 added and answered (a):** an approved run continues without re-asking for a slot, so
> answering several approvals together can briefly exceed the concurrency limit. G5 agreed with §9.
>
> **Review round 9 (owner, 2026-11-18): §10 Endings, §11 Storage.** Agreed, with one gap closed: admin user delete
> removes the user's monitor Schedules best effort, as it does for job Schedules; the cascade alone would leave
> each one firing into "stopped" for ever.
>
> **Review round 10 (owner, 2026-11-18): §12 Meters, §13 Keys and destinations, §14 Crawls and leftovers.** Agreed,
> with two fixes. (1) §2d gains the check §13 relies on: the pinned version's LLM keys exist and are the user's,
> under `lock_user` (a key can be deleted after the pipeline was saved). (2) §14's cleanup removes
> `CrawlResponse.schedule_cron` in release A (stop using), before the column drop in release B; no caller of ours
> reads it. §14 was walked through again with background (what `schedule_cron` promised, why scheduled crawls are
> not planned, why two releases).
>
> **Review round 11 (owner, 2026-11-18): §15 Workflow code, §16 Rollout.** Agreed, with two improvements. (1) The
> slice-1 release gate drives 23 checks with Check now and one with a real firing (minutes, not 2 hours at the
> 5-minute floor). (2) The LLM worker's half of slice 1 (the `changed` rule, Diff, Capture — additive) ships with
> slice 0's release, so *workers first* holds by construction instead of needing a two-step push.

**Deciders:** @karthik
**Resolves:** PRD-018's open questions OQ-1 to OQ-14 (OQ-12 was removed in the PRD's review), and the item ADR-015's
review carried here: a scheduled check that fires after its LLM key was deleted (ADR-015 review round 3). Confirms
for ADR-015 §4 that monitor notices reuse `webhook_deliveries` and its `kind` (ADR-015 review round 6).
Not a supersession. It widens ADR-014's closed lists (`stop_reason`, `status_reason`) as ADR-014 §5c expects, and
gives monitors their own Schedule policy beside ADR-013's for jobs, which it leaves as it is.

**Builds on:** PRD-018 (Draft, owner review complete 2026-11-05), ADR-014 and ADR-015 (Accepted 2026-11-18),
ADR-009 §4, §6, §8c, §15a, §15e; ADR-010 §1; ADR-012; ADR-013. Code read for this draft: `workflows/pipeline.py`,
`workflows/scheduled_job.py`, `core/schedules.py`, `activities/schedules.py`, `activities/db.py`,
`activities/webhook.py`, `workflows/webhook_delivery.py`, `core/quota.py`, `core/ledger.py`, `core/diff.py`,
`pipelines/catalog.py`, `routers/pipelines.py`, `routers/jobs.py` (permanent delete), `routers/users.py` (LLM-key
delete), `routers/admin.py` (operational stats), migration `9db1dcda44f7` (the views' pipeline arms),
`llm-worker/worker/content.py` and `rules.py`.

---

## 1. The problem

PRD-018 asks for three things layer A cannot do: **repeat** a pipeline on a schedule, **compare** each run with
"last time", and **wait for a person**. All three have to fit the machinery that exists:

- Runs are `PipelineWorkflow`s whose only inputs are fixed at the trigger. A rule may read only its block's input and
  the run's inputs (PRD-019 D5), so "last time" has to be **handed in** when a check starts. Nothing can look it up
  mid-run.
- Job schedules are already Temporal Schedules with per-tick admission (ADR-010 §1, ADR-013). A monitor should
  reuse that pattern, not invent a second clock.
- The meters count rows. A monitor-level event (a skipped firing) must not be a `pipeline_runs` row, or the monthly
  meter charges it (ADR-010 §1).
- ADR-014 already records how runs and blocks end, as reasons beside three run outcomes. Monitors' new endings
  (unchanged, rejected, expired, superseded) must fit that shape.

What follows is one picture of a check, end to end, then a section per open question.

```
API  POST /monitors ──► monitors row (active) ──► Temporal Schedule monitor-schedule-{id} (SKIP, catch-up 365 d)
                                                      │ each firing
                                                      ▼
                       MonitorCheckWorkflow ── admit_monitor_check (workflow queue, user lock)
                         │   monitor still active? PIPELINES_ENABLED? a check already running?
                         │   meters: monthly → skip · storage → failed check · concurrency → wait, re-ask
                         │   baseline lookup → fingerprints + carried values + the baseline's copies
                         │   inserts pipeline_runs (+ monitor_id, baseline_run_id) and its block rows
                         ▼
                       child PipelineWorkflow (pipeline-run-{run_id}), monitor context in its input
                         scrape ─► clean ─► Validate[changed] ─► llm_extract ─► webhook
                                             │ fingerprint = SHA-256 of its input
                                             ├─ same as baseline → stop, "unchanged", copies deleted
                                             └─ differs → Diff (content queue) → carry on
                                                                      └─ Capture values, Diff extraction
                         mirror_run_status(completed, baseline_eligible) ─► monitor counters, notices
```

---

## 2. What a monitor is in the database (R1, D1, D15)

### 2a. `monitors`

| Column | Type | Meaning |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID → `users` ON DELETE CASCADE | |
| `name` | TEXT, `UNIQUE (user_id, name)` | editable |
| `pipeline_id` | UUID → `pipelines` (no `ondelete`) | fixed for life |
| `pipeline_version_id` | UUID → `pipeline_versions` (no `ondelete`) | **fixed for life** (D15) |
| `inputs` | JSONB | the fixed value of every run input that is not carried, by name; **fixed for life** |
| `carried` | JSONB | the carried inputs: `[{input, from_block, path, start}]` (§6b); **fixed for life** |
| `schedule_cron` | TEXT NOT NULL | editable; UTC, no more often than `schedule_min_interval_minutes` (5) |
| `state` | VARCHAR(20), CHECK IN (`active`, `paused`, `paused_itself`) | §7 |
| `consecutive_failures` | INTEGER NOT NULL DEFAULT 0 | §7 |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

`notice_destination_id` (UUID → `destinations`) is **not** in this table until PRD-017's `destinations` table exists.
Q9 says monitors do not wait for PRD-017, and a column pointing at a table that is not there yet would be code
added only to fill in later. Whichever of the two ships second adds the column in its own migration.

**The two foreign keys to the pipeline have no `ondelete`.** A pipeline is only ever soft-deleted while it has
runs, so the FK itself never fires. The refusal is explicit instead: `DELETE /pipelines/{id}` takes
`quota.lock_user` and answers `409` naming every monitor (active, paused or paused itself) that uses the pipeline.
The monitor's create takes the same lock, so the two cannot interleave.

### 2b. A check is a pipeline run, marked

`pipeline_runs` gains:

| Column | Type | Meaning |
|---|---|---|
| `monitor_id` | UUID → `monitors` ON DELETE SET NULL | the monitor that started this run; NULL for a hand-run. `SET NULL`: deleting a monitor leaves its checks as ordinary runs of the pipeline (R1) |
| `baseline_run_id` | UUID → `pipeline_runs` ON DELETE SET NULL | the check this one was compared with (§4) |
| `change_status` | VARCHAR(20) NULL, CHECK IN (`first_check`, `no_baseline`, `changed`, `unchanged`) | the first change gate's verdict (§5). NULL = the run has no change gate |
| `change_summary` | JSONB NULL | the bounded "what changed" (§6a) |
| `carried_sources` | JSONB NULL | for each carried input, whether its value came from the baseline or from its start value, and why (§6b) |
| `captured_values` | JSONB NULL | what this check offers the next one as carried values (§6b) |
| `baseline_eligible` | BOOLEAN NOT NULL DEFAULT false | written with the run's outcome (§4) |

Index: `(monitor_id, created_at DESC) WHERE baseline_eligible` for the baseline lookup, and
`(monitor_id, created_at DESC)` for the monitor's history.

**"Compared with a check that has since been deleted"** (R8) needs no extra column: `change_status` is set, it is
not `first_check`, and `baseline_run_id` is NULL.

`pipeline_run_blocks` gains `fingerprint VARCHAR(64) NULL`: the SHA-256 of a change gate's input (§5). It is a
column, not an object, so it survives deletion of the stored copy, which is what PRD-018 OQ-3 asks for.

None of these columns is read by a quota view, so the migration does not touch the views (ADR-014 §5c's reasoning,
unchanged).

### 2c. The API

| Route | Notes |
|---|---|
| `POST /monitors` | save-time checks (§2d); `lock_user`; the scheduled cap (§12); then the Schedule (§3) |
| `GET /monitors`, `GET /monitors/{id}` | with state, next check (from the cron), the last check's outcome, and whether a newer pipeline version exists |
| `PATCH /monitors/{id}` | **`name`, `schedule_cron`** (and `notice_destination_id` once it exists). Any other field → `422` naming it (D15). A cron change on an active monitor re-arms it: if the engine refuses, the row is put back and the answer is `503` "the schedule was not changed" (the job lane's rule, `routers/jobs.py`) |
| `POST /monitors/{id}/pause`, `/resume` | resume is checked against the cap like create; resume also resets `consecutive_failures` (D12) |
| `POST /monitors/{id}/check` | Check now (§3d) |
| `DELETE /monitors/{id}[?with_checks=true]` | §11 |
| `GET /monitors/{id}/history` | checks (runs with `monitor_id`) and monitor events (§7b), newest first |
| `GET /approvals`, `POST /pipelines/{pid}/runs/{rid}/approval` | slice 2 (§9) |
| `GET /pipelines/{pid}/runs/{rid}` (and the admin read) | the run response gains `monitor_id`, `baseline_run_id`, `change_status`, `change_summary`, `carried_sources` — the run page's verdict, "compared with" link and where each carried value came from |
| `GET /pipelines/{pid}/runs/{rid}/diff?block=` | the stored `diff.json` (§6a) for the run page's code-review diff. `?block=` on the result route returns a block's own output only, so the diff needs its own read |
| `GET /pipelines/{pid}/runs?monitor_id=` | the run list filtered to one monitor's checks; each summary gains `monitor_id` and `change_status` |
| `GET /pipelines/{pid}` | gains `monitors: [{id, name, state}]`, the monitors using it, so the delete refusal (§2a) is visible before it happens |
| `GET /users/me/scheduled-jobs` | already counts monitors once §12 widens the count; gains a `jobs` / `monitors` split so the SPA can show "18 of 20 schedules: 12 jobs, 6 monitors" |

Every route is owner-scoped with `404` for anyone else, as everywhere. `require_pipelines_enabled` guards create,
resume and Check now. Reads never are.

**Admin** (R9, read-only): `GET /admin/monitors` (every user's, with the owner's email) and
`GET /admin/monitors/{id}` with its history, beside the admin pipeline routes; the admin run reads gain the same
columns as the user's. `OperationalStats` gains `active_monitors` and `monitors_paused_itself` (§12) — the second
is the operator's count of monitors that broke and stopped themselves. Quota editing is unchanged:
`scheduled_jobs_limit` now limits jobs and monitors together, and the SPA labels it "schedules".

**Not here:** pipeline and monitor metrics over time (checks per day, LLM calls the change gate saved, skips).
The admin statistics have no pipeline-lane numbers at all today; that gap predates monitors and is a backlog item
of its own (`phase4-backlog.md` §4).

### 2d. Save-time checks (R1, D4, D7)

Refused with a `422` naming the problem, in this order:

1. The pipeline is the user's and not deleted, and the version exists. Unknown → `404`. **The pinned version's
   LLM keys exist and are the user's**, checked under `lock_user` (§13): a key can be deleted after the pipeline was
   saved, since nothing protects the keys of a pipeline no monitor uses. Resume needs no re-check — the key cannot
   be deleted while even a paused monitor uses it.
2. **Inputs.** Every declared run input is in exactly one of `inputs` (a fixed value) or `carried`. A fixed value
   is checked by ADR-014 §4's strict per-type rule (a `number` is a JSON number, never `"100"` or `true`). A
   carried input must be of type `number` or `text`. Its `start` must be of that same type. Its `from_block` must be
   a content block of the version whose output is `extraction` or `page:json`, and its `path` must be a JSON
   Pointer. Unknown names → refused.
3. **The fixed URL inputs pass the SSRF check**, as the trigger checks them.
4. **The schedule** is valid cron and no more often than every 5 minutes.
5. **D4.** If the version has an `llm_extract` block, a change gate (§5) must come before the first one in the
   chain. Otherwise the save is refused, naming the LLM block. A version with no LLM block saves, with a warning.
6. **The run fits its ceiling.** The blocks' **need** plus the monitor allowance (§6c) must not exceed
   `pipeline_max_run_seconds`; the refusal names both numbers. The check's ceiling is the version's resolved ceiling
   plus the allowance, **capped** at `pipeline_max_run_seconds`. Not "the resolved ceiling plus the allowance": a
   version may declare `time_budget_seconds` up to the maximum (`validator._budget_problems`), and would then never
   be monitorable however little its blocks need. Need plus allowance fits under the cap, so every activity keeps its
   full budget.
7. **The scheduled cap** (§12), on create and on resume.

**Warnings**, which do not refuse the save, are returned as `warnings: [...]`: no LLM block and no change gate
(every check stores a copy and runs every block); and no failure route (no Webhook block with `pipeline.failed`
in its `events`, and no notice destination), so a failing monitor would be visible only in the SPA (R6).

---

## 3. The firing (OQ-2)

### 3a. One Temporal Schedule per monitor

**Decision: ADR-013's job pattern, copied, not shared.** A Schedule `monitor-schedule-{monitor_id}` starts a
`MonitorCheckWorkflow` (id `monitor-check-{monitor_id}`, which the Schedule suffixes with each firing's time) on
the `workflow` queue. Its policy is set explicitly, as `core/schedules.py` does for jobs:
`overlap=SKIP`, `catchup_window=365 d`, `pause_on_failure=False`.

- **The monitors row is the truth for *armed*; the Schedule is only the clock** (F.1's rule). Pausing writes the
  row, then pauses the Schedule best effort. Arming (create, resume) writes the row, then creates or unpauses the
  Schedule. If the engine refuses, the arming is undone and the API answers `503`, as for jobs. Deleting deletes
  the row, then the Schedule best effort. A Schedule left behind fires into a no-op, because admission refuses any
  firing whose monitor is not `active`.
- **`pause_on_failure=False` is kept.** Self-pause (§7) is decided by ScrapeFlow from the checks' outcomes, not by
  Temporal from the workflow's outcome. A `MonitorCheckWorkflow` does not fail when its check fails.
- ADR-013 stays as it is: it decides job schedules. This section decides monitor schedules, with the same
  parameters, because R7 asks monitors to behave exactly as scheduled jobs do.

### 3b. Admission per firing — `admit_monitor_check`

A workflow-queue activity, the monitor-shaped twin of `admit_scheduled_run`. In order:

```
before the lock (network calls are not allowed inside it — lock_user's rule):
  read the monitor; not active / deleted               → "stopped"            (no event: the user did it)
  parse the pinned version; SSRF-check its URLs        → failure → a failed check (below)
lock_user(user)                                          every monitor_events write happens under it (§7b)
  re-read the monitor under the lock; not active       → "stopped"
  PIPELINES_ENABLED is false                           → event skipped_pipelines_disabled
  a check of this monitor is running and NOT waiting   → event skipped_check_in_progress (§3c)
    for an approval
  monthly_runs reached                                 → event skipped_monthly_runs (episode, §7b)
  storage reached                                      → a failed check naming the remedy (ADR-010 §1)
  concurrency reached                                  → "waiting": event waiting_for_slot; the workflow re-asks
                                                         on a doubling timer (15 s → 5 min), and gives up at 24 h
                                                         → event gave_up_waiting
  otherwise: baseline lookup (§4) → insert the run and its block rows → commit → "admitted" + PipelineRunInput
```

- **A retried call finds its own run.** The workflow mints the run id, so an activity retry after a lost commit
  finds the row it already inserted and returns it. This is `admit_scheduled_run`'s rule.
- **A firing that is admitted but cannot run** (the pinned version no longer parses, or a URL now resolves to a
  private address) **is a failed check**: a `pipeline_runs` row `failed` with the reason, its blocks `skipped`
  with ADR-014's `not_started`. It costs one monthly run and counts towards self-pause, exactly as a storage-full
  firing does (ADR-010 §1). The alternative, recording it as a monitor event, would let a broken monitor fire for
  ever without ever pausing. (§17, escalation G3.)
- **Admission is a third writer of `failed`.** ADR-014 §8a names two (`mirror_run_status`, and the trigger's
  "could not start" branch), each calling `queue_failure_alerts` in the transaction that marks the run failed. The
  failed checks admission inserts (storage full, refused firing) never reach either, so admission calls
  `queue_failure_alerts` and `monitor_check_ended` (§7a) in its own insert transaction. Otherwise a Webhook block
  set to *the run breaks* stays silent for exactly the failures that need a person.
- **The run rows and the `PipelineRunInput` are built by one function** shared with the API's trigger
  (`create_run`) and Check now. Today `create_run` builds both inline (`routers/pipelines.py`). The build moves it
  to `core/pipeline_trigger.py`, so a monitor's check and a hand-run cannot drift. This is P9's lesson about the
  dispatch-message builders, applied to pipelines.

### 3c. The tick stays open while its check runs

`MonitorCheckWorkflow` runs the admitted check as a **child** `PipelineWorkflow` (`pipeline-run-{run_id}`,
`REJECT_DUPLICATE`, `ParentClosePolicy.ABANDON`), and waits for it, as `ScheduledJobWorkflow` waits for its
`JobWorkflow`. While the tick is open, `SKIP` drops the next firing at the engine. That is the cheap first layer of
R7's one-check-at-a-time rule.

**The second layer is the database.** Admission refuses a firing while any check of the monitor is `running` and
not waiting for an approval. That covers what `SKIP` cannot see: Check now (§3d), which is not a firing, and slice
2's approval wait, where the tick has already closed (§9e). The database rule is the one that is always right. The
engine rule only saves a workflow start.

### 3d. Check now

`POST /monitors/{id}/check` does what admission does, synchronously, in the API: `lock_user`, the same overlap
rule (`409` naming the running check), `admit()` for the meters (`429`, as for any trigger), the baseline lookup,
the shared builder, then `start_workflow(PipelineWorkflow)` directly. There is no `MonitorCheckWorkflow` and no
Schedule trigger. (A `trigger()` under `SKIP` would be silently dropped while a tick is open, and the user must
hear why.) A scheduled firing that comes during a Check now finds a running check and is skipped. The check-now
run is marked with `monitor_id` like any check.

---

## 4. The baseline: found at admission, handed to the check (OQ-3)

**Decision: admission finds the baseline with one indexed query, and hands its fingerprints, its carried values
and the paths of its stored copies to the check in the workflow's input. The check never looks anything up.**

### 4a. Which runs can be a baseline: `baseline_eligible`

Written once, in the same `mirror_run_status` call that ends the run. **The workflow computes it**, because it is
the only party that knows the block order and where the run stopped, and the computation is deterministic:

```
baseline_eligible =
      the run is a monitor check
  and it ended completed
  and (the version has no change gate  or  the run passed its first change gate)
  and stop_reason not in (approval_expired, approval_superseded)
```

"Passed its first change gate" covers both cases R2 excludes: stopped *at* the first gate (unchanged), and stopped
by a Validate *before* it (check 2b in R2's table). A later stop (a later gate, another Validate after the first
gate, a rejected approval) leaves the run eligible. Failed and cancelled runs are never eligible: they are not
`completed`.

### 4b. The lookup

```sql
SELECT … FROM pipeline_runs
WHERE monitor_id = :monitor AND baseline_eligible
ORDER BY created_at DESC
LIMIT 1
```

- **Newest by `created_at`, the check's start, not its finish.** In slice 2 a check waiting for approval can
  finish after a later one. The later check saw newer content, so it is the newer baseline.
- **A fresh lookup at every firing** (R8). A deleted check is simply not found, and the next check compares with
  the newest remaining one, or is a first check.
- It runs **inside the lock**, after the overlap rule. It reads only the database: no storage, no network.

### 4c. What is handed over

`PipelineRunInput` gains one optional field, `monitor: MonitorContext | None = None`. A hand-run has none.

```
MonitorContext
  monitor_id
  baseline_run_id         None on a first check
  waiting_run_id          slice 2: the check waiting for approval, if any (§9e)
  gates: {block_id: {baseline: sha256 | None, waiting: sha256 | None}}
  baseline_outputs: {block_id: path}   keyed by the CONTENT block that produced the object (its output_ref),
                                       only while that block's collected_at is empty — for the diff (§6a)
```

**Which entry each diff reads.** A change gate stores nothing (it is an effect block), so the page it compares is
the output of the content block that feeds it: the page diff looks up that block's id (`clean` in
`scrape → clean → gate`). The extraction diff looks up the `llm_extract` block's own id. A copy deleted since
(a later stop with *delete*, ADR-014 §6, which sets `collected_at`) has no entry, which §6a reports as
`baseline_gone`.

The carried values are **not** in `MonitorContext`. They are ordinary run inputs (D7), written into
`pipeline_runs.inputs` like any input and visible on the run page. Admission fills each one from the baseline's
`captured_values` when it holds a value of the right type, else from the monitor's `start`, and records which in
`carried_sources` (§6b).

**The fingerprints are not run inputs** (D3). The user never declares or types them, the pipeline's input list is
what the user wrote, and a hand-run sends none. That is the line PRD-019 R3 draws.

---

## 5. The change gate (OQ-4)

### 5a. Shape

**Decision: one new rule kind, `{"kind": "changed"}`, met when the gate's input differs from every fingerprint
handed in.** A change gate is a Validate block whose rules are exactly `[{"kind": "changed"}]` with
`when_unmet: "stop"`.

- **Why "changed" and not "same as baseline":** ADR-014 §2 stops a run when a rule is *not met*. PRD-018 writes the
  rule so that "same" means stop. Stated as "the content changed", "not met" is "same", and ADR-014's stop path
  applies with no special case. The PRD's wording and this shape say the same thing.
- **Save-time rules** (`pipelines/validator.py`): `changed` only with `when_unmet: "stop"`, as the only rule of its
  block, and without `negate`. A reversed change gate would stop on every change, which nobody wants. Mixing
  it with other rules would make "the first change gate" and D5's default depend on which rule stopped. All three
  restrictions can be loosened later without breaking a saved definition. (§17, escalation G6.)
- **The first change gate** is the first Validate block in chain order whose rule is `changed`. The workflow and
  the save-time check derive it the same way from the pinned block list.
- **What it reads** is its block's input and the fingerprints in `MonitorContext`. That is the statement PRD-019 R3
  asks every new rule kind to make.

### 5b. D5's *delete* default at the first gate only

ADR-014 §9 gives `on_stop_outputs` the default `keep`. That default stays for every ordinary Validate. **The
save-time check writes the resolved value into the stored definition:** at the first change gate an absent
`on_stop_outputs` is stored as `"delete"`, and at a later gate as `"keep"`. A pinned version therefore never
depends on position logic at run time, and a user's explicit `keep` at the first gate is kept.

**"Absent" is read from the submitted JSON, not from the parsed model.** The stored config is a full dump of the
model, defaults included (`validator.py`: `config.model_dump(mode="json")`), so after ADR-014 every stored Validate
carries `on_stop_outputs` explicitly and the parsed model cannot tell "left out" from "chose `keep`". The validator
checks the raw block config for the key. An edit round-trips cleanly: the stored `"delete"` comes back in the
resubmitted JSON.

### 5c. The fingerprint

**SHA-256 of the gate's input object, byte for byte, computed by the Validate activity** (it reads the object
anyway). It hashes the bytes as read, **before** the UTF-8 decode the activity does today (`errors="replace"`
would make two different byte strings equal).

- **Not the scrape's `content_hash` (xxh64).** That hash exists only for a Scrape block's output, and a gate after
  Clean or after an extraction needs the fingerprint of *its* input. One rule for every position is simpler than
  reusing a hash for one of them.
- **Collision risk, as OQ-3 asks:** SHA-256 has no known collisions, accidental or crafted, so "a changed page
  read as unchanged" does not happen in practice. xxh64 is fine for chance collisions (about 1 in 2⁶⁴ per
  comparison) but is not designed to resist a crafted one. A site that wanted to hide a change from a watcher can
  already serve it the old page, so a crafted collision would buy it nothing. SHA-256 is chosen anyway because it
  costs a few milliseconds on a 4 MiB page and removes the question.

### 5d. The contract (additive, ADR-012 §2.4)

- `ValidateInput` gains `known_fingerprints: dict[Literal["baseline", "waiting"], str] = {}`.
- ADR-014's `ValidateVerdict` gains `fingerprint: str | None = None` and
  `matched: Literal["baseline", "waiting"] | None = None`.
- The `changed` rule is **met** when the computed fingerprint equals none of `known_fingerprints`. That includes an
  empty map: first check, or a hand-run. It is **not met** when it equals one, and `matched` says which.
- **"No fingerprint" is an answer, not "cannot be checked".** A first check or a hand-run carries on. The run
  records `change_status = first_check` (a monitor check) or `no_baseline` (a hand-run). PRD-019 D4's "cannot be
  checked fails the run" is untouched: the `changed` rule has no "cannot be checked" case at all. Any bytes can be
  hashed, and a storage read failure is the activity's ordinary transient error, retried as for any Validate.
- Both contract copies (`activities/contracts.py`, `llm-worker/worker/contracts.py`) and
  `contracts/test_activity_contract.py` gain the fields. **The LLM worker is released first** (ADR-014 §2's note):
  an old worker parses `rules` against its closed list of rule kinds (`list[Rule]`, a discriminated union), so a
  `changed` rule fails to decode and **the check fails** at its gate. Loud, not silent. ADR-014 §2's "an old worker
  ignores unknown fields" holds for new *fields* (`known_fingerprints`), not for a new rule *kind*.

### 5e. What the workflow does with the verdict

```
gate verdict          workflow                                                  run records
met, no fingerprints  carry on                                                  change_status = first_check / no_baseline
met                   Diff (§6a), then carry on                                 change_status = changed
not met               ADR-014 §6 stop path (on_stop_outputs = delete → copies)  change_status = unchanged,
                                                                                stop_reason = rule_not_met
```

`pipeline_run_blocks.fingerprint` is written by `mirror_block_status(completed)` in every case. The run list's
label "unchanged since <check>" reads `change_status` and `baseline_run_id` with no join. When `matched` is
`waiting` (slice 2), the label names the waiting check instead. The verdict carries which one, and the run page
reads it from the block's `verdict`.

---

## 6. What changed (OQ-5), carried values, and the time they take

### 6a. The diff

**Decision: a new activity, `Diff`, on the `content` queue (the LLM worker), that compares two stored objects,
writes the full comparison as an object, and returns a bounded summary.**

- **Which worker:** the content queue, beside Clean, which already runs CPU work off its event loop
  (`asyncio.to_thread`) so the LLM activities in the same process keep heartbeating. Never the API (the v1 diff
  blocked its event loop, 2026-09-19), never workflow code (it needs storage).
- **When:**
  - **page diff**: right after the first change gate answers "changed" with a baseline, comparing the gate's input
    with the baseline's output of the block that feeds the gate (`baseline_outputs`, §4c);
  - **extraction diff**: right after each `llm_extract` block whose block id has a baseline output, comparing the
    two extractions.

  Both run **before** the block that triggered them is mirrored `completed`, so the run holds its concurrency slot
  while they run (the view counts a run only while a block is `running` — §19 trap).
- **What it computes:**
  - for a page: a unified diff (changed lines with three lines of context) via `difflib`;
  - for an extraction: a nested comparison at any depth, path by path (`/products/0/price: 10 → 8`). Lists are
    compared item by item by position (R4).
  - **A guard on size:** above 50,000 lines a side, or past the activity's time limit, it returns *"too large to
    compare"*. `difflib` is quadratic in the worst case.
  - **A diff of raw HTML reads poorly.** Minified HTML is often a few very long lines, so a gate placed straight
    after Scrape gives "line 1 changed" and 200 characters of it. R3 already recommends the gate after Clean,
    whose Markdown diffs line by line; the docs say so. Nothing is built for it.
- **What it writes:** the full comparison as `history/{artifact_id}/diff.json` (ADR-011's stage naming; the
  artifact is the block that triggered it), recorded in the ledger like any object, so it is charged and freed with
  its run. It is capped at 1 MiB and marked cut beyond that. The run page's full diff reads this object. It never
  recomputes, because by then the baseline's copy may be gone.
- **What it returns:** a summary capped at **16 KiB**: up to 200 changed lines, or 100 changed paths, each value cut
  to 200 characters, with `truncated: true` when cut and the counts of everything. That is well under Temporal's
  2 MB payload limit, so the workflow can hold it. The workflow puts it in `change_summary` (through the run mirror)
  and in the Webhook payload.
- **The diff never fails the check, and never reads "no change"** (R4):

  | Case | `change_summary.status` |
  |---|---|
  | computed | `ok` |
  | the baseline's copy is gone (`NoSuchKey`, or the baseline's block has `collected_at` so no path was handed in) | `baseline_gone` — "changed; the previous copy is no longer stored" |
  | anything else after the activity's retries (storage down, not JSON, too large) | `unavailable` — "changed; the comparison could not be computed" |

  The activity returns `baseline_gone` and "too large" as results. Only transient storage errors raise, and the
  workflow catches the final `ActivityError` and records `unavailable`. *Changed* is decided by the fingerprint.
  The diff only describes it.
- **`core/diff.py` is not reused.** It returns line counts, compares top-level fields only, and reads an error as
  "no change" (PRD-018 Background). The new code lives in the LLM worker. Its two `compute_*` functions have no caller
  since D.2; its `DiffResult` type is still imported by `core/webhooks.py` for `create_webhook_delivery`'s `diff`
  parameter, which every caller leaves `None`. The cleanup (§14) removes all three together.

**The webhook payload.** `WebhookInput` gains `monitor_id: str | None = None` and `change: dict | None = None`
(additive). On a monitor check the Webhook block's payload gains `monitor_id` and `change`: `{status:
changed | first_check, baseline_run_id, summary}`. **`diff_detected` follows `change.status == "changed"`** on a
monitor check, so a receiver reading the old field is not told "no change" about a change. `diff_summary` stays
`null`, and the summary is in `change`. Runs outside a monitor keep today's payload byte for byte. (§17, escalation
G7.)

### 6b. Carried values (D7)

**Decision: a check captures its own carried values while it runs. The next check reads them from the database,
never from storage.**

- A carried input is declared on the monitor as `{input, from_block, path, start}`.
- **Capture:** after `from_block` completes in a monitor check, a `Capture` activity (content queue: it reads one
  object) resolves each path with the same JSON Pointer code Validate uses. It returns `{input: {value}}` or
  `{input: {missing: "<why>"}}`, and the workflow writes the result to `pipeline_runs.captured_values` through the
  run mirror. A value of the wrong type (a string where the input is `number`, or a boolean) counts as missing, by
  ADR-014 §4's strict rule.
- **Hand-over:** admission reads the baseline's `captured_values` (§4c) and fills each carried input from it, or
  from `start` when the baseline has nothing to carry. It records each choice in `carried_sources`, for example
  `{"last_price": {"from": "start", "why": "the baseline's extraction has no /price"}}`.
- **Why not read the baseline's extraction at admission:** that is a storage read inside the user lock, which
  `lock_user` forbids. And a baseline that stopped at a later Validate with *delete* has no extraction left to
  read. A captured value is a few bytes in a column, and it survives both.
- A failed check never moves a carried value, because a failed check is never the baseline (R4's rule falls out of
  §4a).

### 6c. The monitor allowance in the run budget

Diff and Capture are activities no pipeline block owns, so the catalog's per-block budgets do not cover them.
**Admission adds a monitor allowance to the check's run ceiling**: one Diff budget for the first gate, one per
`llm_extract` block, and one Capture budget per distinct `from_block`. Each budget is computed with the catalog's
`Timing` helper (`start_to_close` 120 s for Diff, 60 s for Capture, the workers' 3-attempt transient policy), plus
`DB_ACTIVITY_SECONDS` for each extra mirror call. The monitor save refuses a version whose ceiling plus allowance
exceeds `pipeline_max_run_seconds` (§2d). A hand-run has no allowance and runs neither activity.

---

## 7. Self-pause and the monitor's own history (OQ-8, OQ-1's second half)

### 7a. Self-pause is a counter, kept by the writer that ends a check

**Decision: `monitors.consecutive_failures`, moved in the same transaction that ends the check, by whichever
writer ends it. No Temporal setting, and no change to ADR-013.**

| The check ends | Counter | Writer |
|---|---|---|
| `failed` (any cause, storage-full and refused firings included) | +1 | `mirror_run_status`; the trigger's could-not-start branch (Check now); admission for the failed checks it writes (§3b) |
| `completed` (any stop included) | reset to 0 | `mirror_run_status` |
| `cancelled` | unchanged (D12) | — |
| a skipped firing | unchanged (D12) | — |

At the 5th failure the same transaction sets `state = paused_itself`, records a `paused_itself` event naming the
last failure (§7b), and queues its notice (§8). After the commit the writer pauses the Schedule best effort. The
row is already the truth, so a Schedule left running fires into "stopped".

- **Every writer calls one helper**, `monitor_check_ended(db, run)`, as ADR-014 §8a has `queue_failure_alerts` for
  its writers. A check of a deleted monitor (`monitor_id` NULL) moves nothing.
- **Pause_on_failure stays `False`** on the Schedule: Temporal sees a `MonitorCheckWorkflow`, which succeeds even
  when its check fails, and in slice 2 it has often closed before its check ends (§9e). Only the row knows.
- Resume (§2c) re-arms and resets the counter. A user pause or resume does not count as a check.
- **The counter moves only in the call that ends the check** (owner, review round 6). `mirror_run_status` is an
  activity and is retried when its reply is lost; a retry finds the run already terminal and changes nothing today
  (`if run.status == "running"`). `monitor_check_ended` runs inside that branch, never after it, or each retry
  adds another failure and a monitor pauses itself after fewer real failures than five. Admission's retry that finds
  its run already inserted returns without touching the counter.

### 7b. Monitor events: one row per *situation*, not per firing

**Decision: a table `monitor_events`. A run of identical skipped firings is one row with a count, which is also how
"sent once per situation" is tracked.**

```
monitor_events
  id            UUID PK
  monitor_id    UUID → monitors ON DELETE CASCADE
  kind          VARCHAR(30) CHECK IN (skipped_monthly_runs, skipped_check_in_progress,
                                      skipped_pipelines_disabled, waiting_for_slot, gave_up_waiting,
                                      paused_itself)
  first_at      TIMESTAMPTZ   the first firing of the situation
  last_at       TIMESTAMPTZ   the latest firing of it
  count         INTEGER       firings it covers
  ended_at      TIMESTAMPTZ NULL   NULL = the situation is still going on
  detail        JSONB         e.g. {resets_at} for monthly runs, {failed_run_id, error} for paused_itself
```

- **Not a run, so the meters cannot see it** (ADR-010 §1). No view reads this table.
- **How a situation starts and ends:** a skip of kind *k* looks for this monitor's open row of kind *k*. If it finds
  one, it bumps `count` and `last_at`. If not, it inserts a new row, and only that insert queues a notice (§8). The
  next firing that does anything else (a check admitted, or a different skip) sets `ended_at` on the open row. So a
  month of skipped hourly firings is one row with `count = 216` and one notice. Every firing is still accounted for:
  the row spans them all, and no firing in between did anything else.
- **Every event write happens under `lock_user`** (owner, review round 6): "find the open row, then bump it or
  insert one" is a read followed by a write, and only the lock keeps two writers from both inserting.
- **Pausing closes any open situation** (by the user, or by itself). A skip after resuming starts a new row and sends
  one notice: the user has just resumed and should hear "still skipping".
- `waiting_for_slot` is one row per firing that waits (`first_at` → `ended_at` when it is admitted or gives up). It
  is history only. `gave_up_waiting` is its own row and does send a notice.
- `skipped_check_in_progress` is history only. R6's opt-in notice for it is *not yet* (owner, review round 1): the
  history already shows every such skip, and the opt-in can be added later without breaking anything.
- The history the SPA shows (R9) is the monitor's checks and these rows, merged by time:
  *unchanged · unchanged · **changed → sent** · skipped ×216 (monthly runs, 1–22 Nov) · failed*. (§17,
  escalation G1: one counted row, not one line per firing.)
- Retention: none in slice 1 (Q12). The rows are small and bounded by situations, not firings.

---

## 8. Notices: recorded first, then delivered (OQ-14)

**Decision: a notice is a `monitor_events` row (the record, always shown in the SPA) plus, when the monitor has a
notice destination, a `webhook_deliveries` row written in the same transaction, delivered by
`WebhookDeliveryWorkflow` and `webhook-sweep` like every other row. This confirms ADR-015 §4's carry.**

- **Until PRD-017's `destinations` exist, notices are SPA-only** (Q9). Nothing is queued, and no code waits for the
  table.
- **When destinations exist**, the migration that adds `monitors.notice_destination_id` also widens
  `webhook_deliveries`:
  - `monitor_id UUID NULL → monitors ON DELETE CASCADE`, `monitor_event_id UUID NULL → monitor_events ON DELETE
    CASCADE`;
  - `ck_webhook_deliveries_job_or_batch_or_crawls` → `num_nonnulls(job_id, batch_id, crawl_id, pipeline_run_id,
    monitor_id) = 1`;
  - `ck_webhook_deliveries_run_or_crawl` → `num_nonnulls(run_id, crawl_id, pipeline_run_id, monitor_id) = 1`;
  - `UNIQUE (monitor_event_id) WHERE monitor_event_id IS NOT NULL`, so a retried writer inserts once;
  - `webhook_delivery.pending()` gains a `monitor_event_id` filter (ADR-014 added `pipeline_run_id`), so the writer's
    best-effort start finds the row; without it every notice waits for the 5-minute sweep.
- The row carries `destination_id` and `kind` (`webhook` | `email`, ADR-015 §4). The attempt resolves the
  destination's URL or mail settings and its secret at each attempt (ADR-015 §2b). The delivery activity's
  explicit owner arms (ADR-014 §8b) gain **monitor → the destination's secret**. An unknown owner still raises.
- **Payload, event `monitor.notice`:** `{event, monitor_id, notice: <kind>, detail, first_at, timestamp}`, signed
  like every ScrapeFlow webhook, with SSRF checked on every attempt and BUG-030's pin from ADR-015 §3.
- **An undeliverable notice changes nothing about the monitor** (R6). The history shows the delivery's state next
  to its event.
- **Who writes it:** the same transaction that writes the event: admission (skips, give-up), `monitor_check_ended`
  (`paused_itself`). "Recorded" and "queued" become true together, as ADR-014 §8a requires for failure alerts.
- **Lock order: the user first, then the run.** Every event write is under `lock_user` (§7b). `mirror_run_status`
  locks the run row today (`_lock_run`); for a monitor check it takes `lock_user` **before** the run row, the order
  admission and §11's deletes use. The opposite order can deadlock against a delete (Postgres aborts one side; the
  mirror would retry), so it is avoided rather than absorbed.
- **Run failures are not notices.** They keep PRD-019's *the run breaks* on the pipeline's own blocks (D13).
  Approval messages are not notices either: they go to the Approval block's destination (§9c).
- Notices cost no quota: `webhook_deliveries` is in no view.

---

## 9. The Approval block — slice 2 (OQ-6, OQ-7)

### 9a. Shape

- Catalog: `approval`, kind `effect` (it passes its input on). Config: `deadline_hours: int = 24`, from 1 to
  `approval_max_deadline_hours` (operator setting, default 168). The block's destination field arrives with
  PRD-017's destinations (Q9), additively.
- Save-time: not the first block, not the last (R5). **At most one Approval block per pipeline in slice 2**, so
  the budget rule below has one deadline to add. Several are additive later. (§17, escalation G5.)
- **Its catalog `Timing` covers only its own database activities** (`request_approval`, `resolve_approval`), sized
  like any DB step and counted in the run's need as every block's is. The wait is not in it (§9d).
- `pipeline_run_blocks` gains `approval_deadline TIMESTAMPTZ`, `approval_answer VARCHAR(20) CHECK IN (approved,
  rejected, expired, superseded)`, `answered_at`, `answered_by UUID → users ON DELETE SET NULL`, and
  `superseded_by_run_id UUID → pipeline_runs ON DELETE SET NULL`.
- ADR-014's `status_reason` list gains `awaiting_approval`, which its CHECK already allows on `waiting`.

### 9b. The wait: the row is the record, a signal is the wake-up

**Decision: the answer is written to the block row first, under the row's lock and conditionally. A Temporal
signal only wakes the workflow, which then reads the outcome back through an activity.** This is the cancel
route's pattern (row first, engine best effort), and the two-writer rule of ADR-009 §11a.

```
PipelineWorkflow reaches Approval
  mirror_block_status(waiting, status_reason=awaiting_approval)
  request_approval   (activity: approval_deadline; the request's delivery rows, §9c)
  [monitor check, slice 2] signal the parent tick "settled" (§9e)
  wait_condition(woken or cancel requested, timeout = deadline − 2 h) → reminder row, then wait to the deadline
  resolve_approval   (activity: under the run lock —
                        an answer recorded → return it
                        none and past the deadline → set expired (conditional on answer IS NULL) → return it)
  approved            → mirror_block_status(completed); carry on
  rejected/expired/superseded → ADR-014 §6 stop path; copies always kept (Q5, R8)
```

- **Answering** (`POST …/runs/{rid}/approval {decision}`): owner only, `404` for anyone else (admins included).
  `UPDATE … SET approval_answer = :d, answered_at, answered_by WHERE block is waiting AND approval_answer IS NULL
  AND now() < approval_deadline`. If no row is updated, the answer is refused with `409` and the current state
  (answered, expired, superseded). Then the signal `approval_answered`, best effort. **A lost signal is not lost
  work:** the workflow wakes at the reminder or the deadline, and `resolve_approval` reads the recorded answer.
- **An answer and the expiry cannot both win.** Both are conditional updates on `approval_answer IS NULL` under
  the row lock. Whichever commits first stands, and the other sees it.
- **Cancel while waiting** ends the run `cancelled` at once. The wait condition includes `_cancel_requested`, and
  the Temporal cancel raises inside `wait_condition`. The block is mirrored as today's cancelled block. No block
  was executing, so nothing finishes first.
- **No slot while waiting.** No block is `running` (the Approval block is `waiting`), so
  `quota_active_submissions` does not count the run (ADR-009 15a). The views do not change.
- **On approve the run continues at once, without asking for a slot again** (G8, owner: option a). ADR-009 15a
  accepted the same for a webhook retry resuming. A user who answers several approvals together can briefly run
  more blocks than their concurrency limit — by at most the number answered, each run already admitted and charged
  at its start. The usual placement (just before the delivery) makes the resumed tail a short send. Waiting for a
  slot after approval is additive later if it is ever needed.
- **History stays small.** A wait of 7 days is a timer, a signal and three activities, whatever its length. The
  workflow has no execution timeout (ADR-014 §7), and the namespace's 30-day retention only starts at close.

### 9c. The request and the other approval messages (D9)

- `request_approval` writes, in one transaction, the deadline and, when the block names a destination,
  `webhook_deliveries` rows for `pipeline.approval_requested`. These use ADR-014 §8b's
  `(pipeline_run_id, block_id, event)` key, so a retry inserts nothing twice. The reminder
  (`pipeline.approval_expiring`, at deadline − 2 h, only when the deadline is longer than 2 h), `expired` and
  `superseded` are rows of the same shape, written by the activity that decides them. **All of them go to the
  block's destination, none to the monitor's** (R6, review round 10: one thread, never duplicated).
- **The request is not an announcement** (D9). `announced` is workflow state set only by irreversible sinks
  (ADR-014 §7, which carries this line since acceptance). An approval message is written by an activity and never
  touches it, so a run that fails after approval still rolls back what it delivered before the Approval block.
- The payload says what changed (`change_summary`), what has already been delivered (the blocks' `delivery`
  records, ADR-014 §5a), the deadline and a link to the run in the SPA. **No answer links** (Q11).
- Until destinations exist: SPA-only, through `GET /approvals` and the run page.

### 9d. The run's time budget does not run during the wait (OQ-6's composition)

**Decision: the run deadline is pushed back by however long the run actually waited.** `PipelineWorkflow` keeps
`deadline` as workflow state today. After the wait it adds `workflow.now() − wait_started`, which is deterministic.

- The save-time rule therefore has two independent parts: the blocks' need ≤ the run ceiling (today's rule), and
  the deadline ≤ `approval_max_deadline_hours`. A run lasts at most ceiling + deadline. Nothing has to fit 7 days
  into the 24 h ceiling, and the refusal names whichever number is over.
- The declared run ceiling, if the user set one, is counted the same way: it measures working time, not waiting
  time. The run page shows the deadline separately.

### 9e. Supersede under `SKIP` (OQ-7)

The problem: a tick that waits for its child holds `SKIP`'s overlap for as long as an approval waits, which is up to
7 days of dropped firings (R1: a waiting check is not in flight).

**Decision: the tick closes when its check settles, not when it ends.** In slice 2, a `PipelineWorkflow` that
reaches an Approval block and has a parent sends the parent a `check_settled` signal (after `request_approval`).
`MonitorCheckWorkflow` waits for whichever comes first: the child's result, or that signal. On the signal it
returns. The child runs on alone (`ABANDON`), and the next firing is no longer dropped. Admission's database rule
(§3c) treats a run whose Approval block is `waiting` as not in flight. That is the one place R1's exception lives.

The next check, and supersede:

```
admission (firing r3), approval r1 waiting
  waiting_run_id = r1, gates[g].waiting = r1's fingerprint at g (§4c)
r3's first gate
  matched = waiting   → stop, "unchanged since r1"; r1 untouched; no LLM call       (D10, first bullet)
  matched = baseline  → stop, "unchanged since <baseline>"; r1 untouched
  met (differs)       → supersede_approval(r1, by=r3), then Diff against the baseline, then carry on
supersede_approval   (activity, under r1's lock: conditional on answer IS NULL → superseded, superseded_by;
                      then signal r1 best effort)
r1 wakes → resolve_approval → superseded → stop path, stop_reason approval_superseded
```

- **Superseded by any later check whose content differs**, whatever that check does next, including stopping at a
  Validate (D10). That is why the supersede happens at the first gate's verdict, not at r3's Approval block.
- **Diffs and carried values still compare with the baseline**, never the waiting check: `baseline_outputs` and
  `captured_values` come from the baseline (§4).
- **At most one approval waits per monitor:** a check reaching Approval while another waits is impossible, because
  the later check either matched the waiting one and stopped, or superseded it first.
- **Race, stated:** if the user approves r1 just before r3's gate runs, the conditional update finds an answer and
  the supersede is a no-op. r3 still compares with the baseline handed to it when it started, so it may report the
  same change r1 is now delivering. R2 already says a check compares with the baseline as it stood when it began.
  Accepted.
- **Expired approvals ask again once per deadline** (Q4) with no extra code: an expired check is not
  `baseline_eligible` (§4a), and the checks in between matched the waiting one.

---

## 10. How the new endings are recorded (OQ-1)

All of these are reasons beside ADR-014's states. Run outcomes stay `completed | failed | cancelled`.

| Ending (PRD-018 Appendix A) | `pipeline_runs` | `pipeline_run_blocks` | Slice |
|---|---|---|---|
| Unchanged | `completed`, `stop_reason = rule_not_met`, `stopped_at_block_id = <gate>`, `change_status = unchanged`, `baseline_run_id` | gate: `verdict` (rule `changed`, `matched`), `fingerprint`; later: `skipped`/`stopped` | 1 |
| First check / no baseline | `change_status = first_check / no_baseline` | gate `fingerprint` | 1 |
| Changed | `change_status = changed`, `change_summary` | gate `fingerprint`; the diff object on the gate's ledger row | 1 |
| Waiting for approval | `running` | Approval: `waiting`, `status_reason = awaiting_approval`, `approval_deadline` | 2 |
| Approved | `running` → … | Approval: `completed`, `approval_answer = approved`, `answered_at/by` | 2 |
| Rejected | `completed`, `stop_reason = approval_rejected`, `stopped_at_block_id = <approval>` | later blocks `skipped`/`stopped` | 2 |
| Approval expired | `completed`, `stop_reason = approval_expired` | as above | 2 |
| Superseded | `completed`, `stop_reason = approval_superseded` | Approval: `superseded_by_run_id` | 2 |
| Monitor deleted during a check | `cancelled` | as any cancel | 1 |

- **Widenings:** `stop_reason` gains `approval_rejected`, `approval_expired`, `approval_superseded`, and
  `status_reason` gains `awaiting_approval`, both in slice 2's migration. Slice 1 adds no value. "Unchanged" is a
  `rule_not_met` stop, labelled from `change_status` (ADR-014 §5c foresaw exactly this).
- **"Monitor deleted" gets no reason of its own.** The PRD marked it tentative. A cancelled check of a deleted
  monitor reads "cancelled", and once the monitor is gone nothing shows the monitor anyway. A cancel-reason column
  would serve this one case. (§17, escalation G4.)
- **Monitor-level endings and pauses** (Appendix A, table C) are `monitors.state` and `monitor_events` rows (§7):
  never a run.
- **Live updates:** `notify_run` gains `change_status`, an identifier and never text (ADR-009 §11d). The run page
  shows the verdict live. The monitor page refreshes (R9) and needs no channel.

---

## 11. Storage: BUG-031's deletes, slice 0 (OQ-9)

**Decision: three permanent deletes, each releasing through the ledger with the job lane's refuse-on-failure rule,
and each following the job lane's handling of a run still in flight: cancel it, then delete it now.**

| Route | Deletes |
|---|---|
| `DELETE /pipelines/{pid}/runs/{rid}` | one run: its objects, its block rows, its webhook rows (cascade) |
| `DELETE /pipelines/{pid}?permanent=true` | every run as above, then the versions and the pipeline. Refused `409` while a monitor uses it (§2a) |
| `DELETE /monitors/{id}?with_checks=true` | the monitor and every run with its `monitor_id` |

```
lock_user                                                    (the monitor and pipeline deletes; a run delete locks the run row)
runs still running → cancel each (row: cancel_requested_at; then handle.cancel(), best effort)
for each run: ledger.release_pipeline_run_objects            objects first, then rows (external before internal)
any object not removed → commit what was released, 503 "N stored objects could not be removed; nothing was
                         deleted. Retry."                     (the job lane's rule: a parent row must never outlive
                                                              an object on disk)
else delete the rows, commit, delete the Schedule (a monitor) best effort → 204
```

- **A run deleted mid-flight drops what it writes afterwards.** This is the job lane's precedent ("a v2 run still
  in flight finds its row gone and drops what it writes after this", `routers/jobs.py`). Two activity changes make
  it true for pipelines:
  - **`record_storage` locks the run row first** (`FOR KEY SHARE`, which a row delete waits for), and, finding the
    run gone, **deletes the objects it was given** instead of raising. Without the lock, a delete enumerating the
    ledger and a record inserting into it can interleave and leave an uncharged object on disk.
  - **The mirrors treat a missing run as cancelled**: they return `cancelled`, so the workflow stops at its next
    boundary. Today `_lock_run` raises a non-retryable error, which would fail the workflow.
- **Plain monitor delete** (no `with_checks`) cancels a running or waiting check and deletes the monitor row. Its
  checks keep their history as ordinary runs (`monitor_id` becomes NULL).
- **An unchanged check deleting its own copy keeps ADR-009 §8c's per-run collection safe.** Every object has
  exactly one ledger row (`UNIQUE (object_key)`) on exactly one block of exactly one run, and nothing adopts another
  run's object, as v1's dedup did. So deleting one run can never break another's result. After the delete its
  `result_path` is NULL (ADR-014 §6, which also answers PRD-019 OQ-8). The run page reads "unchanged — see
  <baseline check>", from `change_status` and `baseline_run_id`.
- Deleting a baseline makes the next check compare with the newest remaining eligible check (§4b). Other runs'
  `baseline_run_id` pointing at it become NULL (§2b).
- Admin deletes: today admins have no pipeline delete. Admin user delete already releases everything through the
  ledger (lane-blind). Nothing new is added for admins (R9: read-only), with one exception:
- **Admin user delete also deletes the user's monitor Schedules**, best effort, as it already does for job
  Schedules (`schedules.forget`, `routers/admin.py`). The cascade removes the monitor rows; without this each
  monitor's Schedule would fire for ever into "stopped" — harmless, and a permanent leak of Schedules and
  workflows.

---

## 12. Meters and admin views (OQ-10)

| Reader | Today | Change |
|---|---|---|
| `quota_run_units` (monthly) | counts every `pipeline_runs` row | **none.** A check is a run (1 unit). A skipped firing is not a row. A storage-full or refused firing is a failed check (1 unit, as for jobs) |
| `quota_active_submissions` (concurrency) | a pipeline run while a block is `running` | **none.** Approval waits, retry waits and the time between firings hold no slot. Diff and Capture run while their block is still `running` (§6a) |
| The scheduled cap (`scheduled_jobs_usage`) | `jobs.schedule_status = 'active'` | **widened:** + `monitors.state = 'active'`. `check_scheduled_jobs_quota` gains `exclude_monitor_id`. The error shape is unchanged (`quota_type: "scheduled_jobs"`) |
| `active_recurring_jobs`, `…_by_lane` | jobs only | **kept, and named for what they count.** New `active_monitors` and `monitors_paused_itself` beside them (R9's "named for the lane") |
| `job_lane_webhooks_*` | ADR-014 adds `pipeline_run_id IS NULL` | **+ `monitor_id IS NULL`**, when notices land (§8) |
| Storage | the ledger | none. Diff objects are ledger rows on their block |

---

## 13. LLM keys and destinations under a monitor (ADR-015's carry)

**Decision: deleting an LLM key that a monitor's pinned version uses is refused with `409`, naming the monitors,
whether they are active, paused or paused themselves.**

- ADR-015 §2c refuses a destination delete for the same reason: otherwise the surprise arrives at a scheduled check
  at 3 a.m. instead of on the screen where the user is deleting. Here it would be five failed checks, a self-pause,
  and a notice that says what the refusal could have said.
- `DELETE /users/llm-keys/{id}` takes `lock_user`, then looks for the key in the user's monitors' pinned versions
  (a JSONB path query over `pipeline_versions.definition`, joined through `monitors`). Monitor create takes the
  same lock and checks the keys exist. A user's monitors are few, so scanning their pinned definitions at delete
  time is cheap. ADR-015 used a reference table because its scope is every pipeline version, including
  soft-deleted ones. Here the scope is one user's monitors.
- **Hand-run pipelines are not covered.** A trigger with a deleted key fails with a `422` while the user is looking,
  which is fine.
- **Destinations:** ADR-015 §2c's list of things that block a destination delete gains **every monitor naming it as
  its notice destination**. Destinations inside a monitor's version are already covered, because a pipeline a
  monitor uses cannot be deleted (§2a).
- Admission still checks the keys under the lock. A key that is missing anyway (an admin deleted the user's key
  directly in the database) makes a failed check, never a silent skip.

(§17, escalation G2.)

---

## 14. Scheduled crawls and the job lane's leftovers (OQ-13)

- **`POST /crawls` refuses a non-null `schedule_cron`** with a `422`: *"scheduled crawls are not supported"*. The
  field stays in the request schema so the message is ours, not a generic "extra field". This can ship on its own,
  before slice 1.
- **Existing values:** the release runbook's first step is `SELECT count(*) FROM crawls WHERE schedule_cron IS NOT
  NULL` in production. Crawls have run there since E.7 (2026-10-27), so it cannot be assumed to be zero. Whatever it
  finds stays ignored, as today: nothing has ever run it.
- **The column is dropped later, in two releases** (unmap, then drop; the H.2 → I.1 pattern), **in the same cleanup
  as the job lane's leftovers**: stop writing `job_runs.content_hash`, then drop it and `job_runs.diff_detected` /
  `diff_summary`; remove `create_webhook_delivery`'s unused `diff` parameter and delete `core/diff.py` with it (§6a). No quota view reads any of these columns (checked against the
  views' column list in CLAUDE.md). The payload fields `diff_detected` / `diff_summary` on `job.*` events stay,
  always `null` (D17). That cleanup is a backlog item, not part of slice 1.
- **The two releases, spelled out** (the api runs two replicas side by side during a rollout, I.2):
  - **A — stop using:** stop writing `job_runs.content_hash`; stop storing and returning `crawls.schedule_cron`.
    **`CrawlResponse.schedule_cron` is removed from the API's output here** — a small output change, read by no
    caller of ours (the SPA uses `schedule_cron` on jobs only). Keeping it as a permanent `null` would keep a field
    that means nothing, the problem the refusal fixes.
  - **B — drop:** the migration drops the four columns; `core/diff.py` and `create_webhook_delivery`'s unused `diff`
    parameter are deleted (§6a).

---

## 15. Changing the workflow code (OQ-11)

Per ADR-012 §2.1 and ADR-014 §10's precedent: every new path is wrapped, even where only new input can reach it.

| Patch id | In | Wraps | Slice |
|---|---|---|---|
| `monitor-check` | `PipelineWorkflow` | passing `known_fingerprints` to a change gate; the verdict's `change_status`; Diff; Capture; `change`/`monitor_id` on `WebhookInput`; `baseline_eligible` on the run mirror | 1 |
| `approval` | `PipelineWorkflow` | the Approval wait, the deadline extension (§9d), `check_settled` to the parent, the supersede call at the first gate | 2 |
| `approval-settled` | `MonitorCheckWorkflow` | waiting for `check_settled` beside the child's result | 2 |

`MonitorCheckWorkflow` is new in slice 1, so its first shape needs no patch, only histories. **Not patched:**
everything written inside activities (`monitor_check_ended`, events, notices, the counter, the record/mirror changes
of §11), because activity code is not replayed. The new fields on `ValidateInput`, `ValidateVerdict`,
`MirrorRunInput`, `WebhookInput` and `PipelineRunInput` are optional with defaults.

**Histories to add** (recorded, never edited; ADR-012 §2.3). This is PRD-018's minimum list:

| Slice 1 | Slice 2 |
|---|---|
| a first check; a changed check with a page and an extraction diff; an unchanged stop with *delete*; a stop at a later gate | approve; reject; expiry (with the reminder); a cancel while waiting |
| a diff that comes back `baseline_gone`; one that comes back `unavailable` | supersede by a later check that reaches Approval; supersede by one that then stops at a Validate |
| `MonitorCheckWorkflow`: admitted; skipped (monthly runs); waited for a slot then admitted; gave up at 24 h; pipelines switched off | a check matching the waiting check's content; `MonitorCheckWorkflow` closing on `check_settled` |
| a hand-run through a change gate (`no_baseline`) | |

---

## 16. Rollout

| Slice | Ships | Schema | Release order |
|---|---|---|---|
| — | D16's crawl refusal (§14) | none | api only, any time |
| **0** | BUG-031 (§11), **plus the LLM worker's half of slice 1** (the `changed` rule, Diff, Capture — additive, nothing calls them yet) | none: the ledger, the FKs and the cascades exist | one `main` push: api and LLM worker images |
| **1** | monitors (§2–§8, §10–§13), after PRD-019's build (ADR-014: stop mode, typed inputs, `when_unmet`) | `monitors`, `monitor_events`, the `pipeline_runs` and `pipeline_run_blocks` columns (§2b) | api only: the LLM worker's half shipped with slice 0, so ADR-014 §2's *workers first* holds by construction (an old worker cannot decode the `changed` rule, and every check would fail at its gate) |
| **2** | Approval (§9) | the approval columns, the reason widenings | api only (the workflow worker is the api image) |
| with PRD-017 | the notice destination, `webhook_deliveries` widened for monitors (§8) | `monitors.notice_destination_id`, the `webhook_deliveries` columns and CHECKs | whichever of the two ships second |

During a rollout, an old api pod refuses a monitor route it does not have (`404`), and an old workflow-worker pod
that picks up a monitor tick fails to find `MonitorCheckWorkflow` and retries the task until the new pod takes it.
Both heal by themselves. No monitors exist in production before the first release, so neither has anything to
strand (the owner's standing note on rollout windows).

**Release gate for slice 1** (PRD-018's measured criterion, on compose before production): 24 checks of a fixture
page that changes twice after the first check give exactly 3 LLM calls, 3 kept result copies, 3 webhook messages
and 24 monthly units. Storage usage moves only for the 3 kept checks. **23 checks are driven by Check now and one
by a real scheduled firing**: Check now runs the same admission and check path minus the Schedule, so the gate takes
minutes, not the 2 hours 24 firings need at the 5-minute floor, and the Schedule path is still exercised once.

**Why the LLM worker's half ships early.** One `main` push rolls out every changed image together, so "LLM worker
before api" cannot be ordered inside a single release. Shipping the worker's additive half one release earlier
makes the order a fact rather than a step to remember.

---

## 17. Escalations to the PM (owner)

Product-visible consequences of the design, each with a recommendation.

| # | Question | Recommendation |
|---|---|---|
| **G1** | The monitor's history shows a run of identical skips as **one counted line** ("skipped ×216 — monthly runs used up, 1–22 Nov"), not 216 lines. Every firing is still accounted for. The same row is what makes the notice go once. | **Accept.** 216 identical lines bury the checks R9 wants scannable. |
| **G2** | **Deleting an LLM key a monitor uses is refused**, naming the monitors (active, paused or paused itself). | **Accept.** It is ADR-015 §2c's rule for destinations, for the same 3 a.m. reason. |
| **G3** | **A firing that is admitted but cannot run** (its URL now resolves to a private address; its version no longer parses) is a **failed check**: it costs one monthly run and counts towards self-pause. | **Accept.** This is what a storage-full firing already does. Otherwise a broken monitor never pauses. |
| **G4** | A check cancelled because its monitor was deleted reads plain **"cancelled"**, with no "monitor deleted" label (Appendix A marked it tentative). | **Accept.** The monitor is gone, so nothing would show the label in context, and it would need a column for one case. |
| **G5** | **At most one Approval block per pipeline** in slice 2, and **waiting for approval does not count against the run's time budget** (the deadline moves back by the wait). | **Accept.** It keeps R5's budget rule to two plain numbers. Several Approval blocks can be added later. ✅ **owner, round 8** (with §9) |
| **G6** | A change gate is a Validate holding **only** the `changed` rule, set to stop, never reversed. | **Accept.** Each restriction can be lifted later without breaking a saved pipeline. |
| **G7** | On a monitor check, the Webhook payload's old `diff_detected` field is **`true` on a change** (it is a constant `false` on pipelines today). `diff_summary` stays `null`, and the details are in the new `change` field. | **Accept.** A receiver still reading the old field must not be told "no change" about a change. |
| **G8** | **After an approval, the run continues without asking for a concurrency slot again**, so answering several approvals together can briefly exceed the limit (by at most the number answered). | **(a) Accept**, over (b) refusing the approval when full or (c) waiting for a slot after it. ✅ **owner, round 8** |

---

## 18. Alternatives rejected

- **One long-lived workflow per monitor** (sleep, check, sleep), instead of a Schedule. It needs continue-as-new to
  bound its history, its own pause/edit/catch-up logic, and its own overlap rule. ADR-013's pattern is built,
  tested and in production for jobs, and a firing that waits for a slot already exists there.
- **Overlap by the database alone** (Schedule `ALLOW_ALL`, admission refuses). A tick waiting up to 24 h for a slot
  would let every later firing start its own waiting tick: 288 open workflows for a 5-minute monitor. `SKIP`
  bounds that. The database rule is kept beside it, for what `SKIP` cannot see.
- **The workflow comparing fingerprints itself.** It would save nothing, because the Validate activity reads the
  object anyway, and ADR-014 §2 keeps rule decisions in the activity.
- **Reusing the scrape's xxh64 `content_hash`.** It exists only after Scrape, and a gate after Clean or an extraction
  needs its own input's hash.
- **Reading carried values from the baseline's extraction at admission.** That is a storage read inside the user
  lock, and it breaks when the baseline's copy was deleted at a later stop.
- **The diff computed on demand when the run page opens.** It is CPU work on the API, the v1 lesson. And by then
  the baseline's copy may be gone, so the page would lose a diff it once had.
- **Approval answered by a Temporal Update alone.** It needs the worker up to accept an answer, leaves no database
  record for the SPA or the late-answer rule, and races the expiry inside workflow code. Row first, signal as a
  wake-up, matches cancel.
- **Monitor history as `pipeline_runs` rows** with a "skipped" status. Every view would count them (ADR-010 §1), and
  ADR-014 keeps the run outcomes at three.
- **A change gate config flag on Validate** (`compare_with_baseline: true`) instead of a rule kind. PRD-018 D3 chose
  a rule kind, and a flag would sit beside the rules, outside ADR-014's evaluator and its verdict.

---

## 19. Consequences

- **Two new tables** (`monitors`, `monitor_events`), seven `pipeline_runs` columns and one `pipeline_run_blocks`
  column in slice 1; five `pipeline_run_blocks` columns and two reason widenings in slice 2; the `webhook_deliveries`
  widening when destinations exist. The quota views are untouched.
- **Two new activities on the content queue** (Diff, Capture) and a widened Validate contract. `contracts/` grows
  accordingly.
- **The pipeline trigger becomes a shared builder** (`core/pipeline_trigger.py`) for the API, Check now and
  admission.
- **BUG-031 closes in slice 0**, and with it ADR-009 §8c's "no collection" gap for deleted runs. Per-run retention
  stays *not yet* (Q12).
- **The scheduled cap is shared** between jobs and monitors.
- **Traps to carry into the build:**
  - the SSRF and version checks run **before** `lock_user`. A DNS lookup inside the lock stalls every admission for
    that user (§3b);
  - `baseline_eligible` is computed by the workflow and written with the outcome. A later writer must never set it
    on a failed, cancelled, expired or superseded run (§4a);
  - newest baseline by **`created_at`**, not `finished_at` (§4b);
  - the fingerprint hashes the **bytes**, before the activity's UTF-8 decode with `errors="replace"` (§5c);
  - the save-time check writes `on_stop_outputs` explicitly at every change gate, deciding "absent" from the
    submitted JSON — the stored config is a full dump with defaults. Do not change ADR-014's catalog default (§5b);
  - Diff and Capture run **before** their block is mirrored `completed`, or the run drops out of the concurrency
    view mid-check (§6a);
  - the Diff activity returns `baseline_gone` and "too large" as results. Only transient storage errors raise, and
    the workflow turns a final error into `unavailable`, never into "unchanged" (§6a);
  - `record_storage` locks the run row and deletes its objects when the run is gone, and the mirrors read a
    missing run as cancelled. Otherwise a mid-flight delete leaves uncharged objects on disk, or fails the
    workflow (§11);
  - `monitor_check_ended` runs only in the call that moves the run out of `running` — the mirror and admission are
    retried, and a counter bumped on every retry pauses a monitor early (§7a);
  - every `monitor_events` write happens under `lock_user`, and pausing closes any open situation (§7b);
  - for a monitor check, `mirror_run_status` takes `lock_user` before the run row — user then run, everywhere (§8);
  - all three writers of a check's outcome (`mirror_run_status`, the trigger's could-not-start branch, admission's
    failed checks) call `monitor_check_ended`, and the two that write `failed` before any workflow runs also call
    `queue_failure_alerts`, each in the transaction that writes the outcome (§3b, §7a);
  - an approval answer and the expiry are both conditional on `approval_answer IS NULL`. Neither may overwrite the
    other (§9b);
  - an approval message never sets `announced` (§9c; ADR-014 §7);
  - the LLM worker ships before the api image in slice 1 (§5d);
  - the job-lane webhook meters gain `monitor_id IS NULL` in the same migration that lets a monitor own a delivery
    row (§12).
