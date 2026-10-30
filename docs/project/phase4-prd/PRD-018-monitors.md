# PRD-018 — ScrapeFlow Workflows: Monitors

**Priority:** P1 for the change check (it is the launch requirement); P2 for the rest.
**Layer:** B (Monitors). A monitor runs a saved pipeline (layer A, PRD-016) again and again on a schedule. It
adds one layer-A rule kind and one layer-A block, both additive, and it delivers through PRD-017's sinks.
**Source:** `phase4-backlog.md` §2 (this PRD's row: both halves of change detection, the job lane's change
detection dropped at D.2, implementation-backlog open item 10, the scheduled-crawl gap); PRD-016 OQ-10 (why
change detection belongs here), OQ-4 (metering), R5 (the job lane's second named exception); PRD-019 and
PRD-017 "What this hands forward"; ADR-009 §4, §8, §15a; ADR-010 §1; ADR-013.
**Status:** **Draft — not reviewed.** Written 2026-10-30 by the PM persona. Every decision below is a proposal
for the owner's section-by-section review. Not Accepted.
**Last updated:** 2026-10-30 (first draft)

> **Built last, on top of three PRDs.** This PRD consumes PRD-019 (a Validate set to *stop*, run inputs of type
> number and text, failure alerts) and PRD-017 (sinks, saved destinations, rollback), both Draft and in their
> Architect pass as this is written. Where this PRD leans on a decision those two have not yet had confirmed by
> an ADR, it says so. Nothing here re-opens a decision in PRD-016, PRD-019, PRD-017, ADR-009, ADR-010 or ADR-013.

> **Handshake with the Architect (2026-10-30).** The new ways a monitor's check, a block or the monitor itself can
> end or pause were sent to the Architect before this body was written, so the run/block "reason" design in
> progress for PRD-019 and PRD-017 can carry them in the same migration. That list is reproduced, as revised by
> writing this draft, in [Appendix A](#appendix-a--new-endings-and-pauses-for-the-joint-migration).

---

## What this PRD owes, and where each item is answered

| # | Obligation (from `phase4-backlog.md` §2 and the hand-forwards) | Answered in |
|---|---|---|
| 1 | **Both halves of change detection**: the *cost gate* (do not pay for an LLM call or store a copy when the page has not changed) and the *reporting diff* (say what changed). B does not ship without the gate. | **R3** (gate), **R4** (diff) |
| 2 | **Define "the previous run of this same thing"**, which layer A cannot (PRD-016 OQ-10). | **R2** |
| 3 | **Long-lived, scheduled, durable**: a pipeline run repeatedly, surviving redeploys, with a **human approval** step. | **R1**, **R7** (schedule), **R5** (approval) |
| 4 | **The job lane's change detection**, dropped at D.2 (2026-10-12): say what jobs get back, and when. | **R10** |
| 5 | **Implementation-backlog open item 10**: does `/jobs` become a front end over pipelines? Decide it, or put it to the owner. | **R10**, owner question **Q1** |
| 6 | **The scheduled-crawl gap** (`crawls.schedule_cron` is accepted and never runs). | **R10** |
| 7 | **Storage**: failed runs keep their copies (PRD-017 D12) and nothing frees a pipeline run's outputs (BUG-031). | **R8** |
| 8 | **Quota, the scheduled-jobs cap, overlap** for repeated runs. | **R7** |
| 9 | **Confirm in writing** that the cost gate can be built on PRD-019 without a further layer-A change (PRD-019's last success check). | **R3**, What this hands forward |

## Decisions this draft proposes, for the owner's review

None of these is agreed. "Rec." marks the PM's recommendation where the owner is asked to choose.

| # | Proposed decision | Where | The alternative rejected | Status |
|---|---|---|---|---|
| D1 | **A monitor is a saved pipeline, pinned to one version, plus fixed run inputs, plus a schedule.** Each firing is a **check**, and a check is an ordinary pipeline run: same run page, same outcomes, same metering. No new kind of run. | R1 | A monitor as a separate run type with its own statuses and pages | proposed |
| D2 | **"The previous check" (the baseline) is the most recent earlier check of the same monitor that ended `completed`**, counting stops (unchanged, a Validate stop, an approval rejected), since the monitor's version or run inputs last changed. **Failed and cancelled checks never become the baseline.** | R2 | The most recent check of any outcome (a failed delivery would then make the next check "unchanged", and the change would never be delivered) | proposed |
| D3 | **The cost gate is a Validate set to stop (PRD-019) with one new rule kind: "the content is the same as at the baseline"** (exact, byte for byte). The monitor hands the baseline's fingerprint over as a run input. The rule reads only its block's input and that run input, so it stays inside PRD-019's rule on what a rule may read (D5 there). | R3 | A dedicated Gate block; a gate built into the monitor rather than placed in the chain | proposed |
| D4 | **A monitor whose pipeline contains an LLM block cannot be armed unless a change check comes before that LLM block.** Refused at save, naming the block. A pipeline with no LLM block gets a warning, not a refusal. | R3 | Making the gate optional everywhere (the cost delta PRD-016 accepted would return on every scheduled run) | proposed — owner question **Q7** on an explicit opt-out |
| D5 | **An unchanged check ends `completed`, labelled "unchanged since <check>", and by default deletes what it stored** (PRD-019's *delete* choice, defaulted on for this rule kind only). *Keep* stays available. | R3 | PRD-019's default *keep* (a monitor would pile up one identical copy per check against the storage limit) | proposed |
| D6 | **Every changed check records what changed**: a text diff of the page when the change check reads a page, and a field-by-field diff of the extraction when the check and its baseline both have one. The first check says "first check" and has no diff. What changed travels in the run's messages. | R4 | A diff only on request; a diff only in the SPA | proposed |
| D7 | **A monitor can carry a value from the baseline to the next check as a run input** (e.g. `last_price` from the extraction's `/price`), so "only tell me if the price dropped" is an ordinary PRD-019 `compare` rule. On the first check there is no carried value and the check proceeds as changed. | R4 | Field-level comparison as a second built-in monitor feature with its own rule language | proposed |
| D8 | **Human approval is a new block, *Approval*, usable in any pipeline**, monitor or not. The run waits, holding no concurrency slot. *Approve* → the run continues. *Reject* → the run ends `completed`, stopped at the Approval block, and **nothing is undone**. No answer by the deadline → treated as a reject, labelled "expired". | R5 | Approval as a monitor-only setting outside the chain; reject as `failed` (a "no" is a normal answer, and `failed` would fire alerts and rollback) | proposed — owner questions **Q3–Q5** |
| D9 | **An approval never undoes a delivery.** An Approval block may sit after a sink, but rejecting does not remove what that sink delivered, and the approval request says what has already gone out. | R5 | Rejection triggering rollback (it would make a reject behave like a failure, which PRD-017 D4 and D13 keep separate) | proposed |
| D10 | **At most one approval waits per monitor. A newer check that also needs approval supersedes the older one**, which ends "superseded". Unchanged checks never disturb a waiting approval. | R5, R7 | No new checks while an approval waits (the monitor goes blind until someone answers) | proposed — owner question **Q3** |
| D11 | **Monitors use every existing limit, unchanged**: the per-user cap of 20 active schedules counts monitors and scheduled jobs together (paused ones do not count); each check is one monthly run, unchanged checks included; quota-blocked checks behave exactly as scheduled jobs do (ADR-010 §1); a check still running when the next is due makes that next one skip (ADR-013); after an outage, one late check. | R7 | A separate monitor quota | proposed |
| D12 | **A monitor pauses itself after 5 failed checks in a row**, and tells the owner once. Resuming re-arms it and resets the count. | R6 | Never pausing, as job schedules do (ADR-013 §2.3); see **Q6** | proposed — owner question **Q6** |
| D13 | **Monitor-level notices** (paused itself, a check skipped or waiting for quota, an approval waiting or about to expire) **always appear in the SPA and can also be sent to one saved destination** chosen on the monitor (PRD-017's saved webhook or email destination). **Run failures** keep PRD-019's *the run breaks* choice on the pipeline's own message-sending blocks. | R6 | Moving run-failure alerts onto the monitor too (the same URL typed twice, which PRD-019 rejected) | proposed |
| D14 | **BUG-031 is fixed before Monitors ship**: a user can permanently delete a pipeline run, a pipeline with its runs, and a monitor with its checks, and the storage they held is freed. | R8 | Shipping monitors first (a monitor that keeps failing piles up charged copies with no way out — PRD-017's warning) | proposed |
| D15 | **Changing a monitor's pipeline version or run inputs starts a new baseline**: the next check is a first check. Changing only the schedule or the notice destination does not. | R1, R2 | Comparing across versions (a changed extraction schema would report every field as changed) | proposed |
| D16 | **Scheduled crawls are not absorbed by this PRD.** A crawl is not a block (ADR-009 §13), so a monitor cannot run one. **`POST /crawls` refuses `schedule_cron`** with a message, from slice 1, so the API stops accepting a setting that does nothing. A "watch a whole site" monitor is later work. | R10 | Keeping the silent accept; building a crawl-shaped monitor now | proposed — owner question **Q8** |
| D17 | **Change detection returns to jobs only through monitors, never by porting it into the job lane** (D.2 stays dropped). Rec. for open item 10: **scheduled jobs run as monitors behind the unchanged job API**, which brings back dedup and the diff for exactly the jobs that had them; one-off jobs stay as they are; the full "/jobs is a front end over pipelines" is not taken now. | R10 | Porting D.2 into the job lane (two ways to do one thing); retiring job schedules in favour of monitors | proposed — owner question **Q1** |
| D18 | **Three slices.** Slice 1: monitors with the change check, the diff, carried values, alerts and self-pause, behind BUG-031's fix — **B ships here**, with its gate. Slice 2: the Approval block. Slice 3: scheduled jobs, per Q1. | Slices | One release with approval included; see **Q2** | proposed — owner question **Q2** |

---

## Problem

Since 2026-10-12 a user can build a pipeline and run it, but **only by pressing Run**. Nothing in layer A
repeats. The use case in ScrapeFlow's charter, *change detection to feed ML and data pipelines*, needs the
opposite: something that watches a page on its own and speaks up only when there is something to say.

```
What a user wants                          What ScrapeFlow offers today
──────────────────────────────────────     ─────────────────────────────────────────────────────
"Watch this pricing page every 6 hours.    • a pipeline: runs once per click
 If it changes, extract the prices,        • a scheduled job: repeats, but on the Temporal lane it
 tell me, and wait for my OK before          has had no change detection since 2026-10-12, and it
 pushing them to my systems."                cannot clean, validate, deliver to a bucket or wait for
                                             anyone
```

Three gaps, each with a cost on record.

**1. Repeating a pipeline without a gate is expensive, and the cost lands on the user.** PRD-016 shipped
pipelines without a change check on purpose, because nothing in layer A repeats, so the cost could not arise
(PRD-016 OQ-4(b)). The moment something runs a pipeline on a timer, it does:

```
scrape → clean → extract → webhook, every hour, page changes twice a week

                   LLM calls (user's own key)   stored copies    messages sent
without a gate     ~168 a week                  ~168 a week      ~168 a week
with a gate        ~2 a week                    ~2 a week        ~2 a week
```

The LLM calls are billed to the user's own provider key. The stored copies count against a hard 5 GB storage
limit (`default_quota_storage_bytes`), and once a user reaches it, every later run is refused. The messages
tell a receiver "here is a result" 166 times a week when nothing happened. PRD-016 made the gate a **launch
requirement**: Monitors does not ship without it.

**2. "Has it changed?" needs a definition of "since when".** A job is one URL, so its previous run is obvious.
A pipeline takes run inputs, so one saved pipeline run against 50 URLs has 50 separate histories, and layer A
cannot say which earlier run is "the previous one" (PRD-016 OQ-10). A monitor can: it is one pipeline, one set
of inputs, one schedule. **The monitor is the identity.** But even within one monitor the answer is not
"whichever ran last": if the last check found a change and then failed to deliver it, comparing the next check
against it would report "unchanged" and the change would never reach anyone (R2).

**3. Nothing in ScrapeFlow can wait for a person.** Every run goes straight from start to end. A user who wants
to look at a detected change before it is pushed to a database, a bucket or a customer-facing webhook has no
way to say "stop here and ask me". The engine was chosen partly because it makes waiting cheap and durable
(`workflows-scoping.md` §B); nothing uses it yet.

**And the job lane lost something to make room for this.** When jobs moved to Temporal (2026-10-12), the owner
dropped their change detection (implementation backlog D.2) rather than build it twice: no content-hash dedup
(every run calls the LLM and stores its copy) and no reporting diff (`diff_detected` and `diff_summary` are
`null`). PRD-016 R5 records that as its second named exception, "to return with Monitors". No jobs ran in
production when that was decided, so nothing was lost then. This PRD has to say when and how it comes back
(R10).

## Background — what change detection did on the job lane, and what this PRD inherits

### Today's behaviour, before 2026-10-12 (the NATS lane, now deleted)

Two mechanisms, in two places, for two reasons (PRD-016 OQ-10):

| | The cost gate (dedup) | The reporting diff |
|---|---|---|
| **When** | after the scrape, **before** the LLM call | after the last step |
| **Compared** | an exact hash of the scraped bytes with the previous run's | the new output with the previous run's: line by line for a page, field by field for an LLM extraction (`api/app/core/diff.py`, still in the codebase) |
| **On "same"** | no LLM call, **no webhook**, and the new stored copy was **deleted**; the run pointed at the previous run's file | `diff_detected: false` |
| **On "different"** | carried on | `diff_detected: true`, `diff_summary` describing the change, sent in the webhook |
| **Limits** | always on, invisible, byte equality only — a page with a timestamp in it was "changed" every time | first run: no diff |

The Temporal job lane still records the scrape's hash (`job_runs.content_hash`) but uses it for nothing.

### What PRD-019 hands over (Draft, in its Architect pass)

- **A Validate block can *stop* a run**: the run ends `completed`, later blocks are skipped, the run is
  labelled "stopped at <Validate>", and nothing is sent.
- **On a stop, *keep* or *delete*** what ScrapeFlow stored for the run. *Delete* frees the storage at once.
- **Rule values can come from run inputs**, of type number and text, so a rule can compare against a value
  handed over when the run starts. That is how a monitor supplies "what it was last time".
- **What a rule may read is fixed for good**: its block's input and the run's own inputs. No clock, no
  randomness, no lookups, no database. "Compare with the previous run" is allowed only as a value **handed
  over**, never looked up.
- **"The run breaks"**: a message-sending block can alert on any failure, wherever in the chain it happened.
- **What PRD-019 says this PRD must not assume:** that layer A defines "the previous run", or that a hash rule
  exists. Both are this PRD's.

### What PRD-017 hands over (Draft, in its Architect pass)

- **Sinks** (a bucket, a Postgres row, email; several webhooks) and **saved destinations** with write-only
  credentials. A monitor delivers through these.
- **Rollback**: when a run *fails*, reversible deliveries are undone, unless a webhook or email has already
  announced them. Never on cancel, never on a stop.
- **Failed runs keep ScrapeFlow's own copies** (D12 there).
- **What PRD-017 says this PRD must not assume:** that an approval step can undo an irreversible sink, or that a
  run's outputs can be freed before BUG-031 is fixed.

---

## Goals

1. A user can **turn a saved pipeline into a monitor**: pick a version, fix its inputs, set a schedule, and
   leave it. It keeps running across redeploys and restarts until the user pauses or deletes it.
2. **An unchanged page costs no LLM call, keeps no new copy and sends no message.** It still costs the one
   monthly run its fetch used.
3. **"Changed since when" has one written answer** the user can see on every check.
4. **A changed check says what changed**, on the run page and in the messages it sends.
5. A user can make a run **wait for their approval** before it continues, and the wait costs them no capacity.
6. **An unattended monitor that breaks does not stay broken quietly**: the owner hears about failures, and a
   monitor that keeps failing stops itself before it fills the storage limit.
7. **Monitors use the limits users already have** — the same scheduled cap, the same three quotas — and
   behave exactly as scheduled jobs do when a quota is reached.
8. **Jobs get a clear answer** about change detection, and the dormant scheduled-crawl setting stops pretending
   to work.
9. **Pipelines, jobs, batches, crawls and schedules keep working unchanged** except where R10 says otherwise.

## Non-goals

```
A — not yet:   can be added later; nothing saved today breaks
B — not here:  another PRD or decision owns it
C — never:     would break a rule ScrapeFlow already holds
```

**A — Not yet** (each additive later):

- **Fuzzy change detection** — "ignore the timestamp", "changed by more than 5%", "ignore this part of the
  page". The first change check is exact. A user with a noisy page places the check after a Clean block, or
  compares extracted fields (R4), which costs the LLM call. Both are stated in the docs.
- **A wait inside a run** — "wait 2 hours, then scrape again to confirm". ADR-009 §15a left room for a run to
  sleep between blocks; this PRD does not use it. The long sleep is *between* checks, and that is the schedule's.
- **A monitor end date** ("watch until 31 December"). Pausing or deleting covers it for now.
- **Time-zone-aware schedules.** Monitors use the same cron rules as scheduled jobs (UTC, 5-minute floor).
- **Several approvers, teams, or an approval delegated to someone else.** Only the monitor's owner approves.
- **One-click approve links in emails or webhooks** that work without signing in (owner question **Q11**).
- **A form in the SPA to create or edit a monitor.** Authoring is API-first, like pipelines; the SPA shows
  monitors and offers the actions a person needs while one runs (R9).
- **Per-monitor retention** ("delete stored results older than 30 days") — owner question **Q12**.
- **MCP tools for monitors.**

**B — Not here:**

- **Watching a whole site** (a scheduled crawl as a monitor). A crawl is not a block (ADR-009 §13). D16 says
  what happens to `crawls.schedule_cron` meanwhile.
- **Branching** ("if the price dropped, email me; otherwise write a row"). PRD-019 deferred it; nothing here
  needs it.
- **"Any of" between rules** ("price changed **or** stock changed"). PRD-019 deferred it. A monitor can get
  close with two carried values and two Validate blocks, but not an OR. If users ask, it is PRD-019's rule
  vocabulary that grows, under its R3.
- **Re-delivering a failed check from its stored outputs.** PRD-017 named this a non-goal. R2's baseline rule
  covers the common case instead: the next check finds the change again and delivers it.
- **Fixing BUG-019** (the nightly job-run cleanup has never succeeded in production). It is infra-only and
  unrelated to pipelines. It is named so nobody counts it as a retention rule for monitors: it does not cover
  pipeline runs, and it has never run.

**C — Never:**

- **A change check that looks anything up mid-run** — the previous run's row, another site, the clock.
  PRD-019 D5 forbids it permanently. The baseline is **handed over** when the check starts (R2).
- **Bypassing a quota for a monitor.** Every check is metered like any run.

## User stories

**As a price watcher**, I want a monitor on a product page every hour that extracts the price only when the
page has changed, so I pay for an LLM call twice a week, not 168 times.

**As a data engineer**, I want *scrape → clean → change check → extract → my Postgres table*, every 6 hours,
so my table gets a row when the page changes and stays quiet when it does not.

**As a user reading a monitor**, I want its history to read "unchanged, unchanged, **changed → sent**,
unchanged" and to open the changed one and see what changed, so I trust it without reading every check.

**As a deal watcher**, I want "tell me only if the price dropped since last time", not "only if the page
changed", so a changed review count does not wake me up.

**As a user publishing to customers**, I want a detected change to **wait for my approval** before it goes to
the customer-facing webhook, so a bad extraction is caught by a person, not by a customer.

**As a user who did not answer in time**, I want the approval to lapse quietly and be asked again when the
next change comes, so a missed request does not push data I never saw, nor lose the change for good.

**As a user whose webhook receiver was down**, I want the next check to deliver the change it failed to
deliver, so one outage does not mean I never hear about it.

**As a user with twelve unattended monitors**, I want to be told when one breaks, and I want a monitor that
breaks every time to stop itself, so a dead site does not eat my storage and my monthly runs for weeks.

**As a user at my storage limit**, I want to delete a monitor's old checks and get the space back, so a full
quota is something I can fix myself.

**As a user of scheduled jobs**, I want to know whether my jobs will get change detection back, and when,
without having to rebuild them as pipelines.

---

## Requirements

### R1 — What a monitor is

- **A monitor belongs to one user** and has a **name** (unique per user), **one pipeline**, **one pinned
  version** of that pipeline, **values for every run input** the version declares, a **schedule**, and an
  optional **notice destination** (R6). Cross-tenant access returns **404**, as everywhere else.
- **Each firing is a check, and a check is a pipeline run** (D1) of the pinned version with the monitor's
  inputs — plus any values the monitor hands over (the baseline's fingerprint, carried values; R2–R4). It
  appears in the pipeline's run list like any run, marked as belonging to the monitor, and in the monitor's own
  history.
- **The version is pinned, not "latest"**, because a monitor's checks are compared with each other and an
  edit to the pipeline can change what they produce. Editing the pipeline does not change any monitor. The
  user moves a monitor to a newer version explicitly, and moving it starts a **new baseline** (D15). The SPA
  shows, on the monitor, when a newer version of its pipeline exists.
- **Actions:** create, read, list, edit (schedule, inputs, version, notice destination, name), **pause**,
  **resume**, **check now**, delete.
  - **Pause** stops future checks. It does **not** cancel a check in flight; cancelling one check is the run's
    own Cancel (PRD-016 R3). The same separation jobs already draw between cancelling a run and retiring a
    schedule.
  - **Check now** starts one check outside the schedule. It is an ordinary check: compared with the baseline,
    metered, refused if a check is already in flight (R7).
  - **Delete** stops the monitor and cancels a check in flight (at the next block boundary, nothing undone).
    The checks stay as pipeline runs with their history, like runs of a deleted pipeline, unless the user
    chooses **delete with its checks**, which frees their storage (R8).
- **Edits take effect from the next check.** A check in flight finishes on what it started with (PRD-016's
  pinning rule).
- **Deleting a pipeline that any monitor uses, active or paused, is refused**, naming the monitors — the same
  rule PRD-017 proposes for a destination in use.
- **Save-time checks** for a monitor, refused with a message naming the problem: every run input of the pinned
  version has a value of the right type; the schedule is a valid cron no more often than the scheduled-job
  floor (5 minutes); D4's change-check rule; creating or resuming past the scheduled cap (R7).
- **Survives restarts.** A redeploy of any ScrapeFlow component, or a Temporal restart, never loses a monitor,
  never runs a check twice, and after an outage runs **one** late check, not one per missed firing (ADR-013).

### R2 — "The previous check": the baseline

*(Obligation 2.)*

**The rule (D2):** a check is compared with **the most recent earlier check of the same monitor that ended
`completed`, since the monitor's version or run inputs last changed.**

```
              outcome                         becomes the baseline?
check 1       completed (first check)         yes
check 2       completed, unchanged (stop)     yes  (same content as 1)
check 3       failed at Webhook (receiver     NO   → check 4 is compared with check 2,
              down) — change detected               sees the change, and delivers it
check 4       completed, changed, sent        yes
check 5       cancelled by the user           NO
check 6       completed, approval rejected    yes  → the user has seen this content; don't ask again
check 7       completed, approval expired     NO (proposed, Q4) → the next change check asks again
```

Why each line:

- **Failed checks are never the baseline.** A failed check may have detected a change and not delivered it.
  If it became the baseline, the next check would say "unchanged" and stop, and the change would be lost
  without a sound. Skipping failed checks makes the next check find the change again and deliver it — the
  common case of PRD-017's "re-deliver from stored outputs" non-goal, at the cost of a fresh fetch.
- **Cancelled checks are never the baseline.** The user stopped them; they say nothing about what was seen.
- **Stopped checks are the baseline.** A stop is a success (PRD-019): the check saw the page and decided.
  "Unchanged" stops have the same content as their own baseline, so it makes no difference to the next
  comparison; Validate stops ("price not under £100") and rejections are real decisions about that content.
- **Expired approvals are not the baseline** (proposed; owner question **Q4**): nobody looked at the change, so
  the next check that finds the page still different from the last baseline asks again.
- **A change of version or inputs starts over** (D15). A different URL, or a different extraction schema, is a
  different thing being watched. The next check is labelled "first check since the monitor was changed".
- **Checks from a deleted monitor, or of another monitor on the same pipeline, are never a baseline.** The
  monitor is the identity.

**What the user sees:** every check after the first names its baseline — "compared with the check of
14 Nov 06:00" — with a link. If there is no baseline (the first check, or the first after a change), the check
says so and proceeds as **changed**.

**The baseline is handed over when the check starts**, as run inputs, never looked up mid-run (PRD-019 D5). A
check therefore compares with the baseline as it stood when the check began; if an earlier check is still
finishing (it cannot be, under R7's overlap rule, except for an approval wait — R5), the later check does not
see its result.

### R3 — The change check (the cost gate)

*(Obligation 1, first half. The launch requirement.)*

- **A change check is a Validate block set to stop, with the new rule "the content is the same as at the
  baseline"** (D3) — written so that "same" means **stop**. The user places it in the chain like any Validate:

  ```
  scrape ──► clean ──► [change check] ──► extract($) ──► webhook

  page same as baseline     ■ stop — completed, "unchanged since 14 Nov 06:00"
                              extract and webhook skipped: no LLM bill, no message
  page differs              ✔ carry on — "changed"
  first check               ✔ carry on — "first check"
  ```

- **What "same" means:** the content the change check receives is **byte-for-byte identical** to what the
  change check received in the baseline check. Placed after Scrape, it compares the raw page; after Clean, the
  cleaned page; after an extraction, the extraction. Placing it after Clean is recommended for pages with
  rotating ads or timestamps, and the docs say so.
- **The rule reads only its block's input and one run input** — the baseline's fingerprint, handed over by
  the monitor. That is inside PRD-019's permanent rule on what a rule may read, and is the statement PRD-019 R3
  asks every new rule kind to make.
- **Outside a monitor** (a pipeline containing a change check run by hand), there is no baseline: the rule
  always says "differs" and the run carries on, labelled "no baseline (not run by a monitor)". The same
  pipeline is usable both ways.
- **An unchanged check ends `completed`**, labelled **"unchanged since <baseline check>"** in the run page, the
  run list and the monitor's history — PRD-019's "stopped at" label, worded for this rule. Later blocks are
  skipped with the reason "stopped: unchanged".
- **On "unchanged", ScrapeFlow's copies are deleted by default** (D5), freeing their storage at once — what the
  job lane's dedup did. The user may choose *keep* on the change check (PRD-019 D10's choice) if they want a
  copy per check. The check's result then reads "unchanged — see <baseline check>" rather than nothing.
- **Costs:** an unchanged check costs **one monthly run** (the fetch happened — PRD-016 OQ-4) and, with
  *delete*, **no storage**. It makes no LLM call and sends no message. This is the job lane's dedup cost
  exactly.
- **Required where it matters (D4):** a monitor whose pinned version contains an LLM block is refused at save,
  and on a version move, unless a change check comes **before** the first LLM block. A version with no LLM
  block saves with a warning that every check will store a copy and run every block.
- **Several change checks in one pipeline** are allowed (e.g. one on the cleaned page before the LLM, one on
  the extraction after it). Each compares with its own position in the baseline check.
- **Never retried**: "same" and "differs" are final answers (PRD-019 R1). Trouble reading the content from
  storage is retried as for any Validate.

### R4 — What changed: the reporting diff, and carried values

*(Obligation 1, second half.)*

- **Every changed check records what changed** (D6):
  - **the page** — when a change check reads a page: a line-by-line summary of what was added and removed
    against the baseline's page;
  - **the extraction** — when the check and its baseline both produced an extraction: which fields were added,
    removed or changed, with old and new values.

  The run page shows both. A first check shows "first check — nothing to compare". An unchanged check shows
  nothing changed.
- **What changed travels in messages.** The Webhook block's payload on a monitor's check gains a **`change`**
  field — changed / first check, the baseline check's id, and the summary — and the monitor's id. Additive:
  receivers that ignore unknown fields are unaffected, and payloads of runs outside a monitor do not change.
  PRD-017's Email block includes the same summary in its body. The approval request (R5) shows it.
- **The summary is bounded.** A page that changed everywhere must not produce a message of unbounded size; the
  summary is cut to a stated limit and says it was cut, with a link to the full comparison on the run page.
- **If the baseline's stored copy is gone** (the user deleted it, or it was deleted on a stop), the check still
  knows it changed — the fingerprint survives — but says "changed; the previous copy is no longer stored, so
  no diff" instead of a diff.
- **Carried values (D7).** A monitor may declare, for a run input of the pinned version, "fill this from the
  baseline's extraction at path *P*". Example: the input `last_price` ← the baseline's `/price`. The pipeline
  uses it in an ordinary PRD-019 rule:

  ```
  scrape ──► clean ──► [change check] ──► extract($) ──► Validate(/price < {last_price}, stop) ──► webhook

  page unchanged                     ■ unchanged — no LLM call
  page changed, price 120 → 120      ■ stopped at Validate — "price not lower" (reviews changed, not price)
  page changed, price 120 → 99       ✔ webhook: "price dropped", change summary attached
  first check                        ✔ carry on — "first check" (no last_price yet)
  ```

  - The value comes from the **baseline**, so "since last time" means "since the last check that completed",
    by R2's rule — a failed check never moves it.
  - **On the first check the carried value has no value, and the check proceeds as if changed.** This must not
    weaken PRD-019 D4 ("a rule that cannot be checked fails the run") for ordinary missing data — a missing
    `/price` in the *new* extraction still fails loudly. How the two are told apart is an Architect question
    (OQ-4).
  - If the baseline had no value at *P* (its extraction lacked the field), the check behaves as a first check
    for that rule and says so.
- **Today's diff code is the starting point, not a target.** `diff.py` compares two stored outputs line by
  line or field by field; this PRD needs the same two comparisons. Its output shape is not a contract.

### R5 — Human approval (slice 2)

*(Obligation 3, the approval half.)*

- **A new block, Approval** (D8). It holds no content and passes its input on unchanged, like Validate. When a
  run reaches it, the run **waits for the pipeline's owner** to approve or reject. Usable in any pipeline; a
  monitor is the main user.

  ```
  scrape ─► clean ─► [change check] ─► extract($) ─► bucket ─► [Approval] ─► webhook to customers

  unchanged    ■ unchanged — no approval asked
  changed      ⏸ waiting for your approval (expires 15 Nov 06:00)
                  ├─ Approve  ✔ → webhook sent → completed
                  ├─ Reject   ■ completed, "stopped: rejected"; webhook skipped; the bucket file STAYS
                  └─ no answer by the deadline
                              ■ completed, "stopped: approval expired"; webhook skipped; bucket file stays
  ```

- **Asking.** On reaching the block, the request appears in the SPA (on the run, on the monitor, and in a list
  of everything waiting for the user) and is sent to one destination named on the block — a saved webhook or
  email destination (PRD-017). The request says: which pipeline and monitor, **what changed** (R4), what has
  **already been delivered** and will not be undone (D9), and the deadline. It links to the SPA; approving
  needs the user signed in (owner question **Q11** on one-click links). Delivery of the request follows the
  webhook rules: retried over the usual window, checked against private addresses on every attempt, and an
  undeliverable request is shown as undelivered — the approval still waits, and the SPA still shows it.
- **Answering.** Approve or reject from the SPA or the API, by the run's owner only; anyone else gets 404.
  Answering twice, or after the deadline, is refused with the current state; it never flips a decision. The
  run records who answered and when.
- **Approve** → the block completes and the run carries on.
- **Reject** → the run ends **`completed`**, stopped at the Approval block, later blocks skipped with the
  reason "approval rejected". **Nothing is undone** (D9): a stop is a success, and PRD-017 undoes only on
  failure. **No failure alert fires.** ScrapeFlow's own copies are kept (owner question **Q5**).
- **Expiry** → the same as reject, labelled **"approval expired"**, plus a monitor notice (R6). Proposed: an
  expired check is not the baseline (R2, **Q4**).
- **The deadline** is set on the block: default **24 hours**, maximum **7 days**, both operator-configurable
  (owner question **Q10**). It must be stated in the request and on the run.
- **Waiting costs nothing.** A run waiting for approval **holds no concurrency slot** (ADR-009 §15a: a run
  parked on a timer is not active) and no monthly run beyond the fetch it already used.
- **Cancel** while waiting ends the run `cancelled` at once — there is no block executing to wait for.
- **An approval cannot undo anything** (D9, PRD-017's hand-forward). An Approval block **may** come after a
  sink — "save to my bucket always, publish only after I approve" is the example above — but the request says
  what already went out. An Approval block may not be the first block (there is nothing to show) and may not be
  the last (there is nothing to hold back); both refused at save.
- **One approval waiting per monitor (D10).** While a monitor's check waits for approval, the monitor keeps
  checking on schedule:
  - a later check that finds **no change** against the baseline stops as unchanged and leaves the waiting
    approval alone;
  - a later check that **also** reaches an Approval block **supersedes** the waiting one, which ends
    `completed`, "stopped: superseded by the check of <time>", and only the newest change waits.

  Rec. over the alternative — no new checks while an approval waits — because a monitor that goes blind until
  someone answers, and then asks about a stale change, fails the watch-it-for-me promise (owner question
  **Q3**). Outside a monitor, each run's approval stands alone.
- **Approval and time budgets.** A wait of up to 7 days does not fit inside today's 24-hour run ceiling
  (`pipeline_max_run_seconds`). The product rule: the deadline is declared and visible like every block budget
  (PRD-016 R4), and a pipeline whose blocks *plus* its approval deadlines cannot complete is refused at save,
  naming the numbers. How the waiting time composes with the ceiling is OQ-6.

### R6 — Unattended running: alerts, notices and self-pause

- **Run failures alert through PRD-019.** A monitor's checks are pipeline runs, so a message-sending block set
  to *the run breaks* alerts on any failed check, wherever it broke. Nothing new.
- **A monitor with no failure route is warned about at save**: if neither the pipeline's blocks (*the run
  breaks*) nor the monitor's notice destination would tell anyone about a failure, the save succeeds with a
  warning that a failing monitor will be visible only in the SPA.
- **Monitor notices (D13)** are events that belong to the monitor, not to one run — some have no run at all:

  | Notice | When |
  |---|---|
  | **paused itself** | after the 5th failed check in a row (D12), naming the last failure |
  | **check skipped — monthly runs used up** | a firing skipped by ADR-010 §1 (no run exists) |
  | **check waiting for a free slot** / **gave up waiting** | ADR-010 §1's wait, and its 24-hour cap |
  | **approval waiting** / **approval expires in 2 hours** / **approval expired** | R5 |
  | **check skipped — previous check still in progress** | R7 (shown in history; sent only if the user opts in, since a slow page would send it every time) |

  Every notice appears in the monitor's history in the SPA. If the monitor names a **notice destination** (one
  saved webhook or email destination, PRD-017), notices are also sent there, signed, retried and checked
  against private addresses like any webhook. Notices cost no quota.
- **Self-pause (D12).** After **5** failed checks in a row, the monitor pauses itself and sends a "paused
  itself" notice once. A completed check (including a stop) resets the count; skipped firings neither count nor
  reset. Resuming re-arms the monitor and resets the count. Why: a failed check keeps its stored copies and
  costs a monthly run (PRD-017 D12, PRD-016 OQ-4), so a monitor on a dead site would otherwise spend the user's
  monthly runs and storage indefinitely. ADR-013 chose *never pause* for job schedules because "a paused
  Schedule nobody un-pauses is a silent stop"; this rule is not silent — it notifies, and the SPA shows it.
  Whether it should also apply to scheduled jobs is out of scope (owner question **Q6**).

### R7 — Schedule, overlap and quotas

- **Schedules are cron expressions, as for scheduled jobs**: UTC, no more often than every 5 minutes
  (`schedule_min_interval_minutes`).
- **The scheduled cap counts monitors** (D11). The per-user cap on active schedules (ADR-013: default 20,
  overridable per user, paused ones not counted) counts **active monitors and active scheduled jobs together**.
  ADR-013 §4 already says the cap is lane-blind and that the lane adding schedules widens the count. Creating or
  resuming a monitor past the cap is refused with the same `quota_exceeded` shape (`quota_type:
  "scheduled_jobs"`), and resume is checked like create.
- **Every check is one monthly run**, whatever it does after the fetch — changed, unchanged, failed, rejected.
  That is PRD-016 OQ-4's unit (one attempted fetch) and the job lane's behaviour for a deduplicated run. Approval
  and notices cost nothing.
- **Show the cost before it surprises anyone.** At save, the API response and the SPA state the monitor's
  projected monthly runs against the user's limit: *"every hour ≈ 720 checks a month; your monthly limit is 500
  runs"*. A warning, not a refusal — the user may have a raised limit or intend to pause.
- **Quota-blocked firings behave exactly as for scheduled jobs** (ADR-010 §1), checked per firing:

  | Meter reached | The firing | Visible as |
  |---|---|---|
  | monthly runs | **skipped**; next firing runs normally | a notice; no run, no unit charged |
  | concurrency | **waits** for a slot, holding none; gives up after 24 hours | a notice; a check once it starts |
  | storage | **fails now**, naming the remedy | a failed check (counts towards self-pause; alerts via *the run breaks*) |

- **Overlap (ADR-013's `SKIP`).** A firing that comes while the monitor's previous check is still running is
  **skipped**, recorded in the history, and costs nothing. A monitor never has two checks running at once, so
  checks finish in order and each compares with a settled baseline. **The one exception is an approval wait**
  (R5, D10): a check waiting for approval does not stop later checks.
- **Check now** follows the same rule: refused while a check is running, with the running check named.
- **Concurrency.** A running check holds one slot like any pipeline run. Waiting for approval, waiting between
  delivery retries (PRD-017), sleeping until the next firing, and waiting for a quota slot hold none.

### R8 — Storage

- **BUG-031 is fixed before Monitors ship (D14)**, with three user actions, each freeing the storage it removes
  through the same accounting every other delete uses, and refusing with an error (not half-deleting) if any
  stored object cannot be removed:
  - **delete one pipeline run** — its stored outputs are freed and it disappears from lists;
  - **permanently delete a pipeline with its runs**;
  - **delete a monitor with its checks**.

  Deleting in ScrapeFlow never reaches into the user's own destinations (PRD-017).
- **What a monitor stores, by outcome:**

  | Check | ScrapeFlow's copies |
  |---|---|
  | unchanged | **deleted** by default (D5); kept if the change check says *keep* |
  | changed, completed | kept (it is the new baseline, and the diff needs it next time) |
  | stopped by another Validate, rejected, expired, superseded | kept (proposed; PRD-019's *keep* default; Q5 for reject) |
  | failed | **kept** (PRD-017 D12), charged — hence self-pause (R6) |

- **Copies are never shared between checks.** An unchanged check deletes its own copy and *points the user* at
  its baseline; it does not adopt the baseline's stored file as its own, as the job lane's dedup did. Each
  stored object belongs to exactly one check, so deleting one check never breaks another's result. (ADR-009 §8c
  warns that its per-run collection rule becomes unsafe the day objects are shared between runs; this keeps
  that day from arriving.)
- **At the storage limit** the user sees what they see on any pipeline run — the storage wall, naming the
  remedy — and, with BUG-031 fixed, the remedy works.
- **No automatic retention in slice 1** (owner question **Q12**). The nightly cleanup does not cover pipeline
  runs and has never run in production (BUG-019).

### R9 — Visibility

- **API:** monitors (CRUD, pause, resume, check now), each monitor's history (checks, skipped and waiting
  firings, notices), pending approvals, approve / reject. Checks are also readable through the existing pipeline
  run routes.
- **SPA:**
  - a **Monitors list**: name, pipeline and version, schedule, state (active / paused / paused itself), next
    check, last check's outcome, approvals waiting;
  - a **monitor page**: the history as a scannable list — *unchanged · unchanged · **changed → sent** · failed ·
    skipped (quota)* — with links into each check's run page, the baseline each check used, what changed,
    notices, and Pause / Resume / Check now / Delete;
  - **approvals waiting**: one list across monitors and pipelines, with Approve / Reject and what changed;
  - the run page gains the change verdict, the baseline link and the diff (R4), and the Approval block's state
    (R5), live, as it already shows other block states.
- **Admins** see every user's monitors, histories and pending approvals wherever they already see pipelines.
  An admin **can pause** any monitor (a misbehaving one hammering a site) and the owner is told; an admin
  **cannot approve or reject** on a user's behalf.
- **The operator can stop all monitors firing** without deleting them (as `PIPELINES_ENABLED` already gates
  pipelines). Firings during the stop are skipped and recorded, not run late.
- **No meter counts one lane and reports for all** (ADR-009 §15e; the BUG-028 class). Admin statistics that
  count recurring work (today `active_recurring_jobs`) either include monitors or are named for the lane they
  count. Monitor checks are pipeline runs, so the run-counting views already include them; this PRD's success
  checks confirm it rather than assume it.

### R10 — Jobs and crawls

*(Obligations 4, 5 and 6.)*

**What jobs get back, and when (D17).**

- **Change detection is never ported into the job lane.** D.2 stays dropped. The owner's principle at D.2 — one
  way to do a thing — holds: change detection lives in monitors.
- **Until slice 3 ships, nothing changes for jobs**: no dedup, `diff_detected` and `diff_summary` stay `null`
  (PRD-016 R5's second exception continues). The docs say so, and say that a monitor is the way to watch a page
  for changes.
- **Rec. for open item 10 (owner question Q1): scheduled jobs run as monitors behind the job API, which does
  not change.** A scheduled job is the one kind of job that ever had a previous run, so it is the only kind
  change detection ever applied to. Under this option, in slice 3:
  - a scheduled job's firings run as checks of a fixed recipe — *scrape → (change check) → (extract) →
    (webhook)* — so they regain **exactly today's job-lane behaviour**: an unchanged page makes no LLM call,
    stores no new copy and sends no webhook; a changed one sends `diff_detected` / `diff_summary` again;
  - **the job API, its runs list, its result route, its `job.completed` / `job.failed` payload, its webhook
    secret and its storage-wall behaviour do not change** — this is the hard constraint. A user of scheduled
    jobs notices only that change detection is back;
  - one-off jobs stay exactly as they are; they never had a previous run;
  - the full version of open item 10 — every job, one-off included, running as a pipeline — is **not taken
    now**. It would change what job users see (open item 10 lists five differences) for no feature gain. Revisit
    it if slice 3 shows the translation is cheap.
- **The alternatives**, for the owner:

  | Option | Jobs get change detection back? | What users see change | One way to do it? |
  |---|---|---|---|
  | **(a) Rec. — scheduled jobs run as monitors, job API unchanged** | yes, scheduled jobs, slice 3 | nothing but the return of dedup + diff | yes |
  | **(b) Retire job schedules**: new jobs cannot be scheduled; existing ones are converted to monitors | no; users move to monitors | scheduling leaves the simple job API; job users must learn pipelines | yes |
  | **(c) Port D.2 into the job lane** | yes, sooner | nothing | **no** — two change-detection paths, which the owner declined at D.2 |
  | **(d) All of `/jobs` becomes a front end over pipelines** | yes | the open-item-10 differences, unless each is hidden | yes, and the most work |

**Scheduled crawls (D16).**

- `crawls.schedule_cron` is accepted and stored today and **nothing ever runs it**. `phase4-backlog.md` §3 says
  the gap is "absorbed by Monitors". **This PRD proposes it is not, for now**: a monitor runs a pipeline, and a
  crawl is not a pipeline block (ADR-009 §13), so a monitor cannot run one.
- **From slice 1, `POST /crawls` refuses `schedule_cron`** with a message saying scheduled crawls are not
  supported, so the API stops accepting a setting that silently does nothing. Existing stored values are
  ignored as today; whether any exist in production is for the Architect to check (OQ-13). Owner question
  **Q8**.
- **"Watch a whole site for changes"** — a scheduled crawl with per-page change detection — is later work. It
  needs a per-page baseline (each page its own history) and a decision about whether a crawl becomes a block.

### R11 — Compatibility

- **Additive for pipelines.** A pipeline saved before this PRD runs exactly as before. The change-check rule
  kind and the Approval block are new; no existing block changes.
- **Runs in flight at a release finish on the behaviour they started with** (ADR-012). Monitors add new paths
  to the pipeline workflow (the change verdict, the approval wait), so each needs the replay test's coverage.
- **Jobs, batches, crawls and schedules are untouched by slices 1 and 2**, except D16's refusal of
  `schedule_cron` on new crawls. Slice 3 changes scheduled jobs only as R10 describes, behind an unchanged job
  API.
- **Admin pages and meters** keep working; new statistics follow R9's lane rule.

---

## Slices

| Slice | Contains | Ships when | B "ships"? |
|---|---|---|---|
| **0 — precondition** | BUG-031's fix (R8) | before slice 1 | — |
| **1 — monitors** | R1, R2, R3 (the change check), R4 (diff, carried values), R6, R7, R8, R9 (without approvals), D16's crawl refusal | after PRD-019 is built, and after PRD-017's saved destinations exist (R6's notice destination; until then, notices are SPA-only) | **yes** — with the cost gate, as PRD-016 requires |
| **2 — approval** | R5, and R9's approvals list | after slice 1 | — |
| **3 — scheduled jobs** | R10 per the owner's answer to Q1 | after slice 1 | — |

Slice 1 is the thin end-to-end path: a pipeline repeated on a schedule that stays quiet and cheap when nothing
changed and says what changed when something did. Approval is the showcase of the engine but not what makes
monitors safe to run (owner question **Q2**).

---

## Success criteria

**Monitors (R1)**
- [ ] A monitor on *scrape → clean → change check → extract → webhook* every 5 minutes runs on schedule, and its
      checks appear in its history and in the pipeline's run list, marked as the monitor's.
- [ ] Pausing stops future checks without cancelling one in flight; resuming re-arms; deleting cancels the
      check in flight at the next block boundary.
- [ ] Editing the pipeline does not change the monitor; moving the monitor to the new version makes the next
      check a "first check since the monitor was changed".
- [ ] Deleting a pipeline used by a monitor (active or paused) is refused, naming the monitor.
- [ ] A restart of the workflow worker, the API and Temporal mid-schedule loses no monitor and runs no check
      twice; a 5-minute Temporal outage produces **one** late check.
- [ ] Another user's monitor returns 404 on every route.

**The baseline (R2)**
- [ ] With a check that failed at the Webhook block after detecting a change, the next check compares with the
      last **completed** check, finds the change, and delivers it.
- [ ] Cancelled checks are never a baseline; a rejected check is; each check names its baseline.
- [ ] Changing the URL input makes the next check a first check.

**The change check (R3)**
- [ ] On an unchanged page: the check ends `completed`, labelled "unchanged since <check>"; **no LLM call** is
      made (provider call count unchanged), **no message** is sent, and with the default the check's stored
      copies are **deleted** and storage usage is unchanged by the check.
- [ ] On a changed page the check runs every block and is labelled "changed".
- [ ] The same pipeline run by hand (no monitor) carries on past the change check, labelled "no baseline".
- [ ] A monitor whose version has an LLM block and no change check before it is refused at save, naming the
      block; with the check after the LLM block, also refused; a version with no LLM block saves with a warning.
- [ ] Measured: over 24 hourly checks of a page that changes twice after the first check, exactly 3 LLM calls
      (the first check and the two changes), 3 kept copies, 3 webhook messages and **24** monthly runs.
- [ ] An unchanged check's result reads "unchanged — see <baseline check>", and deleting the baseline check
      never breaks a later check's result (no shared objects).
- [ ] The change-check step's answer is never retried and survives a worker kill between the decision and the
      next block (replays to the same decision).

**What changed (R4)**
- [ ] A changed check shows a page diff and, with an extraction, a field diff naming old and new values; the
      webhook payload carries `change` and the monitor id, and a receiver that ignores unknown fields still
      accepts it.
- [ ] A page changed throughout produces a summary cut to the stated limit, marked as cut.
- [ ] With the baseline's copy deleted, a changed check says "changed; the previous copy is no longer stored".
- [ ] The *price dropped* pipeline (carried `last_price`): unchanged page → no LLM call; changed page with the
      same price → stopped at the Validate; lower price → webhook sent; first check → carries on.
- [ ] A carried value never moves on a failed check.
- [ ] A missing `/price` in the **new** extraction still fails the check loudly (PRD-019 D4 intact).

**Approval (R5, slice 2)**
- [ ] A changed check reaching an Approval block waits, shown as waiting with its deadline; the request is sent
      to the block's destination naming what changed and what was already delivered.
- [ ] While it waits, the user can start runs up to their full concurrency limit.
- [ ] Approve → the run carries on. Reject → `completed`, "stopped: rejected", later blocks skipped, a bucket
      file delivered before the Approval block **stays**, no failure alert.
- [ ] No answer → "approval expired" at the deadline, a notice is sent, and the next check that still differs
      from the baseline asks again.
- [ ] A second, later change supersedes the waiting approval; an unchanged later check leaves it alone.
- [ ] Approving twice, after the deadline, or as another user is refused and changes nothing.
- [ ] Cancel while waiting ends the run `cancelled` within seconds.
- [ ] An Approval block first or last is refused at save; a pipeline whose blocks plus approval deadline cannot
      finish is refused, naming the numbers.

**Unattended running (R6)**
- [ ] A monitor on a dead site pauses itself after the 5th failed check in a row and sends one notice; a
      completed check in between resets the count.
- [ ] Each notice in R6's table appears in the monitor's history, and is sent to the notice destination when
      one is set.
- [ ] A monitor with no failure route saves with a warning.

**Schedule and quotas (R7)**
- [ ] With 20 active schedules (jobs and monitors mixed), a 21st monitor — created or resumed — is refused with
      `quota_type: "scheduled_jobs"`; a paused monitor does not count.
- [ ] With monthly runs used up, a firing is skipped with a notice and **no run and no unit**; at concurrency
      full, the firing waits holding no slot; at the storage limit, it fails naming the remedy.
- [ ] A check longer than the interval makes the next firing skip, recorded; two checks of one monitor never
      run at once; Check now is refused while one runs.
- [ ] The save response states projected monthly runs against the limit.

**Storage (R8)**
- [ ] BUG-031: deleting one pipeline run, a pipeline with its runs, and a monitor with its checks each lowers
      storage usage by exactly the bytes removed; an object that cannot be removed makes the delete refuse,
      with nothing half-deleted.

**Visibility (R9)**
- [ ] The SPA's monitor page reads as a scannable history with the change verdicts, and updates live.
- [ ] An admin can see and pause any monitor and cannot approve; the owner is told of an admin pause.
- [ ] Admin statistics for recurring work count monitors, or are named for the lane they count.
- [ ] The run-counting views count monitor checks (checked, not assumed).
- [ ] With monitors stopped by the operator, firings are skipped and recorded, and none runs late on restart.

**Jobs and crawls (R10)**
- [ ] `POST /crawls` with `schedule_cron` is refused with a message (slice 1).
- [ ] Slice 3 (if Q1 = a): a scheduled job on an unchanged page makes no LLM call, keeps no new copy and sends
      no webhook; on a change it sends `diff_detected: true` and a `diff_summary`; the job API and payload are
      otherwise byte-for-byte as before.

**Compatibility and replay (R11)**
- [ ] Every pipeline saved before release runs unchanged; existing suites and the replay corpus pass unchanged.
- [ ] The replay corpus gains histories of an unchanged stop, a changed check with a diff, and (slice 2) an
      approve, a reject, an expiry and a supersede, all replaying cleanly in the test that gates the api build.

---

## Open questions for the owner

Product calls this draft could not make alone. Each has the PM's recommendation; none is decided.

| # | Question | Rec. | Why it matters |
|---|---|---|---|
| **Q1** | **Open item 10: what happens to jobs?** (a) scheduled jobs run as monitors behind the unchanged job API; (b) retire job schedules in favour of monitors; (c) port D.2 into the job lane; (d) all of `/jobs` over pipelines. | **(a)** | Decides whether and when jobs get change detection back (R10) and the size of slice 3 |
| **Q2** | **Approval in slice 1 or slice 2?** | slice 2 | Slice 1 is safe and useful without it; approval is the biggest new mechanism |
| **Q3** | **While a monitor's approval waits, do later checks run?** Supersede (newest change waits) vs no new checks until answered. | supersede | Decides whether a monitor goes blind while waiting, and adds Appendix A's "superseded" ending |
| **Q4** | **An expired approval: `completed` + "expired" (and not the baseline, so it asks again), or `failed` (so *the run breaks* alerts fire)?** | `completed`, not the baseline | Expiry is "nobody looked", not "something broke"; R6's notice already tells the owner |
| **Q5** | **On reject: always keep ScrapeFlow's copies, or offer keep / delete like a Validate stop?** | always keep | Simplest; a rejected change is the one a user may want to inspect |
| **Q6** | **Self-pause after repeated failures: in? After how many?** And should job schedules get it too (a change to ADR-013's decision, so a new ADR)? | in, 5, monitors only | Failed checks keep charged copies (PRD-017 D12) |
| **Q7** | **May a user opt out of D4's required change check** ("run every block every time, I know")? | no opt-out in slice 1 | Opt-out is additive later; taking it away later breaks saved monitors |
| **Q8** | **`schedule_cron` on crawls: refuse from slice 1, keep accepting silently, or build a site monitor now?** | refuse | Today it is a setting that does nothing |
| **Q9** | **Should Monitors wait for PRD-017's saved destinations**, or ship slice 1 with SPA-only notices if PRD-017 is later? | ship SPA-only if needed | Decides slice 1's dependency on PRD-017 |
| **Q10** | **Approval deadline: default 24 hours, maximum 7 days?** | yes | Product numbers, operator-configurable |
| **Q11** | **One-click approve / reject links** in the email or webhook, working without signing in? | no — the message links to the SPA | A forwarded email would let anyone approve |
| **Q12** | **Per-monitor retention** ("delete checks older than N days") in slice 1? | no; BUG-031's deletes are the remedy | Additive later |

## Open questions for Architect

**OQ-1 — Recording the new endings, together with PRD-019 OQ-2 and PRD-017 OQ-1.** Appendix A lists every new
way a check, a block or a monitor ends or pauses. All of them are reasons beside existing states (run outcomes
stay three; ADR-009 §4 prefers a reason to a new state): **unchanged**, **approval rejected**, **approval
expired**, **superseded**, **monitor deleted**, and a run-level "stopped at" for an Approval stop. One migration
for all three PRDs if it can be done. *Seen while drafting:* ADR-014 (Draft, 2026-10-30) §5 already plans this
shape — block skip reason `stopped`, the *why* on the run's stop reason, which this PRD widens; "unchanged" drawn
from the rule kind; "waiting for your approval" as a reason on `waiting` — and admits nothing for Monitors yet.
This PRD's endings fit it as written. Separately, **monitor-level history** (skipped firings, waits, notices,
self-pause) is not a run: ADR-010 §1 requires a skip to be visible to the user and **invisible to the meter**,
so it must not be a `pipeline_runs` row. Say where it lives.

**OQ-2 — The monitor on the engine.** A Temporal Schedule per monitor (ADR-013's pattern for jobs, with `SKIP`,
`catchup_window` 365 days, explicit policies) or one long-lived workflow per monitor. Either way: per-firing
admission per ADR-010 §1 under BUG-023's per-user lock; the scheduled cap counting monitors and jobs together
(ADR-013 §4); check-now's refusal while a check runs; the operator stop (R9).

**OQ-3 — Handing over the baseline.** The check must receive the baseline's fingerprint(s) and carried values as
run inputs **when it starts** (PRD-019 D5; ADR-009 §6 forbids the workflow loading state itself). Say who finds
"the most recent completed check since the last version or input change" (R2), where each change check's
fingerprint is recorded (per block position, surviving the deletion of the stored copy), and how the
fingerprint is computed. The scrape activities already return an xxh64 hash of the page
(`job_runs.content_hash`); a change check after Clean or an extraction needs the fingerprint of *its* input.
State the accepted collision risk.

**OQ-4 — The new rule kind.** Its config shape inside Validate, the statement PRD-019 R3 requires (it reads its
block's input and one run input), its stop defaulting to *delete* while PRD-019's default stays *keep*, and how
"no baseline" (first check, or a hand-run pipeline) is represented so the check proceeds **without** weakening
PRD-019 D4 — the same question for a carried value that is absent on the first check (R4). PRD-019 OQ-4 (run
inputs beyond URL) is the dependency. ADR-014 (Draft) §4 guarantees a missing input is refused at the trigger,
never filled in silently, so the monitor must send something explicit that means "no baseline".

**OQ-5 — Computing the diff.** In a step that reads storage, never in workflow code. The summary's size bound
for webhook and email payloads and for workflow history (payload limits; pages measured up to 4.1 MiB). A text
diff of a large page is CPU-heavy (a code review on 2026-09-19 noted the v1 diff blocked the API's event loop);
say which worker runs it. The case where the baseline's copy is gone (R4).

**OQ-6 — The Approval block.** How the answer reaches the waiting run (signal or update); that a waiting run
holds no slot (ADR-009 §15a's `waiting`, and the run-counting view's "run `running` and a block `running`"
rule); how a deadline of up to 7 days composes with `pipeline_max_run_seconds` (86,400) and PRD-016 R4's
save-time composition; history size for long waits; cancel ending the wait (as ADR-009 §15d does for webhook
backoff); idempotent answers; the request's delivery reusing the webhook / email machinery with SSRF on every
attempt (and BUG-030's check-versus-connect gap).

**OQ-7 — Supersede under `SKIP` (if Q3 = supersede).** A check waiting for approval is an open workflow; under
ADR-013's `SKIP` the next firing would be dropped. Design how later checks run while an earlier one waits, how
the older approval is closed as superseded, and how the one-check-at-a-time rule (R7) still holds for checks
that are actually running.

**OQ-8 — Self-pause versus ADR-013.** ADR-013 (Accepted, immutable) sets `pause_on_failure=False` for job
schedules. Say whether monitors' self-pause is a different Schedule setting, a counter in the monitor's own
admission, or needs a new ADR; and what "failed" counts (a storage-wall failure does; a skipped firing does not).

**OQ-9 — Storage.** BUG-031's three deletes through the ledger with the refuse-on-any-failure rule; deleting a
monitor with its checks; confirm that an unchanged check deleting its own copy (instead of adopting the
baseline's, as v1's dedup did) keeps ADR-009 §8c's per-run collection safe; what an unchanged check's
`result_path` holds afterwards (PRD-019 OQ-8 asks the same for any *delete* stop).

**OQ-10 — Lane completeness of meters and admin views.** `active_recurring_jobs`, the scheduled cap's count, the
webhook meters (BUG-028, ADR-009 §15e) and the run-counting views: which read monitors already, which must be
widened, which renamed.

**OQ-11 — Workflow code changes under ADR-012.** Which of the change verdict, the carried values, the approval
wait and supersede need `patched()` in `PipelineWorkflow`, and the replay histories to add (the list in Success
criteria).

**OQ-12 — Scheduled jobs as monitors (if Q1 = a).** How a scheduled job's firings become checks of a fixed recipe
while `/jobs`, the job's runs list and result routes (today `job_runs`), the `job.completed` payload with
`diff_detected` / `diff_summary`, the generated webhook secret, the webhook as a side delivery that never fails
the job, and the storage-wall behaviour all stay as they are; and what happens to `ScheduledJobWorkflow` and
`job_runs.content_hash`. Open item 10 lists the five differences to hide.

**OQ-13 — Existing `crawls.schedule_cron` values.** Confirm none exist in production before D16's refusal, and
whether the column stays.

---

## What this hands forward

| To | What it gets | What it must not assume |
|---|---|---|
| **Architect** | OQ-1–13. Appendix A for the joint endings migration with PRD-019 OQ-2 and PRD-017 OQ-1 (also sent ahead as a scratch note on 2026-10-30). | That every new ending is a new status value — they are reasons beside the three run outcomes. That a monitor-level skip may be a run row (ADR-010 §1 forbids it). |
| **PRD-019 (conditional execution)** | **The confirmation its last success check asks for:** the cost gate **can** be built as a Validate set to stop plus a run input, with **one new rule kind** ("the content is the same as at the baseline") — which PRD-019's hand-forward already assigned to this PRD ("that a hash rule exists … is PRD-018's"), and which reads only the two sources PRD-019 R3 allows. No other layer-A change is needed for the gate. **One interaction PRD-019 does not cover:** a rule comparing with a carried value that is absent on a monitor's first check must proceed, not fail under D4 (OQ-4 here). | That PRD-018 changes Validate's fail / stop semantics, or adds an OR. |
| **PRD-017 (delivery sinks)** | Monitors deliver through sinks; reversible sinks under a monitor still undo on a **failed** check. Rejected, expired and superseded approvals are stops and undo nothing. The approval request and monitor notices are sent to **saved destinations** (D7 there). | That an approval can undo an irreversible sink. |
| **Tech Lead** | Slices 0–3; BUG-031 as a hard precondition; the dependency on PRD-019 being built and on PRD-017's saved destinations (Q9). | That slice 3 is defined before the owner answers Q1. |
| **Backlog maintainers** (not edited by this draft) | Proposed answers to record if the owner agrees: implementation-backlog **open item 10** (Q1); `phase4-backlog.md` **§3's scheduled-crawl row** ("absorbed by Monitors") becomes "refused until a site monitor exists" (D16); **PRD-016 R5's second exception** ends with slice 3 for scheduled jobs (if Q1 = a). | — |

## Appendix A — new endings and pauses, for the joint migration

Run outcomes stay **completed / failed / cancelled**. "Firm" means this draft's proposal is unlikely to move;
"tentative" means an owner question can change or remove it. Already-covered endings (PRD-016's, PRD-019's stop,
PRD-017's sink states, ADR-009 §15a's waiting between retries) are not repeated.

**A. On a check (a pipeline run) or a block**

| Ending / pause | User sees | Final? | Holds a slot? | Monthly run? | Firm? |
|---|---|---|---|---|---|
| **Unchanged** (change check stopped the run) | `completed`, "unchanged since <check>"; later blocks skipped, reason "unchanged" | final | no | yes, 1 (the fetch) | firm |
| **No baseline** (first check; first since a change; hand-run) | a verdict, not an ending: carries on, labelled "first check" / "no baseline" | — | — | — | firm; label tentative |
| **Waiting for approval** | in progress; Approval block "waiting for your approval, expires <time>" | resumable | **no** | no extra | firm (slice 2) |
| **Approved** | Approval block completed, "approved at <time>"; run carries on | not an end | as any running block | no extra | firm |
| **Rejected** | `completed`, "stopped: rejected"; later blocks skipped, reason "approval rejected"; nothing undone; no alert | final | no | no extra | firm (Q5 only on copies) |
| **Approval expired** | `completed`, "stopped: approval expired"; skipped reason "approval expired"; a notice | final | no | no extra | tentative (Q4) |
| **Superseded** | `completed`, "stopped: superseded by the check of <time>"; skipped reason "superseded" | final | no | no extra | tentative (Q3) |
| **Monitor deleted** during a check | `cancelled`, reason "monitor deleted" | final | no | already counted | tentative |

**B. Recorded on every check:** the change verdict (changed / unchanged / first check), the baseline check it
was compared with, and what changed (R4). Firm.

**C. On the monitor (no run exists, or the monitor itself changes)**

| State | User sees | Final? | Slot? | Monthly run? | Firm? |
|---|---|---|---|---|---|
| **Active, sleeping until the next check** | "next check <time>" | normal state | no | no | firm |
| **Paused** by the user | "paused since <time>"; not counted in the cap | resumable | no | no | firm |
| **Paused by an admin** | "paused by an administrator"; owner told | resumable by an admin (proposed) | no | no | tentative |
| **Paused itself** after 5 failures in a row | "paused itself after 5 failed checks — last: <block>: <error>" | resumable | no | no | tentative (Q6) |
| **Firing skipped — previous check still running** | history entry | final for that firing | no | **no** | firm |
| **Firing skipped — monthly runs used up** | notice + history entry (ADR-010 §1) | final for that firing | no | **no** | firm |
| **Firing waiting for a slot** / **gave up after 24 h** | notice + history entry | resumable, then final | no | not until it starts | firm |
| **Firing skipped — monitors stopped by the operator** | history entry | final for that firing | no | no | firm |
| **Check failed — storage full** | a `failed` check naming the remedy | final | no | as ADR-010 §1 does for jobs | firm (inherited) |
| **Deleted** | gone from lists; checks keep their history unless deleted with it | final | no | no | firm |

**Changes from the scratch note sent on 2026-10-30:** "ended — reached its end date" is **removed** (end dates
are now a non-goal); "paused by an admin" and "firing skipped — monitors stopped by the operator" are **added**
(R9); "first check" is widened to "no baseline", covering a pipeline run by hand outside a monitor (R3).

## Related

- `docs/project/phase4-prd/PRD-016-workflows-pipelines.md`: OQ-10 (both halves of change detection go to
  Monitors), OQ-4 (metering; the waiver that expires here), OQ-1 (the halt-early obligation), R3 (cancellation,
  storage wall), R4 (budgets compose), R5 (the job lane's second named exception)
- `docs/project/phase4-prd/PRD-019-conditional-execution.md`: R1 (stop, keep / delete), R2 (run inputs), R3 (what
  a rule may read), R4 (how a stop looks), R5 (*the run breaks*), What this hands forward
- `docs/project/phase4-prd/PRD-017-delivery-sinks.md`: R3 (rollback, only on failure), R5 (saved destinations),
  D12 (failed runs keep copies), What this hands forward
- `docs/adr/ADR-009-workflow-engine-temporal.md`: §4 (block state, reasons beside states), §6 (pinned versions),
  §8 / 8c (metering; per-run collection and shared objects), §13 (a crawl is not a block), §15a (waiting holds no
  slot), §15d (cancel ends a wait), §15e (lane-blind meters)
- `docs/adr/ADR-010-crawl-admission-and-scheduled-quota.md` §1 (quota-blocked scheduled runs, per meter)
- `docs/adr/ADR-013-schedule-policy.md` (`SKIP`, the cap of 20, catch-up)
- `docs/adr/ADR-012-workflow-code-versioning.md` (Draft): changing workflow code under replay
- `docs/project/phase4-implementation-backlog.md`: D.2 (change detection dropped from the job lane), open
  item 10 (`/jobs` over pipelines)
- `docs/project/phase4-backlog.md` §2 (this PRD's row), §3 (the scheduled-crawl row)
- `docs/project/workflows-scoping.md` §B (Monitors), the scheduled-crawl gap
- `docs/project/open-bugs.md`: **BUG-031** (nothing frees a pipeline run's outputs — slice 0), BUG-019 (the
  cleanup that never ran), BUG-030 (check versus connect), BUG-028 (lane-blind webhook meters)
- Live code: `api/app/core/diff.py` (today's comparisons), `api/app/workflows/job.py` and
  `activities/job_runs.py` (`content_hash` recorded, unused), `api/app/pipelines/catalog.py` (Validate's rules,
  bindable fields), `api/app/workflows/pipeline.py`, `api/app/core/schedules.py` and
  `api/app/workflows/scheduled_job.py` (the job-schedule pattern a monitor follows)
