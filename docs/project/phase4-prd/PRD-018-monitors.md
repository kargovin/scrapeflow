# PRD-018 — ScrapeFlow Workflows: Monitors

**Priority:** P1 for the change gate (it is the launch requirement); P2 for the rest.
**Layer:** B (Monitors). A monitor runs a saved pipeline (layer A, PRD-016) again and again on a schedule. It
adds one layer-A rule kind and one layer-A block, both additive, and it delivers through PRD-017's sinks.
**Source:** `phase4-backlog.md` §2 (this PRD's row: both halves of change detection, the job lane's change
detection dropped at D.2, implementation-backlog open item 10, the scheduled-crawl gap); PRD-016 OQ-10 (why
change detection belongs here), OQ-4 (metering), R5 (the job lane's second named exception); PRD-019 and
PRD-017 "What this hands forward"; ADR-009 §4, §8, §15a; ADR-010 §1; ADR-013.
**Status:** **Draft — owner's section-by-section review complete (2026-11-03 → 2026-11-05, 16 rounds).** Written
2026-10-30 by the PM persona. Every decision carries the round that agreed it. Architect pass drafted 2026-11-18 as
**ADR-016** (Draft, not reviewed), answering OQ-1–14; ADR-014 and ADR-015 Accepted 2026-11-18. Not Accepted.
**Last updated:** 2026-11-05 (review round 16: D1, Q9 — owner review complete)

> **Built last, on top of three PRDs.** This PRD consumes PRD-019 (a Validate set to *stop*, run inputs of type
> number and text, failure alerts) and PRD-017 (sinks, saved destinations, rollback), both Draft and in their
> Architect pass as this is written. Where this PRD leans on a decision those two have not yet had confirmed by
> an ADR, it says so. Nothing here re-opens a decision in PRD-016, PRD-019, PRD-017, ADR-009, ADR-010 or ADR-013.

> **Review round 1 (owner, 2026-11-03): Problem.** Agreed as written — repeat a pipeline, check for a change,
> wait for a person. One correction in gap 3: durable waiting is not unused (scheduled-run quota waits, webhook
> retries and the per-host limiter all sleep on durable timers); what is new is waiting for a **person**.

> **Review round 2 (owner, 2026-11-04): Background.** Agreed, with five additions from the deleted result consumer,
> `diff.py` and ADR-014: the old lane already compared against the last *completed* run (D2 keeps it); the page
> diff is counts only and the extraction diff top-level only; `diff.py` reads an error as "no change", which R4
> must not inherit; ADR-014 §4's typed inputs and missing-input refusal, which collide with D7's first check; the
> two hand-over status labels brought up to date.

> **Review round 3 (owner, 2026-11-04): Goals and Non-goals.** Goal 6 no longer promises the storage limit (5 failures
> *in a row* never trips on a site that fails every other hour; storage is R8's and BUG-031's); Goal 1 names the
> self-pause; the SPA gets a JSON editor for monitors, as pipelines have — no form builder; re-delivery's
> non-goal names its exception (a page that changed back). **Declined:** a "reported changed on every check"
> warning for byte-noisy pages — not worth another status in the UI. "Any of" (OR) moves from *not here* to *not
> yet*: PRD-019 handed the question to this PRD, and the old workaround (two Validate blocks) gives AND, not OR.

> **Review round 4 (owner, 2026-11-04): User stories.** Agreed as written. Checking them found that D2/R2 made an
> unchanged check the baseline, which breaks stories 3 and 4: an unchanged check stops before the LLM and deletes
> its copy, so the next change had no extraction to carry `last_price` from and no copy to diff against. **An
> unchanged check never becomes the baseline** (D2, R2). Parked for R5: an unanswered approval is re-asked at the
> very next check, not "the next change" (story 6).

> **Review round 5 (owner, 2026-11-04): R1.** Agreed, with two changes: a run input may be filled by the monitor
> (a carried value or the baseline's fingerprint) instead of a fixed value — the save-time check refuses only an
> input with neither; and a check waiting for an approval is not in flight, for Check now or the schedule's
> overlap rule (the requirement behind OQ-7). Owner's steer for R3: "check" means one firing of a monitor; the gate
> gets its own name.

> **Review round 6 (owner, 2026-11-04): R2.** Two decisions. **A monitor's version and inputs are fixed for its
> life** (D15 rewritten): a newer version or a different input is a new monitor; only name, schedule and notice
> destination are editable, and none touches the baseline. **The baseline is the most recent completed check
> that did not stop at the first change gate or before it** (D2): this covers the unchanged case (round 4) and a
> guard Validate stopping a junk page before the comparison, which would otherwise make the next normal page read
> "changed". Q4 (expired approvals) is answered with R5. Parked for R4: "dropped since last time" means since
> the last baseline check, not the last alert.

> **Review round 7 (owner, 2026-11-04): R3.** The gate is renamed the **change gate**; "check" means only one firing
> of a monitor. The baseline's fingerprint is attached by the monitor, **not a run input** (a hand-run would
> otherwise be refused for a hash nobody can type). D5's default delete applies only at the **first** change gate.
> D4 keeps the gate required before the LLM with **no opt-out (Q7 answered)**: R3 now states the three kinds of
> page — on noise inside the content the required gate always passes, but costs one comparison, and only fuzzy
> matching could save the LLM call. Clean is not an LLM call. "On a version move" dropped from D4 (round 6).

> **Review round 8 (owner, 2026-11-04): R4.** The page diff gives the **actual changed lines**, shown in the SPA
> as a code-review diff (red removed, green added, context, collapsed stretches); the extraction diff goes to
> **any depth**, path by path; a diff that cannot be computed says so and never reads as "no change"; **a carried
> value has a start value**, sent whenever the baseline has nothing to carry — so every check sends a real input
> and ADR-014 §4 is untouched (OQ-4 trimmed); "since last time" means since the baseline check, with an example;
> the "copy gone" case names its one remaining cause.

> **Review round 9 (owner, 2026-11-04): R5.** **While an approval waits, later checks are handed the waiting check's
> fingerprint beside the baseline** (D10): the same content stops as unchanged with no LLM call and no new
> request; any other content closes the waiting approval as superseded, even if that check then stops at a
> Validate — nobody approves a page that is gone; diffs and carried values still compare with the baseline. The
> approval request is **not an announcement** (D9): rollback after an approval still undoes earlier deliveries,
> and the request's wording says so. Deleting a monitor cancels a waiting approval; pausing does not. Owner
> questions answered as recommended: **Q2** approval in slice 2, **Q3** supersede, **Q4** expiry `completed`
> and not the baseline (re-asked once per deadline), **Q5** reject always keeps copies (it is the baseline),
> **Q10** 24 h default / 7 d maximum, **Q11** no one-click links.

> **Review round 10 (owner, 2026-11-05): R6.** Notices are **sent once per situation** (a month of skipped hourly
> firings is one message; the history keeps every firing); waiting for a slot is history-only. **Approval messages
> go to the Approval block's destination**, not the monitor's (it works outside monitors, and both would
> duplicate). Notices are **recorded first, then delivered**, like failure alerts (new OQ-14); the destination is
> a webhook until PRD-017's email sink ships. Cancelled checks neither count towards self-pause nor reset it.
> **Q6:** in, 5 in a row, monitors only.

> **Review round 11 (owner, 2026-11-05): R7.** Agreed, with D11 as proposed (no separate monitor quota). **The
> projected-monthly-runs warning at save moves to *not yet*** — monthly runs are a cap, not a bill, and the
> "monthly runs used up" notice already tells the user; adding it later breaks nothing. The quota table matches
> round 10's notices, and its storage row says a storage-full check costs one monthly run, as for jobs.

> **Review round 12 (owner, 2026-11-05): R8.** D14 agreed (BUG-031 first, slice 0). Its deletes follow the job lane's
> permanent delete: cancel a running or waiting check first; a delete that cannot remove an object is refused and
> retried, nothing left uncounted. The storage table matches rounds 7 and 9. **Superseded checks are never the
> baseline** (R2, D2): only approved or rejected content is. **Deleting a check removes it from comparison** —
> the next check compares with the newest remaining baseline, so a change may be reported again; the baseline is
> a fresh lookup at every firing, and "unchanged since …" is only a link for the user. **Q12:** no retention in
> slice 1; when added, it never deletes a current baseline or a waiting check.

> **Review round 13 (owner, 2026-11-05): R9.** The SPA gains the monitor JSON editor (round 3). **The operator's stop
> is `PIPELINES_ENABLED`**, checked in each firing's start step — today it guards only the API routes, which a
> monitor's firing never passes through; no separate switch. **Admin pause moves to *not yet*** (the schedule
> floor and per-site limiter already prevent hammering; admins have no job-schedule pause either): admins see
> monitors read-only, and Appendix A loses "paused by an admin". The monitor page refreshes; it is not live.

> **Review round 14 (owner, 2026-11-05): R10.** **Q1 = (e): jobs keep no change detection; it is a monitor
> feature.** Scheduled jobs stay a fetch on a timer; the payload's diff fields stay, always `null`; all of `/jobs`
> over pipelines (d) is the long-term direction, not planned; (a) — scheduled jobs secretly run as monitors — is
> rejected (a job mode hiding five differences, protecting no production user), and (c) too. **Slice 3 and
> OQ-12 are removed.** The job lane's unread `content_hash` / diff columns go to a cleanup item. **Q8: refuse
> `schedule_cron` on crawls; scheduled crawls are not planned** — a crawl's page set changes on every run, so there
> is nothing stable to compare. The refusal can ship on its own.

> **Review round 15 (owner, 2026-11-05): R11.** Three "change-check" leftovers renamed. R11 names the two existing
> things that do change: the scheduled cap is shared with monitors, and `PIPELINES_ENABLED` also stops monitor
> firings. The replay obligation is ADR-012's rule — every new workflow behaviour gets a history — with a minimum
> list brought up to rounds 7–13, including the monitor's new firing workflow.

> **Review round 16 (owner, 2026-11-05): decisions table — review complete.** **D1** agreed: a monitor is a pinned
> pipeline version and one set of inputs, fixed for its life, plus a schedule; a check is an ordinary pipeline run.
> **Q9:** Monitors do not wait for PRD-017 — notices and approval requests are SPA-only until saved destinations
> exist; the hard dependency is PRD-019. D2, D3, D5, D6 and D7 marked with the rounds that agreed them. Every
> decision D1–D18 and every owner question Q1–Q12 is now answered.

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
| 5 | **Implementation-backlog open item 10**: does `/jobs` become a front end over pipelines? Decide it, or put it to the owner. | **R10**, owner question **Q1** — answered: not now; (d) is the direction |
| 6 | **The scheduled-crawl gap** (`crawls.schedule_cron` is accepted and never runs). | **R10** |
| 7 | **Storage**: failed runs keep their copies (PRD-017 D12) and nothing frees a pipeline run's outputs (BUG-031). | **R8** |
| 8 | **Quota, the scheduled-jobs cap, overlap** for repeated runs. | **R7** |
| 9 | **Confirm in writing** that the cost gate can be built on PRD-019 without a further layer-A change (PRD-019's last success check). | **R3**, What this hands forward |

## Decisions this draft proposes, for the owner's review

All agreed in the owner's review (2026-11-03 → 2026-11-05); the Status column names the round. "Rec." marks the PM's original recommendation.

| # | Proposed decision | Where | The alternative rejected | Status |
|---|---|---|---|---|
| D1 | **A monitor is a saved pipeline, pinned to one version and one set of run inputs — fixed for the monitor's life (D15) — plus a schedule.** Each firing is a **check**, and a check is an ordinary pipeline run: same run page, same outcomes, same metering. No new kind of run. | R1 | A monitor as a separate run type with its own statuses and pages (a second copy of the run machinery) | ✅ owner, round 16 |
| D2 | **"The previous check" (the baseline) is the most recent earlier check of the same monitor that ended `completed` and did not stop at the first change gate or before it**. So Validate stops after the first change gate and rejected approvals count; **an unchanged check never becomes the baseline** (its content is the baseline's, and it holds no outputs of its own), nor does a check a Validate stopped before the change gate (it never reached the comparison). **Failed, cancelled, expired and superseded checks never become the baseline.** | R2 | The most recent check of any outcome (a failed delivery would then make the next check "unchanged", and the change would never be delivered) | ✅ owner, rounds 4, 6, 12 |
| D3 | **The cost gate is a Validate set to stop (PRD-019) with one new rule kind: "the content is the same as at the baseline"** (exact, byte for byte). The monitor attaches the baseline's fingerprint to the check when it starts — **not a run input the user declares or types**; a run started by hand has none. The rule reads only its block's input and that handed-over fingerprint, so it stays inside PRD-019's rule on what a rule may read (D5 there). | R3 | A dedicated Gate block; a gate built into the monitor rather than placed in the chain | ✅ owner, round 7 |
| D4 | **A monitor whose pipeline contains an LLM block cannot be armed unless a change gate comes before that LLM block.** Refused at save, naming the block. A pipeline with no LLM block gets a warning, not a refusal. | R3 | Making the gate optional everywhere (the cost delta PRD-016 accepted would return on every scheduled run) | ✅ owner, round 7 — no opt-out (Q7) |
| D5 | **An unchanged check ends `completed`, labelled "unchanged since <check>", and by default deletes what it stored** (PRD-019's *delete* choice, defaulted on for this rule kind only). *Keep* stays available. **The default applies only to a stop at the first change gate**: a check that stops at a later one has passed the first, becomes the baseline (R2), and keeps its copies for the next diff. | R3 | PRD-019's default *keep* (a monitor would pile up one identical copy per check against the storage limit) | ✅ owner, round 7 |
| D6 | **Every changed check records what changed**: the **actual lines** added and removed on the page when the change gate reads a page (shown in the SPA like a code-review diff), and a **nested, path-by-path** diff of the extraction when the check and its baseline both have one. A diff that cannot be computed says so; it never reports "no change". The first check says "first check" and has no diff. What changed travels in the run's messages. | R4 | A diff only on request; a diff only in the SPA | ✅ owner, round 8 |
| D7 | **A monitor can carry a value from the baseline to the next check as a run input** (e.g. `last_price` from the extraction's `/price`), so "only tell me if the price dropped" is an ordinary PRD-019 `compare` rule. **Each carried value has a start value**, set on the monitor and refused at save without one: it is sent whenever the baseline has no value to carry (the first check, or a baseline whose extraction lacked the field). Every check therefore sends a real value, and ADR-014 §4's missing-input refusal is untouched. | R4 | Field-level comparison as a second built-in monitor feature with its own rule language | ✅ owner, round 8 |
| D8 | **Human approval is a new block, *Approval*, usable in any pipeline**, monitor or not. The run waits, holding no concurrency slot. *Approve* → the run continues. *Reject* → the run ends `completed`, stopped at the Approval block, and **nothing is undone**. No answer by the deadline → `completed`, labelled "expired", **not the baseline**, so the next check asks again (Q4). On reject, ScrapeFlow's copies are **always kept** (Q5): the rejected check is the baseline, and its copies are the next diff's base. | R5 | Approval as a monitor-only setting outside the chain; reject or expiry as `failed` (a "no" or "nobody looked" is a normal answer, and `failed` would fire alerts and rollback) | ✅ owner, round 9 (Q3–Q5) |
| D9 | **An approval never undoes a delivery.** An Approval block may sit after a sink, but rejecting does not remove what that sink delivered, and the approval request says what has already gone out. **The approval request is not an announcement** (PRD-017 D13): it goes to the owner, not to a receiver that acts on the data, so a run that fails after approval still rolls back what it delivered before the Approval block. | R5 | Rejection triggering rollback (it would make a reject behave like a failure, which PRD-017 D4 and D13 keep separate); the request counting as an announcement | ✅ owner, round 9 |
| D10 | **At most one approval waits per monitor, and checks keep running while it waits.** The monitor hands each later check the baseline **and** the waiting check's fingerprint. A later check whose change gate finds the waiting check's content stops as unchanged and leaves the approval alone (no LLM call, no new request). A later check that finds **any other content** closes the waiting approval as **superseded**, whatever it then does — nobody approves a page that is gone. Diffs, carried values and the request's "what changed" always compare with the baseline. | R5, R7 | No new checks while an approval waits (the monitor goes blind, then asks about a stale change); superseding only when the later check also reaches Approval (a later Validate stop would leave a stale approval waiting) | ✅ owner, round 9 (Q3) |
| D11 | **Monitors use every existing limit, unchanged**: the per-user cap of 20 active schedules counts monitors and scheduled jobs together (paused ones do not count); each check is one monthly run, unchanged checks included; quota-blocked checks behave exactly as scheduled jobs do (ADR-010 §1); a check still running when the next is due makes that next one skip (ADR-013); after an outage, one late check. | R7 | A separate monitor quota | ✅ owner, round 11 |
| D12 | **A monitor pauses itself after 5 failed checks in a row**, and tells the owner once. Resuming re-arms it and resets the count. Cancelled checks and skipped firings neither count nor reset. Monitors only; job schedules keep ADR-013's *never pause*. | R6 | Never pausing, as job schedules do (ADR-013 §2.3); a time span instead of a count (a count tracks cost: each failure is one run) | ✅ owner, round 10 (Q6) |
| D13 | **Monitor-level notices** (paused itself, checks skipped, gave up waiting for a slot — sent once per situation, every firing kept in the history) **always appear in the SPA and can also be sent to one saved destination (webhook until PRD-017's email sink ships)** chosen on the monitor (PRD-017's saved webhook or email destination). **Run failures** keep PRD-019's *the run breaks* choice on the pipeline's own message-sending blocks. | R6 | Moving run-failure alerts onto the monitor too (the same URL typed twice, which PRD-019 rejected) Approval messages go to the Approval block's destination, not here. | ✅ owner, round 10 |
| D14 | **BUG-031 is fixed before Monitors ship**: a user can permanently delete a pipeline run, a pipeline with its runs, and a monitor with its checks, and the storage they held is freed. | R8 | Shipping monitors first (a monitor that keeps failing piles up charged copies with no way out — PRD-017's warning) | ✅ owner, round 12 |
| D15 | **A monitor's pipeline version and run inputs are fixed for its life.** Watching a newer version or a different input is a **new monitor**; the old one is paused or deleted. Only the name, the schedule and the notice destination can be edited, and none of them touches the baseline. | R1, R2 | Editing version or inputs in place with a baseline reset (one monitor would hold several comparison histories, and "the monitor is the identity" would stop being true); comparing across versions (a changed extraction schema would report every field as changed) | ✅ owner, round 6 |
| D16 | **Scheduled crawls are not planned.** A crawl's set of pages changes on every run (new links, rotating "related" links), so there is no stable thing to compare; and a crawl is not a block (ADR-009 §13), so a monitor cannot run one. **`POST /crawls` refuses `schedule_cron`** with a message, so the API stops accepting a setting that does nothing — a small fix that can ship on its own, before slice 1. | R10 | Keeping the silent accept; building a crawl-shaped monitor | ✅ owner, round 14 (Q8) |
| D17 | **Jobs keep no change detection; it is a monitor feature** (D.2 stays dropped). Scheduled jobs stay what they are today — fetch on a timer, no comparison; `diff_detected` / `diff_summary` stay in the job payload, documented as always `null`. To watch a page for changes, the user makes a monitor. Open item 10's long-term direction is **all of `/jobs` over pipelines (d)**, someday, not planned. | R10 | (a) scheduled jobs secretly run as monitors behind an unchanged job API (a job mode hiding five differences, protecting no production user); (b) retiring job schedules (not needed now); (c) porting D.2 into the job lane (two ways to do one thing) | ✅ owner, round 14 (Q1 = e) |
| D18 | **Two slices after slice 0.** Slice 1: monitors with the change gate, the diff, carried values, alerts and self-pause, behind BUG-031's fix — **B ships here**, with its gate. Slice 2: the Approval block. (No slice 3: jobs keep no change detection, D17.) | Slices | One release with approval included | ✅ owner, round 9 — approval in slice 2 (Q2); slice 3 removed, round 14 |

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
pipelines without a change gate on purpose, because nothing in layer A repeats, so the cost could not arise
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
(`workflows-scoping.md` §B). Durable timers are already in use — a quota-blocked scheduled run's wait, webhook
retries, the per-host limiter — but nothing waits for a person yet.

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

Three details of that code this PRD inherits (read from the deleted result consumer, `65fc93f^`, and
`api/app/core/diff.py`):

- **The previous run was already the most recent *completed* one** (`_get_previous_completed_run`): failed runs
  were never compared against. D2 keeps that rule; it does not invent it.
- **The page diff reports counts, not lines**: `{"similarity", "added_lines", "removed_lines"}`. The extraction
  diff reports `{"field": {"from", "to"}}` for **top-level** fields only, so a change inside a list returns the
  whole list twice. Which of these R4's "what changed" means is R4's to say.
- **`diff.py` answers "no change" when it cannot compare**: a MinIO read error or an extraction that is not JSON
  returns `detected: false`. On the job lane that only lost a summary. For a monitor it loses the change itself,
  silently, so **R4 must not inherit it**: a check that cannot compare must say so, never report "unchanged"
  (the posture ADR-014 §3 takes for a rule that cannot be checked).

### What PRD-019 hands over (Draft, owner-reviewed; its Architect pass is ADR-014, owner review complete 2026-11-03, acceptance held until this PRD is reviewed)

- **A Validate block can *stop* a run**: the run ends `completed`, later blocks are skipped, the run is
  labelled "stopped at <Validate>", and nothing is sent.
- **On a stop, *keep* or *delete*** what ScrapeFlow stored for the run. *Delete* frees the storage at once.
- **Rule values can come from run inputs**, of type number and text, so a rule can compare against a value
  handed over when the run starts. That is how a monitor supplies "what it was last time". ADR-014 §4 fixes the
  mechanics: an input is declared `url`, `number` or `text`; only `compare`'s value and `contains`'s text can be
  bound to one; and **a missing or wrongly typed input is refused at the trigger (`422`), never filled in**.
  A carried value therefore always has a start value, sent when the baseline has nothing to carry (R4, D7).
- **What a rule may read is fixed for good**: its block's input and the run's own inputs. No clock, no
  randomness, no lookups, no database. "Compare with the previous run" is allowed only as a value **handed
  over**, never looked up.
- **"The run breaks"**: a message-sending block can alert on any failure, wherever in the chain it happened.
- **What PRD-019 says this PRD must not assume:** that layer A defines "the previous run", or that a hash rule
  exists. Both are this PRD's.

### What PRD-017 hands over (Draft, owner-reviewed; its Architect pass is ADR-014 + ADR-015, ADR-015 not yet reviewed)

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
   leave it. It keeps running across redeploys and restarts until the user pauses or deletes it, or it pauses
   itself (Goal 6).
2. **An unchanged page costs no LLM call, keeps no new copy and sends no message.** It still costs the one
   monthly run its fetch used.
3. **"Changed since when" has one written answer** the user can see on every check.
4. **A changed check says what changed**, on the run page and in the messages it sends.
5. A user can make a run **wait for their approval** before it continues, and the wait costs them no capacity.
6. **An unattended monitor that breaks does not stay broken quietly**: the owner hears about failures, and a
   monitor that keeps failing pauses itself.
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
  page". The first change gate is exact. A user with a noisy page places the change gate after a Clean block, or
  compares extracted fields (R4), which costs the LLM call. Both are stated in the docs.
- **A wait inside a run** — "wait 2 hours, then scrape again to confirm". ADR-009 §15a left room for a run to
  sleep between blocks; this PRD does not use it. The long sleep is *between* checks, and that is the schedule's.
- **A monitor end date** ("watch until 31 December"). Pausing or deleting covers it for now.
- **Time-zone-aware schedules.** Monitors use the same cron rules as scheduled jobs (UTC, 5-minute floor).
- **Several approvers, teams, or an approval delegated to someone else.** Only the monitor's owner approves.
- **One-click approve links in emails or webhooks** that work without signing in (owner question **Q11**).
- **A form builder in the SPA to create or edit a monitor.** A monitor is written as JSON, like a pipeline: the
  SPA gets a JSON editor for it, as pipelines have (`PipelineEditor.tsx`, C.12), and shows monitors with the
  actions a person needs while one runs (R9).
- **Per-monitor retention** ("delete stored results older than 30 days") — Q12. When added, it must never
  delete a monitor's current baseline or a check waiting for approval (R8).
- **MCP tools for monitors.**
- **An admin pausing a user's monitor** (and an "admin-paused" state only an admin can lift). The schedule floor
  and the per-site limiter already stop a monitor hammering a site; admins see monitors read-only.
- **Projected monthly runs at save** (for one monitor, or across all of a user's schedules). Until then, the
  "monthly runs used up" notice (R6) tells the user when it happens; monthly runs are a cap, not a bill.
- **"Any of" (OR) between rules** ("price changed **or** stock changed"). PRD-019 left this for PRD-018 to ask
  for; this PRD does not ask now. Two Validate blocks in a row give AND, not OR. For "price changed or stock
  changed", put the *content same as baseline* check (R3) after an extraction that holds only those fields —
  every check then pays for its LLM call. If it is added later, it is PRD-019's rule vocabulary that grows,
  under its R3.

**B — Not here:**

- **Watching a whole site** (a scheduled crawl as a monitor) — **not planned** (owner, round 14): a crawl's set
  of pages changes on every run, so there is nothing stable to compare. D16 refuses `crawls.schedule_cron`.
- **Change detection on jobs**, and running scheduled jobs as monitors. Jobs stay as they are (D17); all of
  `/jobs` over pipelines is the long-term direction, not planned.
- **Branching** ("if the price dropped, email me; otherwise write a row"). PRD-019 deferred it; nothing here
  needs it.
- **Re-delivering a failed check from its stored outputs.** PRD-017 named this a non-goal. R2's baseline rule
  covers the common case instead: the next check finds the change again and delivers it — unless the page
  changed back in the meantime. A monitor reports the page as it is at each check, not every value it passed
  through.
- **Fixing BUG-019** (the nightly job-run cleanup has never succeeded in production). It is infra-only and
  unrelated to pipelines. It is named so nobody counts it as a retention rule for monitors: it does not cover
  pipeline runs, and it has never run.

**C — Never:**

- **A change gate that looks anything up mid-run** — the previous run's row, another site, the clock.
  PRD-019 D5 forbids it permanently. The baseline is **handed over** when the check starts (R2).
- **Bypassing a quota for a monitor.** Every check is metered like any run.

## User stories

**As a price watcher**, I want a monitor on a product page every hour that extracts the price only when the
page has changed, so I pay for an LLM call twice a week, not 168 times.

**As a data engineer**, I want *scrape → clean → change gate → extract → my Postgres table*, every 6 hours,
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

**As a user of scheduled jobs**, I want to know whether my jobs will get change detection back — the answer
(D17): no; a scheduled job is a fetch on a timer, and a monitor is the way to watch a page for changes.

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
  edit to the pipeline can change what they produce. Editing the pipeline does not change any monitor.
- **A monitor's version and inputs are fixed for its life (D15).** To watch a newer version or a different
  input, the user creates a new monitor and pauses or deletes the old one. The SPA shows, on the monitor, when
  a newer version of its pipeline exists. Consequences, stated so nobody is surprised:
  - while both are active they count **2** against the scheduled cap (R7); at the cap, pause or delete the old
    one first (paused monitors do not count);
  - the new monitor has a **new id**, so a receiver keyed on the monitor id sees a new one;
  - the new monitor's first check is a first check (one LLM call, delivered as "first check");
  - the old monitor keeps its own history (its checks stay as pipeline runs unless deleted with it).
- **Actions:** create, read, list, edit (**name, schedule, notice destination** only), **pause**,
  **resume**, **check now**, delete.
  - **Pause** stops future checks. It does **not** cancel a check in flight; cancelling one check is the run's
    own Cancel (PRD-016 R3). The same separation jobs already draw between cancelling a run and retiring a
    schedule.
  - **Check now** starts one check outside the schedule. It is an ordinary check: compared with the baseline,
    metered, refused if a check is already in flight (R7).
  - **A check waiting for an approval is not in flight** — not for Check now, not for the schedule's overlap
    rule (R5, R7). Otherwise one unanswered approval would skip every firing until someone answers, which D10
    rejects. How the engine does this is Architect OQ-7.
  - **Delete** stops the monitor and cancels a check in flight **or waiting for approval** (at the next block
    boundary, nothing undone). Pause does not: a waiting approval can still be answered while the monitor is
    paused.
    The checks stay as pipeline runs with their history, like runs of a deleted pipeline, unless the user
    chooses **delete with its checks**, which frees their storage (R8).
- **Edits take effect from the next check.** A check in flight finishes on what it started with (PRD-016's
  pinning rule).
- **Deleting a pipeline that any monitor uses, active or paused, is refused**, naming the monitors — the same
  rule PRD-017 proposes for a destination in use.
- **Save-time checks** for a monitor, refused with a message naming the problem: every run input of the pinned
  version has either a fixed value of the right type or a rule saying the monitor fills it from the baseline (a
  carried value, R4) — an input with neither is refused, naming it; what a
  first check sends for a monitor-filled input is R4's; the schedule is a valid cron no more often than the scheduled-job
  floor (5 minutes); D4's change-gate rule; creating or resuming past the scheduled cap (R7).
- **Survives restarts.** A redeploy of any ScrapeFlow component, or a Temporal restart, never loses a monitor,
  never runs a check twice, and after an outage runs **one** late check, not one per missed firing (ADR-013).

### R2 — "The previous check": the baseline

*(Obligation 2.)*

**The rule (D2):** a check is compared with **the most recent earlier check of the same monitor that ended
`completed` and did not stop at the first change gate or before it.**

```
              outcome                         becomes the baseline?
check 1       completed (first check)         yes
check 2       completed, unchanged (stop)     NO   → check 1 stays the baseline (same content,
                                                    and check 1 still has the outputs)
check 2b      completed, a Validate before    NO   → a junk page ("Service unavailable") stopped by a
              the change gate stopped it           guard never becomes what the next check compares with
check 3       failed at Webhook (receiver     NO   → check 4 is compared with check 1,
              down) — change detected               sees the change, and delivers it
check 4       completed, changed, sent        yes
check 5       cancelled by the user           NO
check 6       completed, approval rejected    yes  → the user has seen this content; don't ask again
check 7       completed, approval expired     NO (Q4) → the next check that finds a change asks again
check 8       completed, superseded           NO → nobody accepted it; the baseline stays the last
                                                    approved or rejected content
```

Why each line:

- **Failed checks are never the baseline.** A failed check may have detected a change and not delivered it.
  If it became the baseline, the next check would say "unchanged" and stop, and the change would be lost
  without a sound. Skipping failed checks makes the next check find the change again and deliver it — the
  common case of PRD-017's "re-deliver from stored outputs" non-goal, at the cost of a fresh fetch.
- **Cancelled checks are never the baseline.** The user stopped them; they say nothing about what was seen.
- **A check that stopped at the first change gate, or before it, is never the baseline.** Two cases:
  - **Unchanged** (stopped *at* the change gate). It stops before the LLM and, by default (D5), deletes its
    copy, so it holds no extraction for a carried value (R4) and no copy for the diff. Its content is its
    baseline's, so skipping it changes no fingerprint comparison, and the label reads "unchanged since <the
    baseline check>".
  - **Stopped by a Validate before the change gate** (a guard such as "at least 200 characters" set to stop).
    It never reached the comparison. If it became the baseline, the next normal page would be compared with the
    junk one and reported "changed" — a false alert after every hiccup on the site.
- **Validate stops after the first change gate, and rejected approvals, are the baseline.** A stop is a success
  (PRD-019): the check saw a new page and decided ("price not under £100", "rejected") about that content.
  *First* change gate because a pipeline may have two (R3): a check that passed the first and stopped at a
  second (the extraction is the same) still saw a new page, and must become the baseline, or every later check
  would compare with the old page, see "changed", and pay for the LLM again.
- **A pipeline with no change gate** (allowed with a warning when it has no LLM block, D4): every completed
  check is a baseline candidate.
- **Expired approvals are not the baseline** (Q4): nobody looked at the change, so the next check that finds the
  page still different from the last baseline asks again — once per deadline, since the checks in between
  compare with the waiting check (R5).
- **Superseded checks are not the baseline** either: nobody accepted them. Otherwise a superseding check that
  then fails would leave the superseded one as the newest candidate, and the next diff and carried values would
  be measured from content nobody approved. So under an Approval block, **only approved or rejected content
  becomes the baseline**.
- **While an approval waits**, later checks are also handed the waiting check's fingerprint, for the change
  gate only (R5, D10). The baseline is unchanged by it.
- **A monitor's version and inputs never change** (D15). A different URL, or a different extraction schema, is a
  different thing being watched, so it is a different monitor with its own first check.
- **Checks from a deleted monitor, or of another monitor on the same pipeline, are never a baseline.** The
  monitor is the identity.

**What the user sees:** every check after the first names its baseline — "compared with the check of
14 Nov 06:00" — with a link. If there is no baseline (the monitor's first check), the check
says so and proceeds as **changed**.

**The baseline is handed over when the check starts**, as run inputs, never looked up mid-run (PRD-019 D5). A
check therefore compares with the baseline as it stood when the check began; if an earlier check is still
finishing (it cannot be, under R7's overlap rule, except for an approval wait — R5), the later check does not
see its result.

### R3 — The change gate (the cost gate)

*(Obligation 1, first half. The launch requirement.)*

- **A change gate is a Validate block set to stop, with the new rule "the content is the same as at the
  baseline"** (D3) — written so that "same" means **stop**. The user places it in the chain like any Validate:

  ```
  scrape ──► clean ──► [change gate] ──► extract($) ──► webhook

  page same as baseline     ■ stop — completed, "unchanged since 14 Nov 06:00"
                              extract and webhook skipped: no LLM bill, no message
  page differs              ✔ carry on — "changed"
  first check               ✔ carry on — "first check"
  ```

- **What "same" means:** the content the change gate receives is **byte-for-byte identical** to what the
  change gate received in the baseline check. Placed after Scrape, it compares the raw page; after Clean, the
  cleaned page; after an extraction, the extraction. Placing it after Clean is recommended for pages with
  rotating ads or timestamps, and the docs say so.
- **Where it sits decides what it catches.** Clean is not an LLM call (`llm-worker/worker/cleaner.py` — it drops
  `<head>`, scripts, forms, nav, `<aside>`, iframes and the page's top-level header and footer, then converts to
  markdown), so placing the change gate after Clean costs nothing:

  | Page | Its noise | Change gate after Clean | Cost |
  |---|---|---|---|
  | static | none | catches "same" | LLM only on a real change |
  | noise outside the content | footer timestamp, ads, form tokens | catches "same" (Clean removed the noise) | LLM only on a real change |
  | noise inside the content | "23 people viewing", a shuffled "you may also like" | always "differs" | **LLM on every check** |

  For the third kind, a second change gate after the extraction stops the messages, but not the LLM bill:
  only the LLM can tell the price from the counter. That is fuzzy matching, a *not yet* non-goal. D4 still
  requires the first change gate (a Validate stores nothing and calls no LLM, so on such a page it costs one
  comparison); the docs state the three cases.
- **The rule reads only its block's input and the baseline's fingerprint**, which the monitor attaches to the
  check when it starts. The fingerprint is **not a run input**: the user never declares or types it, and the
  pipeline's inputs stay what the user wrote. That is inside PRD-019's permanent rule on what a rule may read,
  and is the statement PRD-019 R3 asks every new rule kind to make.
- **Outside a monitor** (a pipeline containing a change gate run by hand), there is no baseline: the rule
  always says "differs" and the run carries on, labelled "no baseline (not run by a monitor)". The same
  pipeline is usable both ways. A carried input (R4) is an ordinary
  input on a hand-run, typed like any other.
- **An unchanged check ends `completed`**, labelled **"unchanged since <baseline check>"** in the run page, the
  run list and the monitor's history — PRD-019's "stopped at" label, worded for this rule. Later blocks are
  skipped with the reason "stopped: unchanged".
- **On "unchanged", ScrapeFlow's copies are deleted by default** (D5), freeing their storage at once — what the
  job lane's dedup did. The user may choose *keep* on the change gate (PRD-019 D10's choice) if they want a
  copy per check. The check's result then reads "unchanged — see <baseline check>" rather than nothing.
- **Costs:** an unchanged check costs **one monthly run** (the fetch happened — PRD-016 OQ-4) and, with
  *delete*, **no storage**. It makes no LLM call and sends no message. This is the job lane's dedup cost
  exactly.
- **Required where it matters (D4):** a monitor whose pinned version contains an LLM block is refused at save,
  unless a change gate comes **before** the first LLM block. A version with no LLM
  block saves with a warning that every check will store a copy and run every block.
- **Several change gates in one pipeline** are allowed (e.g. one on the cleaned page before the LLM, one on
  the extraction after it). Each compares with its own position in the baseline check. **D5's default *delete* applies only at the first
  one.** A check that passes the first and stops at a later one saw a new page, so it becomes the baseline (R2)
  and keeps its copies — deleting them would leave the next change with nothing to diff against.
- **Never retried**: "same" and "differs" are final answers (PRD-019 R1). Trouble reading the content from
  storage is retried as for any Validate.

### R4 — What changed: the reporting diff, and carried values

*(Obligation 1, second half.)*

- **Every changed check records what changed** (D6):
  - **the page** — when a change gate reads a page: the **actual lines** added and removed against the
    baseline's page (counts alone do not say what changed);
  - **the extraction** — when the check and its baseline both produced an extraction: which fields were added,
    removed or changed, with old and new values, **at any depth**, each named by its path in the notation
    carried values use (`/products/0/price: 10 → 8`). Lists are compared item by item, by position: an item
    inserted at the top makes the items below it read as changed, within the size limit.

  The run page shows both. The page diff is shown **as a code-review diff**: removed lines in red, added lines
  in green, a few unchanged lines around each change for context, long unchanged stretches collapsed. A first
  check shows "first check — nothing to compare". An unchanged check shows nothing changed.
- **What changed travels in messages.** The Webhook block's payload on a monitor's check gains a **`change`**
  field — changed / first check, the baseline check's id, and the summary — and the monitor's id. Additive:
  receivers that ignore unknown fields are unaffected, and payloads of runs outside a monitor do not change.
  PRD-017's Email block includes the same summary in its body. The approval request (R5) shows it.
- **The summary is bounded.** A page that changed everywhere must not produce a message of unbounded size; the
  summary is cut to a stated limit and says it was cut, with a link to the full comparison on the run page.
- **If the baseline's stored copy is gone** (the user deleted it, or chose *delete* on a Validate stop after the
  change gate), the check still knows it changed — the fingerprint survives — but says "changed; the previous
  copy is no longer stored, so no diff" instead of a diff.
- **If the diff cannot be computed** (a storage read fails, an extraction is not JSON), the check says
  "changed; the comparison could not be computed". It never reports "no change": the change gate's fingerprint
  decides *changed*, and the diff only describes it. `diff.py` today returns "no change" on these errors; that is
  not inherited.
- **Carried values (D7).** A monitor may declare, for a run input of the pinned version, "fill this from the
  baseline's extraction at path *P*, starting at *S*". Example: `last_price` ← the baseline's `/price`, start
  `999999`. The pipeline
  uses it in an ordinary PRD-019 rule:

  ```
  scrape ──► clean ──► [change gate] ──► extract($) ──► Validate(/price < {last_price}, stop) ──► webhook

  page unchanged                     ■ unchanged — no LLM call
  page changed, price 120 → 120      ■ stopped at Validate — "price not lower" (reviews changed, not price)
  page changed, price 120 → 99       ✔ webhook: "price dropped", change summary attached
  first check (last_price = start)   ✔ 120 < 999999 → webhook, "first check"
  ```

  - The value comes from the **baseline**, so "since last time" means "since the baseline check" (R2) — not
    since the last alert. A failed check never moves it, and a Validate stop does:

    ```
    check A   price 120   → alert sent          (the user last heard 120)
    check B   price 125   → "not lower" stop    → the baseline; last_price = 125
    check C   price 122   → 122 < 125 → "price dropped"   (still above the 120 last heard)
    ```
  - **Each carried value has a start value** (number or text, the input's type), refused at save without one.
    It is sent whenever the baseline has nothing to carry: the first check, or a baseline whose extraction
    lacked the field (the check says which). Every check sends a real value, so ADR-014 §4's refusal of a
    missing input is untouched, and a missing `/price` in the *new* extraction still fails loudly (PRD-019 D4).
    The start value also lets the user shape the first check: `start: 120` alerts only if the price is already
    lower.
- **Today's diff code is the starting point, not a target.** `diff.py` compares two stored outputs, but returns
  only line counts for a page, compares top-level fields only, and reads an error as "no change"; this PRD needs
  all three to change. Its output shape is not a contract.

### R5 — Human approval (slice 2)

*(Obligation 3, the approval half.)*

- **A new block, Approval** (D8). It holds no content and passes its input on unchanged, like Validate. When a
  run reaches it, the run **waits for the pipeline's owner** to approve or reject. Usable in any pipeline; a
  monitor is the main user.

  ```
  scrape ─► clean ─► [change gate] ─► extract($) ─► bucket ─► [Approval] ─► webhook to customers

  unchanged    ■ unchanged — no approval asked
  changed      ⏸ waiting for your approval (expires 15 Nov 06:00)
                  ├─ Approve  ✔ → webhook sent → completed
                  ├─ Reject   ■ completed, "stopped: rejected"; webhook skipped; the bucket file STAYS
                  └─ no answer by the deadline
                              ■ completed, "stopped: approval expired"; webhook skipped; bucket file stays
  ```

- **Asking.** On reaching the block, the request appears in the SPA (on the run, on the monitor, and in a list
  of everything waiting for the user) and is sent to one destination named on the block — a saved webhook or
  email destination (PRD-017); **until saved destinations exist, the request is SPA-only** (Q9). The request says: which pipeline and monitor, **what changed** against the
  baseline (R4), what has **already been delivered** — "a reject keeps it; it is removed only if this run later
  fails" (D9) — and the deadline. It links to the SPA; approving needs the user signed in. **No one-click
  links** (Q11): anyone holding the message could answer, and some mail filters open every link to scan it.
  Delivery of the request follows the
  webhook rules: retried over the usual window, checked against private addresses on every attempt, and an
  undeliverable request is shown as undelivered — the approval still waits, and the SPA still shows it.
- **Answering.** Approve or reject from the SPA or the API, by the run's owner only; anyone else gets 404.
  Answering twice, or after the deadline, is refused with the current state; it never flips a decision. The
  run records who answered and when.
- **Approve** → the block completes and the run carries on.
- **Reject** → the run ends **`completed`**, stopped at the Approval block, later blocks skipped with the
  reason "approval rejected". **Nothing is undone** (D9): a stop is a success, and PRD-017 undoes only on
  failure. **No failure alert fires.** ScrapeFlow's own copies are **always kept** (Q5): the rejected check
  becomes the baseline (R2), so its copies are what the next change is diffed against.
- **Expiry** → `completed`, labelled **"approval expired"**, plus a monitor notice (R6). **Not the baseline**
  (R2, Q4): the next check compares with the old baseline, finds the change, and asks again.

  ```
  Fri 18:00  ⏸ asked (expires Sat 18:00)      hourly checks in between: same as the waiting check, no LLM
  Sat 18:00  expired → Sat 19:00 asked again (1 LLM call)
  Mon 09:00  the owner approves the latest request
  ```
- **The deadline** is set on the block: default **24 hours**, maximum **7 days**, both operator-configurable
  (Q10). It must be stated in the request and on the run.
- **The request is not an announcement** (D9; PRD-017 D13). It goes to the owner, not to a receiver that acts
  on the data, so it does not make earlier deliveries final: if the run fails after an approval, rollback still
  undoes what was delivered before the Approval block.
- **Waiting costs nothing.** A run waiting for approval **holds no concurrency slot** (ADR-009 §15a: a run
  parked on a timer is not active) and no monthly run beyond the fetch it already used.
- **Cancel** while waiting ends the run `cancelled` at once — there is no block executing to wait for.
- **An approval cannot undo anything** (D9, PRD-017's hand-forward). An Approval block **may** come after a
  sink — "save to my bucket always, publish only after I approve" is the example above — but the request says
  what already went out. An Approval block may not be the first block (there is nothing to show) and may not be
  the last (there is nothing to hold back); both refused at save.
- **One approval waiting per monitor (D10).** While a monitor's check waits for approval, the monitor keeps
  checking on schedule, and hands each later check the baseline **and** the waiting check's fingerprint:
  - a later check whose change gate finds **the waiting check's content** stops as unchanged ("unchanged since
    <waiting check>") and leaves the approval alone — no LLM call, no new request;
  - a later check that finds **any other content** closes the waiting approval as **superseded** ("stopped:
    superseded by the check of <time>"), **whatever that later check then does** — even if it stops at a
    Validate before reaching an Approval block. Nobody approves a page that is no longer there;
  - diffs, carried values and the request's "what changed" always compare with the **baseline** (the last
    accepted state), never with the waiting check.

  ```
  baseline r0: price 10
  r1  price 8   → changed → LLM → ⏸ waiting ("10 → 8")
  r2  price 8   → same as r1 → unchanged, no LLM; r1 still waits
  r3  price 11  → differs from r1 → r1 superseded → LLM → Validate "11 < 10?" no → stop
                  (r1 is closed, so approving it can no longer publish 8)
  r4  price 7   → differs → LLM → ⏸ waiting ("10 → 7")   — compared with the baseline, not with r1
  ```

  Chosen over no new checks while an approval waits (Q3): a monitor that goes blind until someone answers, and
  then asks about a stale change, fails the watch-it-for-me promise. Outside a monitor, each run's approval
  stands alone.
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

  | Notice | When it is sent |
  |---|---|
  | **paused itself** | after the 5th failed check in a row (D12), naming the last failure |
  | **checks skipped — monthly runs used up** | **once**, when the first firing is skipped (ADR-010 §1), naming the date the limit resets |
  | **gave up waiting for a free slot** | when a firing's ADR-010 §1 wait reaches its 24-hour cap |
  | **checks skipped — previous check still in progress** | only if the user opts in (a slow page would send it every time) — **opt-in moved to *not yet*** (ADR-016 review round 1): history only for now |

  **A notice is sent once, when a situation starts, not on every firing.** An hourly monitor on the default plan
  (500 monthly runs) runs out in about three weeks; one "checks skipped until 1 Dec" message, not ~216. Waiting
  for a free slot is history-only; only giving up is sent. **The history records every firing** — each skip and
  each wait — whatever was sent.

  **Approval messages are not monitor notices.** The request, the "expires in 2 hours" reminder, "expired" and
  "superseded" all go to the **Approval block's** destination (R5), as one thread: an Approval block works outside
  a monitor too, and sending them to both places would duplicate them.

  Every notice appears in the monitor's history in the SPA. If the monitor names a **notice destination** (one
  saved destination, PRD-017 — **webhook until PRD-017's email sink ships, then webhook or email**), notices are
  also sent there. **Notices are recorded first, then delivered**, as failure alerts are (ADR-014 §8): signed,
  retried over the usual window, checked against private addresses on every attempt. A notice that cannot be
  delivered is shown as undelivered in the history and never affects the monitor. Notices cost no quota.
- **Self-pause (D12).** After **5** failed checks in a row, the monitor pauses itself and sends a "paused
  itself" notice once. A completed check (including a stop) resets the count; skipped firings and cancelled
  checks neither count nor reset (a cancel is the user's decision and says nothing about the site). Resuming re-arms the monitor and resets the count. Why: a failed check keeps its stored copies and
  costs a monthly run (PRD-017 D12, PRD-016 OQ-4), so a monitor on a dead site would otherwise spend the user's
  monthly runs and storage indefinitely. ADR-013 chose *never pause* for job schedules because "a paused
  Schedule nobody un-pauses is a silent stop"; this rule is not silent — it notifies, and the SPA shows it.
  **Monitors only** (Q6): job schedules keep ADR-013's *never pause*, because they have no notice channel to
  report a pause. A count, not a time span, because each failure is one run: a 5-minute monitor pauses after
  25 minutes of an outage (announced; the user resumes), a daily one after 5 days, and both have spent 5 runs.

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
- **Quota-blocked firings behave exactly as for scheduled jobs** (ADR-010 §1), checked per firing:

  | Meter reached | The firing | Visible as |
  |---|---|---|
  | monthly runs | **skipped**; next firing runs normally | one notice when skipping starts (R6); every skipped firing in the history; no run, no unit charged |
  | concurrency | **waits** for a slot, holding none; gives up after 24 hours | history only while waiting; a notice only if it gives up; a check once it starts |
  | storage | **fails now**, naming the remedy | a failed check (counts towards self-pause; alerts via *the run breaks*); costs one monthly run, as for jobs — self-pause stops it after 5 |

- **Overlap (ADR-013's `SKIP`).** A firing that comes while the monitor's previous check is still running is
  **skipped**, recorded in the history, and costs nothing. A monitor never has two checks running at once, so
  checks finish in order and each compares with a settled baseline. **The one exception is an approval wait**
  (R5, D10): a check waiting for approval does not stop later checks.
- **Check now** follows the same rule: refused while a check is running, with the running check named.
- **Concurrency.** A running check holds one slot like any pipeline run. Waiting for approval, waiting between
  delivery retries (PRD-017), sleeping until the next firing, and waiting for a quota slot hold none.

### R8 — Storage

- **BUG-031 is fixed before Monitors ship (D14)**, with three user actions, each freeing the storage it removes
  through the same accounting every other delete uses, as the job lane's permanent delete does: a running or
  waiting check is **cancelled first**; if any stored object cannot be removed, the delete is **refused and can be
  retried** — what was freed stays freed, and nothing is left uncounted:
  - **delete one pipeline run** — its stored outputs are freed and it disappears from lists;
  - **permanently delete a pipeline with its runs**;
  - **delete a monitor with its checks**.

  Deleting in ScrapeFlow never reaches into the user's own destinations (PRD-017).
- **What a monitor stores, by outcome:**

  | Check | ScrapeFlow's copies |
  |---|---|
  | unchanged at the **first** change gate | **deleted** by default (D5); kept if the change gate says *keep* |
  | unchanged at a **later** change gate | kept (it is the new baseline, R2, and the diff needs it next time) |
  | changed, completed | kept (it is the new baseline, and the diff needs it next time) |
  | rejected | **always kept** (Q5): it is the baseline |
  | stopped by another Validate | the user's *keep* / *delete* choice on that Validate (PRD-019 D10), default *keep* |
  | expired, superseded | kept |
  | failed | **kept** (PRD-017 D12), charged — hence self-pause (R6) |

- **Stored files are never shared between checks.** An unchanged check deletes its own copy; its run page shows
  "unchanged since <baseline check>" with a link — history for the user, **not** how the next baseline is found
  (that is a fresh lookup at every firing, R2). It never takes over the baseline's stored file as its own, as the
  job lane's dedup did. Each stored object belongs to exactly one check, so deleting one check never breaks
  another's result. (ADR-009 §8c
  warns that its per-run collection rule becomes unsafe the day objects are shared between runs; this keeps
  that day from arriving.)
- **At the storage limit** the user sees what they see on any pipeline run — the storage wall, naming the
  remedy — and, with BUG-031 fixed, the remedy works.
- **Deleting a check removes it from comparison too.** The next check compares with the newest remaining
  baseline (R2), or is a first check if none is left, so a change already delivered may be reported again:

  ```
  r0  price 10   baseline
  r4  price 7    sent "10 → 7" → the baseline; the user deletes r4
  r5  price 7    baseline = r0 → "10 → 7" sent again
  ```

  A check that was compared with a since-deleted check says "unchanged since a check that has since been
  deleted", with no link.
- **No automatic retention in slice 1** (Q12). The nightly cleanup does not cover pipeline runs and has never
  run in production (BUG-019). **When retention is added, it never deletes a monitor's current baseline or a check
  waiting for approval**: on a stable page the baseline is old, and deleting it by age would make the next check
  a false "first check" every retention period.

### R9 — Visibility

- **API:** monitors (CRUD, pause, resume, check now), each monitor's history (checks, skipped and waiting
  firings, notices), pending approvals, approve / reject. Checks are also readable through the existing pipeline
  run routes.
- **SPA:**
  - a **JSON editor** to create a monitor and edit its name, schedule and notice destination, as pipelines have
    (`PipelineEditor.tsx`);
  - a **Monitors list**: name, pipeline and version, schedule, state (active / paused / paused itself), next
    check, last check's outcome, approvals waiting;
  - a **monitor page**: the history as a scannable list — *unchanged · unchanged · **changed → sent** · failed ·
    skipped (quota)* — with links into each check's run page, the baseline each check used, what changed,
    notices, and Pause / Resume / Check now / Delete. It **refreshes** when opened and every minute while open —
    not a live channel (a monitor checks at most every 5 minutes); a check's run page stays live;
  - **approvals waiting**: one list across monitors and pipelines, with Approve / Reject and what changed;
  - the run page gains the change verdict, the baseline link and the diff (R4, shown as a code-review diff), and
    the Approval block's state
    (R5), live, as it already shows other block states.
- **Admins** see every user's monitors, histories and pending approvals wherever they already see pipelines —
  **read-only**, plus the deletes admins already have. An admin **cannot pause** a monitor (*not yet*: the
  5-minute floor and the per-site limiter, E.8, already stop a monitor hammering a site, and admins have no pause
  for job schedules either), and **cannot approve or reject** on a user's behalf.
- **The operator's stop is `PIPELINES_ENABLED`.** Today it is checked only at the pipeline API routes (create,
  edit, trigger — `routers/pipelines.py:79`), and a monitor's firing does not pass through them. So the firing's
  start step checks it too: with `PIPELINES_ENABLED=false`, **every monitor firing is skipped**, recorded in the
  history, and not run late. No separate switch.
- **No meter counts one lane and reports for all** (ADR-009 §15e; the BUG-028 class). Admin statistics that
  count recurring work (today `active_recurring_jobs`) either include monitors or are named for the lane they
  count. Monitor checks are pipeline runs, so the run-counting views already include them; this PRD's success
  checks confirm it rather than assume it.

### R10 — Jobs and crawls

*(Obligations 4, 5 and 6.)*

**What jobs get back (D17).**

- **Nothing. Change detection is a monitor feature** (Q1 = e). D.2 stays dropped; the owner's principle at D.2 —
  one way to do a thing — holds. Scheduled jobs stay what they are today: a fetch on a timer, every firing
  scraping, calling the LLM if configured, storing its copy and sending its webhook.
- **`diff_detected` and `diff_summary` stay in the `job.completed` payload, documented as always `null`.**
  Removing them changes the webhook contract; that is done once, if and when jobs are rebuilt (below).
- **The docs say so**, and say that a monitor is the way to watch a page for changes.
- **PRD-016 R5's second exception is answered**: change detection returned *as monitors*, not on the job lane.
- **Open item 10** ("should `/jobs` become a front end over pipelines?"): **not now**. The long-term direction is
  **(d), all of `/jobs` over pipelines**, someday, not planned. Recorded options:

  | Option | Decision |
  |---|---|
  | **(e) Jobs stay as they are; change detection is a monitor feature** | ✅ chosen |
  | (a) Scheduled jobs secretly run as monitors behind an unchanged job API | rejected: a job mode inside the pipeline workflow hiding five user-visible differences (open item 10), protecting no production user — none run before Phase 4 completes |
  | (b) Retire job schedules (jobs become one-off only) | not taken now |
  | (c) Port D.2 back into the job lane | rejected: two change-detection paths |
  | (d) All of `/jobs` rebuilt over pipelines | the long-term direction, not planned |

- **Leftovers on the job lane, for cleanup** (not this PRD's build; listed in *What this hands forward*): the
  job lane still writes `job_runs.content_hash`, and keeps `job_runs.diff_detected` / `diff_summary`, all unread.
  The **Scrape activity's** hash stays — it is shared with pipelines, and a change gate right after Scrape can
  use it (OQ-3). `api/app/core/diff.py` stays — monitors reuse or rewrite it (R4).

**Scheduled crawls (D16).**

- `crawls.schedule_cron` is accepted and stored today (`routers/crawls.py:148`) and **nothing ever runs it**.
  `phase4-backlog.md` §3 says the gap is "absorbed by Monitors". **It is not: scheduled crawls are not planned**
  (Q8, owner round 14). A crawl's set of pages changes on every run, so there is no stable thing to compare:

  ```
  crawl Mon: seed → 120 links → 120 pages
  crawl Tue: seed → 134 links (new articles, rotating "related" links) → 134 pages
             "what changed?" → 14 pages have no previous version; the set itself is not comparable
  ```

  And a crawl is not a pipeline block (ADR-009 §13), so a monitor cannot run one.
- **`POST /crawls` refuses `schedule_cron`** with a message saying scheduled crawls are not supported, so the API
  stops accepting a setting that silently does nothing. **It can ship on its own, before slice 1** — a small fix
  to a dormant setting. Existing stored values are ignored as today; whether any exist in production, and
  whether the column can then be dropped, is OQ-13.

### R11 — Compatibility

- **Additive for pipelines.** A pipeline saved before this PRD runs exactly as before. The change-gate rule
  kind and the Approval block are new; no existing block changes.
- **Runs in flight at a release finish on the behaviour they started with** (ADR-012). Monitors add new paths
  to the pipeline workflow (the change verdict, the approval wait, supersede) and a new workflow (the monitor's
  firing), so **every new workflow behaviour gets a recorded history** — the replay test protects only what its
  corpus covers.
- **Jobs, batches, crawls and schedules are untouched by slices 1 and 2**, except: D16's refusal of
  `schedule_cron` on new crawls; the scheduled cap now counting monitors too (R7), so a user with 20 job
  schedules cannot add a monitor, and the reverse; and `PIPELINES_ENABLED=false` also stopping monitor firings
  (R9). Jobs keep no change detection (D17).
- **Admin pages and meters** keep working; new statistics follow R9's lane rule.

---

## Slices

| Slice | Contains | Ships when | B "ships"? |
|---|---|---|---|
| **0 — precondition** | BUG-031's fix (R8) | before slice 1 | — |
| **1 — monitors** | R1, R2, R3 (the change gate), R4 (diff, carried values), R6, R7, R8, R9 (without approvals), D16's crawl refusal | after PRD-019 is built (the change gate is a Validate stop — the hard dependency). **Does not wait for PRD-017** (Q9): until its saved destinations exist, notices are SPA-only, and the notice destination is added when they land | **yes** — with the cost gate, as PRD-016 requires |
| **2 — approval** | R5, and R9's approvals list | after slice 1; approval requests SPA-only until PRD-017's saved destinations exist (Q9) | — |

Slice 1 is the thin end-to-end path: a pipeline repeated on a schedule that stays quiet and cheap when nothing
changed and says what changed when something did. Approval is the showcase of the engine but not what makes
monitors safe to run (owner question **Q2**).

---

## Success criteria

**Monitors (R1)**
- [ ] A monitor on *scrape → clean → change gate → extract → webhook* every 5 minutes runs on schedule, and its
      checks appear in its history and in the pipeline's run list, marked as the monitor's.
- [ ] Pausing stops future checks without cancelling one in flight; resuming re-arms; deleting cancels the
      check in flight at the next block boundary.
- [ ] Editing the pipeline does not change the monitor. A monitor's version and inputs cannot be edited (refused,
      naming the field); its name, schedule and notice destination can, and leave its baseline unchanged.
- [ ] Deleting a pipeline used by a monitor (active or paused) is refused, naming the monitor.
- [ ] A restart of the workflow worker, the API and Temporal mid-schedule loses no monitor and runs no check
      twice; a 5-minute Temporal outage produces **one** late check.
- [ ] Another user's monitor returns 404 on every route.

**The baseline (R2)**
- [ ] With a check that failed at the Webhook block after detecting a change, the next check compares with the
      last **completed** check, finds the change, and delivers it.
- [ ] Cancelled checks are never a baseline; a rejected check is; each check names its baseline.
- [ ] Changing the URL input makes the next check a first check.

**The change gate (R3)**
- [ ] On an unchanged page: the check ends `completed`, labelled "unchanged since <check>"; **no LLM call** is
      made (provider call count unchanged), **no message** is sent, and with the default the check's stored
      copies are **deleted** and storage usage is unchanged by the check.
- [ ] On a changed page the check runs every block and is labelled "changed".
- [ ] The same pipeline run by hand (no monitor) carries on past the change gate, labelled "no baseline".
- [ ] A monitor whose version has an LLM block and no change gate before it is refused at save, naming the
      block; with the check after the LLM block, also refused; a version with no LLM block saves with a warning.
- [ ] Measured: over 24 hourly checks of a page that changes twice after the first check, exactly 3 LLM calls
      (the first check and the two changes), 3 kept copies, 3 webhook messages and **24** monthly runs.
- [ ] An unchanged check's result reads "unchanged — see <baseline check>", and deleting the baseline check
      never breaks a later check's result (no shared objects).
- [ ] The change-gate step's answer is never retried and survives a worker kill between the decision and the
      next block (replays to the same decision).

**What changed (R4)**
- [ ] A changed check shows the page's actual added and removed lines (in the SPA as a code-review diff) and,
      with an extraction, a path-by-path diff at any depth naming old and new values; the
      webhook payload carries `change` and the monitor id, and a receiver that ignores unknown fields still
      accepts it.
- [ ] A page changed throughout produces a summary cut to the stated limit, marked as cut.
- [ ] With the baseline's copy deleted, a changed check says "changed; the previous copy is no longer stored".
- [ ] With a storage read failing during the diff, a changed check says "changed; the comparison could not be
      computed" — never "no change".
- [ ] The *price dropped* pipeline (carried `last_price`): unchanged page → no LLM call; changed page with the
      same price → stopped at the Validate; lower price → webhook sent; first check → sends the start value.
- [ ] A monitor with a carried value and no start value is refused at save, naming it.
- [ ] A carried value never moves on a failed check.
- [ ] A missing `/price` in the **new** extraction still fails the check loudly (PRD-019 D4 intact).

**Approval (R5, slice 2)**
- [ ] A changed check reaching an Approval block waits, shown as waiting with its deadline; the request is sent
      to the block's destination naming what changed and what was already delivered.
- [ ] While it waits, the user can start runs up to their full concurrency limit.
- [ ] Approve → the run carries on. Reject → `completed`, "stopped: rejected", later blocks skipped, a bucket
      file delivered before the Approval block **stays**, no failure alert.
- [ ] No answer → "approval expired" at the deadline, a notice is sent, and the next check that still differs
      from the baseline asks again; checks between the request and the deadline make no LLM call.
- [ ] A later check with the waiting check's content stops as unchanged and leaves the approval alone; a later
      check with any other content closes it as superseded, including one that then stops at a Validate.
- [ ] A rejected check's copies are kept, and the next changed check diffs against them.
- [ ] A run that fails after an approval rolls back what it delivered before the Approval block (the request is
      not an announcement).
- [ ] Deleting a monitor cancels a check waiting for approval; pausing it does not.
- [ ] Approving twice, after the deadline, or as another user is refused and changes nothing.
- [ ] Cancel while waiting ends the run `cancelled` within seconds.
- [ ] An Approval block first or last is refused at save; a pipeline whose blocks plus approval deadline cannot
      finish is refused, naming the numbers.

**Unattended running (R6)**
- [ ] A monitor on a dead site pauses itself after the 5th failed check in a row and sends one notice; a
      completed check in between resets the count; a job schedule never pauses itself.
- [ ] Each notice in R6's table appears in the monitor's history, and is sent to the notice destination when
      one is set — once per situation: a month of skipped hourly firings sends one notice and records every
      firing.
- [ ] Approval messages go to the Approval block's destination only, never also to the notice destination.
- [ ] An undeliverable notice shows as undelivered and changes nothing about the monitor.
- [ ] Cancelled checks neither count towards self-pause nor reset it.
- [ ] A monitor with no failure route saves with a warning.

**Schedule and quotas (R7)**
- [ ] With 20 active schedules (jobs and monitors mixed), a 21st monitor — created or resumed — is refused with
      `quota_type: "scheduled_jobs"`; a paused monitor does not count.
- [ ] With monthly runs used up, a firing is skipped with a notice and **no run and no unit**; at concurrency
      full, the firing waits holding no slot; at the storage limit, it fails naming the remedy.
- [ ] A check longer than the interval makes the next firing skip, recorded; two checks of one monitor never
      run at once; Check now is refused while one runs.

**Storage (R8)**
- [ ] BUG-031: deleting one pipeline run, a pipeline with its runs, and a monitor with its checks each lowers
      storage usage by exactly the bytes removed; a running or waiting check is cancelled first; an object that
      cannot be removed makes the delete refuse with a retry, and a retry completes it with nothing uncounted.
- [ ] Deleting a monitor's current baseline makes the next check compare with the newest remaining baseline (or
      be a first check); a check compared with a deleted one shows "since deleted" with no link.
- [ ] A superseded check never becomes the baseline, even when the check that superseded it fails.

**Visibility (R9)**
- [ ] The SPA's monitor page reads as a scannable history with the change verdicts, and refreshes while open.
- [ ] A monitor is created from the SPA's JSON editor, and its name, schedule and notice destination edited there.
- [ ] An admin can see every monitor and cannot pause it, approve or reject.
- [ ] Admin statistics for recurring work count monitors, or are named for the lane they count.
- [ ] The run-counting views count monitor checks (checked, not assumed).
- [ ] With `PIPELINES_ENABLED=false`, monitor firings are skipped and recorded, and none runs late when it is
      switched back on.

**Jobs and crawls (R10)**
- [ ] `POST /crawls` with `schedule_cron` is refused with a message.
- [ ] Jobs, scheduled or not, behave exactly as before; `diff_detected` / `diff_summary` stay `null` and are
      documented as always `null`.

**Compatibility and replay (R11)**
- [ ] Every pipeline saved before release runs unchanged; existing suites and the replay corpus pass unchanged.
- [ ] Every new workflow behaviour gets a recorded history (ADR-012), replaying cleanly in the test that gates the
      api build. At least: an unchanged stop, a later-gate stop, a changed check with a diff; the monitor's
      firing workflow skipped (monthly runs), waited for a slot, and skipped while pipelines are switched off;
      and in slice 2, approve, reject, expiry, supersede, and supersede by a Validate stop.

---

## Open questions for the owner

Product calls this draft could not make alone. All answered in the owner's review; each row names the round.

| # | Question | Rec. | Why it matters |
|---|---|---|---|
| **Q1** | **Open item 10: what happens to jobs?** (a) scheduled jobs run as monitors behind the unchanged job API; (b) retire job schedules in favour of monitors; (c) port D.2 into the job lane; (d) all of `/jobs` over pipelines. | **(a)** — ✅ **owner, round 14: (e)** — jobs stay as they are; change detection is a monitor feature; (d) is the long-term direction; (a) and (c) rejected | Decides whether and when jobs get change detection back (R10) |
| **Q2** | **Approval in slice 1 or slice 2?** | slice 2 — ✅ **owner, round 9** | Slice 1 is safe and useful without it; approval is the biggest new mechanism |
| **Q3** | **While a monitor's approval waits, do later checks run?** Supersede (newest change waits) vs no new checks until answered. | supersede — ✅ **owner, round 9** (D10 as revised) | Decides whether a monitor goes blind while waiting, and adds Appendix A's "superseded" ending |
| **Q4** | **An expired approval: `completed` + "expired" (and not the baseline, so it asks again), or `failed` (so *the run breaks* alerts fire)?** | `completed`, not the baseline — ✅ **owner, round 9** | Expiry is "nobody looked", not "something broke"; R6's notice already tells the owner |
| **Q5** | **On reject: always keep ScrapeFlow's copies, or offer keep / delete like a Validate stop?** | always keep — ✅ **owner, round 9** | Simplest; a rejected change is the one a user may want to inspect, and it is the baseline, so its copies are the next diff's base |
| **Q6** | **Self-pause after repeated failures: in? After how many?** And should job schedules get it too (a change to ADR-013's decision, so a new ADR)? | in, 5, monitors only — ✅ **owner, round 10** | Failed checks keep charged copies (PRD-017 D12) |
| **Q7** | **May a user opt out of D4's required change gate** ("run every block every time, I know")? | no opt-out in slice 1 — ✅ **owner, round 7: no opt-out** | Opt-out is additive later; taking it away later breaks saved monitors |
| **Q8** | **`schedule_cron` on crawls: refuse from slice 1, keep accepting silently, or build a site monitor now?** | refuse — ✅ **owner, round 14**: refuse; scheduled crawls not planned | Today it is a setting that does nothing |
| **Q9** | **Should Monitors wait for PRD-017's saved destinations**, or ship slice 1 with SPA-only notices if PRD-017 is later? | ship SPA-only if needed — ✅ **owner, round 16**: don't wait; notices and approval requests are SPA-only until saved destinations exist | Decides slice 1's dependency on PRD-017 |
| **Q10** | **Approval deadline: default 24 hours, maximum 7 days?** | yes — ✅ **owner, round 9** | Product numbers, operator-configurable |
| **Q11** | **One-click approve / reject links** in the email or webhook, working without signing in? | no — the message links to the SPA — ✅ **owner, round 9** | A forwarded email would let anyone approve |
| **Q12** | **Per-monitor retention** ("delete checks older than N days") in slice 1? | no; BUG-031's deletes are the remedy — ✅ **owner, round 12** | Additive later; must never delete a current baseline (R8) |

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
(ADR-013 §4); check-now's refusal while a check runs; `PIPELINES_ENABLED` checked in the firing's start step (R9).

**OQ-3 — Handing over the baseline.** The check must receive the baseline's carried values as run inputs, and
its fingerprint(s) — and, while an approval waits, the waiting check's (D10) — attached to the check but
**not** as a declared run input (R3), **when it starts** (PRD-019 D5;
ADR-009 §6 forbids the workflow loading state itself). Say who finds "the most recent completed check that did
not stop at the first change gate or before it" (R2), where each change gate's
fingerprint is recorded (per block position, surviving the deletion of the stored copy), and how the
fingerprint is computed. The scrape activities already return an xxh64 hash of the page
(`job_runs.content_hash`); a change gate after Clean or an extraction needs the fingerprint of *its* input.
State the accepted collision risk.

**OQ-4 — The new rule kind.** Its config shape inside Validate, the statement PRD-019 R3 requires (it reads its
block's input and the fingerprint the monitor attaches, not a run input), its stop defaulting to *delete* — at
the first change gate only (D5) — while PRD-019's default stays *keep*, and how
"no fingerprint" (first check, or a hand-run pipeline) is represented so the gate says "differs" **without**
weakening PRD-019 D4. A carried value needs no such case: it always has a start value (R4). PRD-019 OQ-4 (run
inputs beyond URL) is the dependency, answered by ADR-014 (Draft) §4.

**OQ-5 — Computing the diff.** In a step that reads storage, never in workflow code. The summary's size bound
for webhook and email payloads and for workflow history (payload limits; pages measured up to 4.1 MiB). A text
diff of a large page is CPU-heavy (a code review on 2026-09-19 noted the v1 diff blocked the API's event loop);
say which worker runs it. The case where the baseline's copy is gone, and the case where the diff cannot be
computed (R4). What the run page's full diff reads from (both stored copies, computed on demand, or stored with
the check).

**OQ-6 — The Approval block.** How the answer reaches the waiting run (signal or update); that a waiting run
holds no slot (ADR-009 §15a's `waiting`, and the run-counting view's "run `running` and a block `running`"
rule); how a deadline of up to 7 days composes with `pipeline_max_run_seconds` (86,400) and PRD-016 R4's
save-time composition; history size for long waits; cancel ending the wait (as ADR-009 §15d does for webhook
backoff); idempotent answers; the request's delivery reusing the webhook / email machinery with SSRF on every
attempt (and BUG-030's check-versus-connect gap); the request must **not** set ADR-014 §7's `announced` flag (D9),
so rollback after an approval still undoes earlier deliveries.

**OQ-7 — Supersede under `SKIP` (Q3 = supersede, owner round 9).** A check waiting for approval is an open
workflow; under ADR-013's `SKIP` the next firing would be dropped. Design how later checks run while an earlier
one waits (R1: a waiting check is not in flight), how the waiting check's fingerprint reaches them beside the
baseline (D10), how the older approval is closed as superseded — by any later check with different content, not
only one that reaches Approval — and how the one-check-at-a-time rule (R7) still holds for checks
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

**OQ-12 — *(removed in review round 14: Q1 = e, jobs keep no change detection; nothing to translate).***

**OQ-13 — Existing `crawls.schedule_cron` values.** Confirm none exist in production before D16's refusal, and
whether the column can then be dropped (two releases, unmap then drop).

**OQ-14 — Recording and delivering monitor notices.** Where a notice is recorded and how it is delivered, as
failure alerts are (ADR-014 §8 widens `webhook_deliveries` for pipeline runs; a notice has a monitor and often no
run); how "once per situation" is tracked (when a situation starts and ends); and, when PRD-017's email sink
ships, what sends an email notice or failure alert — ADR-015 designs email only as a pipeline block on the
delivery worker, with no record-then-deliver path.

---

## What this hands forward

| To | What it gets | What it must not assume |
|---|---|---|
| **Architect** | OQ-1–14 (OQ-12 removed). Appendix A for the joint endings migration with PRD-019 OQ-2 and PRD-017 OQ-1 (also sent ahead as a scratch note on 2026-10-30). | That every new ending is a new status value — they are reasons beside the three run outcomes. That a monitor-level skip may be a run row (ADR-010 §1 forbids it). |
| **PRD-019 (conditional execution)** | **The confirmation its last success check asks for:** the cost gate **can** be built as a Validate set to stop plus a fingerprint the monitor attaches, with **one new rule kind** ("the content is the same as at the baseline") — which PRD-019's hand-forward already assigned to this PRD ("that a hash rule exists … is PRD-018's"), and which reads only the two sources PRD-019 R3 allows. No other layer-A change is needed for the gate. A carried value is an ordinary typed input that always has a value (its start value on a first check), so PRD-019 D4 and ADR-014 §4 apply unchanged. | That PRD-018 changes Validate's fail / stop semantics, or adds an OR. |
| **PRD-017 (delivery sinks)** | Monitors deliver through sinks; reversible sinks under a monitor still undo on a **failed** check. Rejected, expired and superseded approvals are stops and undo nothing. The approval request and monitor notices are sent to **saved destinations** (D7 there). | That an approval can undo an irreversible sink. |
| **Tech Lead** | Slices 0–2; BUG-031 as a hard precondition; D16's crawl refusal, which can ship on its own; the dependency on PRD-019 being built and on PRD-017's saved destinations (Q9). | That jobs gain change detection. |
| **Backlog maintainers** (not edited by this draft) | Owner answers to record: implementation-backlog **open item 10** — not now; all of `/jobs` over pipelines is the long-term direction (Q1 = e); `phase4-backlog.md` **§3's scheduled-crawl row** ("absorbed by Monitors") becomes "refused; scheduled crawls not planned" (D16); **PRD-016 R5's second exception** is answered by monitors, not on the job lane. **A cleanup item:** stop writing `job_runs.content_hash`, then drop it and `job_runs.diff_detected` / `diff_summary` (two releases, unmap then drop; no quota view reads them); keep the payload fields (always `null`), the Scrape activity's hash and `diff.py`. | — |

## Appendix A — new endings and pauses, for the joint migration

Run outcomes stay **completed / failed / cancelled**. "Firm" means this draft's proposal is unlikely to move;
"tentative" means an owner question can change or remove it. Already-covered endings (PRD-016's, PRD-019's stop,
PRD-017's sink states, ADR-009 §15a's waiting between retries) are not repeated.

**A. On a check (a pipeline run) or a block**

| Ending / pause | User sees | Final? | Holds a slot? | Monthly run? | Firm? |
|---|---|---|---|---|---|
| **Unchanged** (change gate stopped the run) | `completed`, "unchanged since <check>"; later blocks skipped, reason "unchanged" | final | no | yes, 1 (the fetch) | firm |
| **No baseline** (first check; hand-run) | a verdict, not an ending: carries on, labelled "first check" / "no baseline" | — | — | — | firm; label tentative |
| **Waiting for approval** | in progress; Approval block "waiting for your approval, expires <time>" | resumable | **no** | no extra | firm (slice 2) |
| **Approved** | Approval block completed, "approved at <time>"; run carries on | not an end | as any running block | no extra | firm |
| **Rejected** | `completed`, "stopped: rejected"; later blocks skipped, reason "approval rejected"; nothing undone; no alert; copies kept | final | no | no extra | firm (Q5) |
| **Approval expired** | `completed`, "stopped: approval expired"; skipped reason "approval expired"; a notice | final | no | no extra | firm (Q4) — not the baseline |
| **Superseded** | `completed`, "stopped: superseded by the check of <time>"; skipped reason "superseded" — by any later check with different content; not the baseline | final | no | no extra | firm (Q3) |
| **Monitor deleted** during a check, or while one waits for approval | `cancelled`, reason "monitor deleted" | final | no | already counted | tentative |

**B. Recorded on every check:** the change verdict (changed / unchanged / first check), the baseline check it
was compared with, and what changed (R4). Firm.

**C. On the monitor (no run exists, or the monitor itself changes)**

| State | User sees | Final? | Slot? | Monthly run? | Firm? |
|---|---|---|---|---|---|
| **Active, sleeping until the next check** | "next check <time>" | normal state | no | no | firm |
| **Paused** by the user | "paused since <time>"; not counted in the cap | resumable | no | no | firm |
| **Paused itself** after 5 failures in a row | "paused itself after 5 failed checks — last: <block>: <error>" | resumable | no | no | firm (Q6) |
| **Firing skipped — previous check still running** | history entry | final for that firing | no | **no** | firm |
| **Firing skipped — monthly runs used up** | notice + history entry (ADR-010 §1) | final for that firing | no | **no** | firm |
| **Firing waiting for a slot** / **gave up after 24 h** | notice + history entry | resumable, then final | no | not until it starts | firm |
| **Firing skipped — pipelines switched off** (`PIPELINES_ENABLED=false`) | history entry | final for that firing | no | no | firm |
| **Check failed — storage full** | a `failed` check naming the remedy | final | no | as ADR-010 §1 does for jobs | firm (inherited) |
| **Deleted** | gone from lists; checks keep their history unless deleted with it | final | no | no | firm |

**Changes from the scratch note sent on 2026-10-30:** "ended — reached its end date" is **removed** (end dates
are now a non-goal); "firing skipped — pipelines switched off" is **added** ("paused by an admin" was added, then
moved to *not yet* in review round 13)
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
- `docs/adr/ADR-012-workflow-code-versioning.md` (Accepted 2026-11-03): changing workflow code under replay
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
