# PRD-017 — ScrapeFlow Workflows: Delivery Sinks

**Priority:** P2. This is layer C, which makes results actionable rather than just retrievable.
**Layer:** C (Delivery). It adds new block types to PRD-016's catalog, plus one new run behaviour: undoing
deliveries when a run fails partway.
**Source:** `workflows-scoping.md` §C; `phase4-backlog.md` §2 (this PRD's row and its inherited commitment);
ADR-009 §5 (effect blocks), §14b (the "succeeded, then undone" obligation), §15 / 15a–15e (a delivery is a step
the run waits for).
**Status:** **Draft — owner's section-by-section review complete (2026-10-30, 17 rounds).** Next: the Architect
pass. Not Accepted.
**Last updated:** 2026-10-30 (review round 17: D10–D11 — owner review complete)

> **Not blocked by PRD-019 (conditional execution), but coupled to it in one place.** Both PRDs add a new way
> for a block to *end*. PRD-019 adds "skipped because a Validate set to stop ended the run", and this PRD adds "delivered,
> then undone". The Architect is asked to settle both in **one** design and one migration (OQ-1 here, OQ-2 in
> PRD-019). PRD-019 also passed one question here: whether a single block can be skipped on its own condition.
> That is answered in D6.

> **Review round 1 (owner, 2026-10-30): Problem.** One decision: **on a failed run, ScrapeFlow's own copies are
> always kept; rollback touches only the user's destinations** (D12). PRD-019 D10's keep / delete choice stays
> on the stop alone — its delete exists so Monitors do not pile up a copy per unchanged page, which failures do
> not do, and a failed run's stored output is what the user inspects (and what any later re-delivery would read).
> Also: the difference between a failed pipeline Webhook block (fails the run) and a failed job webhook (the job
> stays `completed`) is stated in R4 as deliberate.
>
> Two more decisions in the same round. **The first irreversible delivery makes everything before it final**
> (D13): once a webhook or email has gone out, rollback no longer undoes the bucket or database deliveries before
> it, so a receiver is never told about a file ScrapeFlow then deletes. And **only `completed` is final** for a
> reader of the user's destinations (R3's reader's contract): rollback guarantees cleanup, not that nobody saw the
> data. **Re-delivery from stored outputs** is a named non-goal.

> **Review round 2 (owner, 2026-10-30): Goals and Non-goals.** Goal 5 reworded in plain terms; **Goal 8 added**
> (no sink reaches a private address — D9 had no goal); the fan-out non-goal corrected — a sink's position
> already decides what it delivers, and only reaching back past the previous block by name is deferred.

> **Review round 3 (owner, 2026-10-30): User stories.** The bucket → Postgres story's reason corrected — its
> downstream job read every file as it appeared, the reader round 1 says cannot be protected; it now acts on the
> run completing. Two stories added: the webhook receiver's (D13) and the failure email's (R1's *the run breaks*).

> **Review round 4 (owner, 2026-10-30): R1.** Five changes: the opening sentence corrected (a sink delivers what
> the block before it produced); the webhook payload gains an additive **`deliveries`** field (R4), because today's
> payload never says where the bucket file landed; the Postgres sink writes to the **user's own database, never
> ScrapeFlow's**, and its table needs a **unique key on (run id, block id)**, checked by the destination test (R5);
> **deleting in ScrapeFlow never reaches into a destination**. D12's "until the retention cleanup collects it" →
> "until removed": nothing collects or deletes a pipeline run's outputs today (filed as BUG-031).

> **Review round 5 (owner, 2026-10-30): R2.** A duplicate is the same **run id + block id** — with several
> Webhook blocks per run, the run id alone cannot tell a retry from a second block — so the webhook payload gains
> an additive **`block_id`** (R4) and emails carry both ids. An email's **Message-ID is fixed** by run id + block
> id. The Validate bullet now says its undo is subject to D13.

> **Review round 6 (owner, 2026-10-30): R3.** A rollback waiting between attempts **holds no slot**. **Already
> gone counts as undone** — a 0-row delete included — and the undo uses the location recorded at delivery, so a
> destination edited mid-run cannot turn "looked in the wrong place" into "undone". The failure alert **keeps
> waiting for rollback to finish** (PRD-019's call stands), with its cost stated: up to ~3 h late when an undo
> is retrying.

> **Review round 7 (owner, 2026-10-30): R4.** Each Webhook block keeps its **own, independent ~3 h retry
> window** — a shared window would punish one receiver for another's outage and break R4's "today's contract,
> unchanged". The in-order delay and the 24 h cap are stated. **Correction:** the PRD's "about 2.6 h" was the job
> webhook's horizon; the pipeline Webhook block's is about 3 h (`catalog.py`), fixed in four places.

> **Review round 8 (owner, 2026-10-30): R5.** The **Webhook block may reference a saved webhook destination**
> (URL + secret), inline form kept — so webhook secrets rotate in one edit too. **Deleting a destination in use is
> refused**, naming the pipelines (PM preference; Architect may overrule with a reason). Stated: each attempt uses
> the destination's **current credentials**; an undo uses the **recorded location**.

> **Review round 9 (owner, 2026-10-30): R6.** Walked through with a worked example (catalog figures: playwright
> scrape ~36 min, LLM extract ~24 min, Webhook ~3 h). Added: bucket and Postgres sinks **retry delivery on the
> Webhook block's order (~3 h)**, figures in the catalog; the **rollback reserve** is every reversible sink's full
> undo horizon; the **save-time message names the reserve separately**.

> **Review round 10 (owner, 2026-10-30): R7–R9.** R7 and R9 agreed as written. R8: a **webhook destination's URL
> is a credential** — shown host-only after save, to the owner and admins alike.

> **Review round 11 (owner, 2026-10-30): Success criteria.** Thirteen checks added, one for each rule rounds 1–10
> introduced without a test: D12's retained output, the payload's `block_id` / `deliveries`, deliveries surviving a
> user delete, no slot during an undo, already-gone and edited-destination undos, the reserve in the save-time
> refusal, webhook destinations (same payload, one-edit rotation), delete-in-use refused, host-only URLs, the
> connect-time address check, the Postgres unique key and 0-row undo, and the fixed email Message-ID.

> **Review round 12 (owner, 2026-10-30): Open questions.** Five brought up to date with rounds 1–11: OQ-1 (D13's
> kept state; each delivery's location recorded once at write time), OQ-2 (no slot while undoing), OQ-3 (webhook
> destinations, current credentials per attempt), OQ-5 (run id + block id), OQ-6 (sink retry horizons, the reserve).
> OQ-4 names BUG-030.

> **Review round 13 (owner, 2026-10-30): What this hands forward.** PRD-019's row gains the alert-delay knock-on
> (up to ~3 h, round 6) — recorded here, not in PRD-019, whose review is complete. PRD-018's row gains the D12 +
> BUG-031 storage warning. Related links BUG-024, BUG-030, BUG-031. **Every section is reviewed; decisions D1–D11
> remain.**

> **Review round 14 (owner, 2026-10-30): D1–D3** agreed as written — three slices (bucket + rollback + several
> webhooks, then Postgres, then email; no spreadsheet), every sink labelled undoable or not, undoable sinks first
> and the order enforced at save.

> **Review round 15 (owner, 2026-10-30): D4–D6** agreed as written — rollback on failure only (never on cancel
> or a Validate stop; narrowed by D13), several Webhook blocks allowed, per-block conditions deferred with the
> uncovered case named.

> **Review round 16 (owner, 2026-10-30): D7–D9** agreed as written — saved destinations (webhooks included, round
> 8), email through the user's own SMTP server, and no private destinations ever, a homelab LAN included: a home
> destination needs a public hostname. An operator allow-list, if a single-tenant install ever needs one, is its own
> later decision.

> **Review round 17 (owner, 2026-10-30): D10–D11** agreed — delivered bytes and sinks cost nothing extra; delivery
> is at least once, a duplicate being the same run id + block id (D11's wording brought up to round 5). **This
> completes the owner's section-by-section review of the draft.** Next: the Architect pass.

---

## What this PRD owes, and where each item is answered

| # | Obligation | From | Answered in |
|---|---|---|---|
| 1 | **Send one result to several destinations, with rollback.** PRD-016 capped layer A at one Webhook block and sent "several destinations" here; layer C owes it **with rollback attached**. | `phase4-backlog.md` §2, PRD-016 R2 | **R1–R3** |
| 2 | **How a block that succeeded and was then undone is recorded.** `pipeline_run_blocks.status` has no value for it. This must be settled **before C builds**. | ADR-009 §14b | **R3**, **OQ-1** |
| 3 | **Whether the one-Webhook-block cap lifts.** PRD-016: "it lifts when C ships, with rollback attached". | PRD-016 R2 | **R4**, D5 |
| 4 | **Conditions on a single block** ("write to the bucket always, email only if the price dropped"). | PRD-019 Non-goals | **D6**, Non-goals |
| 5 | **A sink waiting on a down destination must not hold the user's concurrency slot.** ADR-009 §15a decided this for every effect block, and every sink is one. | ADR-009 §15a | **R6** |
| 6 | **Cancelling while a sink waits between retries**, which can be hours. | ADR-009 §15d | **R6** |
| 7 | **No new admin meter that reads one lane and reports for all.** | ADR-009 §15e | **R8** |

## Decisions this draft makes, for the owner's review

| # | Decision | Where | Alternative rejected |
|---|---|---|---|
| D1 | **The first slice is one new sink, the S3-compatible bucket, plus rollback, plus lifting the Webhook cap.** A Postgres row sink is the second slice and email the third. A spreadsheet sink is **not** in this PRD. ✅ owner, review round 14 | R1 | Shipping all four sink kinds at once; leading with email because it is the most asked-for |
| D2 | **Sinks are either *reversible* (a bucket object, a database row ScrapeFlow inserted) or *irreversible* (a webhook, an email).** Each sink kind declares which it is, and rollback promises only what it can actually do. ✅ owner, review round 14 | R2 | Calling every sink "rolled back"; or trying to undo webhooks with a follow-up "please disregard" message in the first slice |
| D3 | **Irreversible sinks must come after every reversible sink in the chain. A pipeline that breaks this order is refused at save time.** ✅ owner, review round 14 | R2 | Reordering the user's blocks automatically; allowing any order and warning |
| D4 | **Rollback runs when the run *fails* after a reversible sink succeeded** (and, per D13, before any irreversible sink delivered). **It does not run on cancel, and it does not run when a Validate set to stop ends the run.** Cancel means "stop", not "undo". ✅ owner, review round 15 | R3 | Rolling back on cancel too |
| D5 | **The one-Webhook-block cap lifts.** Several webhooks are allowed. Webhooks stay irreversible, and D3's ordering plus per-block reporting is what makes that honest. ✅ owner, review round 15 | R4 | Keeping one webhook per pipeline and allowing several of the new sinks only |
| D6 | **Conditions on a single block stay deferred.** Under D3 the irreversible sinks form the tail of the chain, so a Validate set to stop (PRD-019) in front of that tail covers "bucket always, email only if the price dropped". What remains uncovered is named in Non-goals. ✅ owner, review round 15 | Non-goals | Specifying per-block conditions here |
| D7 | **Destination credentials are saved once as named, per-user *destinations*, stored encrypted, never returned by the API, and referenced from blocks by id.** This works like LLM keys today. The Webhook block may use one too (its inline form keeps working — round 8). ✅ owner, review round 16 | R5 | A secret typed inline into each block's config, as the Webhook block's signing secret is today |
| D8 | **Email uses the user's own mail server** (SMTP credentials the user brings). ScrapeFlow does not operate a mail relay. ✅ owner, review round 16 | R1 | A ScrapeFlow-run relay. In a multi-tenant system that relay would let any user send mail to anyone, from us |
| D9 | **No private destinations, ever.** The SSRF rule applies to every sink with a network address, at save time and at every attempt, including a user's own LAN bucket. ✅ owner, review round 16 | R5 | An allow-list or a homelab exception |
| D10 | **Bytes written to a user's destination are not charged to their ScrapeFlow storage, and a sink costs no monthly run.** ✅ owner, review round 17 | R7 | Charging delivered bytes against `storage_bytes_used` |
| D11 | **Delivery is at least once.** A retry never writes a *second copy* to a reversible sink (it overwrites or upserts the same key). Webhooks and emails may arrive twice after an ambiguous failure; a duplicate is the same **run id + block id** — the webhook payload carries `block_id` (R4), and an email's Message-ID is fixed by both (R2). ✅ owner, review round 17 (wording updated to round 5) | R2 | Promising exactly-once for every sink |
| D12 | **On a failed run, ScrapeFlow's own copies are always kept.** Rollback undoes deliveries to the user's destinations only; what earlier blocks stored in ScrapeFlow stays stored, charged and retrievable per block until removed, as PRD-016 R3 already requires. ✅ owner, review round 1 | R3 | A keep / delete choice on failure, mirroring PRD-019 D10's choice on stop |
| D13 | **The first irreversible delivery makes everything before it final.** Once a webhook or email has gone out, the reversible deliveries before it are never undone; a later failure still fails the run and is reported plainly. Rollback happens only while nothing has been announced to anyone. ✅ owner, review round 1 | R2, R3 | Rolling back on any failure, which can delete a file a webhook has just told a receiver about |

---

## Problem

Today a pipeline result has **two places to go**: ScrapeFlow's own storage, where the user fetches it through
the API, and **one** webhook, which tells a receiver the result exists and where it is stored. The webhook
does not carry the content. Everything else, such as putting the extraction in the user's own bucket,
appending it to their database, or emailing a summary, needs code the user writes and hosts outside
ScrapeFlow. No bucket, database, spreadsheet or email code exists anywhere in ScrapeFlow
(`workflows-scoping.md` §C, re-checked against `api/app/` on 2026-10-29).

Two things make "just add more destinations" the wrong answer on its own:

**1. Several deliveries can half-succeed.** PRD-016 capped layer A at **one** Webhook block for exactly this
reason. Delivery is a step the run depends on, so if the second of two deliveries fails, the first has already
happened and the run reports `failed` having half-delivered. The user's bucket holds a file whose matching
database row was never written, and nothing tells the user that the file is now an orphan. This layer exists to
add the destinations **and** the undo together, so users never learn a half-delivered pattern that would later
have to be taken away.

**2. Some deliveries cannot be undone, and pretending otherwise is worse than not trying.** A bucket object can
be deleted. A row ScrapeFlow inserted can be deleted if ScrapeFlow knows its key. A webhook cannot be unsent,
and neither can an email. A "rollback" that silently skips the parts it cannot undo is the quiet lie PRD-016's
per-block status exists to eliminate (PRD-016 R3).

## Goals

1. A user can deliver one run's result to **several destinations** in one pipeline.
2. When a run fails after some deliveries succeeded, **every delivery that can be undone is undone** — unless a
   webhook or email has already announced it (D13) — and the user can see per block what was delivered, what was
   undone, and what could not be undone.
3. **Nothing irreversible happens before something that might still need undoing.** The order of blocks
   guarantees this, not luck.
4. Destination credentials are **entered once, stored encrypted, never shown again**, and reused across
   pipelines.
5. **A retry never adds a second copy.** A retried bucket or database write overwrites the same object or row
   (ScrapeFlow picks its name), however many times it is retried.
6. A destination that is down **does not lock the user out**: no concurrency slot is held while a sink waits
   between retries.
7. Pipelines saved before this PRD, and jobs, batches, crawls and schedules, keep working unchanged.
8. **No sink can reach a private or internal address**, checked when the destination is saved and on every
   attempt (D9). A sink is a way for any user to make ScrapeFlow open a connection to an address they choose.

## Non-goals

- **A spreadsheet sink.** Every hosted spreadsheet needs an OAuth app registered with one vendor, plus token
  refresh and per-user consent. That is its own product and security decision. Revisit after slice 3.
- **Data-warehouse and cloud-specific sinks** beyond an S3-compatible bucket and a Postgres database, such as
  managed analytics databases and message queues. Each is additive later as a new sink kind.
- **Mapping fields to columns** in the database sink. The first database slice writes one row in a fixed,
  documented shape (R1). Choosing columns per field comes later.
- **Undoing a webhook or an email** by sending a follow-up retraction ("disregard run X"). It is a plausible
  later addition for webhooks. It is not rollback, and the first slices do not pretend it is.
- **Conditions on a single block** (D6). Deferred again, with the case it leaves uncovered stated plainly: **two
  irreversible sinks with different conditions** (email if the price dropped, webhook if it rose). A Validate set to stop can
  only stop the *rest* of the chain. Revisit when a user needs exactly that, or with Monitors (PRD-018) if its
  approval step needs it.
- **Rollback on cancel** (D4).
- **Re-delivering a failed run from its stored outputs** ("deliver run X again" without re-scraping or
  re-extracting). ScrapeFlow keeps a failed run's outputs (D12), so nothing blocks adding it later; jobs already
  have an admin-only retry of a single webhook delivery. **The cost of leaving it out:** after a failed delivery
  the only way to deliver is a fresh run, which scrapes again (the page may have changed or now be walled), pays
  for the LLM call again on the user's own key, and uses another monthly run.
- **Data-flow fan-out** (a block choosing an earlier block's output by name). A sink passes its input through
  unchanged (ADR-009 §5), so **its position in the chain decides what it delivers**:
  `scrape → bucket → extract → bucket → webhook` sends the raw page to the first bucket and the extraction to
  the second bucket and the webhook. What is deferred is a block reaching back past its neighbour ("webhook,
  send the page, not the extraction"), which the save-time check refuses today. Fan-out stays deferred past
  Phase 4.
- **Sinks on jobs, batches or crawls.** Destinations are a pipeline feature. Jobs keep their single webhook.
- **A visual editor in the SPA.** Authoring stays API-first. The SPA shows destinations (names and kinds, never
  secrets) and per-block delivery state.
- **MCP tools for sinks.**

## User stories

**As a data engineer**, I want *scrape → extract → put the JSON in my bucket → webhook my ingest service*, so
the file is already in place when my service is told.

**As a data engineer**, I want *… → bucket → my Postgres table*, and **if the database write fails, I don't
want the bucket file left behind**, so that once the run has finished, my bucket and my table agree. My
downstream job picks up a run's files when the run completes, not the moment a file appears (R3's reader's
contract).

**As the service a webhook notifies**, I want any file the webhook told me about to **still be there**, even if a
later step in the run fails (D13).

**As a user reading a failed run**, I want to see "bucket: delivered, then undone; database: failed —
connection refused; webhook: not attempted", so I know exactly what state my systems are in.

**As a user whose bucket refused the undo** (the key cannot delete), I want the run to tell me **which object
was left behind and where**, so I can remove it myself.

**As a user with twelve pipelines writing to one bucket**, I want to rotate the bucket's key **once**, not edit
twelve pipelines.

**As a user who wants an email**, I want *… → bucket → email me a summary*, sent through my own mail server,
and the email should arrive only if everything before it worked.

**As a user with unattended pipelines**, I want **an email when a run breaks**, naming the failed step and what
was undone, sent even if the run broke before reaching the email step (R1, PRD-019 R5).

**As an operator**, I want no sink to be able to reach an internal address, so a pipeline cannot be used to
probe ScrapeFlow's own network.

---

## Requirements

### R1 — The sink catalog, in slices

Every sink is an **effect block** (ADR-009 §5). It reads the content its input refers to, delivers it, stores
nothing in ScrapeFlow, and passes its input on unchanged. So a sink delivers whatever the block before it
produced, and several sinks in a row deliver the same thing.

| Slice | Sink | Delivers | Reversible? | Accepts |
|---|---|---|---|---|
| **1** | **S3-compatible bucket** | the input's content as one object, under a prefix the user chooses, with a key that includes the run and block so it is unique per run | ✅ delete the object | any page or extraction |
| **1** | **Webhook** (exists; the cap lifts, R4) | the signed `pipeline.result` notification, plus two added fields — `block_id`, and `deliveries` saying where the sinks before it wrote (R4) | ❌ | any |
| **2** | **Postgres row** | one row in a table the user created **in their own database** (never ScrapeFlow's), in a fixed documented shape: run id, pipeline id, block id, delivery time and the content as JSON, with a **unique key on (run id, block id)**. ScrapeFlow never creates or alters the user's tables. | ✅ delete the row by its run/block key | JSON only (an extraction, or a page scraped as JSON) |
| **3** | **Email** | a message to the addresses on the destination, through the user's own SMTP server (D8): a subject naming the pipeline and run, the content inline up to a size cap, and above the cap a note saying it was too large | ❌ | any |

- **Email is a message-sending block, so it gets PRD-019 R5's two choices**, exactly as the Webhook block does:
  *the run finishes OK* (default; the result, as above) and/or *the run breaks* (a `pipeline.failed` message
  naming the failed block and the reason, sent even when the run failed before reaching the Email block).
  The **bucket** and **Postgres row** blocks store data and do **not** get the choice: writing "it broke" into
  the user's storage tells nobody.

- **Each slice ships on its own** and is useful on its own. Slice 1 alone delivers the layer's defining
  promise: bucket → webhook, with the bucket undone if the webhook fails.
- **A sink delivers content, not a pointer.** This differs from the Webhook block, which sends a ScrapeFlow
  storage path the receiver cannot open without the API. A bucket object or database row is useless unless it
  holds the result itself.
- **The Postgres sink writes to a database the user owns**, reachable from the internet (D9), and **never to
  ScrapeFlow's own database**, which holds every tenant's data and sits at an internal address D9 refuses anyway.
  Its table must carry a **unique key on (run id, block id)**: a retry overwrites that row instead of adding a
  second (Goal 5), and rollback deletes exactly that row. The destination test (R5) checks the key exists.
- **Deleting in ScrapeFlow never reaches into a destination.** Deleting a run, a pipeline or a user removes
  ScrapeFlow's own data only; what sinks delivered belongs to the user's systems. Only rollback, during a failed
  run, removes a delivery.
- **Save-time checks** (PRD-016 R1's rule: refused with a message naming the block and reason): a sink's input
  type must suit it (the Postgres sink needs JSON); its destination must exist and belong to the user; and the
  ordering rule in R2 holds.

### R2 — Reversible and irreversible, and the ordering rule

- **Each sink kind declares whether it can be undone** (the table in R1). That declaration is part of the
  catalog, visible to users in the API and docs, not something they must infer.
- **Irreversible sinks come last** (D3). In the chain, no reversible sink may follow an irreversible one. A
  pipeline that breaks this is **refused at save time**, with a message naming both blocks and saying why:
  "`bucket` cannot follow `notify`: if `bucket` failed, `notify` could not be undone."
  *Why refuse and not reorder:* the chain is the user's statement of order, and silently changing it means the
  saved pipeline no longer says what runs. *Why refuse and not warn:* a warning nobody reads produces the
  half-delivered state this layer exists to prevent.
  *What this still allows:* `bucket → db → webhook → email`. All reversible work happens first, so a failure
  anywhere before the webhook can still be fully undone.
- **The first irreversible delivery is the point of no return** (D13). Once the webhook in that chain has gone
  out, the bucket object and the row are final: a later failure (the email) fails the run but undoes nothing,
  because the webhook has already told a receiver they exist.
- **What remains possible, stated so it is not discovered:** two irreversible sinks in a row can still
  half-deliver (the webhook arrived, then the email failed). Nothing can undo that. The run reports it plainly
  (R3), and the failure notification (PRD-019 R5) says so.
- **Validate blocks**, set to fail or to stop, may sit anywhere PRD-016 and PRD-019 allow. A Validate block that fails after a
  bucket write fails the run, which triggers the undo (R3), unless a webhook or email has already gone out (D13).
  It is a failure like any other.
- **Retries never duplicate on a reversible sink** (D11). A retried bucket write lands on the same key, and a
  retried row write lands on the same row. **Webhooks and email are at least once:** after an ambiguous failure
  (the request was sent but no answer came back) they may arrive twice. **A duplicate is the same run id and
  block id**: with several Webhook blocks allowed (R4), two blocks of one run share a run id, so the run id
  alone cannot tell a retry from a second block. Every webhook payload therefore gains an additive **`block_id`**
  field, and every email carries both ids. This is today's webhook contract, extended to email.
- **An email's Message-ID is fixed by its run id and block id**, so a retried email repeats the first one's
  Message-ID and many mail systems drop or merge the duplicate on their own. The exact format is the
  Architect's.

### R3 — Rollback, and what the user sees

- **When it runs.** When a run **fails** after one or more reversible sinks have delivered, ScrapeFlow undoes
  each of those deliveries, **latest first**. It does not matter what caused the failure: a later sink, a
  Validate rule, or the run's time budget running out. **Only if no irreversible sink has delivered yet** (D13):
  after a webhook or email has gone out, the reversible deliveries before it stay, and the run fails without
  undoing anything.
- **When it does not run** (D4):
  - **Cancel.** The run stops at the next block boundary as PRD-016 R3 already says, and whatever was delivered
    stays delivered. "Stop" and "undo what you did" are different intentions, the same distinction ScrapeFlow
    already draws between cancelling a run and retiring a schedule. A user who wants the undo can delete the
    object themselves, and the run tells them where it is.
  - **A Validate set to stop ending the run** (PRD-019). The run *completed*, so there is nothing to undo.
  - **A run that completes.**
- **The run's outcome stays `failed`.** Rollback is cleanup after a failure, not a fourth outcome. The run
  names the block that failed, as PRD-016 R3 requires.
- **Rollback never touches ScrapeFlow's own copies** (D12). What earlier blocks stored in ScrapeFlow — the
  scraped page, the extraction — stays stored, charged and retrievable per block, so the user can see what the
  run produced. PRD-019 D10's *delete* applies to a stop only, never to a failure.
- **The reader's contract: only `completed` is final.** Rollback guarantees cleanup, not that nobody saw the data.
  A reader watching the user's bucket or table may see a delivery that rollback later removes; one that must
  only ever see complete deliveries acts on the run being `completed`, or on the last block being a webhook. The
  documentation states it per outcome:
  | Run is… | What is in the user's destinations |
  |---|---|
  | running | provisional — may be removed if the run fails before any webhook or email goes out |
  | completed (including a Validate stop) | final; ScrapeFlow never touches it again |
  | failed | removed, unless the block reads **undo failed: left in place** (with the location) or a webhook or email had already gone out (D13) |
  | cancelled | left as is (D4); the run shows where |
- **While undoing, the run says so.** Before it reaches its final `failed`, the run reports that it is
  **undoing deliveries**, how many remain, and an upper bound on how long that can take. This is the standard
  PRD-016 R3 set for a pending cancellation, for the same reason: a run that looks finished while it is still
  deleting things is lying.
- **A cancel request during rollback** is acknowledged but **does not stop the rollback**. Undoing is cleanup
  the user benefits from, and stopping halfway would leave a state nobody chose.
- **Each sink block ends in exactly one of these states, and the user can tell them apart** (obligation 2):
  | State | Meaning |
  |---|---|
  | delivered | it worked, and the run did not need it undone — or the run failed after a webhook or email had already announced it (D13), which the block says |
  | **delivered, then undone** | it worked, and rollback removed it |
  | **undo failed: left in place** | it worked, rollback could not remove it, and **the exact location is shown** (bucket + key, or table + row key) so the user can remove it |
  | failed | this block's own delivery failed |
  | not attempted | an earlier block failed, the run was cancelled, or a Validate set to stop ended the run (PRD-019's skip reasons) |
  | delivered (irreversible) | a webhook or email went out. If the run then failed, this says **"could not be undone"** rather than looking like a success that stood |
- **Rollback retries.** An undo that fails for a transient reason (the destination is down) is retried over a
  horizon of the same order as the Webhook block's (about 3 hours). Then it gives up and records "undo
  failed: left in place". An undo the destination refuses outright (the credential has no delete permission)
  gives up at once, with that reason.
- **A rollback waiting between attempts holds no concurrency slot**, exactly as a sink waiting between retries
  does not (Goal 6, R6). A run whose undo is retrying against a down destination for hours does not lock the
  user out.
- **Already gone counts as undone.** If the delivery is no longer there when the undo runs (the user or a
  lifecycle rule removed it), the block reads **delivered, then undone**: a bucket delete of a missing object
  succeeds, and a row delete that finds **0 rows** is a success too, since the (run id, block id) key belongs to
  this delivery alone. **The undo uses the location recorded when the delivery was written**, not the
  destination's current settings; if the destination's address or table was changed since, a 0-row result is not
  trusted and the block reads **undo failed: left in place**, showing the original location.
- **The failure notification (PRD-019 R5) fires once rollback has finished**, and says what was undone and what
  was left in place. Its delivery still never holds the run open. **Accepted, stated so it is not discovered
  (owner, review round 6):** when an undo is retrying against a down destination, the alert waits with it and
  can arrive up to about 3 hours after the run broke. The alert always gives the final picture; a capped wait
  that sends "cleanup still in progress" was considered and not taken.

### R4 — The Webhook cap lifts

- **A pipeline may contain several Webhook blocks** (D5), as PRD-016 R2 promised once layer C shipped. The
  save-time refusal and its message go away.
- They are irreversible sinks, so R2's ordering rule places them after every reversible sink. Two webhooks in a
  row can still half-deliver, and the run reports it as R3 describes.
- Every Webhook block keeps today's contract **unchanged**: payload, signing, about 3 h of retries, SSRF on
  every attempt, and holding no slot while it waits. Receivers built against the layer-A webhook do not notice
  this PRD.
- **Two fields are added to the payload, nothing removed:**
  - **`block_id`** — which Webhook block sent it. With several per run, run id + block id is what identifies a
    duplicate (R2).
  - **`deliveries`** — what the sinks before this webhook wrote and where (block id, sink kind, location — bucket
    + key, or table + row key; never credentials). Today's payload names only a ScrapeFlow storage path, so a
    receiver told "the file is in place" (the first user story) could not find it without rebuilding the key.
    An empty list when no sink came before.

  Receivers that ignore unknown fields are unaffected.
- **Each Webhook block has its own, independent retry window of about 3 hours** (5 attempts, waits of 30 s,
  5 min, 50 min and 2 h). Windows are not shared: a receiver gets the same chance whether or not another webhook
  in the run had trouble. **Stated so it is not discovered:** the chain runs in order, so a down receiver delays
  the next webhook by up to ~3 h, and if it gives up the next is **not attempted**. Sending several webhooks at
  once is fan-out, deferred (Non-goals). A run's total length is capped by the existing save-time budget check
  (24 h run limit): about seven Webhook blocks fit, and a chain that cannot fit is refused at save with its
  totals.
- A pipeline-wide limit on the number of sink blocks is the existing blocks-per-pipeline limit. No new limit is
  added.
- **A failed Webhook block fails the run; a failed job webhook does not.** A job's webhook is a separate
  delivery after the run has finished, so the job stays `completed`. In a pipeline the user placed the webhook
  inside the chain, so its failure is the run's failure and triggers rollback (R3). The difference is
  deliberate and is documented for users, so nobody files it as a bug.

### R5 — Destinations, credentials and egress

- **A destination is a named, per-user saved connection**: its kind (bucket, Postgres, SMTP, webhook), its
  address, and its credentials (D7). Sink blocks reference a destination by id. One destination can serve many
  pipelines, and rotating its credentials is one edit.
- **The Webhook block may reference a saved webhook destination** (URL + signing secret) instead of carrying them
  inline. The inline form keeps working, so pipelines saved before this PRD are unchanged; a webhook destination
  gives webhooks the same one-edit secret rotation as every other sink. Both forms send the same payload.
- **Each delivery attempt uses the destination's current credentials**, so a key rotated mid-run takes effect on
  the next attempt without editing any pipeline. **An undo uses the location recorded when the delivery was
  written** (R3), not the destination's current address or table.
- **Credentials are write-only.** They are stored encrypted, never returned by any user or admin API, never put
  in workflow history in plain text, and never written to logs. This is the standard LLM keys and job secrets
  already meet.
- **A destination can be tested on demand.** The test checks that ScrapeFlow can reach it, write, and **delete**
  (the permission rollback needs). It reports which of those failed. A destination whose credentials cannot
  delete may still be used, but the user is told **before saving** that rollback to it will fail. For a Postgres
  destination the test also checks that the table has the **unique key on (run id, block id)** (R1); without it
  a retry adds a duplicate row, so that destination is refused.
- **No private destinations** (D9). Every address a sink connects to (a webhook URL, a bucket endpoint, a
  database host, a mail server) is checked against ScrapeFlow's SSRF rule **when the destination is saved and
  at every delivery attempt**. A refusal is final and never retried, as it already is for the Webhook block
  (ADR-009 §15). This includes a user's own LAN or homelab address. ScrapeFlow is multi-tenant, and a sink is
  a way for a user to make ScrapeFlow open a connection.
  - The check must cover **the address actually connected to**, not only a lookup made just before connecting.
    Otherwise a hostname that changes its answer between the check and the connection (DNS rebinding) gets
    through. See OQ-4: today's webhook check resolves the name separately from the request.
  - **Redirects are not followed** by any sink.
- **Deleting a destination that pipelines use is refused**, and the refusal names them: any version of a
  pipeline that is not deleted (a run can be triggered on a chosen version), and any run still in flight,
  including one that is undoing. The user re-points or deletes those pipelines first. *PM preference; the
  Architect may overrule it with a reason (OQ-3).* The hard rule either way: a run never fails with an
  unexplained error because its destination disappeared — the alternative is a run that fails at its trigger
  saying the destination was deleted.

### R6 — Waiting, time budgets and cancellation

- **A sink waiting between retries holds no concurrency slot.** This was decided for every effect block in
  ADR-009 §15a, and sinks are effect blocks. A user whose bucket is down for two hours is not locked out of
  starting other runs.
- **Every sink declares a time budget** (PRD-016 R4). **A bucket or Postgres sink retries its delivery on the
  same order as the Webhook block (about 3 hours)**; the exact numbers are the Architect's, and each sink kind's
  figure appears in the catalog so users can work out their totals.
- **Rollback's time is part of the run's budget.** The run reserves enough to undo every reversible sink in the
  chain, each at its full undo horizon, newest first. A run ceiling that would cut rollback short is refused at
  save time, as an under-sized ceiling already is. The undo is the last thing that must not be cut off silently:
  a run killed mid-rollback leaves deliveries in place **and** never records "undo failed: left in place".
  *Worked example:* `scrape → extract → bucket → db → webhook` needs about 10 h forward (36 min + 24 min + 3 × 3 h)
  plus a 6 h reserve (two undos × 3 h) = 16 h, inside the 24 h run limit.
- **The save-time message shows the reserve separately**, so a user adding up their blocks can see where the
  extra time comes from: *"blocks need 28 h (19 h of blocks + 9 h to undo 3 deliveries); the limit is 24 h"*.
- **Cancel while a sink waits between retries ends the wait** (ADR-009 §15d, already built for the Webhook
  block). An attempt already in progress finishes, and no further attempt starts.
- **Retry lives in one visible layer** (PRD-016 R4). Any client library used to reach a destination must not
  retry beneath the block's own retry policy.

### R7 — Metering

- **A sink costs no monthly run unit.** It fetches nothing, and the unit is one fetch (PRD-016 OQ-4).
- **Bytes written to a user's own destination are not charged to `storage_bytes_used`** (D10). That meter
  measures what ScrapeFlow stores. The content being delivered is already stored and already charged, once.
- Email and webhook deliveries cost no quota, consistent with webhooks today.
- **Metering parity still holds**: a pipeline does not cost more because it has more sinks.

### R8 — Visibility and admin

- **The run view** in the API and the SPA shows each sink's state from R3's table, live, as it shows other
  block states today.
- **Admins** see destinations (names, kinds and addresses, **never credentials**), sink states and rollback
  outcomes wherever they already see pipeline runs.
- **A webhook destination's URL is treated as a credential.** Many carry their secret in the path
  (`https://hooks.slack.com/services/T0001/B0002/<token>`), and anyone holding the URL can post to the channel. After
  save it is shown **host only** (`https://hooks.slack.com/…`), to the owner and to admins alike; changing it means
  pasting a new one, as with any credential.
- **No meter may count one lane and report for all** (ADR-009 §15e). Any new delivery statistic either covers
  every lane that delivers or is named for the lane it covers. The existing job-lane webhook meters stay
  job-lane only, under names that say so.

### R9 — Compatibility

- **Additive.** Pipelines saved before this PRD run unchanged. A pipeline whose only delivery is one Webhook
  block behaves exactly as it does today.
- **Runs in flight at release finish on the behaviour they started with** (ADR-012: changing workflow code).
- Jobs, batches, crawls, schedules and their webhooks are untouched.

---

## Success criteria

**Slice 1: bucket + rollback + several webhooks**
- [ ] *scrape → extract → bucket → webhook*: on success the object is in the user's bucket under the chosen
      prefix, holding the extraction, and the webhook arrives. The run reports both as delivered.
- [ ] Same pipeline with the webhook receiver refusing: once the webhook block gives up, the bucket object is
      **deleted**, the bucket block reads **delivered, then undone**, the webhook reads **failed**, and the run
      ends **`failed`** naming the webhook block.
- [ ] While the undo is pending, the run reports **undoing deliveries** with a count and an upper bound, not
      `failed`.
- [ ] With a bucket credential that cannot delete: the run ends `failed`, the bucket block reads **undo failed:
      left in place** with the **exact bucket and key**, and the destination test had warned about the missing
      delete permission before save.
- [ ] A bucket write retried after a lost response produces **one** object, not two.
- [ ] *bucket → webhook → bucket* is **refused at save**, naming both blocks and the reason. *bucket → bucket →
      webhook → webhook* saves and runs.
- [ ] Cancelling during the webhook's retry wait ends the run `cancelled` within seconds. The bucket object
      **stays**, and the run says where it is.
- [ ] A Validate set to stop (PRD-019) ending a run after the bucket block leaves the object in place, and the run is
      `completed`.
- [ ] Two webhooks: the first delivered and the second failed reports the first as **delivered (irreversible)
      — could not be undone**.
- [ ] *bucket → webhook → webhook* with the second webhook failing: the bucket object **stays** (D13), the bucket
      block reads **delivered** and says a webhook had already announced it, and the run ends `failed` naming the
      second webhook.
- [ ] A bucket endpoint, or webhook URL, that resolves to a private address is refused at save **and** at
      delivery time, and is not retried.
- [ ] While a bucket sink waits on a down endpoint, the user can start another run up to their full
      concurrency limit.
- [ ] Destination credentials never appear in any API response, workflow history or log line (checked by
      searching for a known test secret).
- [ ] Bytes delivered to the bucket do not change `storage_bytes_used`, and the run consumes exactly one
      monthly unit.
- [ ] Every pipeline saved before release runs unchanged, and the pipeline test suite and replay corpus pass
      unchanged.
- [ ] After a failed, rolled-back run, the extraction is still retrievable from ScrapeFlow per block (D12).
- [ ] The webhook payload carries `block_id` and `deliveries` (naming the bucket and key the bucket block wrote);
      a receiver that ignores unknown fields still accepts it (R4).
- [ ] After an admin deletes the user, an object already delivered to their bucket is still there (R1).
- [ ] While an **undo** waits on a down bucket, the user can start runs up to their full concurrency limit (R3).
- [ ] Undo of an object the user already deleted reads **delivered, then undone** (R3).
- [ ] With the destination's bucket changed mid-run, the undo reads **undo failed: left in place** with the
      **original** location (R3).
- [ ] A chain that cannot fit the 24 h run limit is refused at save, with the undo reserve shown separately (R6).
- [ ] A Webhook block using a saved destination sends the same payload as an inline one; after the secret is
      rotated once, the next delivery is signed with the new secret (R5).
- [ ] Deleting a destination in use is refused, naming the pipelines that use it (R5).
- [ ] After save, a webhook destination's URL is shown host-only, to the owner and to an admin (R8).
- [ ] A host that resolves public at the check and private at the connection is still refused: the test
      covers the connection, not only the lookup (R5, BUG-030).

**Slice 2: Postgres row**
- [ ] A row with the documented shape lands in the user-created table. A retry produces **one** row. A later
      failure deletes it.
- [ ] A Postgres sink after an HTML page is refused at save (JSON only).
- [ ] ScrapeFlow never issues DDL against the user's database.
- [ ] A table without the unique key on (run id, block id) is refused by the destination test (R1, R5).
- [ ] An undo whose delete finds **0 rows** reads **delivered, then undone** (R3).

**Slice 3: email**
- [ ] An email arrives through the user's own SMTP server, naming the pipeline and run, with the content inline
      under the cap and a "too large" note above it.
- [ ] An email sink before a reversible sink is refused at save.
- [ ] An Email block set to *the run breaks* sends one failure message when an earlier block fails, after any
      rollback has finished, naming the failed block and what was undone and left in place. Set to *finishes OK*
      only, it sends nothing on failure.
- [ ] A retried email keeps the first one's Message-ID, and every email carries the run id and the block id (R2).

---

## Open questions for Architect

**OQ-1 — Recording "delivered, then undone" and "undo failed" (ADR-009 §14b), together with PRD-019's skip
reasons.** `pipeline_run_blocks.status` is `pending / running / waiting / completed / failed / skipped`. R3
needs more facts: undone, undo failed (with a location), kept after a failure because a webhook or email had
already announced it (D13), and, through PRD-019, *why* a block was skipped. **Each delivery's location is recorded
when it is written** — the undo uses it (R3), the webhook's `deliveries` field reports it (R4), and "undo failed:
left in place" displays it — so it is one record, not three.
ADR-009 §4 preferred **a reason beside the state** to new status values. ⚠️ **Decide this jointly with PRD-019
OQ-2, in one migration.** Remember too that the run-counting views read `pipeline_run_blocks.status`, and
Postgres refuses to alter a column those views depend on without dropping and recreating both views
(CLAUDE.md, *Run-counting views*).

**OQ-2 — Where rollback runs.** The compensation sequence (latest first, its own retries, never cut off by the
run ceiling) lives in or around `PipelineWorkflow`. Say how it composes with the existing failure path, what
`patched()` it needs under ADR-012, and which replay histories to add: at least a full undo, an undo failure, a failure after a webhook that undoes nothing (D13),
and a cancel that does not undo. **An undo waiting between attempts holds no concurrency slot** (R3): the slot
rule is "run `running` **and** a block `running`", so say which block, if any, counts as running while undoing.

**OQ-3 — The destination model.** Storage of saved destinations and their encrypted credentials, following
`user_llm_keys` as precedent. How a pinned pipeline version refers to one (ADR-009 §6). Delete-while-referenced
behaviour (R5 states the PM's preference: refuse, naming the pipelines). How the on-demand test is built without leaving debris in the user's bucket. **Webhook destinations** too
(R5): URL + signing secret, the URL stored and treated as a credential and shown host-only after save (R8); the
Webhook block's inline form keeps working. Each delivery attempt reads the destination's **current**
credentials.

**OQ-4 — Egress checks for non-HTTP sinks, and the check-versus-connect gap.** `validate_no_ssrf_core` resolves
a hostname with `getaddrinfo` and returns. The HTTP client then resolves it again to connect, so a hostname
that changes its answer in between passes the check. That is the DNS-rebinding case the per-attempt check
exists for, and it applies to the Webhook block today. Sinks add a bucket client, a Postgres driver and an SMTP
client, each of which resolves names on its own. Design one way for every sink to connect **to the address
that was checked** (filed for the Webhook block as **BUG-030**). Also note **BUG-024**: the check admits `100.64.0.0/10` (carrier-grade NAT) and multicast,
and every new sink inherits that until it is fixed.

**OQ-5 — Idempotency keys per sink.** The bucket object key (user prefix + run id + block id), the Postgres
row's unique key on (run id, block id), and for webhooks and email the **run id + block id** a receiver uses to
spot a duplicate (the payload's `block_id`, R4; email's fixed Message-ID, R2). Confirm
each against retry-after-lost-response.

**OQ-6 — Time budgets.** Per-sink timings in the catalog (the Webhook block's are 20 s per attempt, 5
attempts, a 30 s first wait growing ×10 and capped at 7200 s), bucket and Postgres delivery retries on the Webhook block's order (~3 h, R6), the rollback reserve inside the run
ceiling — the sum of every reversible sink's full undo horizon (`pipeline_max_run_seconds` is 86,400 today) — and
the save-time composition check, which names the reserve separately (R6).

**OQ-7 — Which worker reaches the outside world.** The Webhook block runs on the workflow worker's queue today.
Bucket, database and mail clients are heavier and are the platform's egress to user infrastructure. Say
whether sinks get their own queue, and what network policy confines it.

**OQ-8 — Content size.** What a sink reads from ScrapeFlow storage can be several MB (pages measured up to
4.1 MiB). Settle the streaming approach and the email inline cap.

---

## What this hands forward

| To | What it gets | What it must not assume |
|---|---|---|
| **PRD-019 (conditional execution)** | The decision (D6) that per-block conditions stay deferred, and why a Validate set to stop covers the irreversible tail. A joint design for block end-states (OQ-1 ↔ PRD-019 OQ-2). A request that the `pipeline.failed` payload (PRD-019 R5) report what rollback undid and left in place. **A knock-on PRD-019 does not state:** that alert waits for rollback to finish (R3, owner round 6), so it can arrive up to ~3 h after the run broke when an undo is retrying against a down destination. | That rollback runs when a Validate stops the run. It does not. |
| **PRD-018 (Monitors)** | Sinks as the way a monitor delivers. Reversible sinks under a monitor still undo on failure. **A warning:** every failed run keeps its ScrapeFlow copies, charged (D12), and nothing can free a pipeline run's outputs yet (**BUG-031**) — a monitor that keeps failing piles up storage with no way out. | That a monitor's approval step can undo an irreversible sink. That a run's outputs can be freed before BUG-031 is fixed. |
| **A later spreadsheet / warehouse PRD** | The destination model, the reversible/irreversible declaration and the ordering rule, which every new sink kind slots into. | That OAuth-based destinations fit R5's credential model unchanged. |

## Related

- `docs/project/phase4-prd/PRD-016-workflows-pipelines.md`: R2 (the one-Webhook cap and why), R3
  (cancellation, per-block status), R4 (retry in one layer, budgets compose), OQ-4 (metering), OQ-11 (delivery
  is a step)
- `docs/project/phase4-prd/PRD-019-conditional-execution.md`: the Validate stop setting, skip reasons (OQ-2), failure
  notification (R5)
- `docs/adr/ADR-009-workflow-engine-temporal.md`: §5 (effect blocks pass through), §6 (pinned versions), §8
  (metering), §14b (the undone-state obligation), §15 / 15a–15e (waiting, horizons, cancellation, admin meters)
- `docs/adr/ADR-012-workflow-code-versioning.md` (Draft)
- `docs/project/workflows-scoping.md` §C
- `docs/project/open-bugs.md`: **BUG-024** (the address check admits `100.64/10`; every sink inherits it),
  **BUG-030** (check and connection resolve separately; OQ-4), **BUG-031** (nothing frees a pipeline run's
  outputs; D12, PRD-018)
- Live code: `api/app/pipelines/catalog.py` (the Webhook block), `api/app/workflows/activities/webhook.py`
  (the delivery contract), `api/app/core/security.py` (the SSRF check), `api/app/models/llm_keys.py` (the
  credential precedent)
