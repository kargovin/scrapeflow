# ADR-011: Artifact Identity — Run-keyed Paths, Stage-named Objects, and the Removal of `latest/`

**Status:** **Draft.** The decisions below were taken by the owner on 2026-09-08; this document is
my write-up of them and has not been reviewed. Promoting it to Accepted is a separate step.
**Date:** 2026-09-08
**Deciders:** @karthik
**Supersedes:** [ADR-002](./ADR-002-phase2-worker-contract.md) **§4** (the MinIO path convention)
only. ADR-002 remains authoritative for NATS subjects and message schemas.
**Resolves:** [BUG-005](../project/open-bugs.md#bug-005) fix part (2) — *"key the artifact path on
something that always exists"* — and with it the entry condition for **P6**. Also answers
**PRD-016 OQ-1** (*what a run that is not a job is keyed on*), which pipelines ask next.
**Footnotes:** [ADR-006](./ADR-006-batch-scraping-data-model.md) — its *"workers are unchanged"*
claim was true of **routing** and false of **identity**;
[ADR-004](./ADR-004-phase3-fat-message-schema.md) — the v2 fat-message field set changes.

---

## Context — the problem in one sentence

**The object key carries relationship information the database already holds**, so the key is
forced to answer *"who is my parent?"* — and for two of the three lanes there is no honest answer.

ADR-002 §4 keys every artifact on `job_id`:

```
latest/{job_id}.{ext}              overwritten each run
history/{job_id}/{unix_ts}.{ext}   immutable per-run record
```

That was correct when every run belonged to a job. Three lanes now dispatch scrapes, and each
answered the identity question differently, without anyone deciding:

| lane | what it puts in `job_id` | honest? |
|---|---|---|
| job (one-off + scheduled) | the `jobs` row id | ✅ |
| batch | `null` — a batch item is not a job (ADR-006) | ✅ honest, and it breaks all three execution paths |
| crawl | `str(page.id)` — a `crawl_pages` row id, in a field named `job_id` | ❌ a lie that works |

BUG-005 is what the honest answer costs: Python workers reject `null` as malformed and **ack+drop**;
Go unmarshals `null` to `""` with no error and writes every batch item of every tenant to
`latest/.html`. The crawl lane avoided both by lying.

**The field named `job_id` is doing the work of "what are this execution's artifacts named after."
Those are two different questions.**

---

## 1. Artifacts are keyed on the row that produced them, not on the job

**Decision: the path key is the primary key of the row that owns the execution.**

| lane | key | a real row? |
|---|---|---|
| job — one-off and scheduled | `job_runs.id` | ✅ |
| batch | `job_runs.id` (batch items have runs) | ✅ |
| crawl | `crawl_pages.id` | ✅ |
| v2 pipelines | the pipeline-run row id | ✅ (by construction) |

Every lane now supplies an identifier that **exists, is unique, and points at something**. `null`
never appears, so the malformed-message branch stops being reachable by ordinary traffic, and no
lane has to impersonate another.

### Why not simply `run_id`

`run_id` is already in the message and looks like the obvious answer. It is not, because **the
crawl lane's `run_id` is fabricated**: `coordinator/dispatcher.py` emits `str(uuid.uuid4())` for a
lane that never creates a `job_runs` row. Nothing looks it up — the API result consumer acks and
drops any message carrying `crawl_context`, deferring to a coordinator consumer that has never
existed ([BUG-008](../project/open-bugs.md#bug-008), will-not-fix on v1).

Keying artifacts on that UUID would produce objects named after a row that does not exist —
untraceable, and unlinkable from P8's ledger, whose producer FK would have nothing to point at.
Reusing the name `run_id` for a field that means three different things is the bug this ADR
removes, committed a second time under a better-looking name.

---

## 2. The key is an explicit, lane-neutral field supplied by the dispatcher

**Decision: the fat message carries one identifier field naming what the artifacts are keyed on.
The dispatcher fills it; the worker uses it verbatim and never interprets it.**

Field name: **`artifact_id`** — settled 2026-09-08.

This preserves the light-worker rule exactly. A worker does not know which lane it is on, does not
branch, and does not consult the database — it receives a string and builds a path from it. The
lane knowledge stays in the three dispatchers, which already have it.

### `job_id` is removed from the wire

Verified against live code before deciding — it is used in exactly four ways, and none survives:

| use | disposition |
|---|---|
| building the artifact path | replaced by `artifact_id` |
| worker log lines | dropped; `artifact_id` and `run_id` identify the work |
| echoed back in `ResultMessage` | dropped |
| read by the result consumer (`data.get("job_id")`) | reads `run.job_id` from the row it already loads |

The last is a **de-duplication, not a new lookup**: `_handle_message` already calls
`db.get(JobRun, run_id)` and already uses `run.job_id` for the cancellation and status
`pg_notify` payloads, while other paths use the message copy. Today those two sources can disagree;
after this they cannot, because there is only one.

### `run_id` becomes explicitly optional, and the crawl lane stops sending one

**Decision: `run_id` stays on the job and batch lanes as the result-routing key, and is *absent*
from crawl dispatches. The fabricated `uuid4()` is removed.**

A crawl page has no run. Sending a made-up identifier so a required field can be populated is the
same move as putting a `crawl_pages` id in `job_id` — it satisfies a schema by lying, and the lie
then reads as fact to everything downstream. Nothing consumes it: the API result consumer acks and
drops crawl messages, and the coordinator's own consumer has never existed (BUG-008).

⚠️ **This forces a change to the result consumer's parse order, and it is not optional.**
`_handle_message` reads `run_id = data["run_id"]` as a **required** key, and does so *before* the
`crawl_context is not None` check that drops crawl messages. Omitting `run_id` therefore routes
every crawl page down the **malformed-message** branch: same end state (acked and dropped), but an
`ERROR`-level "Malformed result message" line per page, for traffic that is not malformed. That is
the BUG-001 shape — log noise indistinguishable from a real fault — manufactured on purpose. The
`crawl_context` check must move **above** the required-field parse.

---

## 3. Objects are named by stage, not by timestamp

**Decision: the second path segment names the producing stage.**

```
history/{artifact_id}/scrape.{fmt}      the scraped page, in the job's output format
history/{artifact_id}/llm.json          the LLM stage's structured output
screenshots/{artifact_id}/{index}.png   one per action-driven screenshot
```

**A run is not one object** — the same realisation P8 reached about the storage counter, on a
second surface. Keying on the producing row answers *which parent*; it does not answer *which
object*, and one run can produce several.

### Why the timestamp goes, and why it must be replaced rather than dropped

`{unix_ts}` was doing two jobs: ordering runs of a recurring job, and separating a run's own
objects from each other. The first is now redundant — `history/{job_runs.id}/…` is unique per run
by construction, and run *ordering* is a database question (`completed_at`), never a path question.

The second cannot simply be dropped. **A flat `history/{artifact_id}.{ext}` silently reintroduces
the collision** for a job with `output_format=json` **and** an LLM stage: `llm-worker` hardcodes
`ext = "json"`, so the scrape and the LLM stage resolve to the identical key and the LLM output
overwrites the scraped page. Today the timestamp saves that case only because an LLM call takes
seconds — the same *"unique by luck via timestamp"* mechanism that let concurrent batch items
overwrite each other within one second.

A stage name is deterministic, known to the worker without being told (a worker *is* its stage),
self-describing when browsing the bucket, and extends to pipelines, where the segment becomes the
block that produced the object.

---

## 4. `latest/` is removed

**Decision: workers write `history/` only. The dual write is deleted from all three workers, and
existing `latest/` objects are swept once.**

`latest/` is **write-only in the entire codebase.** Three workers write it
(`http-worker/internal/storage/minio.go`, `playwright-worker/worker/storage.py`,
`llm-worker/worker/storage.py`), one route deletes it (`routers/jobs.py`), and **nothing reads
it** — not the result endpoint, not MCP, not the SPA. The API serves results from
`job_runs.result_path`, which by convention always holds the `history/` path.

Why it goes rather than being re-keyed:

- **`latest/{run_id}` is a contradiction.** "Newest" is job-shaped. Every run has a unique id, so a
  run-keyed `latest/` never overwrites anything — it is `history/` written twice under two names.
- **It duplicates an authenticated route.** `GET /jobs/{id}/result` already means *"the newest
  result for this job"*, and it performs the owner check. The object-storage copy is the same
  feature without the 404 guard, in a single shared bucket where no tenant can safely be given
  direct access anyway.
- **It is the direct cause of [BUG-007](../project/open-bugs.md#bug-007)'s fourth symptom.** An LLM
  job leaves `latest/{job}.{fmt}` *and* `latest/{job}.json` — different keys, neither overwriting
  the other — while hard delete rebuilds **one** filename from `job.output_format` and orphans the
  rest. Removing `latest/` deletes that symptom by construction rather than fixing it.
- **v2 drops it regardless.** Keeping it means porting a concept already agreed to be dead into a
  brand-new path convention, carrying it through the ledger and the migration, and then deleting it.

**The sweep needs no accounting adjustment.** `latest/` is uncounted on both sides today — the
counter is incremented from the `history/` object, and `routers/jobs.py` removes the `latest/` key
without decrementing. Volume is bounded: the bucket was emptied at the Clerk production cutover
(2026-07-03), so only objects written since then exist.

---

## 5. A missing identifier is an error on every worker

**Decision: no worker may proceed with an absent or empty path identifier. Go must fail loudly
rather than defaulting.**

`artifact_id` is never null by design, so this rule should never fire. It is written down anyway
because **the behaviour that converted BUG-005 Path A's loud crash into Path B's silent
cross-tenant corruption was not the null — it was Go's `encoding/json` unmarshalling `null` into a
`string` as `""` and returning no error.** Per the JSON spec Go implements, that is a no-op, not a
fault. A hard failure on one worker became silent data corruption on another purely because of the
two languages' default behaviour on a missing value.

The invariant is: **a missing identifier fails loudly or is explicitly optional — never defaulted.**
Fixing only the current field leaves the mechanism intact for the next schema change.

**Both halves are now live, and each field takes exactly one of them:**

| field | rule | why |
|---|---|---|
| `artifact_id` | **fail loudly** — no worker proceeds without it | absent means the dispatcher is broken; there is no valid execution without a place to put the output |
| `run_id` | **explicitly optional** — declared nullable in every worker's schema | legitimately absent on the crawl lane |

⚠️ The Go worker cannot express "optional" by omission — an absent JSON key leaves `RunID` as `""`,
indistinguishable from a present-but-empty one, which is the Path B mechanism. Optionality there
has to be a deliberate presence check, not a zero value.

---

## 6. The cross-service contract test

**Decision: a test takes the payload each dispatcher actually builds and runs it through each
worker's real parser. It ships with the fix. The assertion in `api/tests/test_batch.py` that pins
`payload["job_id"] is None` as correct is deleted.**

### ⚠️ Precondition — the producing side has no schema to build through, and must get one

Verified against live code: there are **ten publish sites**, every one of which constructs a raw
`dict` literal and `json.dumps` it — `routers/jobs.py` ×2, `core/scheduler.py` ×4,
`routers/batch.py`, `core/result_consumer.py` ×2 (the LLM dispatch), and
`coordinator/dispatcher.py`. The API defines **no message model at all**.

So the count is **three consumer-side schemas and zero producer-side ones.** That asymmetry is the
root of BUG-005 restated: the three worker models were not wrong and had not drifted from each
other — each says `job_id` is a required string, which is a reasonable thing to say. They disagreed
with a producer that **had no definition to disagree in.**

**This makes a single typed definition on the producing side a precondition of §6, not an
improvement on it.** A contract test that hand-writes the payload is making the same guess the
worker's test fixture already makes, one layer up — two handwritten approximations of a shape
nothing declares. The test is only meaningful if it can call the thing the API actually publishes
through.

The order is therefore: **define the message once in the API → route all ten publish sites through
it → have the contract test build from it and feed each worker's real parser** (including Go's,
which is the only way the Path B class is covered at all).

A useful side effect: with the producer typed, `batch.py` attempting `job_id=None` fails **at
dispatch, in development, on the first batch anyone runs** — a loud local error instead of a silent
production one. That is where this bug should have surfaced.

### Considered and not taken — a shared contracts package

Extracting the message definitions into a package shared by all services was weighed and is **not
proposed here.** Recorded so it is not rediscovered as an obvious missing idea:

- **It cannot reach the boundary that broke.** A Python package covers four of five services; the
  silent cross-tenant corruption happened on the Python→Go wire. Covering both needs JSON Schema or
  protobuf codegen — real infrastructure for five services.
- **The cost is not a directory.** Each worker builds from its own context with its own dependency
  manifest (the seven of BUG-006). A shared package means either moving every build context to the
  repo root — bloating images, wrecking layer caching — or vendoring copies.
- **It would factor out a transport, not a contract.** Phase 4 deletes NATS. A package built around
  *the JSON we happen to send over NATS* dies with it; one built around *what a scrape request is*
  survives. Today those are the same object, which makes extracting the wrong one easy.

**Revisit at the pipeline layer**, where the calculus changes for a reason that is not true today:
there are three producer/consumer pairs now, but a pipeline makes every arrow between blocks a
contract, and a block-output shape produced and consumed by many block types is a shared vocabulary
rather than a serialization detail.

Every suite was green throughout BUG-005 because API tests proved the API emits what it emits, and
each worker's tests proved that worker parses well-formed messages. **No test ever fed an
API-produced message into a worker's parser**, so the wire between the two services was severed
while both sides reported healthy.

This is the half of the fix that outlives the transport: the same test shape applies to Temporal
activity inputs, and PRD-016's pipeline blocks are the next producer/consumer pair to need it.

---

## What does not change

- **Change detection and dedup.** "Which run came before" is a SQL query on `job_runs`
  (`job_id`, `status='completed'`, `ORDER BY completed_at DESC`), and dedup compares
  `content_hash` — a column. `compute_text_diff` / `compute_json_diff` take two paths as **opaque
  strings** and fetch the bytes; neither parses a path. Nothing anywhere lists storage by a
  `history/{job_id}/` prefix.
- **Existing objects.** `result_path` is opaque, so historical runs keep their old-format strings
  and keep diffing correctly against each other. **No backfill, and no dual-format branch.**
  `cleanup_old_runs.py`'s `key.startswith("history/")` guard also still holds — the top-level
  prefix is unchanged.
- **The light-worker rule.** Workers still touch only the queue and object storage. This ADR
  *reduces* their coupling: they lose the last field they had to interpret.
- **ADR-002 §2 (subjects) and §3 (schemas)** beyond the field changes named here.

---

## Consequences

- **P8's ledger gets simpler.** With `latest/` gone, every stored object is real, charged and
  deletable — one uniform rule. Had `latest/` stayed, the ledger would need an *uncharged mirror*
  class: rows that exist so delete can find them but that the meter must skip, reintroducing
  exactly the "the meter must know about categories" property that per-lane tables were rejected
  for (ADR-009 §8d).
- **The delete path stops guessing.** It enumerates ledger rows instead of reconstructing
  filenames, which kills the orphan class rather than the known instances of it — including
  [BUG-004](../project/open-bugs.md#bug-004)'s screenshots, with no separate mechanism.
- **P7 inherits a real key.** Crawl artifacts become keyed on `crawl_pages.id`, so per-page storage
  accounting and reclaim have something to link to. Nothing here decides *whether* crawl pages get
  `job_runs` rows — that stays P7's question.
- **Three workers change, not one.** The dual write is implemented independently in Go, Playwright
  and the LLM worker. The LLM worker's copy is where the hardcoded `ext = "json"` collision
  originates.
- **BUG-005 ships whole or not at all.** Fixing only the schemas leaves the collisions; fixing only
  the paths leaves the drops. Both halves are in this ADR for that reason.

---

## Open for review

1. **The crawl lane gains correctness it cannot yet exercise.** This ADR gives crawls an honest
   artifact key and removes their fabricated `run_id`, but crawl results are still acked and
   dropped by the API consumer (BUG-008, will-not-fix on v1). The work is deliberate — the
   alternative is leaving the lie in place for a lane the `CrawlWorkflow` port will read as
   precedent — but it is worth confirming that spending the change on a dead v1 path is intended.

**Settled 2026-09-08 in review:** the field name `artifact_id`, and the removal of the crawl
lane's fabricated `run_id` (§2).
