# PRD-019 — ScrapeFlow Workflows: Conditional Execution and Failure Notification

> **Why failure notification is in this PRD — it is not a condition.** PRD-016 parked "a failing pipeline tells
> nobody" until conditions were designed, because one possible fix *was* a condition (an "on failure, run these
> blocks" branch), and ADR-009 §15f assigned it here. Review round 1 chose a design that is **not** a condition —
> tick boxes on the message-sending blocks (R5) — so that link is gone. It stays here on purpose: Monitors
> (PRD-018) needs it before it can ship, this PRD is built just before Monitors, and moving it would add a document
> to keep in step for one setting (owner, review round 9).

**Priority:** P1. Required before Monitors (PRD-018), which cannot ship without it.
**Layer:** A (Pipelines). Additive to PRD-016's block model; it replaces nothing in it.
**Source:** `phase4-backlog.md` §2 (this PRD's row and its four obligations); ADR-009 §14 (why this is a
separate layer-A PRD and not part of Monitors), §14c (the condition vocabulary is the hard part), §15f (failure
notification was handed here).
**Status:** **Draft — owner's section-by-section review complete (2026-10-30, 12 rounds).** Next: the Architect pass. Not Accepted.
**Last updated:** 2026-10-30 (review round 12: Open questions — owner review complete)

> **Index order is not build order.** This PRD sorts after PRD-018 but is **written and built before it**.
> Monitors' launch requirement, the cost gate ("skip the paid work when the page has not changed"), needs a
> way to end a run early and still call it a success. Monitors cannot build that itself (PRD-016 OQ-1, ADR-009
> §14). PRD-017 (Delivery sinks) does not depend on this PRD and may be written in parallel.

> **Review round 1 (owner, 2026-10-29): Problem section.** Three changes to the first draft, all decided by
> the owner:
>
> | Changed | From (first draft) | To |
> |---|---|---|
> | **How a run stops early** | a new **Gate** block | **no new block**: Validate gains a setting, *when a rule fails: fail the run* (default, today's behaviour) *or stop the run* (ends `completed`, later blocks skipped). The owner's proposal |
> | **How failure is notified** | a failure-notification URL as a **pipeline setting** | **the Webhook block** gets an events choice, like a job's `webhook_events`: *the run finishes OK* (default) and/or *the run breaks*. One URL, typed once. The choice belongs to **every message-sending block**, so PRD-017's Email block gets it too; storage destinations do not. The alert waits until the run has completely finished, so PRD-017 can report what its rollback undid |
> | **What happens when a run stops early** | (unstated for webhooks) | **no message is sent**. The result is still stored and retrievable. A "stopped early" event is a possible later addition, off by default |
>
> Problem 2 also now says honestly that this PRD supplies only the *stop as a success* half of Monitors' cost
> gate; the *has the page changed?* half is PRD-018's.

> **Review round 2 (owner, 2026-10-30): Background section.** No decision changed. The part on what changes
> for Validate was rewritten around one idea — the same checklist now does a second job, *filter* as well as
> *quality check* — which is where D3 and D4 come from. **D3** (every rule kind gets an opposite) and **D4**
> ("cannot be checked" fails loudly, even in stop mode) are agreed.

> **Review round 3 (owner, 2026-10-30): Goals and Non-goals.** All seven goals agreed as written. Non-goals
> agreed and regrouped by *why* each is excluded — **not yet** (additive later: branching, single-block
> conditions, "any of", a stopped-early message, several alert destinations, a visual editor), **not here**
> (change detection is PRD-018's; jobs are unchanged), and **never** (formulas, regular expressions, and rules
> on time, randomness or the outside world).

> **Review round 4 (owner, 2026-10-30): User stories.** All seven agreed as written, including the one new idea
> they carry: **run inputs of type number and text**, which a rule can compare against (the per-product target
> price). It stays in this PRD rather than waiting for Monitors, because it is useful on its own and Monitors
> needs the same mechanism to hand over "last time's value" (R2).

> **Review round 5 (owner, 2026-10-30): R1.** **D8 agreed** — a Validate set to stop cannot be the last block.
> The owner asked whether a final Validate is still useful once PRD-017 adds rollback. It is, set to **fail**:
> a bad final result fails the run and the rollback deletes what was delivered. That example is now in R1. A
> stop there would not help, because a stop is a success and success is never rolled back.

> **Review round 6 (owner, 2026-10-30): R2.** Agreed as written, with one widening: **`contains`'s text can also
> be handed over as a run input**, not only `compare`'s value — "make sure this page is really about
> {product_name}". Run inputs therefore gain **number** and **text** types. Every other rule field stays fixed.

> **Review round 7 (owner, 2026-10-30): R3.** **D5 agreed.** The owner asked how replay works ("does Temporal
> restart from the beginning — would a re-scrape change the data?"), which exposed an error in the first draft:
> it said a restart **re-checks** the rules, so a clock rule could flip and strand the run. **It does not.**
> Finished steps are never re-run; their results are read back from the run's history, and Validate is a step,
> so its answer is recorded. The stuck-run trap is real only for a decision computed in the workflow code itself.
> R3 is rewritten with the retry / replay / new-run cases, and D5 now rests on predictability, safety, keeping
> simple rules movable into the workflow code, and "time is a schedule's job" — not on the trap.

> **Review round 8 (owner, 2026-10-30): R4.** Agreed, with two decisions added. **D9:** a stopped run stays
> `completed` with a "stopped early" label — no fourth status — and the label shows in the **list of runs** as well
> as on the run page. **D10:** the owner asked whether a stopped run's result is mostly useless and should be
> rolled back. Rollback of the user's own destinations stays failure-only (a stop is a success). But
> **ScrapeFlow's own copies** get a choice on the stop: **keep** (default — e.g. a price history) or **delete**
> (frees storage at once; what Monitors' unchanged-page check wants). The verdict survives either way.

> **Review round 9 (owner, 2026-10-30): R5.** Agreed as written. The alert does **not** include a link to the work
> finished before the break, for now; the run id is enough to fetch it, and adding it later breaks nothing. The
> owner noted that failure notification is not conditional execution; it stays in this PRD (see the note under
> the title), and the PRD is renamed **Conditional Execution and Failure Notification**.

> **Review round 10 (owner, 2026-10-30): R6.** Agreed as written, plus one addition: runs from before this PRD keep
> their bare `skipped` blocks, shown as **"reason not recorded"** — no backfill.

> **Review round 11 (owner, 2026-10-30): Success criteria.** Two checks had not caught up with later decisions and
> are fixed: the retrievable-extraction check now holds for **keep** only (with **delete** the extraction is gone,
> and only a new run — a fresh scrape — gets the data again), and the delete example no longer puts a stop
> Validate last (D8). Four agreed behaviours had no check and now do: an alert when a run could not start, a
> Webhook with no box ticked refused at save, the run page updating live on a stop, and "rule not met" never
> retried. The replay corpus also gains a failure that sends an alert.

> **Review round 12 (owner, 2026-10-30): Open questions and hand-forward.** All agreed. Three gaps closed: **OQ-2**
> now covers a run-level "stopped at" marker (for the list of runs) and "reason not recorded" for old runs;
> **OQ-5** asks who sends the alert for a run that could not be started (its workflow never ran); **OQ-6** adds
> the keep / delete setting. **This completes the owner's section-by-section review of the draft.**

---

## What this PRD owes, and where each item is answered

| # | Obligation (from `phase4-backlog.md` §2) | Answered in |
|---|---|---|
| 1 | **The Validate-block precedent.** Validate is the block that already checks a condition; any condition vocabulary has to be designed against it. | [Background](#background--the-validate-block-precedent), **R2** |
| 2 | **The replay-determinism limit on what a condition may say.** The hard question; it runs into PRD-016's ban on user-written code and expressions. *(Answered more narrowly than ADR-009 §14c feared: the condition is decided inside a step and recorded, so replay reads it back; what a rule may read is limited for other reasons.)* | **R3** |
| 3 | **The halt-early primitive** — a way to end a run before its last block with the outcome `completed`. | **R1**, **R4** |
| 4 | **Run-level failure notification.** Today a job set to `webhook_events: ["job.failed"]` ("only tell me when it breaks") has **no pipeline equivalent at all**. | **R5** |

## Decisions this draft makes, for the owner's review

| # | Decision | Where | The alternative rejected | Status |
|---|---|---|---|---|
| D1 | **No new block.** Validate gains a setting: *when a rule fails* → **fail the run** (default, today's behaviour) or **stop the run** (ends `completed`, later blocks skipped). | R1 | a separate Gate block; a condition on every block; real branching (if/else into different chains) | ✅ owner, round 1 |
| D2 | **Both settings use the same rules**, written the same way. Switching a Validate between *fail* and *stop* changes only what happens when a rule is not met. | R2 | a separate condition language | ✅ follows from D1 |
| D3 | **Every rule kind can be stated in reverse** ("absent" as well as "present", "does not contain" as well as "contains"). | R2 | only Validate's six rule kinds as they are | ✅ owner, round 2 |
| D4 | **A rule that cannot be checked fails the run, even on a Validate set to stop** (e.g. comparing a missing field). "I couldn't check" is not "no". | R2 | treating "cannot be checked" as "rule not met" → stop | ✅ owner, round 2 |
| D5 | **A rule may read only the data its block receives and the run's own inputs.** No clock, no randomness, no live lookups, no mid-run database reads. Permanent — for predictability, safety, and to keep simple rules movable into the workflow code later. | R3 | a wider vocabulary | ✅ owner, round 7 |
| D6 | **Failure notification lives on the message-sending blocks**, as an events choice like a job's `webhook_events`: *the run finishes OK* and/or *the run breaks*. *The run breaks* sends even when the run broke before reaching the block. In layer A the Webhook block is the only message-sending block; PRD-017's Email block gets the same choice. Storage destinations (bucket, database row) do not. | R5 | a separate failure URL in the pipeline's settings (the same URL typed twice); an "on failure" branch | ✅ owner, round 1 (extended to every message-sending block, same round) |
| D7 | **The events are "finishes OK" and "breaks" only.** A run that **stops early sends nothing**; a cancelled run sends nothing. A "stopped early" event may be added later, off by default. | R4, R5 | a message for every outcome | ✅ owner, round 1 (stop sends nothing) |
| D8 | **A Validate set to stop cannot be the last block** — refused at save time. A Validate set to *fail* may still be last (e.g. a final check that, with PRD-017, triggers rollback). | R1 | allowing it; it would have nothing to skip, and a stop is a success, which is never rolled back | ✅ owner, round 5 |
| D9 | **A stopped run is still `completed`, with a "stopped early" label** — no fourth status. The label shows on the run page **and in the list of runs**. | R4 | a new `stopped` status that every reader of run statuses would have to learn | ✅ owner, round 8 |
| D10 | **On stop, the user chooses what happens to what ScrapeFlow saved before the stop: keep (default) or delete.** Delete frees the storage at once. Either way the run keeps the verdict ("price was 120, needed under 100"). The user's own destinations (PRD-017) are never touched by this choice — a stop is a success, and a success is never rolled back. | R1, R4 | always delete on stop (silently loses data some users want, e.g. a price history); always keep (Monitors would pile up identical copies against the storage limit) | ✅ owner, round 8 |

---

## Problem

PRD-016 shipped pipelines as a **straight line**: every block runs, in order, until one fails or the user
cancels. A run can end only three ways:

```
  Scrape ──► Extract ──► Webhook ──► ✅ completed   (everything ran)
  Scrape ──► Extract ──✖            ❌ failed       (something broke)
  Scrape ──► Extract ──■            ⛔ cancelled    (the user pressed Cancel)

  missing: "stop here, everything is fine"
```

That leaves three gaps.

**1. "Nothing to report" can only be recorded as a failure.** A user who wants *"extract the price, and only
tell me if it is under £100"* has one tool, the **Validate** block, and Validate **fails the run** when its rule
is not met:

```
Mon  £120   Scrape ─► Extract ─► Validate(price<100) ✖              ❌ failed
Tue  £118   Scrape ─► Extract ─► Validate(price<100) ✖              ❌ failed
Thu  dead   Scrape ✖                                                ❌ failed   ← the real problem
Fri  £89    Scrape ─► Extract ─► Validate ✔ ─► Webhook 📨           ✅ completed
```

Three "failures", one real. Monday and Tuesday worked correctly; there was simply no deal. A history full of
false failures teaches the user to ignore failures, and so to miss Thursday.

**2. Monitors cannot skip work.** PRD-018's cost gate must skip the LLM call (which the user pays for) and the
delivery when the page has not changed, and still count the run as a success:

```
06:00  changed     Scrape ─► [changed? yes] ─► Extract($) ─► Webhook   ✅
12:00  unchanged   Scrape ─► [changed? no]  ■ stop                     ✅  no LLM bill
```

It needs two pieces. **This PRD supplies one of them**: the *stop, and still count as a success* (the `■ stop`
above). The other, *how to tell whether the page changed*, is PRD-018's. This PRD only makes it possible, by
letting a rule compare against a value supplied when the run starts (R2), so that Monitors can pass in "what the
page looked like last time". Without this PRD, Monitors would have to change the pipeline engine itself, which
is layer A's job (PRD-016 OQ-1). PRD-016 left room for it: a `skipped` block state exists, and nothing produces
it for this purpose yet.

**3. A failing pipeline tells nobody.** On the job lane, *any* failure fires `job.failed` to the user's webhook,
because delivery is triggered by **how the run ended**. In a pipeline, the only notification is the Webhook
block, which is a **position in the line**. When an earlier block fails, the run stops before reaching it:

```
job:       Scrape ✖ (site dead) ──► 📨 job.failed sent

pipeline:  Scrape ✖ ─ ─ Extract ─ ─ Webhook      ❌ failed … and silence
                       (never ran)  (never ran)
```

A job set to `webhook_events: ["job.failed"]` ("don't tell me about successes, only when it breaks") has **no
pipeline equivalent at all**. This is PRD-016 R6's fourth recorded gap, assigned here by ADR-009 §15f.

**How this PRD closes the three gaps:**

| Gap | Fix | Where it lives |
|---|---|---|
| 1. "Nothing to report" recorded as a failure | Validate can **stop** instead of fail | the Validate block (R1) |
| 2. Monitors cannot skip work | the same stop setting; the "has it changed?" check is PRD-018's | Validate (R1, R2) + PRD-018 |
| 3. Failures are silent | the Webhook block can also send **"the run breaks"** (PRD-017's Email block will too) | the Webhook block (R5) |

No other block changes. Scrape, Clean and LLM extract are untouched.

## Background — the Validate-block precedent

*(Obligation 1.)*

**What Validate is today** (built at C.8, released with the pipeline lane 2026-10-12). A block holding a list
of **rules**. It reads what the previous block produced and checks each rule. All rules met → the run continues
and the next block receives the same data. Any rule not met → the run **fails**, naming the rule:

```
Extract produced:  {"title": "Blue Kettle", "price": 120}

Validate rules:    present  /price          ✔
                   type     /price number   ✔
                   compare  /price < 100    ✖   →  run FAILED: "rule 3 (compare /price lt 100) failed"
```

Six rule kinds:

| Rule | Asks | Works on |
|---|---|---|
| `json_schema` | does the document match this JSON Schema? | JSON (a page scraped as JSON, or an LLM extraction) |
| `present` | is there a value at this path (e.g. `/price`)? | JSON |
| `type` | is the value at this path a string / number / integer / boolean / object / array / null? | JSON |
| `compare` | is the value at this path `eq` / `ne` / `lt` / `le` / `gt` / `ge` a **constant**? (ordered comparisons need a number) | JSON |
| `contains` | does the page text contain this string? | a page (HTML / Markdown) |
| `min_length` | is the page at least N characters long? | a page |

Rules are checked **at save time** against what the block will receive: a text rule after an extraction, or a
JSON rule after an HTML page, is refused.

**Why Validate is declarative — a fixed list of rule kinds, no expressions.** PRD-016 R2's three reasons:

1. **An expression evaluator is user-written code by another name**, and PRD-016 excludes user code from blocks
   (it needs a sandbox).
2. **Replay.** Temporal recovers a run after a restart by re-reading its recorded history. A decision that
   answers differently the second time corrupts the run. *(Precisely: this bites a decision computed in the
   workflow code itself. Validate's answer is computed in its own step and recorded, so it is read back, not
   recomputed — R3 explains.)*
3. **Predictability.** Validate can end a run after the LLM block has already billed the user, so the user must
   be able to read a rule and know what it rejects.

**What changes: the same checklist, used for a second job.** Today Validate is a **quality check** — "is the data
OK?" — and a rule not met is **rare**: it means something broke, so the run fails. This PRD reuses the same rules
as a **filter** — "is this worth carrying on for?" — where a rule not met is **normal** (most days there is no
deal), so the run stops quietly as a success. A checklist built for the first job has two gaps when used for the
second:

- **Gap 1 — filters often need "not".** A quality check asks "is it there? is it right?". A filter asks the
  opposite just as often: "carry on only if the page does **not** say *Out of stock*", "only if there is **no**
  error field". Today there is `contains` but no "does not contain", `present` but no "absent". **Fix: every rule
  kind gets an opposite (D3).**
- **Gap 2 — "no" and "broken" were the same answer; now they must differ.** With the rule *price under 100*:

  | Extract returned | As a quality check (today) | As a filter (stop) |
  |---|---|---|
  | `{"price": 80}` | ✔ continue | ✔ continue |
  | `{"price": 120}` | ✖ fail | ■ stop quietly — no deal, **correct** |
  | `{"title": "Kettle"}` — no price at all | ✖ fail | **must fail loudly** — the extraction broke |

  As a quality check, both of the last two rows fail, and that is right. As a filter, a quiet stop on the missing
  price would **hide a broken pipeline** for weeks. **Fix: "cannot be checked" fails the run, even in stop mode
  (D4).** A user who really means "no price is fine, just stop" says so with a *there is a price* rule placed
  first; rules are checked in order, so that one stops the run cleanly.

The rule kinds, the save-time check, naming the rule that decided, and "no expressions" are all unchanged.
A Validate set to stop decides **which blocks run next** (and whether the user pays for them), so its answer must
be recorded once and read back after a restart, never recomputed — which is how Validate already works. R3
covers it, and what a rule may read.

---

## Goals

1. A user can make a run **stop early as a success**: "continue only if …; otherwise stop, nothing is wrong".
2. A run that stopped early is **clearly different** from one that ran every block, and from one that failed or
   was cancelled. The user can see where it stopped and why, without reading logs.
3. Stopping uses **Validate's existing rules**. No new block and no second rule language.
4. A condition's answer is **decided once per run and never changes**: not after a restart, a redeploy or a
   replay.
5. A user can be **told when a run fails**, whichever block failed, including "only tell me when it breaks".
6. **Monitors (PRD-018) can build its cost gate** on this PRD without another change to layer A's model.
7. **Pipelines saved before this PRD keep working unchanged**, and so do jobs, batches, crawls and schedules.

## Non-goals

Three groups, because they are excluded for three different reasons:

```
A — not yet:    can be added later; nothing saved today breaks
B — not here:   another PRD owns it
C — never:      would break the safety of every run
```

**A — Not yet** (each is additive later):

- **Branching** — if/else into two different chains ("if the price dropped, email me; otherwise save it to a
  file"). A run is still one line; Validate can only choose "continue" or "stop here". Branching needs a
  graph-shaped run, deferred with PRD-016's data-flow fan-out.
- **A condition on a single block** — "skip just this block, carry on with the rest" ("always save to a file,
  email only if the price dropped"). It only matters with several delivery blocks in a row, which arrive with
  PRD-017 (Delivery sinks). Decided there.
- **"Any of" (OR) between rules.** All rules must be met (AND), as today. If Monitors needs "price changed
  **or** stock changed", PRD-018 asks for it.
- **A message when a run stops early, or is cancelled** (D7). A "stopped early" choice may be added later, off by
  default.
- **Alerts to several places, or by email.** One Webhook block in layer A means one alert destination. Several
  destinations and the Email block arrive with PRD-017, and each message-sending block there gets the same
  *the run breaks* choice (R5).
- **A visual editor in the SPA.** Authoring stays API-first; the SPA shows run status (PRD-016's split).

**B — Not here:**

- **Change detection** — "has this changed since the last run?" — is **PRD-018's**. This PRD makes the
  comparison possible (R2), but does not define "the previous run" or add a fingerprint rule.
- **Changes to the job lane.** Jobs keep `webhook_events` exactly as they are.
- **A new block for conditions.** Stopping is a setting on Validate (D1).

**C — Never** (permanent, on purpose):

- **Expressions and formulas** — cross-field comparisons and arithmetic (`discount < price`,
  `price * 1.2 > 100`). That is user code, which needs a sandbox (Background, reason 1). Never **through this
  block**; anything like it would be a separate decision with its own security review.
- **Regular expressions** (`/^£\d+/`). One crafted pattern can freeze an evaluation for minutes. Same
  condition as above.
- **Conditions on time, randomness or the outside world** ("only on weekdays", "on 10% of runs", "if that other
  site is up"). They make a rule's answer unpredictable from the run's own data, and a live lookup is a hidden
  network request — see R3. No exception, through any block.

## User stories

**As a deal-watcher**, I want *scrape → extract price → validate (price < £100, stop) → webhook*, so I hear about
the deal and nothing else. A day without a deal should be a quiet success, not a failure in my history.

**As a data engineer**, I want *scrape → validate (page contains "In stock", stop) → extract*, so I don't pay for
an LLM call on an out-of-stock page.

**As a user running one pipeline for many products**, I want the threshold to be a **run input**, so each run can
say "under £100" or "under £40" without a copy of the pipeline per product.

**As a user with an unattended pipeline**, I want a message when a run fails, at whichever block, so a dead site
or an expired key does not go unnoticed for weeks.

**As a user moving a job set to `webhook_events: ["job.failed"]`**, I want a pipeline that messages me only when
it breaks, so I lose nothing by moving.

**As a user reading a run**, I want to see that it stopped at the Validate called `in_stock` because the page did
not contain "In stock", and that the two blocks after it were skipped for that reason, not because something
broke.

**As the Monitors PRD**, I need a way to end a run as `completed` when a condition is not met, with a rule that
can compare against a value supplied when the run starts. That is how the cost gate is built.

---

## Requirements

### R1 — Validate can stop a run

- Validate gains one setting: **when a rule is not met**:
  - **fail the run** — the **default**, and exactly today's behaviour. Every Validate saved before this PRD
    keeps it;
  - **stop the run** — the run ends **`completed`**, every later block is **skipped**, and the run records which
    Validate stopped it and which rule was not met (R4).
- When all rules are met, both settings behave identically: the run continues and Validate passes its input on
  unchanged.
- **A rule that cannot be checked fails the run under either setting** (D4, detailed in R2).
- **Rules are checked in order; the first rule not met decides**, and later rules are not evaluated. So
  `[type /price is number, compare /price < 100]` set to stop ends cleanly when the extraction returned
  `"price": null`, instead of failing on the comparison.
- **What was saved before the stop: keep or delete** (D10). A Validate set to stop has a second choice:

  ```
  when a rule is not met:   ○ fail the run
                            ● stop the run
                                 what was saved before the stop:  ● keep (default)   ○ delete
  ```

  It applies only to what **ScrapeFlow** stored for this run (the scraped page, an extraction, screenshots) — the
  objects counted against the user's storage limit. **Keep** leaves them stored and retrievable (a stopped
  deal-watcher keeps a free price history). **Delete** removes them as the run stops and frees their storage at
  once — what Monitors' unchanged-page check wants, instead of piling up identical copies. Either way the run keeps
  its verdict and the value found. **Anything already delivered to the user's own destinations (PRD-017) is never
  touched**: a stop is a success, and a success is never rolled back.
- **Stopping costs nothing extra.** Validate fetches and stores nothing. A run that stops still costs its one
  monthly unit, because the fetch happened (PRD-016 OQ-4: the unit is one fetch). What earlier blocks stored stays
  stored and charged — unless the stop is set to **delete** it (above), which frees that storage.
- **Save-time checks** (PRD-016 R1's rule — refused with a message naming the block and the reason):
  - a Validate **set to stop** cannot be the **last** block (D8): there is nothing left to skip, so stopping and
    continuing would end the same way. Start strict: loosening a save rule later breaks nothing, tightening one
    breaks saved pipelines;
  - a Validate set to **fail** may still be last, as today. "Check the final result, and treat a bad one as
    broken" is a real use, and with PRD-017's rollback it also **undoes** what was delivered:

    ```
    Scrape ─► Extract ─► Bucket (saves file) ─► Validate (price present, FAIL)

    price there:    ✔  → ✅ completed; the file stays in the user's bucket
    price missing:  ✖  → ❌ failed → rollback deletes the bucket file
                         📨 "broke at Validate: no price. Undone: bucket file"
    ```

    A Validate set to **stop** in that position would not do this: a stop is a **success**, and a successful
    run is never rolled back (PRD-017). *Bad result → undo* is always **fail**; *nothing to do → skip the rest*
    is **stop**;
  - the existing checks are unchanged: Validate cannot be first, and each rule must suit the block's input;
  - a pipeline may contain several Validate blocks, with either setting.
- **Retries and time budget: unchanged** (PRD-016 R4). Trouble reading the input from storage is transient and
  retried. "Rule not met" and "rule cannot be checked" are final and never retried — a retry would read the same
  content and give the same answer.
- **Cancellation: unchanged** (PRD-016 R3) — a cancel takes effect at the next block boundary.

### R2 — The rules (Validate's, extended)

- **The same six rule kinds, under both settings**, with the same save-time check. A rule means the same thing
  whether the Validate fails or stops; only the consequence differs.
- **Every rule kind has a reverse form** (D3). At minimum: absent / present; "is not of type" / "is of type";
  "does not contain" / "contains"; "at most N characters" / "at least N". (`compare` already has `eq`/`ne`; JSON
  Schema already has `not`.) *Why:* "continue only if there is **no** error banner" is as common as its opposite.
  How a reverse is written is an Architect question (OQ-3).
- **"Cannot be checked" is not "not met"** (D4). A rule that names a path that is not there, or compares a value of
  the wrong type (`/price` holds the string `"£99"`), **fails the run** under either setting, naming the Validate,
  the rule and what was found. The rules whose purpose is to ask about existence or type (`present`, `type`, and
  their reverses) never raise this — "no" is their answer.
  *Why:* a Validate that wrongly *stops* hides a problem — the extraction broke, nobody is told, the user concludes
  there was no deal. A wrongly *failed* run is loud, and with R5 the user hears about it. When unsure, be loud.
- **Two rule values may come from a run input** — a value handed over when the run is started, the way the
  Scrape block's URL can be today:
  - **`compare`'s value** (and its opposites) — one saved pipeline serves "under £100" on one run and "under £40"
    on the next;
  - **`contains`'s text** (and "does not contain") — "make sure this page is really about **{product_name}**",
    with a different product name per run.

  This needs run inputs of more types than URL: **number** (for `compare`) and **text** (for `compare` against
  text, and for `contains`). A missing or wrongly typed input is refused **when the run is triggered**, before
  anything executes, and nothing is charged. Other rule fields stay fixed for now; allowing more later breaks
  nothing, taking one back would break saved pipelines.
  *Why now:* useful in layer A on its own (the per-product story), and it is the hook PRD-018 needs — a monitor
  passes "last run's value" as a run input and Validate compares against it. ADR-009 §14c records that adding a
  bindable field breaks nothing.
- **No expressions, ever, through this block.** Every new rule kind is added on purpose, with R3's question
  answered for it — never by widening an existing kind's syntax.

### R3 — What a rule may read (and how restarts work)

*(Obligation 2.)*

**The rule (D5):** a rule may read **only** (a) the data its Validate receives and (b) the run's own inputs, as
fixed when the run was triggered. Nothing else — permanently.

**First, how a restart actually works**, because the first draft of this section got it wrong. Temporal keeps a
**diary** of every run. A run has two kinds of thing in it:

| | What it is | After a restart |
|---|---|---|
| **Steps** — Scrape, Validate, Extract, Webhook | the work, done by workers | **never re-run**; each finished step's result is **read back from the diary** |
| **The glue code** — "run Scrape, then Validate, then Extract…" | the workflow itself | **re-run from the top**, using the diary's results instead of calling the steps again |

So with *Scrape → Validate → Extract*:

```
A. Extract fails (LLM timeout)       → a RETRY of Extract only (5 s, 10 s … up to its attempt limit).
                                        Scrape and Validate are untouched. If every attempt fails, the run fails.

B. the server restarts mid-Extract   → a REPLAY of the glue code:
                                        "run Scrape"   → diary: done, page = history/…/scrape.html  → read, not re-run
                                        "run Validate" → diary: done, answer = carry on              → read, not re-run
                                        "run Extract"  → diary: started, not finished                → wait for it / retry it

C. the user re-runs the pipeline     → a NEW run: a fresh scrape, possibly new data. Expected.
```

**Nothing finished is ever redone, and the page cannot change underneath a run**: the diary holds the reference
to the stored page, and the stored bytes do not change.

**Where determinism really matters.** Validate is a **step**, so its answer is written in the diary and read back
on a restart, never recomputed. The glue code then branches on that **recorded** answer ("stop" → skip the rest),
which is safe. A run gets stuck only if a decision is computed **inside the glue code itself** from something that
changes — the classic trap:

```
glue code computes "hour < 17" itself:
16:58  yes → Extract runs, the user is charged
17:03  routine deploy → replay → glue code recomputes "hour < 17" → no
       → the diary says Extract ran, the code says it should not have → run stuck "running" forever
```

The design keeps every rule inside the Validate step, so this cannot happen (the Architect confirms it, OQ-1). The
first draft gave this trap as the reason for D5; **it is not**, because a Validate step's answer is recorded.

**Why D5 holds anyway** — four reasons, none of them the trap above:

| Reason | What it means |
|---|---|
| **Predictability** | same data + same inputs → same answer, always. A user can open a stopped run's stored page and see exactly why it stopped; re-checking a run's verdict later gives the same verdict |
| **Safety** | a rule that fetches outside data ("if that other site is up") is a hidden network request from ScrapeFlow's servers to an address a user typed — the risk the private-address (SSRF) check exists to contain, in a new place |
| **Keeps the design free** | if rules read only stored data and inputs, the Architect may later evaluate simple ones directly in the glue code (cheaper — no extra step) without creating the trap |
| **Time belongs to schedules** | "only on weekdays" is a schedule — Monitors' job (PRD-018), not a rule's |

So the rules **permanently exclude**:

| Excluded | Example | Why |
|---|---|---|
| the clock | "only on weekdays", "if before 17:00" | not predictable from the run's data; a schedule's job |
| randomness | "on 10% of runs" | not predictable at all |
| a live lookup | "if that URL returns 200", "if the previous run's price was higher" | a hidden network or database request; the answer depends on when it was asked |
| the database, read mid-run | "if I have fewer than 10 results this month" | the answer depends on other runs, not on this one |

**"Compare with the previous run" is not excluded — it is handed over, not looked up.** The value arrives as a
**run input** when the run starts (R2), is written in the diary once, and is part of the run's own data. That is
how PRD-018 builds change detection within this rule.

**Each time the rule list grows** (a new kind, "any of", a regex if ever admitted), the proposal must state what
the rule reads and show it is one of the two allowed sources. A standing requirement, so it is not re-argued per
rule.

### R4 — How a run that stopped early looks

*(Obligation 3, the user-visible half.)*

- **The outcome is `completed`.** PRD-016 R3's three outcomes (completed, failed, cancelled) stay three;
  "stopped early" is **not** a fourth (PRD-016 OQ-1 requires this, so nothing reading outcomes must learn a new
  one).
- **But it is distinguishable** from a run that ran every block (D9) — in the API, in the SPA's **run page**, and
  in the SPA's **list of runs**, where a "stopped at <Validate>" label lets a user scan a week of runs ("stopped,
  stopped, stopped, **sent**") without opening each. The run page states:
  - **which Validate** stopped it;
  - **which rule** was not met, as the user wrote it, plus the value found (`/price` was `120`, needed `< 100`);
  - **which blocks were skipped**.
- **Every skipped block says why.** Today a block is `skipped` when an earlier block failed, and also when the run
  was cancelled; now there is a third reason. For each skipped block the user must be able to tell:
  1. **a Validate stopped the run** (which one);
  2. **an earlier block failed** (which one);
  3. **the run was cancelled**.
  A bare `skipped` is not acceptable once skipping can be the *intended* outcome.
- **The run's result is the output of the last content block that ran** — PRD-016 R3's existing rule (Validate
  passes its input through and stores nothing). Stopped before the LLM block → the scraped page; after it → the
  extraction. With **keep** (the default) it is stored and retrievable through the API and the SPA like any
  result. With **delete** the run has no stored result, and says so ("result deleted when the run stopped"),
  while still showing why it stopped.
- **A run that stopped early sends no message** (D7). The Webhook block after it is skipped — that is the point
  of stopping — and *the run breaks* (R5) does not fire, because the run did not fail.
- **The live run view shows the stop as it happens**, as it already does for completion, failure and
  cancellation.

### R5 — Failure notification, on the message-sending blocks

*(Obligation 4.)*

- **The choice belongs to every block whose job is to send a message to a person or a system**, not to the
  Webhook block alone. In layer A the Webhook block is the only one. PRD-017's **Email** block gets the same two
  choices when it ships. Blocks that **store** data (PRD-017's bucket and database row) do not: writing "it
  broke" into the user's storage tells nobody. The rest of this section describes the Webhook block; every rule
  applies the same way to any later message-sending block.
- **The Webhook block gets an events choice** (D6) — one URL and one signing secret, typed once:

  ```
  Webhook block
    url:     https://me.example/hook
    secret:  ••••
    send a message when:
      [✔] the run finishes OK   — the result (default; today's behaviour)
      [ ] the run breaks        — "broke at <block>: <reason>"
  ```

  At least one must be chosen. Every Webhook block saved before this PRD behaves as *finishes OK* only, exactly
  as today.

- **What each choice does:**

  | Chosen | All blocks ran | Stopped early | Failed |
  |---|---|---|---|
  | finishes OK (default) | 📨 result | — | — |
  | finishes OK + breaks | 📨 result | — | 📨 "broke at …" |
  | breaks only | — | — | 📨 "broke at …" |

  A job's `webhook_events` maps one to one: `["job.completed"]`, `["job.completed", "job.failed"]`,
  `["job.failed"]`.

- **"Finishes OK" works as today** — the message is sent when the run reaches the Webhook block, and a delivery
  that finally fails still fails the run (PRD-016 R2, ADR-009 §15).
- **"The run breaks" is the one exception to "a block acts only when the run reaches it".** It is sent when the
  run ends `failed`, **wherever** the failure happened — including before the Webhook block, which is its whole
  purpose. Triggered by how the run ended, never by position. It covers:
  - any block failing, the **Webhook block itself** included (the alert then goes to the same URL and will
    probably fail too; it is recorded as undelivered — no worse than jobs today);
  - a failure between blocks: the run's time budget running out, storage accounting failing;
  - a run that fails **before its first block starts** (the workflow could not be started). That caller already
    got an error from the API, but under Monitors nobody is watching the trigger.
- **It does not fire on `completed` (including a stop) or `cancelled`** (D7).
- **What it says:** event **`pipeline.failed`**; at least what `job.failed` carries today, adapted the way PRD-016
  R6 adapted the result payload (no `job_id`; `pipeline_id` and `pipeline_run_id`; `run_id` kept), plus **which
  block failed** and **the error**. Signed like every other ScrapeFlow webhook. Exact fields: OQ-5.
- **The alert is sent once the run has completely finished ending**, never part-way through. In layer A that is
  immediately after the failure. Later layers may do work between the failure and the end — PRD-017's rollback
  deletes what reversible destinations already received — and the alert then waits for that work and reports it
  ("undone: bucket file; left in place: nothing"). This PRD only requires that the alert be **able** to carry
  such a report; PRD-017 defines its contents.
- **The alert does not affect the run.** The run is already `failed` and stays `failed` whether or not the alert
  is delivered, and never waits for it. Delivery is **retried over the window job webhooks use today** (about
  2.6 hours) and **checked against private addresses on every attempt**. An undeliverable alert is recorded and
  visible to the user and the admin, never silently dropped.
- **No quota cost**, as with job webhooks.
- **One alert destination in layer A**, because there is one Webhook block. When PRD-017 allows several Webhook
  blocks and adds Email, each of them may choose *the run breaks*, and each sends its own alert.

### R6 — Compatibility

- **Additive.** A pipeline saved before this PRD runs, reports and notifies exactly as before, with no migration
  or re-save: its Validate blocks default to *fail the run*, its Webhook block to *finishes OK*.
- **Runs in flight when this ships finish on the behaviour they started with** (ADR-012), because the stop path
  and the failure alert are new code inside the workflow.
- **Metering unchanged** (R1): stopping costs no unit; a stopped run costs its one fetch (a *delete* stop frees its
  storage, never its monthly unit); an alert costs nothing.
  PRD-016's metering-parity rule still holds.
- **Jobs, batches, crawls and schedules are untouched**, and so are their webhooks.
- **Runs from before this PRD are not rewritten.** Their skipped blocks have no recorded reason, and the run page
  shows **"reason not recorded"** for them. The reason is not worked out after the fact: old runs are history, and
  code that rewrites history is a risk for little gain.
- **Admins** see stop verdicts, skip reasons and alert deliveries wherever they already see pipeline runs and
  webhook deliveries.

---

## Success criteria

**Stopping (R1, R4)**
- [ ] *scrape → extract → validate(`/price` < 100, **stop**) → webhook*: a page at £80 runs every block and the
      webhook sends the result. A page at £120 ends **`completed`**, the Webhook block shows **skipped, stopped by
      the Validate**, and **no message is sent**.
- [ ] The £120 run states, in the API and the SPA run view, **which Validate** stopped it, **which rule** and **the
      value found**, and is distinguishable from a run that ran every block. With **keep** (the default) its
      extraction is retrievable; with **delete** it is not — see the delete check below.
- [ ] The same pipeline with the Validate set to **fail** (or saved before this PRD) fails the £120 run exactly as
      today.
- [ ] A Validate set to stop with `contains "In stock"`, placed before the LLM block, stops an out-of-stock page
      **with no LLM call made**.
- [ ] `[type /price is number, compare /price < 100]` set to stop, against `{"price": null}`, **stops** at the
      first rule and does not fail on the second.
- [ ] A stop consumes **no extra** monthly unit and no storage beyond what earlier blocks stored.
- [ ] With **delete** chosen, a stopped run's stored objects are removed and the user's storage usage drops by
      exactly their size; the run still shows the Validate, the rule and the value found, and says its result was
      deleted. With **keep** (or a Validate saved before this PRD), they stay and are retrievable.
- [ ] *Scrape → Bucket → Validate (stop, delete) → Webhook*: the stop deletes ScrapeFlow's copy of the page,
      **leaves the object in the user's bucket**, and the Webhook is skipped.
- [ ] The SPA's list of runs shows the "stopped at <Validate>" label on stopped runs and nothing on runs that ran
      every block.
- [ ] Save is refused, naming the block and reason, for a Validate **set to stop** as the **last** block. A
      Validate set to fail as the last block still saves. Two Validate blocks in one pipeline save and run.
- [ ] Cancelling during the block before a Validate stops the run at that boundary; the run ends **`cancelled`**,
      not `completed`.
- [ ] A rule not met (and a rule that cannot be checked) is **never retried**: the Validate step makes one
      attempt, and its history shows no second attempt.
- [ ] The SPA's run page shows the stop — the Validate's verdict and the skipped blocks — **as it happens**,
      without a reload.

**The rules (R2)**
- [ ] Every rule kind has a working reverse form under both settings; a reverse and its original, on the same
      input, always disagree.
- [ ] A `compare` against a **missing** path or a **wrong-typed** value **fails** the run under either setting,
      naming the Validate, the rule and what was found. `present` / `type` and their reverses never fail on a
      missing path.
- [ ] One saved pipeline with a `contains` text bound to a run input passes for "Blue Kettle" on a kettle page and
      stops for "Red Toaster" on the same page, with no edit.
- [ ] One saved pipeline with a threshold bound to a run input stops at 100 on one run and 40 on the next, with no
      edit. A trigger missing the input, or with a non-numeric one, is **refused before anything runs**.

**Determinism (R3)**
- [ ] A run whose workflow worker is killed **after** a Validate has decided and **before** the next block
      finishes resumes with the **same** decision; nothing is re-decided and no block runs twice.
- [ ] Recorded histories of a **stop**, a **pass**, and a **failure that sends an alert** are in the replay corpus
      and replay cleanly in the test that gates the api build.
- [ ] No rule kind reads the clock, randomness, the database or the network; the Architect's design records this
      per rule kind.

**Skip reasons (R4)**
- [ ] Each skipped block states one of the three reasons — a Validate stopped the run (named), an earlier block
      failed (named), the run was cancelled. Verified for one run of each kind.

**Failure notification (R5)**
- [ ] A Webhook block set to **breaks only**: a run whose **Scrape** fails sends one signed `pipeline.failed`,
      naming the block and the error; a run that completes sends nothing. (The `["job.failed"]` job, now
      expressible.)
- [ ] Set to **finishes OK + breaks**: a run that completes sends the result; a run that fails at Scrape sends the
      alert to the **same URL**.
- [ ] The alert also fires when the **Webhook block** itself fails, when the run's **time budget** runs out, and
      when the run **could not be started** at all.
- [ ] Saving a Webhook block with **neither** box ticked is refused, naming the block and the reason.
- [ ] A run that **stops early** and a run that is **cancelled** send nothing under any choice.
- [ ] With the receiver down, the run is **`failed` at once** and does not wait for the alert; the alert is retried
      over the job-webhook window, then shown as undelivered to the user and the admin.
- [ ] An alert URL that resolves to a private address is refused **at every attempt**.
- [ ] A run uses the Webhook settings of **its own version**; an edit made while it runs does not redirect its
      alert.
- [ ] Every Webhook block saved before this PRD behaves as *finishes OK* only.

**Compatibility (R6)**
- [ ] Every pipeline saved before release runs unchanged; the existing pipeline tests and replay corpus pass
      unchanged.
- [ ] Jobs, batches, crawls and schedules are unaffected; the existing API suite passes.
- [ ] A run finished before release shows its skipped blocks as "skipped (reason not recorded)"; no stored run is
      modified by the release.

**Handed forward**
- [ ] PRD-018's author confirms in writing that the cost gate can be built as a Validate set to stop plus a run
      input, with no further layer-A change. If not, that is a defect here, raised before PRD-018 is built.

---

## Open questions for Architect

**OQ-1 — Where the stop decision is made, so it is recorded once and never recomputed.** Validate already runs as
its own step that reads the stored content and reports back, so its answer is part of the run's recorded history
and a rebuild reads it rather than re-deciding. Confirm the stop setting inherits exactly that — the step reports
"met / not met (rule i, value found) / cannot be checked", and only the workflow decides fail-or-stop from the
block's setting. Content must be read in that step in any case; the workflow code itself may not read storage.

**OQ-2 — How a run records "stopped early" and why each block was skipped.** Today `pipeline_run_blocks.status`
has `skipped` with no reason, and a `completed` run has no "stopped at" marker. ADR-009 §4 preferred **a reason
beside the state** over new status values. ⚠️ **Decide together with PRD-017's open item** (ADR-009 §14b: how a
delivery that succeeded and was then undone is recorded) — both are new facts about how a block ended; one
migration should carry both. Two needs that shape the answer: the SPA's **list of runs** shows a "stopped at
<Validate>" label (D9) without opening each run, so the **run** needs its own "stopped at" marker, not only its
blocks; and runs from before this PRD keep their bare `skipped` blocks, which must read as **"reason not
recorded"** (R6) — a value or null that means exactly that, with no backfill.

**OQ-3 — How a reverse rule is written.** New rule kinds (`absent`, `not_contains`, `max_length`, …) or a
`negate` flag on any rule. Saved Validate blocks must keep their exact meaning.

**OQ-4 — Run inputs beyond URL.** Today a run input can only be a URL, and only Scrape's `url` is bindable. R2
needs number and text inputs bindable into a `compare` rule's value and a `contains` rule's text (and their
opposites), checked at trigger time. Confirm it is
additive for saved definitions (ADR-009 §14c says widening the bindable list is) and how the save-time type check
treats a value whose type is known only at run time.

**OQ-5 — The failure alert's mechanism and payload.** It must be sent when the run ends `failed`, even though the
Webhook block never ran — so it cannot be the block's normal step. The job lane's delivery record
(`webhook_deliveries`, one `WebhookDeliveryWorkflow` per row, the `webhook-sweep` safety net) already has R5's
properties: decoupled from the run, retried over the job window, SSRF per attempt, visible to the admin. Reusing it
means widening its references to pipeline runs (today `num_nonnulls(job_id, batch_id, crawl_id) = 1`, and `run_id`
points at job runs). Settle the exact `pipeline.failed` fields, and check the admin webhook views and the per-user
webhook meters (BUG-028's class of bug) for blindness to a new lane. ⚠️ The alert reuses the block's URL and
secret, read from the pinned version. ⚠️ **A run that could not be started** (R5) never ran its workflow, so the
workflow cannot send its alert: say who does — most likely the API's run trigger, in the same transaction that
marks the run `failed` — and how that path finds the pinned version's Webhook settings.

**OQ-6 — Config shape.** Three new optional config fields with defaults: Validate's **fail / stop** (default
fail), Validate's **keep / delete on stop** (default keep, meaningful only with stop — say whether setting it with
*fail* is refused or ignored), and the Webhook block's **finishes OK / the run breaks** (default finishes OK only;
neither ticked is refused). The catalog's own rule: add with a default, the config version does not move. Confirm,
and name them.

**OQ-7 — Changing the workflow code safely.** The stop path and the failure alert are new paths in
`PipelineWorkflow`, and skip reasons change what the status mirror writes. Per ADR-012, say which changes need
`patched()` and which replay histories to add — at least a stop, a pass, and a failure that sends an alert.

**OQ-8 — Deleting a stopped run's objects.** D10's *delete* removes objects charged to the user mid-run, so it must
go through the storage ledger (the only releaser) and keep counter and bucket in step, including when the delete
itself hits a storage fault (retry, and never leave a ledger row for a removed object or the reverse). Say what the
run's `result_path` holds afterwards, and confirm that the delete path's refuse-on-failure rule (a delete endpoint
503s if any object could not be removed) has a sensible equivalent inside a workflow.

---

## What this hands forward

| To | What it gets | What it must not assume |
|---|---|---|
| **PRD-018 (Monitors)** | A Validate set to stop, as the cost gate's mechanism; comparison values bindable to run inputs, to supply "last run's value"; `completed` + "stopped at" as the unchanged-page outcome; **stop with *delete*, so unchanged pages do not pile up against the storage limit** (the job lane's content-hash gate deleted the duplicate the same way); *the run breaks* for unattended runs. | That layer A defines "the previous run", or that a hash rule exists — both are PRD-018's (R3 says how a new rule kind is justified). |
| **PRD-017 (Delivery sinks)** | The skip-reason design (OQ-2), to settle with its "succeeded, then undone" status. *The run finishes OK* / *the run breaks* as a choice on **every message-sending block** — its Email block gets it, its bucket and database-row blocks do not — so several Webhook and Email blocks can each alert. An alert that waits until the run has completely finished, so it can report what rollback undid and left in place (PRD-017 defines that report). | That per-block conditions exist — deferred to PRD-017, where several effect blocks first appear. |

## Related

- `docs/project/phase4-prd/PRD-016-workflows-pipelines.md`: R2 (Validate, declarative rules; Webhook block), R3
  (run outcomes, cancellation), R4 (retry and budgets), R6 (the fourth exclusion: failure notification), OQ-1 (the
  halt-early obligation), OQ-10 (why conditionals are not part of Monitors)
- `docs/adr/ADR-009-workflow-engine-temporal.md`: §4 (block model, `skipped`), §6 (pinned versions), §14 / 14a /
  14c / 14d (this PRD's origin and brief), §15 / 15f (webhook as a step; failure notification)
- `docs/adr/ADR-012-workflow-code-versioning.md` (Draft): changing workflow code under replay
- `docs/project/phase4-backlog.md` §2: this PRD's row, and PRD-017's and PRD-018's
- Live code: `api/app/pipelines/catalog.py` (Validate's rules, Webhook config), `api/app/workflows/pipeline.py`
  (the chain), `api/app/workflows/activities/db.py` (`mirror_run_status`, which skips the remaining blocks)
