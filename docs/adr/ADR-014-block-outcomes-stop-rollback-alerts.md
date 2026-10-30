# ADR-014: Block Outcomes — Stopping Early, Rollback, and Failure Alerts

**Status:** **Draft** — Architect pass, 2026-10-30. Not reviewed; awaits the owner's section-by-section review.
Hold acceptance until PRD-018 (Monitors) is drafted (owner, 2026-10-30), so Monitors' new endings can be
checked against §5 while this is still editable.
**Date:** 2026-10-30
**Deciders:** @karthik
**Resolves:** PRD-019's eight open questions (OQ-1 to OQ-8) and PRD-017's first two (OQ-1 recording
"delivered, then undone", OQ-2 where rollback runs). PRD-017's other six (destinations, egress, duplicate-safe
keys, sink timings, the sink queue, content size) are **ADR-015**'s. Discharges ADR-009 **§14b** (how an undone
block is recorded, before layer C builds) and **15f** (run-level failure notification).
Not a supersession: ADR-009 §4 asked for "a reason column beside the state" if two cases ever needed telling
apart, and this ADR is that column.

---

## 1. The problem

Two PRDs add new ways for a pipeline block, and a run, to end. Today the only record is one status word per
block and one per run:

```
pipeline_runs.status          running | completed | failed | cancelled
pipeline_run_blocks.status    pending | running | waiting | completed | failed | skipped
```

| New fact | From | Today's record |
|---|---|---|
| a Validate **stopped** the run, and which rule, and the value it found | PRD-019 R1, R4 | none — Validate can only fail |
| **why** each block was skipped: stopped / an earlier block failed / cancelled | PRD-019 R4 | a bare `skipped` |
| the run list shows "stopped at <Validate>" without opening each run | PRD-019 D9 | none |
| ScrapeFlow's copies were **deleted** when the run stopped | PRD-019 D10 | none (`collected_at` exists, never set) |
| **delivered, then undone** / **undo failed: left in place** (with where) / **kept** because a webhook had already announced it / **irreversible** | PRD-017 R3, D13 | none — ADR-009 §14b's missing value |
| the run is **undoing deliveries**, how many remain, and by when | PRD-017 R3 | none |
| a **failure alert** that fires however the run failed, even before its first block | PRD-019 R5 | none — the Webhook block only acts when the run reaches it |

The two PRDs asked for one design and one migration (PRD-019 OQ-2 with PRD-017 OQ-1). So does the
PRD-018 draft: its handshake list (2026-10-30) adds approval endings (rejected, expired, superseded) and a
"waiting for your approval" pause, all of them reasons beside an existing state.

---

## 2. The stop decision is made in the Validate step and recorded once (PRD-019 OQ-1)

**Decision: the Validate activity returns a verdict; the workflow branches on the recorded verdict and never
evaluates a rule itself.**

```
Validate activity (content queue, LLM worker)          PipelineWorkflow (replayed)
  reads the stored input
  checks rules in order
  ├─ all met            → returns {outcome: met}       → next block
  ├─ rule i not met     → returns {outcome: not_met,   → stop path (§6)      [stop mode]
  │                         rule_index, rule, found}
  └─ rule i can't be    → raises ValidationUnverifiable → run fails (D4)
       checked             (non-retryable)
```

- **Fail mode is today's activity, unchanged.** It still checks every rule, raises `ValidationFailed` naming
  every problem, and returns nothing. PRD-019 R1 says fail is "exactly today's behaviour", so the message keeps
  listing all failing rules. Only stop mode uses "the first rule not met decides".
- **Stop mode returns the verdict as the activity's result.** It is not raised. So "rule not met" makes exactly
  one attempt with nothing to retry, which is PRD-019's "never retried" check. A replay reads the verdict back from
  the history and never re-reads the page.
- **"Cannot be checked" is raised, non-retryable, in both modes** (D4): a path that is not there, or a value of
  the wrong type, for any rule except `present`, `type` and their reverses. Its error names the Validate, the rule
  and what was found.
- **The contract change is additive** (ADR-012 §2.4). `ValidateInput` gains `when_unmet: "fail" | "stop"`, default
  `"fail"`. The result type becomes `ValidateVerdict | None`: `None` in fail mode, as recorded in every existing
  history. Both copies of the contract (`api/app/workflows/activities/contracts.py`, `llm-worker/worker/contracts.py`)
  and `contracts/test_activity_contract.py` gain the field and the verdict.
- ⚠️ **Release order: the LLM worker before the api image.** Today's worker parses `ValidateInput` with no
  `when_unmet` field. If the api ships first, a stop-mode Validate reaches an old worker and fails to parse. That
  error is retryable, so the block retries three times and the run then fails. It is the same order as H.1:
  workers first.

**Why not evaluate rules in the workflow body:** it would save one activity per Validate, but the workflow worker
has no storage reads by design, and rules need the page. PRD-019 D5 keeps the option open for rules that read only
run inputs. None exist yet.

## 3. Reverse rules are a `negate` flag on every rule (PRD-019 OQ-3)

**Decision: every rule kind gains `negate: bool = false`. The evaluator works out the rule's answer, then flips
met ↔ not met. "Cannot be checked" is never flipped.**

| Rule | `negate: true` reads as | Missing path |
|---|---|---|
| `present /price` | absent | met (it is absent) |
| `type /price number` | is not a number | met (nothing there is not a number) |
| `contains "Out of stock"` | does not contain | — (text rules have no path) |
| `min_length 500` | fewer than 500 characters | — |
| `compare /price lt 100` | not below 100 | **cannot be checked → fails** |
| `json_schema {…}` | does not match the schema | — |

- **"A reverse and its original always disagree" (PRD-019's check) holds by construction.** One evaluator flips one
  answer. A set of new rule kinds would need that property proved separately for each one.
- **Saved Validate blocks keep their meaning:** the flag defaults to false and the config version does not move.
- The verdict and the run page show the rule as the user wrote it, rendered in words ("does not contain
  'Out of stock'"). The SPA renders it; the stored rule stays `{kind, …, negate}`.
- Rejected: new rule kinds (`absent`, `not_contains`, `max_length`, …). They read well in JSON, but they make six
  new kinds to validate and test, and a seventh rule kind would then need its reverse as an eighth.

## 4. Typed run inputs; bindings are resolved in the workflow (PRD-019 OQ-4, R2)

**Decision: a run input is declared with a type — `url`, `number` or `text`. Two rule fields become bindable:
`compare.value` and `contains.text`. The workflow puts the input's value into the rule before calling Validate.**

- **The type is known at save time, not only at run time.** `RunInputDeclaration.type` widens from `"url"` to
  `"url" | "number" | "text"`. The save-time check pairs a binding with the declared type:
  - `compare` with `lt`/`le`/`gt`/`ge` needs a `number` input;
  - `compare` with `eq`/`ne` takes `number` or `text`;
  - `contains` takes `text`; a bound text has the same 1–1000 character limit as a literal.

  PRD-019 OQ-4 worried about "a value whose type is known only at run time". With typed declarations no such value
  exists.
- **The trigger checks each value against its declared type.** A missing or wrongly typed input gets a `422` before
  any row is written, so nothing is charged. This is the path an unbound `url` already takes.
- **The workflow substitutes; the LLM worker never sees a binding.** `PipelineRunInput.inputs` widens from
  `dict[str, str]` to `dict[str, str | int | float]`. Every recorded history holds strings, so the change is additive.
  The workflow replaces each `{"$input": name}` in a rule with the value before it dumps the rules into
  `ValidateInput`. The substitution is deterministic: run inputs are part of the workflow's input. The worker
  contract does not change for bindings.
- **Bindable fields name nested paths.** `BlockType.bindable_fields` lists top-level config fields today
  (`{"url"}`). Validate declares `rules[].value` (for `compare`) and `rules[].text` (for `contains`), and the
  validator's binding check walks the rule list.
- **For Monitors (PRD-018), stated narrowly:** a monitor hands over "last run's value" as an ordinary typed input.
  What a monitor sends on its first check, when there is no previous value, is PRD-018's question. This ADR only
  guarantees that a missing input is refused at the trigger, never filled in silently.

## 5. How a run and a block record how they ended (PRD-019 OQ-2 + PRD-017 OQ-1)

**Decision: no new status values. Reasons go in columns beside the existing states, each column is a closed list
widened by migration, and none of them is a column the quota views read.**

### 5a. The new columns

**`pipeline_run_blocks`**

| Column | Type | Meaning | Written by |
|---|---|---|---|
| `status_reason` | `VARCHAR(30)` NULL, CHECK in list | why the block is in its status. Values for `skipped`: `stopped` (the run stopped early), `upstream_failed`, `cancelled`, `not_started` (the workflow never started), `alert_only` (a Webhook set to *breaks only*, reached on a run that is succeeding — §8d). **NULL on a `skipped` block = "reason not recorded"** (runs before this ADR; PRD-019 R6) | the mirror activities; the API's trigger (`not_started`) |
| `reason_block_id` | `TEXT` NULL | the block the reason points at: the Validate that stopped the run, or the block that failed | same |
| `verdict` | `JSONB` NULL | a stop-mode Validate's verdict: `{rule_index, rule, found, expected}`, the rule as saved | `mirror_block_status` (completed, stop mode) |
| `delivery` | `JSONB` NULL | where a sink wrote, recorded **when the delivery succeeds**: `{destination_id, kind, location}` (bucket + key, or host/database/table + row key). Never credentials. **One record serves three readers:** the undo, the webhook's `deliveries` field, and the "left in place" display (PRD-017 OQ-1) | `mirror_block_status` (completed, a sink) |
| `undo_status` | `VARCHAR(20)` NULL, CHECK in list | after a delivery: `undoing`, `undone`, `undo_failed`, `kept` (a webhook or email had already announced it — D13), `not_undoable` (an irreversible sink on a run that failed). NULL = nothing to undo (the run did not fail, or the block delivered nothing) | the rollback mirrors (§7) |
| `undo_error` | `TEXT` NULL | why the undo gave up | same |
| `collected_at` | exists (ADR-009 §8c), never set until now | the block's stored objects were released: set by a stop with *delete* (§6), and later by BUG-031's fix | `release_run_outputs` (§6) |

CHECKs: `status_reason IS NULL OR status IN ('skipped', 'waiting')`, and `undo_status IS NULL OR status =
'completed'`. Only a delivered block can be undone.

**`pipeline_runs`**

| Column | Type | Meaning |
|---|---|---|
| `stopped_at_block_id` | `TEXT` NULL | the block that ended the run early. CHECK `stopped_at_block_id IS NULL OR status = 'completed'`: it is written in the same update that sets `completed` |
| `stop_reason` | `VARCHAR(30)` NULL, CHECK in list | why the run stopped: `rule_not_met` only, for now. This, with `stopped_at_block_id`, is what the run list's label reads (D9), with no join to the blocks |
| `failed_block_id` | `TEXT` NULL | the block a failed run broke at. Today that is only a prefix inside `error` (`"<block>: message"`); the alert payload and the run page need it as a field |
| `rollback_deadline` | `TIMESTAMPTZ` NULL | set when rollback starts: the latest it can finish. While the run is still `running`, a non-null value means **undoing deliveries**; the count of blocks at `undo_status = 'undoing'` says how many remain (PRD-017 R3). Kept afterwards as the record that rollback ran |

### 5b. Reading the record

```
Scrape ─► Extract ─► Validate(price<100, STOP) ─► Webhook          price = 120

pipeline_runs       status=completed  stopped_at_block_id=check  stop_reason=rule_not_met
pipeline_run_blocks fetch    completed
                    extract  completed
                    check    completed  verdict={rule_index:0, rule:{compare /price lt 100}, found:120}
                    notify   skipped    status_reason=stopped  reason_block_id=check
```

```
Scrape ─► Extract ─► Bucket ─► Webhook                          the webhook receiver is down

while undoing:      status=running  failed_block_id=notify  rollback_deadline=14:20
                    store   completed  delivery={bucket,key}  undo_status=undoing
                    notify  failed
after:              status=failed
                    store   completed  delivery={bucket,key}  undo_status=undone
```

### 5c. Why it is shaped this way

- **The quota views are untouched.** `quota_active_submissions` reads `pipeline_runs.status`, `user_id`, `id`, and
  `pipeline_run_blocks.status`, `pipeline_run_id`. Postgres refuses to alter a column a view depends on, but every
  change here adds a column or a CHECK on a column no view reads. So the migration neither drops nor recreates the
  views. A sixth or seventh status value would have meant altering that CHECK. That still needs no view drop,
  because a CHECK is not the column's type. But every reader of `status` would have to learn the new value, which is
  the cost ADR-009 §4 declined.
- **The undo is a second phase of a delivered block, not a replacement status.** "Delivered, then undone" keeps
  `status = completed`, because the delivery did happen, and adds `undo_status = undone`. PRD-017's six user-facing
  states map onto (`status`, `status_reason`, `undo_status`) one to one:

  | PRD-017 R3 state | `status` | `status_reason` | `undo_status` |
  |---|---|---|---|
  | delivered | completed | — | NULL, or `kept` on a failed run (D13 — "a webhook had already announced it") |
  | delivered, then undone | completed | — | `undone` |
  | undo failed: left in place | completed | — | `undo_failed` (location in `delivery`) |
  | failed | failed | — | — |
  | not attempted | skipped | `upstream_failed` / `cancelled` / `stopped` | — |
  | delivered (irreversible) — could not be undone | completed | — | `not_undoable` |

- **The block's skip reason says *that* the run stopped; the run says *why*.** Monitors' rejected, expired and
  superseded endings (PRD-018 handshake A5–A7) skip later blocks with `status_reason = stopped`. What differs is
  `pipeline_runs.stop_reason`, which PRD-018 widens. "Unchanged" (A1) is a Validate stop with a new rule kind, so it
  needs no new value: the label is drawn from the rule kind in the verdict. Monitors' "waiting for your approval"
  (A3) is the reason the CHECK above already allows on `waiting`.
- **Widening a list is one line.** None of these columns is read by a view, so a new reason value is
  `DROP CONSTRAINT` + `ADD CONSTRAINT` in a short migration. Nothing is pre-admitted for PRD-018. Its values are
  still tentative, and admitting them later costs nothing. (The ledger's closed CHECK takes the same stance, on
  purpose.)
- **No backfill.** Old `skipped` blocks keep `status_reason = NULL`, which the API reports as `"reason not
  recorded"`. Old failed runs keep `failed_block_id = NULL`, and the run page falls back to the `error` prefix.

### 5d. Live updates

`notify_run` gains four identifier/state fields, never text (ADR-009 §11d): `stopped_at_block_id`,
`rolling_back` (boolean), `block_status_reason`, `block_undo_status`. The SPA's run page and list update the
stop, the undo and each block live (PRD-019 R4, PRD-017 R8). Old clients ignore unknown fields.

## 6. The stop path (PRD-019 R1, R4, D10; OQ-8)

In `PipelineWorkflow`, when a stop-mode Validate returns `not_met`:

```
1. mirror_block_status(check, completed, verdict)          the run page shows the verdict now
2. [on_stop_outputs = delete] release_run_outputs(run)      ledger release of every object the run holds
3. mirror_run_status(completed, stopped_at_block_id=check,  pending blocks → skipped, reason stopped,
                     stop_reason=rule_not_met,              reason_block_id=check
                     result_path = keep ? last content output : NULL)
```

- **No rollback, no alert.** A stop is a success (PRD-019 D7, PRD-017 D4): user destinations are untouched and
  *the run breaks* does not fire.
- **`release_run_outputs`** is a new workflow-queue activity. It calls `ledger.release_pipeline_run_objects`, which
  already exists, is tested, and has no caller (BUG-031), and it sets `collected_at` on each block whose objects are
  all gone. **The ledger stays the only releaser.**
  - **Bucket and counter stay in step through retries.** `release_objects` deletes the object, decrements the
    counter and deletes the row, in that order, and commits at the end. Deleting an object that is already missing
    is a success. So a retry after a lost commit finds the object gone, releases the row and decrements once. No
    row is ever left for a removed object once the activity has succeeded.
  - **If the activity gives up** (storage unreachable past its retries — the workers' transient policy, three
    attempts), the objects it could not remove keep their rows: they are still on disk and still charged. The run
    still ends `completed` and stopped, carrying a note in `pipeline_runs.error`, the way the storage-wall note
    does today: *"stopped; 2 stored objects could not be deleted and are kept and charged"*. **This is the
    workflow's version of the 503 rule.** A delete endpoint refuses so its parent row never outlives an
    on-disk object. Here the run row outlives nothing, so nothing is refused. The run says what it kept, and the
    objects stay retrievable. (§11, escalation E3.)
  - **Its budget is part of the Validate block's need** when `on_stop_outputs = delete`, so a stop can never be cut
    short by the run deadline.
- **`result_path` after a delete is NULL.** The run page says *"result deleted when the run stopped"* when the run
  has `stopped_at_block_id` and its last content block has `collected_at`. A failed delete (above) leaves
  `result_path` pointing at an object that still exists.

## 7. Rollback is the failure path's first step (PRD-017 OQ-2)

**Decision: rollback runs inside `PipelineWorkflow`, between "a block failed" and `mirror_run_status(failed)`.
It is driven by two pieces of workflow state: the reversible deliveries made so far, and whether anything has
been announced.**

```
forward:   each sink that succeeds → append (block_id, kind, delivery) to `delivered`
           an irreversible sink that actually sends → `announced = True`   (D13)

failure (a block failed / time budget spent / storage accounting failed):
   if delivered and not announced:
       mirror_rollback_started(failed_block_id, error, rollback_deadline)     run stays `running`
       for d in reversed(delivered):                                          newest first
           mirror undo_status = undoing
           undo_<kind>(d.delivery)       own retry policy (ADR-015), NOT cut by the run deadline
           mirror undo_status = undone | undo_failed (+ undo_error)
   elif announced:
       reversible blocks → undo_status = kept; irreversible → not_undoable   (one mirror call)
   mirror_run_status(failed, failed_block_id, error)     → also writes the failure alerts (§8)
```

- **The undo uses the location recorded at delivery** (`delivery` in workflow state and in the row), never the
  destination's current settings. Whether an undo reports "already gone" or "left in place" is the sink's own job,
  and ADR-015 defines it per kind.
- **The undo is not cut short by the run's deadline.** `_execute` trims each block's budget to what is left of the
  run; undo activities get their own full budget. The save-time check is what guarantees room: forward need +
  rollback reserve ≤ the run ceiling (the reserve's size is ADR-015's, from each sink's undo timing). The workflow
  sets no Temporal execution timeout, so nothing outside the code can cut a rollback off.
- **No concurrency slot while undoing.** The view counts a run only while one of its blocks is `running`. During
  rollback none is: the failed block is `failed`, delivered blocks are `completed`, and the undo lives in
  `undo_status`. ⚠️ **This is a deliberate deviation from ADR-009 15a's "holding capacity at this instant",
  which would count each few-second undo attempt.** It is accepted so that the views are not touched. The capacity
  involved is a short HTTP or SQL delete. (§11, escalation E2.)
- **A cancel during rollback is acknowledged and does not stop it** (PRD-017 R3). The API's cancel only sets
  `cancel_requested_at` (today's code). The workflow's `_finish` already shields running work from a Temporal
  cancel. The rollback loop ignores `_cancel_requested`, and the run ends `failed`, not `cancelled`: the failure was
  decided before the cancel arrived. ⚠️ The precedence rule "a cancel written by the API wins" (ADR-009 §11a) does
  not apply here. The API never writes `cancelled` itself, only the request.
- **Cancel and stop never roll back** (D4), by construction: neither goes through the failure path.
- **A webhook that gave up counts as not delivered**, so the rollback still runs. This covers a receiver that
  processed the POST but answered `5xx` every time: it may have seen a file that rollback then deletes. Accepted.
  The receiver answered "failed" five times, and D11 already tells receivers that delivery is at least once.

## 8. The failure alert (PRD-019 OQ-5, R5)

**Decision: a failure alert is a `webhook_deliveries` row, delivered by today's `WebhookDeliveryWorkflow` and
`webhook-sweep`. The row is written in the same transaction that marks the run `failed`, by whichever writer marks
it. The workflow body gains no command for it.**

### 8a. Who writes it

| How the run failed | Writer | Where |
|---|---|---|
| a block failed, the budget ran out, accounting failed (after any rollback) | `mirror_run_status(failed)` activity | in the update that sets `failed` |
| **the workflow could not be started** | the API's run trigger | in its `except` branch's commit, which already marks the run and its blocks |

Both call one helper, `queue_failure_alerts(db, run)`. It reads the run's **pinned version**, finds every
message-sending block whose `events` include `pipeline.failed`, and inserts one row per block. Then it starts each
row's `WebhookDeliveryWorkflow` best effort, as every other row writer does. A row whose start is lost (Temporal is
down: the likely reason the run could not start) is picked up by `webhook-sweep` once the engine is back. **"Run
failed" and "alert queued" become true together or not at all.** That is why the row is written beside the status
change and not by an extra workflow step.

### 8b. Widening `webhook_deliveries`

- new column `pipeline_run_id UUID NULL REFERENCES pipeline_runs ON DELETE CASCADE`, and `block_id TEXT NULL`
  (the sending block);
- `ck_webhook_deliveries_job_or_batch_or_crawls` → `num_nonnulls(job_id, batch_id, crawl_id, pipeline_run_id) = 1`;
- `ck_webhook_deliveries_run_or_crawl` → `num_nonnulls(run_id, crawl_id, pipeline_run_id) = 1`;
- `UNIQUE (pipeline_run_id, block_id, event) WHERE pipeline_run_id IS NOT NULL`, so a retried mirror inserts
  nothing twice (`ON CONFLICT DO NOTHING`). The job lane's `(run_id, event)` index is untouched: pipeline rows
  have `run_id` NULL.
- **The delivery activity's secret lookup gains a pipeline arm.** It reads the encrypted secret of `block_id` from
  the run's pinned version (`parse_definition`'s stored secrets). With PRD-017's saved webhook destinations
  (ADR-015), it reads the destination's current secret.
- `webhook_url` holds the pinned version's URL for an inline Webhook block. For a block using a saved
  destination, the row stores the destination id and the URL is resolved at each attempt. That keeps "current
  credentials per attempt" and keeps a credential-bearing URL out of the row (ADR-015).

### 8c. The payload — `pipeline.failed`

```json
{
  "event": "pipeline.failed",
  "run_id": "…", "pipeline_id": "…", "pipeline_run_id": "…",
  "block_id": "notify",
  "failed_block_id": "extract",
  "error": "extract: HTTP 401 from provider (gave up after 3 attempts)",
  "result_path": null,
  "rollback": {"undone": [], "left_in_place": [], "kept": []},
  "timestamp": "2026-10-30T09:14:02+00:00"
}
```

It carries `job.failed`'s fields, adapted the way PRD-016 R6 adapted the result payload: no `job_id`;
`pipeline_id` and `pipeline_run_id`; `run_id` kept; `diff_*` dropped, since a pipeline has no diff. To those it adds
`block_id` (PRD-017 R4, which block sent it), `failed_block_id`, and `rollback`: each entry is `{block_id, kind,
location}` from the blocks' `delivery` and `undo_status`, and every list is empty in layer A. Signed like every
other ScrapeFlow webhook. It is retried on the job lane's schedule (≈2.6 h, PRD-019 R5), with SSRF checked on
every attempt, which the job lane's delivery activity already does.

### 8d. The Webhook block's `events`, and a block that is alert-only

- **`events`** — `list["pipeline.result" | "pipeline.failed"]`, default `["pipeline.result"]`, at least one, no
  repeats. The values are the event names the receiver sees, as a job's `webhook_events` uses `job.completed` /
  `job.failed`. The config version does not move.
- **A block with `["pipeline.failed"]` only, reached on a run that is succeeding,** sends nothing. The workflow
  mirrors it `skipped` with `status_reason = alert_only` and calls no activity. Its time need is only the block
  overhead, and it does not count as an irreversible delivery for D13. (§11, escalation E1.)

### 8e. What it must not miss — the meters

Pipeline rows in `webhook_deliveries` make three admin meters wrong the day they land (BUG-028's class).
`job_lane_webhooks_pending`, `job_lane_webhooks_exhausted` and `job_lane_webhook_success_rate_7d` count the whole
table when no user is selected, but scope by job/batch/crawl when one is. Pipeline alerts would therefore count in
the platform total and vanish from every user's number. **Each gains `pipeline_run_id IS NULL`**, keeping
its name true. Admin reads of deliveries show pipeline rows with their run and block, and the admin retry route
works on them unchanged (it re-arms the row). PRD-019 R5 also says the user sees an undeliverable alert. Users have
no deliveries endpoint today, so the run page gains the run's alert rows (status, attempts, last error).

## 9. Config shape (PRD-019 OQ-6)

| Block | Field | Values | Default | Save-time rule |
|---|---|---|---|---|
| Validate | `when_unmet` | `"fail"` \| `"stop"` | `"fail"` | `stop` refused on the last block (D8) |
| Validate | `on_stop_outputs` | `"keep"` \| `"delete"` | `"keep"` | **refused** with `when_unmet: "fail"`: start strict, loosening later breaks nothing |
| every rule | `negate` | boolean | `false` | — |
| Webhook | `events` | list of `"pipeline.result"`, `"pipeline.failed"` | `["pipeline.result"]` | at least one (PRD-019's "neither ticked is refused") |
| run input declaration | `type` | `"url"` \| `"number"` \| `"text"` | — (required, as today) | a binding's type must suit its field (§4) |

All are optional with defaults, so `config_versions` stays at `1` for every block type (ADR-009 §4's
historical-shape rule). ⚠️ The configs are `extra="forbid"`. During a rollout an old api pod refuses a save that
uses a new field (a `422` until the rollout finishes), and an old workflow-worker pod that picks up a new run's
first task fails to decode it. That workflow task is retried until the new pod takes it. Both heal by
themselves; neither strands a run.

## 10. Changing the workflow code (PRD-019 OQ-7; PRD-017 OQ-2's versioning half)

Every new path is wrapped in `workflow.patched()`, per ADR-012 §2.1, even where only new config can reach it. No
run started before the deploy carries a stop-mode Validate, an `events` list or a sink. A wrapper still costs one
`deprecate_patch` later, and it means a reviewer never has to argue reachability.

| Patch id | Wraps | Ships with |
|---|---|---|
| `validate-stop` | passing `when_unmet`, the binding substitution, the verdict branch and the stop path (§6) | PRD-019 |
| `webhook-events` | the alert-only skip (§8d) | PRD-019 |
| `rollback` | the `delivered`/`announced` tracking and the rollback loop (§7) | PRD-017 slice 1 |

**Not patched, and why:** the skip reasons, `stopped_at_block_id`, `failed_block_id` and the failure alert are all
written inside activities (`mirror_*`, `queue_failure_alerts`), and activity code is not replayed. The
`mirror_run_status` input gains optional fields only.

**Replay histories to add** (ADR-012 §2.3; recorded, never edited):

| PRD-019 | PRD-017 slice 1 |
|---|---|
| a stop with *keep*; a stop with *delete* | a full undo (bucket → webhook refused) |
| a pass through a stop-mode Validate | an undo that fails (no delete permission) |
| a failure whose Webhook block sends `pipeline.failed` | a failure after a webhook went out (D13: nothing undone) |
| an alert-only Webhook skipped on a successful run | a cancel during a sink's retry wait (no undo) |
| | a cancel requested during rollback (rollback finishes, run `failed`) |

## 11. Escalations to the PM (owner)

Product-visible consequences of the design. Each is my recommendation and stays open until you answer.

| # | Question | Recommendation |
|---|---|---|
| **E1** | A Webhook block set to *breaks only*, on a run that succeeds: what does the run page show for it? | **"skipped — alert only"** (`alert_only`). Calling it "completed" would read as "a message was sent". |
| **E2** | Undo attempts never hold a concurrency slot, not even during the few seconds of each delete (§7). | **Accept.** The alternative changes both quota views for seconds of capacity. |
| **E3** | A stop set to *delete* whose delete fails after retries (storage down): the run ends `completed` and stopped, with a note, and the objects are kept and charged. | **Accept**, rather than failing a run whose verdict was a success, or retrying for hours. |
| **E4** | A run that could not start: its blocks read **"skipped — run could not start"** (`not_started`), a fourth reason PRD-019 R4 does not list. | **Accept**; it is the API's existing failure path, now labelled. |
| **E5** | Failure alerts retry on the job lane's ≈2.6 h schedule (PRD-019 R5); a Webhook block's own delivery on ≈3 h (PRD-017 R4). Two windows on one pipeline. | **Keep both.** The alert reuses the job lane's delivery machinery, and PRD-019 R5 chose that window. |
| **E6** | Users cannot see webhook deliveries today. PRD-019 R5 needs an undeliverable alert to be visible to the user. | **The run page lists that run's alert deliveries.** No new deliveries page. |

## 12. Alternatives rejected

- **New status values** (`stopped`, `undone`, `undo_failed`) on either table. Every reader of status would have to
  learn them, and ADR-009 §4 had already chosen a reason beside the state. On `pipeline_run_blocks.status` they
  would sit in the one column the concurrency view reads.
- **A separate `pipeline_run_deliveries` table** for the delivery and undo record. It looks tidy, but a block
  delivers at most once per run, so it would be a 1:1 table joined on every run read. Revisit if fan-out (one block,
  several deliveries) ever ships.
- **The failure alert as an activity inside `PipelineWorkflow`** after the run is marked failed. It cannot cover a
  run that never started. It also keeps the workflow open for hours after the run ended, and it builds a second
  delivery record beside `webhook_deliveries`, which already has the retry window, sweep, admin views and retry
  route PRD-019 R5 asks for.
- **Rollback in a separate workflow** started at failure. "Run failed" and "rollback finished" would then be two
  workflows to keep in step, and the alert has to wait for the rollback anyway (PRD-017 R3).

## 13. Consequences

- **One migration (4.10)**: the six new block columns and four run columns of §5, plus `webhook_deliveries`'
  widening (§8b). The quota views are not touched. It ships with PRD-019's build. The undo columns sit unused until
  PRD-017 slice 1, admitted on the same day-one logic as `skipped` and `waiting` (ADR-009 §4, 15a).
- **BUG-031 gets its first caller**: `release_pipeline_run_objects`, from the stop path. The bug itself (no
  permanent pipeline delete, no run delete, no §8c collection) stays open. PRD-018 is where it bites.
- **Admin meters** gain a lane filter (§8e). The admin run reads return the new columns.
- **Traps to carry into the build:**
  - release the LLM worker before the api image (§2);
  - the rollback loop must ignore `_cancel_requested` (§7);
  - `queue_failure_alerts` must run in the same transaction as the `failed` write, on both writers (§8a);
  - `undo_status` only on `completed` rows (CHECK);
  - a new reason value is a CHECK widening, never a free-text write.
