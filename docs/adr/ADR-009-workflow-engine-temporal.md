# ADR-009: Workflow Engine — Temporal, and the v1/v2 Coexistence Contract

**Status:** Draft — under section-by-section review by @karthik. **Nothing here is settled yet**;
do not implement against it, and do not cite it as a decision in another document until the
document status is Accepted.
**Date:** 2026-08-04 (drafted) · 2026-08-08 (review in progress) · 2026-08-25 (§8's two blockers closed) · 2026-08-26 (§10 and §11 reviewed) · 2026-08-28 (§13 reviewed) · 2026-09-01 (§14 reviewed) · 2026-09-02 (§15 reviewed) · 2026-09-03 (§16 reviewed) · 2026-09-04 (§17 reviewed)
**Review log:** §1 taken as settled (engine decided pre-ADR). **§2 resolved 2026-08-08** — three
open points closed in place (2a separate Postgres instance · 2b Web UI not exposed · 2c retention
30 days). **§3 reviewed 2026-08-08** — two factual corrections applied (crawls are already an
uncounted lane; the `storage_bytes` exemption was wrong — all three meters are blind to crawls,
not two), plus the recount race named. Crawl metering answered by the PM (PRD-016 OQ-4 round 3)
and wired into §3/§8 — **✅ confirmed by owner 2026-08-08**; no §3 item remains open.
The workflow-ID format is **settled**: it stays user-free, and **§12 was reversed** to match
(its "user identity in the workflow ID" claim is withdrawn — Temporal never parses the ID, so the
property was never structural).
**§4 reviewed 2026-08-10** — five corrections, two of them owner calls: **layer A validates a
single chain in data flow as well as execution order** (data-flow fan-out is wanted but deferred
post-Phase 4, and the PRD problem it leaves unfixed is now a recorded known exclusion), and **run
inputs are config bindings restricted to per-type declared fields** (today only Scrape's `url`).
Also corrected: block identifiers must be **stable across versions**, not merely immutable once
assigned; the block-state column is **named** (`pipeline_run_blocks.status`) rather than asserted;
and per-type config-schema versioning is acknowledged as the residual DSL cost. Knock-ons applied
to **§5** (run-input scalars are an exception to "inputs are references") and **§8** ("final" =
last block in execution order, pinned before fan-out can make it ambiguous).
**§5 reviewed 2026-08-10** — decision upheld and strengthened. The payload figures are now
**measured, not hedged** (256 KiB warn / 2 MiB error), which puts the *entire* BUG-003 page range
past a threshold; the self-hosted config escape hatch is named as a trap that would undo §5 and
§2c together. Two owner calls: the catalog splits into **content-producing (Scrape, Clean, LLM)
and effect (Validate, Webhook)** blocks, with effect blocks passing their input reference through
unchanged — **"one object per block" was false of two of the five types**, which §4's strict-path
data flow turned into a correctness question; and **the run's result, and the charged artifact,
are both the last *content-producing* block's output**, without which **R6's own gate pipeline**
(ending in a Webhook) has no result and charges zero. *(The second half of that call — the charged
artifact — was **superseded by the §8 review on 2026-08-17**; the result half stands.)* Second
call: the intermediate-output
retention window is **a product promise, not a free dial** — result never collected, collection
per run not per block, collected renders as *collected*, and per-block status in the app DB
outlives both Temporal retention and the window. Knock-on applied to **§8** (metering + GC rules).
**§6 reviewed 2026-08-10** — decision upheld, **its stated reason replaced.** The replay argument
was factually wrong (Temporal replays workflow *code* against recorded history, and **input
arguments are part of that history**, so a definition passed in as an argument is pinned
automatically); the described failure needs a workflow body that loads the definition from
Postgres, which this section's own determinism rule forbids. The real basis is **semantic**: a run
that executed the old shape cannot continue into a new one. Four previously unstated things now
decided: the definition travels as a **workflow input argument**; the pinned version is recorded
in **`pipeline_runs.pipeline_version_id`**; a pipeline with a run in flight **may be deleted and
the run finishes** (deleting a definition is not a back-door cancel — the Q4 split again); and
**run history holds the name** — delete with no runs frees it, delete with runs soft-deletes and
409s on reuse. **Worker-code versioning promoted from a passing mention to an explicit deferral**
(Worker Versioning is GA and Temporal's default; needs server-side enablement, so it is infra).
**§7 reviewed 2026-08-10** — the section **under-covered its hardest case**, and gained a fourth
mechanism. Mechanism 1 is now explicitly **pipelines-only**: a migrated job keeps its `job_runs`
row *by requirement* (§3 makes that table a read-model mirror; R5 forbids user-visible change), so
from **the job cutover** the covering set drops to mechanism 2 alone. Into that gap:
**`_recover_stale_pending` (`scheduler.py:131`) re-publishes any `job_runs` row stale at `pending`
past 10 minutes, to NATS, with no lane filter** — so a v2-owned run whose workflow has not started
is dispatched to a v1 worker, and mechanism 2 never intervenes because no second *workflow* was
started. Hence **mechanism 4: a lane marker on `job_runs`, written in the insert transaction,
built at the job cutover** (✅ owner's call). Mechanism 2 also **over-claimed** — the default
`WorkflowIdReusePolicy` is `ALLOW_DUPLICATE`, which permits a new execution once the prior one
closes, so **`REJECT_DUPLICATE` must be pinned** for "once, ever". Two smaller: the
`--retention work` reassurance covered only the safe half (**unacked** messages are the risk, tied
now to §16's drain gate), and mechanism 3's **rollback ordering** is stated since §16 claims
reversibility. Carried to §9: mechanism 1 is true of rows, not messages — under option (a) v2
results land on `scrapeflow.jobs.result` where `result_consumer` is subscribed, with neither FK
set, which is BUG-005's shape one lane later.
**§8 reviewed 2026-08-17** — **the storage rule is reversed, and §5 is amended to match.**
✅ Owner's call: **the meter measures bytes on disk.** Every object a run still holds is charged,
on every lane, for as long as it is stored — replacing "only the final artifact is charged" and
withdrawing §5's clause that the result and the charged artifact are the same object. §5 keeps
*the result* (R3, permanence); §8 owns *what is charged*. The rule is simpler — it needs no notion
of finality, so fan-out, effect blocks and shared objects stop being special cases — and it makes
intermediate-output collection a **user-visible refund** rather than housekeeping. Knock-ons:
screenshots become chargeable (BUG-004's other half), and the parity argument both sections leaned
on was **factually inverted** — the job path charges the *scraped page*, not the LLM output.
That trace surfaced **two unfiled defects on live code**, neither touched by the migration:
hard delete decrements by the JSON while the HTML was what was added, so the counter is
**permanently inflated by every deleted LLM job**, and **the scraped page is never deleted at
all**. Root cause named: `_try_increment_storage`'s idempotency stamp is keyed on the **run**
when it should be keyed on the **stored object**, so a redelivery and a genuinely second artifact
are indistinguishable. ⚠️ **Left open at the time:** the `latest/` + `history/` dual write stores
2× what the meter counts, so "charge what is stored" was not implementable until it was decided
whether the convenience copy is chargeable *(**closed 2026-08-25 — charge one copy**; see the
2026-08-25 entry below)*.
Three more owner calls: **one submission = one concurrency slot on every lane** — which makes cost
and contention disagree *in general* rather than only for crawls, and incidentally fixes
`batch.py:46-47` admitting a 100-URL batch as 1 while metering it as 100; **a pipeline run parked
on a durable timer does not hold its slot, while v1 lanes keep today's behaviour** — §3 and §15
had contradictory definitions of "active" and §15's webhook horizon only survives under one of
them; and **the PM's multi-Scrape rider is closed** — §4's single-chain data flow plus "Scrape
consumes nothing" already makes a second Scrape unsatisfiable at Save, so the rider's premise
("R1 fixes one run to one URL") was wrong even though its conclusion held, and the trigger to
reopen is **multiple roots, not fan-out**. Because that guarantee is emergent from two rules
stated pages apart, §8 now asserts it directly: **a layer-A pipeline has exactly one starting
block, and it is a Scrape block.** Three gaps recorded rather than closed: the unit is now "one
fetch **attempted**" (the meter never waits for a result, and a failed scrape already costs a run
today) with **a retry explicitly not a new unit**; **`crawl_pages` has no accounted-at marker**, so
the "counting starts at cutover, no backfill" promise has no mechanism on two of four lanes; and
**per-run collection is safe only while no object is shared *between* runs** — v1's content-hash
dedup already shares them, so Monitors will break it. §8d names two things no section owns: which
component performs v2 accounting, and what happens when a pipeline hits the storage wall at its
**last** block, after the user's LLM key has already been billed *(**both closed 2026-08-25** —
see the entry below)*.
**§9 reviewed 2026-08-23 — the decision is REVERSED: option (b) first, the NATS bridge rejected.**
✅ Owner's call. The workers gain native Temporal activity entry points in the **first** increment;
activities never dispatch through NATS. The draft's argument — keep workers untouched so an R6
failure is unambiguously the model's fault — was sound in form and failed on four premises.
**(1)** "Rewriting three workers" overstated the change by an order of magnitude: only ~10–22% of
two files per worker is transport, and everything expensive (`blocking.py`'s 313 lines of bot-wall
detection, the Patchright stealth setup, `formatter.go`, `llm.py`, the MinIO clients) touches NATS
**not at all**. The Go worker's `processJob(ctx, job, fetcher) (string, error)` **already has the
shape a Temporal activity wants**. **(2)** About half of what option (a) preserved — `ack_wait`,
the 30s heartbeat, `max_deliver`, the nak ladder — exists *because* JetStream redelivers, so it is
**deleted, not ported**; §10's genuine carry-forwards port identically under either option, so (a)
bought nothing and kept the code alive in two places. **(3) The bridge is blocked, with a dead
service in production proving it:** `SCRAPEFLOW` is `--retention work` (verified on the live
stream), and a work-queue stream refuses a second consumer overlapping `api-result-consumer`'s
claim on `scrapeflow.jobs.result`. The coordinator attempts exactly that at
`result_handler.py:203`, and **`coordinator-result-consumer` has never existed** —
`result_handler_subscribed` appears zero times in its log while the sibling `dispatch_loop_started`
logs fine, because `main.py:82`'s `asyncio.gather(..., return_exceptions=True)` swallows the
exception. The pod is healthy with half of itself dead; **no crawl has ever run in production**, so
nothing surfaced it (BUG-005's shape — filed as **BUG-008**, disposition **will-not-fix on the NATS path**, backlog §3). The remaining answer, polling for the
row `result_consumer.py` writes, puts **the Q8 component on the critical path of every pipeline
run** — the thing this ADR exists to delete. Worse, a v2 result today is **destroyed, not ignored**:
`db.get(JobRun, run_id)` misses, and the handler acks, which on a work-queue stream deletes it
(§7 predicted "neither FK set"; the real failure is a step earlier and final). **(4)** "Workers
unchanged" and "NATS must not retry" are **incompatible** — the retry lives in worker code
(`worker.go:316`, `llm-worker/worker/worker.py:128`) and in per-consumer `max_deliver`, so
neutralising it per-message is a worker change either way. The honest comparison was **a new bridge
plus unchanged workers** vs **a thin permanent adapter plus unchanged worker logic**, where the
bridge is the larger body of novel code, sits in exactly the area that produced Q5–Q8, and is
deleted at the next step regardless. The gate survives via a **required pre-gate**: run the Scrape
activity standalone and diff against a v1 run of the same URL, which is the isolation (a) claimed
and cannot provide. Costs accepted: each worker runs as **two deployments of one image** during
coexistence (mode flag, not one process serving both); three integrations rather than one bridge
(two share a language, and (a)'s "one place" was illusory per premise 4); and ⚠️ **the Playwright
container contract** — Xvfb then `exec python` as pid 1, never `xvfb-run` — must be preserved
exactly, the riskiest part of the port. Sequence: **Go http-worker → LLM → Playwright**. Knock-ons:
§16's sequence moves the worker port **from third to first**, §7's result-path carry-forward is
**moot**, §2's "not in the first increment" is reversed, and the residual retry hazard is now only
that §10's ported classifier must raise **non-retryable application errors** for its terminal
verdicts, not run a loop of its own inside the activity. *(That constraint originally named
`RetryPolicy` non-retryable error types; corrected on the §10 review — see below.)*
**§8's two open blockers closed 2026-08-25 — no section review, four owner's calls on items §8
and §8d had left unresolved.** (1) **The `latest/` copy is not chargeable**: every `history/`
object is charged once, `latest/` is never charged and is deleted with the artifact it mirrors.
`latest/` is kept as-is because v2 already drops it. That unblocks §8's storage rule and BUG-007.
Settling it exposed a second defect — **an LLM job leaves four objects, not two**, since the
scrape writes the job's format and the LLM always writes `.json`, so the two `latest/` keys differ
by extension; hard delete derives **one** filename from `job.output_format` and therefore always
orphans the other. The assumption underneath is *one artifact in one format per job* — the same
per-run granularity error as the accounting stamp, in the deletion path. (2) **§8d's accountant is
named: an activity in the workflow worker**, never the scraper workers, which keep ADR-001's
no-DB rule — enforced by **task-queue routing** rather than by convention, since a scraper pod is
never offered work on a queue it does not listen to. A periodic sweep was considered and
**rejected as the primary counter** (it is a new hand-rolled loop, of the class this ADR exists to
delete; a stale counter breaks the admission buffer; and it cannot attribute bytes without either
a growing full-bucket join or moving tenant identity into the worker pods, where `user_id` appears
in **zero** files today). The sweep is retained as **auditor** — reconciliation and §8c
collection. (3) **The record is one shared per-object ledger, not per-lane**, with the design rule
that **the meter reads `user_id` and `bytes` only**; the producer link is nullable FKs used solely
by delete and collection. Per-lane tables were rejected because their failure is *silent* (a
missing `UNION` arm returns a well-formed, too-small number — **which is P7**), while a shared
table's failure is a **loud rejected insert**. The precedent cuts both ways and is named:
`webhook_deliveries` is already a shared table whose closed `CHECK` structurally rejects the
pipeline lane, which is exactly what the lane-blind meter rule avoids. Knock-ons: **§8b's
per-lane accounted-at markers are withdrawn** (the ledger row is the marker for every lane, so
"counting starts at cutover" holds by construction), **§3 is partly superseded for storage** (the
counting view stays right for `monthly_runs`/`concurrent_jobs`, which count runs living in
per-lane tables; bytes all land in one table keyed on the owner, so a storage arm of the view
would be a layer over a table that needs none — **do not build one**), and **BUG-004's screenshots
need no separate mechanism** — a stored object is a ledger row, so it is charged and deletable by
the same path. (4) **At the wall: the run finishes and is charged; a headroom buffer refuses to
start new runs near the limit.** v1's delete-the-result-and-fail is the wrong shape when the
ceiling is hit at the last block after the user's own LLM key was billed, and admission-time
checking cannot substitute because output size is not knowable in advance. ⚠️ **The buffer holds
only while the largest single admitted run is smaller than it** — true for jobs and pipelines,
**false for crawls**, which need per-page checks; the buffer's number stays an operator dial.
**Sequencing knock-on recorded outside this ADR:** the ledger is **pre-migration** work, not
Temporal-era — BUG-007 cannot be fixed without per-object accounting, and P7 needs per-page
counting plus reclaim, which is the same table. Filed as **P8** in `phase4-backlog.md` §1, ahead
of P7 and BUG-007.
**§10 reviewed 2026-08-26 — the porting *mechanism* was not implementable, and the list was
missing four items.** All findings accepted by the owner. **(1) 🔴 "the classifier becomes
`RetryPolicy` non-retryable error types" is withdrawn**, in all four places it appeared (this log,
§9, §10's table, Consequences). Temporal offers only a **denylist** of error type *names*, while
the classifier is a fail-closed **allowlist** that additionally reads exception attributes
(`APIStatusError` is transient at 429, terminal at 418; `S3Error` transient on `SlowDown`,
terminal on `NoSuchBucket`) and, in Go, keys on **which step raised the error** (`*uploadError`,
because a dead target site and a dead MinIO raise the same type). The cell asserted
"non-retryable error types" and "fail-closed default preserved" simultaneously, which are mutually
exclusive. Replaced by **the classifier decides; Temporal retries** — terminal verdicts re-raised
as `ApplicationError(non_retryable=True)` / `NewNonRetryableApplicationError`, which is not the
in-activity retry loop §9 warned against because it adds no second backoff, counter or ceiling.
**(2) 🔴 The timeout figure was wrong.** "180s request timeout" conflated two budgets:
`llm_warmup_max_wait_seconds` **is** 180, `llm_request_timeout_seconds` is **60 in `config.py`**
and **180 in production**, set in the infra repo's `llm-worker.yaml`. Sizing start-to-close from
the repo defaults yields 240s against a real requirement of ≈360s — short by two minutes, and
failing *only* on cold starts. **(3) The section's risk model was miscalibrated and is now split
in two.** Its opening claim — "these live inside code the migration removes" — is **false for
three of five items**: `llm.py`, both `errors.py`, `errors.go` and `blocking.py` contain **zero**
NATS calls (two comments between them), so nothing points a delete at them. Group A is at risk of
**deletion**; Group B is at risk of **silent semantic change** under the new retry owner, which is
a different danger needing a different instruction. **(4) Non-retryable is now an explicit
three-part obligation**: classifier terminals, **SSRF failures** (today `exhausted` immediately
and not counted as an attempt — a naive port turns that into ≈2.6 h of re-resolving a hostname an
attacker is rebinding), and **bot walls / robots disallows**, which ⚠️ **never raise today** —
`detect_block()` returns a verdict and the worker publishes `failed`, so the classifier has never
seen a block and the two rows meet for the first time in the port. **(5) Four items added:** the
**heartbeat obligation** (its NATS mechanism dies, the duty re-homes onto `activity.heartbeat()`;
forgetting it fails the activity on every cold start, omitting `heartbeat_timeout` hides a dead
worker for the full start-to-close), the **webhook wire contract** (HMAC-SHA256,
`X-ScrapeFlow-Signature: sha256=<hex>`, success `< 300`, 10s, header always sent even with an
empty secret — the most externally visible thing in the migration, and no test in this repo would
catch a change), the **webhook payload schema** (with two fields that have no v2 source: a
pipeline run has no `job_id`, and `diff_detected`/`diff_summary` are homeless until Monitors), and
the **correctly-dissolved list** (terminal-status guard, schedule-drift base, `ack_wait`/
`max_deliver`/nak ladder) so it is not re-derived. **(6) Two precision fixes:** the content-hash
and `diff.py` are **not equally at risk** — `diff.py` is its own module and survives its caller's
deletion intact, the hash is seven lines *inside* `result_consumer.py` — and **"pure logic reused
verbatim" is false of the dedup branch**, which deletes the new object and repoints `result_path`
at the **previous run's** object, the cross-run sharing §8 recorded as breaking per-run GC. Also:
"the Scrape activity" → **"the Playwright scrape activity"**, since bot-wall detection exists on
one lane only. **Two findings relocated by the owner:** the **scheduled-quota waiting room** to
**§7** (a Schedule question — `scheduler.py` defers without advancing `next_run_at`, and no
Temporal overlap policy reproduces that; left as a named open item because it pairs with §8's
headroom buffer) and the **`httpx` requirement for `sitemap.py`** to **§13** (BUG-006's rider, the
one do-not-delete item that must be *modified* rather than copied).
**§11 reviewed 2026-08-26 — no factual error in the decision; four gaps closed and a live bug
filed.** Every claim the section made was verified and holds: the `job_status` payload really is
three colon-separated fields whose widening fails *silently* (`job_notifier.py:51` catches the
`ValueError`, logs "malformed" and drops the update), `batch_status` really does already carry
JSON, the mirror row really is written anyway, and the Web UI really is write-capable. The
findings are therefore all things the section did **not** say. **(1) 11a — the section names one
writer; the contract it preserves has four.** `job_status` is emitted from `result_consumer.py`,
`quota.py` and **two request handlers** — `routers/jobs.py:419` and `routers/admin.py:354`, both
cancellation. ✅ **Owner's call: keep two writers, as today.** Routing cancellation through the
workflow would make the button appear dead for minutes, because the PM's rule is that a block is
never aborted mid-execution; R5 forbids that regression. ⚠️ The accepted cost is that **two writers
on one status column fail silently** when the precedence rule is forgotten — a cancelled run
flipping back to `completed` with the work done and charged — so the rule is now stated in the
section rather than left to the implementer: **a cancellation written by the API wins; a mirror
write never moves a run out of a terminal state it did not set.** It already exists on the job
path (`result_consumer.py:613`) and must be re-established, not invented. **(2) 11b — the socket
gives up after 300s of silence, and §15 deliberately creates runs silent for ≈2.6 hours.**
`jobs.py:745` times out and closes; `JobDetail.tsx:81` has **no reconnect and no polling
fallback**, and the query cache is invalidated only by a terminal message that never arrives — so
a pipeline parked at its Webhook block goes stale at 6m30s and stays stale, and the eventual
completion notify fires into an empty room. Monitors extend this to days. ✅ **Owner's call: the
client reconnects on any non-terminal close; the 300s timeout stays.** A server keep-alive was
**rejected as the primary fix** — it covers only one cause of a dead socket (not Traefik idle cuts,
rolling deploys or a closed laptop), it would make BUG-009 *invisible*, and it **cannot self-heal**,
whereas a reconnect re-reads the row and repairs every update missed while disconnected. Keeping
the timeout turns it from a defect into a five-minute self-healing re-read. **(3) 11c — a failed
mirror write.** It is the only activity whose failure does not affect the work, which is why it
invites best-effort treatment; but this section's "don't stream engine events" and §2b's unexposed
Web UI compose into **the mirror row being the only window into a run that exists**. ✅ **Owner's
call: the run fails.** **(4) 11d — the payload has an 8000-byte ceiling and overflowing it destroys
the status write**, because the notify runs inside the row's transaction, so it raises and rolls
back. Unreachable today; this section's own JSON decision brings it in range, with a failed LLM
block's provider error string as the realistic trigger. ✅ **Owner's call: identifiers and status
only — never error text or content**, which is §5's references-not-payloads rule one layer down;
plus **absolute state, never deltas**, because activities are at-least-once and `batch_status`'s
correct precedent is currently correct by accident. **(5)** The zero-frontend-change property is
**scoped to the job path** and now says so. **Filed against live code: BUG-009 — `JobNotifier`
never reconnects.** One asyncpg connection opened at startup, no termination handler, no reconnect;
if it drops, every WebSocket in that process goes deaf permanently and silently. Pre-migration.
**§13 reviewed 2026-08-28 — the decision holds; one clause withdrawn, and the section was
describing a port that is really a rewrite.** The crawl still migrates to a `CrawlWorkflow`, still
migrates last, and is still **not** a block. Four owner calls. **(1) 🔴 It is a rewrite, not a
port.** BUG-008 is worse than "one consumer is missing": traced through, **only the dispatch half
of `coordinator/` has ever executed** — `result_handler_loop`, `_process_crawl_result`, link
extraction and sitemap discovery have all never run, so **a crawl in production has never got past
dispatching its seed page**, and `crawls`/`crawl_pages` being empty is what that looks like rather
than evidence of disuse. Its tests mock the DB and MinIO and call `_process_crawl_result` directly,
never the loop where the defect is. Consequence now stated: **§9's pre-gate — diff the new
implementation against a v1 run — does not exist for crawls and cannot be made to exist**, so
"migrates last" costs the one lane with no reference implementation the compensating gate every
other lane gets for free. **(2) 🔴 `crawl_queue` does NOT retire** — the clause is withdrawn.
§5's measured limits and the API's own `max_pages` ceiling (`le=10000`) already decide it: 51,200
history events over 10,000 pages is **≈5 events per page**, against **3** for the cheapest possible
activity before any overhead, and a 10,000-URL visited set is **≈800 KB** against §5's 256 KiB warn
/ 2 MiB hard limit. So `continue-as-new` is **mandatory in both** candidate designs (it never
distinguished them) and the visited set **cannot ride in a workflow argument**. ✅ **The frontier
and visited set stay in Postgres**; the workflow reaches them through activities, since §6's
determinism rule forbids a workflow body reading the DB; and the dedup mechanism to preserve is an
**index** (`idx_crawl_queue_url UNIQUE (crawl_id, url)` + `on_conflict_do_nothing`), not a
collection. **(3) 🔴 `crawl_pages` is required, not "may be kept for the UI"** — that sentence is
untouched 2026-08-04 text and predates P7 (per-page metering, 08-08), §8's storage reversal (08-17)
and §8d's shared ledger (08-25), all three of which need it; it is also the **artifact's name**
(`dispatcher.py:120` puts `crawl_page_id` in the message's `job_id` field). Its missing size column
is now **correct**, not a gap, because §8d put bytes on the ledger row. How crawls are *presented*
is deferred past Phase 4 as a product question. **(4) 🔴 The `httpx` swap is the smaller half of
what is wrong in `sitemap.py`.** SSRF is validated exactly twice, both at creation
(`routers/crawls.py:34-36`); the coordinator validates nothing and **no worker validates anything**.
Extracted links survive that by accident (`link_extractor.py:33` restricts to the seed origin);
**sitemap entries do not** — `sitemap.py:39` takes them verbatim from the target's `robots.txt`,
`:45` fetches them, and `result_handler.py:183` enqueues them with no origin filter, after which a
worker scrapes the target and the body is served back through `GET /crawls/{id}/pages`. A **read**
primitive, not a blind fetch, and latent only because the component is dead — **the migration is
what switches it on**. ✅ **Every URL entering the frontier is SSRF-checked at admission**, where
all three discovery routes converge; a rejected URL is **skipped and the crawl continues**, never a
crawl failure. Two riders: whether sitemap entries are also restricted to the seed origin is a real
trade-off and **left open**; and a **worker-side check** — the correct point-of-use position — is
recommended but **filed as a separate cross-lane item**, not the crawl migration's to carry.
**Also corrected: BUG-006 undercounts itself** (7 manifests, 3 with lockfiles; `mcp/` missing from
its list of unscanned services), and **one live fix shipped** — the crawl page status filter
accepted `processing`, which nothing writes, and rejected `running`, which the coordinator does.
**§14 reviewed 2026-09-01 — the decision is upheld unchanged; four gaps closed, and the section
stated a sequencing decision in a notation that could not carry one.** Conditional execution still
gets its own layer-A PRD, written before PRD-018, not absorbed into B. **(1) The ordering line
mixed a shipped product, a document and a layer in one arrow chain**, so it could not distinguish
when the follow-up PRD is *written* from when the feature is *built* — the two orders are now
separated (14a), the build-after-A answer is moved here from PRD-016's Non-goals, where a
sequencing fact did not belong, and the reason it is right is stated rather than assumed: **the
cost gate has no consumer until something makes a pipeline recur, and layer A has no scheduling**,
so building it in layer A means building for a consumer that does not exist. **(2) "C adds block
types without extending what a pipeline can express" covered half of C.** The sink half is now
*verified* rather than assumed — §5's effect blocks pass their input reference through unchanged,
so multi-destination delivery is a chain and **C needs none of the data-flow fan-out §4 deferred
past Phase 4**. **Saga rollback was not examined at all**: it is a run shape, not a block type, and
`pipeline_run_blocks.status` (then `pending`/`running`/`completed`/`failed`/`skipped`; `waiting`
was added later, by the §15 review, and does not help here) has **no value for "succeeded, then
undone"** — the schema-change-plus-backfill trap §4 deliberately avoided for
B, aimed at the layer cleared to start **soonest**. PRD-017 must settle it **before C starts, not
during**. **(3) The "cost is low" list is entirely about wiring** — named references, stable
identifiers, graph storage, `skipped` — and says nothing about the follow-up PRD's hardest
question, **what a condition is allowed to say**, which runs into PRD-016's expression-evaluator
non-goal and, harder, into replay determinism: branching is an `if` in the workflow body, which is
the code Temporal replays, so a condition that answers differently on replay corrupts a run at an
unrelated pod restart. **The Validate block is the precedent** (declarative vocabulary, deterministic
by construction) and belongs in the brief. Recorded as *not* open: the monitor supplies the gate's
comparand, and §4 already made widening the bindable-field list additive. **(4) The deliverable this
section creates has no number, no `phase4-backlog.md` row and no recorded obligations**, while
PRD-017 and PRD-018 have all three — which is the visibility this section's own argument is made
of; a nameless PRD is only marginally better off than an absorbed one. ⚠️ Its **number is left
open**: creation order gives PRD-019, which reads as *after* PRD-018 in every index while being
required before it. Also flagged, found while checking the ordering claims: **`workflows-scoping.md`
is stale in three places and its own banner flags two** — §4A still lists `branch` in layer A's block
catalog, §5's roadmap has no conditional step, and ⚠️ **§6 still recommends option (a)**, the NATS
bridge **rejected and found blocked** by the §9 review on 2026-08-23. That third one reads as a live
recommendation to build the rejected design; 🔴 marker now, redraw with `temporal-full-migration.md`
after the review closes.
**§15 reviewed 2026-09-02 — the decision is upheld, but two of its supporting statements could
not be built as written, and four things it did not say are now settled.** Option (c) stands: the
Webhook block waits for real delivery, and the rejections of (a) and (b) hold — (b)'s two `CHECK`
constraints and the `run_id` FK into `job_runs` were verified against the model, and the ≈2.6 h
figure was verified **against production** (`WEBHOOK_MAX_ATTEMPTS: "5"` in the infra repo matches
`settings.py`), checked deliberately because §10 found exactly that trap on the LLM timeout.
**(1) 🔴 The concurrency argument that answers the PM's only objection had no column to read.**
§8's rule — a run parked on a durable timer is not active — has to execute as a SQL predicate,
and a webhook mid-backoff and an LLM call in flight are **the same row**: §4's block vocabulary
had no waiting value and its run vocabulary is three terminal outcomes. So the objection arrives
anyway (five parked runs lock a user out of five slots for 2.6 h). The tempting fix — *"a running
Webhook block does not count"* — is **wrong, not merely fragile**: every layer-C sink delivers to
user-owned S3, databases and mail servers, exactly as unavailable as a webhook receiver, so a
type-aware predicate would count a parked S3 sink and not a parked webhook, failing in the
direction that locks the user out. ✅ **Owner's call: `waiting` is pre-admitted to
`pipeline_run_blocks.status` now** (the `skipped` precedent — Monitors will park a run on a sleep
belonging to *no* block, which the category rule cannot cover), **and §5's content-producing /
effect split decides which blocks may enter it.** §4 and §8 amended to match. ⚠️ This is the
**third** missing value found in that vocabulary in three sessions (`skipped`; "succeeded then
undone" at §14; `waiting` here), and the last two share a cause — the vocabulary describes a
block's progress while both callers need what the run is doing with resources.
**(2) 🔴 "Reproduces today's behaviour exactly" is withdrawn** — it contradicts this section's own
first bullet. Today's backoff is an explicit list (`30 → 300 → 1800 → 7200`, ratios ×10/×6/×4, not
geometric) and a Temporal retry policy takes four numbers with **no interval list**. Closest fit
(initial 30s, ×10, ceiling 7200s, 5 attempts) preserves the attempt count and drifts attempts 4–5
by ~20 min for a ~13% longer horizon; reproducing the list exactly requires explicit sleeps around
a non-retrying activity, which rebuilds the delivery loop in the workflow — the thing the section
says it is not doing. Retry policy kept, drift recorded, "exactly" gone, so an unqualified claim
is not quoted back during the R6 comparison. **(3) The horizon lives in one of four nested
timeouts set in three files, and the smallest silently wins** — POST 10s < `start_to_close` ~20s <
`schedule_to_close` ≥ 2.6 h < the run's R4 time budget. Two silent failures named: putting 2.6 h
on `start_to_close` (one hung POST for 2.6 h, one attempt instead of five) and a reasonable-sounding
*"no run may exceed one hour"* set elsewhere, which is **Q6's exact shape** — R4 already requires
budgets to compose, and this is the first concrete place they must. **(4) Cancellation collides
with the PM's never-abort-mid-execution rule**, which was priced when blocks lasted minutes: a run
cancelled at 14:05 keeps POSTing until 16:35 and then tells the customer's system it finished.
✅ **Owner's call: the API sends a cancel to the workflow *in addition* to writing the row**
(row first, signal best-effort, so §11a's instant UI is untouched), with a re-check at each backoff
boundary as the fallback — the fallback alone only turns 2.6 h into 2 h. Not webhook-specific:
Monitors' multi-day sleeps are the same problem. **(5) "No `webhook_deliveries` row for v2" removes
live capability and blinds three meters.** ✅ The two admin endpoints (`admin.py:367`, `:387` —
list and manual re-fire) become **job-lane-only by design**, accepted because manual retry mattered
only while failure was invisible and under (c) the run itself fails in the user's own list. ⚠️ But
`webhook_deliveries_pending`, `webhook_deliveries_exhausted` and `webhook_delivery_success_rate_7d`
read that table with **no lane filter**, so the dashboard will report **100% webhook success while
every pipeline delivery fails** — BUG-005's and P7's shape exactly, and the reason §3 moved run
counting onto a view. Declining the feature is fine; leaving a lying meter is not, and the fix is
naming. Also withdrawn: *"and the Web UI shows it"* — §2b does not expose it. **(6) The
failure-notification obligation has no home.** Today any failure notifies
(`result_consumer.py:563–575`); in a pipeline an early terminal failure stops the chain and nobody
is told. PRD-016 handed that to "when conditional execution is settled" — and §14, one session
earlier, created that PRD and enumerated its obligations **without it**. Sharpest form: a job saved
with `webhook_events: ["job.failed"]` has **no expressible layer-A pipeline equivalent at all**.
Added to the conditional-execution PRD's obligations. Plus one rider: **SSRF refusal must be raised
non-retryable** (§10's second obligation lands here) and now **fails the run**, unlike today.
**§16 reviewed 2026-09-03 — the decision is upheld; the *instructions* were stale in five
places, three of them addresses that had silently moved.** Coexistence stands exactly as drafted:
Temporal comes up alongside NATS, pipelines are v2-only from day one (layer A **adds** a lane
rather than splitting one), flows cut over individually, deletion is last and gated.
**(1) 🔴 The sequence was renumbered by the §9 review and the references into it were not.** §16
said *"migration step 2, when jobs move to `JobWorkflow`"* and twenty lines later listed a sequence
in which step 2 was the worker port and jobs were step 4; §7 anchors mechanism 4 — the lane marker,
a schema change — to "step 2" five times, §2d to "steps 2–3", and `temporal-full-migration.md`
still carries its own unrenumbered 7-item list whose step 2 is the *rejected* NATS bridge. The
failure is silent in both directions (build the marker where it is inert, or arrive at the job
cutover believing it was done). ✅ **Owner's call: the sequence is named, not numbered** — engine up
· worker port · pipeline lane · job cutover · batch and crawl cutover · schedule and webhook
cutover · consumer deletion · NATS removal · API thinning — and every cross-reference names a step.
§7 and §2d corrected in place. Also: "four of the seven steps" counted a nine-item list.
**(2) 🔴 The drain gate was described here as a deletion gate, while §7 already asserts it is a
cutover gate too** and says so in those words — one section was never amended, and the unamended
one is the contract an implementer works from. The risk is the **unacked** message: routing a flow
to v2 does not recall a message already on the stream, so a v1 worker consumes it, scrapes again,
bills the user's own LLM key again, and its result returns to `result_consumer.py`, which resolves
the run by id and overwrites the state of the run Temporal is executing. **None of §7's four
mechanisms reaches it** — mechanism 4 in particular cannot, because workers hold no DB access
(ADR-001) and cannot read a lane marker. ✅ **The gate now fires at every flow cutover as well as at
deletion.** Residual recorded, not solved: §11a's precedence rule guards a cancellation and
`result_consumer.py:613` guards `cancelled`; neither refuses a stale v1 result for a run the other
lane owns. **(3) 🔴 Obligation 2 borrows a user-facing switch, and a live meter reads it.**
`schedule_status` is user-writable (`schemas/jobs.py:114` → the generic setattr loop at
`routers/jobs.py:465`, no lane awareness) and is counted by `admin.py:494` →
`active_recurring_jobs` → `UsageStats.tsx:67`, rendered today. So migrating recurring jobs makes
the admin tile count **down toward zero while every one of them still fires** — the fourth
instance of this defect in this ADR (BUG-005, P7, 15e) and **the first caused by an instruction in
it** — and a user may `PATCH {"schedule_status": "active"}` at any time, re-arming **both lanes**,
which is the double scrape and double LLM bill R5 requires be structurally prevented. ✅ Recorded
as obligations of the schedule and webhook cutover; the meter fix is **naming, not features** (15e
precedent). ⚠️ Dormant is not safe: there are no scheduled jobs in production, so the tile reads 0
before and after — 13d's shape — and Monitors is entirely about recurrence. **(4) "Every step is
reversible" is false for the increment that ships first.** §16's own routing rule says pipelines
have no v1 implementation, so a broken pipeline falls back to being *switched off*, not to v1. The
section recorded the favourable half of "adds a lane" (no double-execution risk) and stated a
blanket promise the same fact contradicts. ✅ **Narrowed: reversibility is a property of migrated
flows.** Consequence for planning — R6 runs on a lane with no fallback, which makes §9's standalone
pre-gate a requirement rather than a nicety, and is 13a's crawl exposure arriving one lane earlier.
**(5) The sequence starts after work this ADR moved in front of it.** P6, **P8** (the shared
storage ledger — the v2 charging activity has no table without it) and P7 (crawls join the counting
view — without which pipeline runs consume no meter *by construction*, P7's own bug on a new lane)
all precede "engine up". §8d moved the ledger to pre-migration on 2026-08-25, two days *after* the
sequence was last touched. ✅ **The pre-migration queue is the sequence's entry condition**:
P6 → P8 → P7 + BUG-007, then engine up; `phase4-backlog.md` §1 remains its source of truth.
**§17 reviewed 2026-09-04** — the **deferral stands** (the earlier contracts keep their authority
while v1 serves their flow, and the supersession notices wait), and it is now stated as **this
ADR's own call**: the index defines *how* a notice is written, never *when*, because no previous
supersession here replaced a contract that stayed live afterwards. Six corrections, none to the
decision. **(1)** The ADR-001 entry listed *"§2 subjects, §3 schemas, §8 MinIO paths"* — word for
word the three sections **ADR-002** superseded on 2026-04-02, so it was a copy of that notice
rather than an assessment, pointing at a document that no longer owns those decisions and missing
**all four sections of ADR-001 that are still authoritative**. Two are deleted (§5 ack timing, §6
retry), §7 is **contradicted by name** (it says no cancellation signal is ever sent to NATS or the
worker; §15d has the API signal the workflow), and §4 is **split** — its retry and status-update
rows go, while *"Worker dependencies: NATS + MinIO only. No database access."* **survives
permanently** and is depended on three times in this ADR (§9 keeps it, §8d enforces it through
task-queue routing, and 16b's residual risk exists *because* a v1 worker cannot read the lane
marker). ✅ The list is rebuilt per section and §4 gets a **partial** notice naming the surviving
rule. **(2) 🔴 The §5 departure cited "ADR-002 §8", which is not a section that exists** — ADR-002
has six sections and its MinIO Path Convention is **§4**; §8 was *ADR-001's* number, carried along
when ownership moved. Wrong in **16 places across six files**, including `open-bugs.md`, which is
what P6 is implemented from. ✅ Corrected everywhere it is a live instruction; the archived handoff
blocks are left as the record. **(3)** *"When the corresponding v1 component is deleted"* does not
resolve: ADR-004 belongs to the **stream**, not a component, and dies at the **batch and crawl
cutover** — before either deletion step — while ADR-001 §4's light-worker rule has **no deletion
event at all**. ✅ The scope table **names a step per row** (16a's lesson, one section later), and
leaves the cell empty where a contract survives. **(4)** *"For as long as v1 serves traffic"* was
one global switch on a per-flow migration — after the job cutover ADR-002 is authoritative for
batch and crawl and not for jobs. ✅ Authority is **per flow**. **(5) ⚠️ ADR-001 §6 and ADR-002 §6
are already false of live code** — both say there is no application-level retry loop in the worker,
and all three workers have had one since Q5/UF-003 (`llm-worker/worker/worker.py:107`/`:128`,
`playwright-worker/worker/worker.py:259`/`:281`, `http-worker/internal/worker/worker.go:308`/`:316`).
Predates this ADR; recorded as a known divergence rather than protected as accurate, since ADRs are
not edited to match drifted code. It also reframes §10: the ported classifier moves a retry decision
that **already lives in the workers** onto a new engine — continuity, not a new hazard. **(6) 🔴
ADR-005 and ADR-006 appeared nowhere in this ADR**, though §13 decides all four of ADR-005's
sections — **two of them upheld by name** (`crawl_queue` stays, `crawl_pages` is required), which is
precisely what 13b found people assume otherwise. ✅ Both in scope; the section is renamed
*Relationship to the earlier ADRs* so the list is not fixed in the heading, and ADR-003/007/008 are
stated as unaffected rather than left to inference.
**Next: the closing Consequences and Deliberately-not-decided blocks**, which close the review. §12 was already reviewed and reversed.
**Deciders:** @karthik
**Inputs:** [PRD-016](../project/phase4-prd/PRD-016-workflows-pipelines.md) (11 open questions),
`docs/project/phase4-backlog.md` §2/§3, `docs/project/workflows-scoping.md` §7 (engine
comparison), `docs/project/temporal-full-migration.md` (change inventory + sequence),
`docs/project/open-questions.md` **Q8** (the incident), `docs/project/open-bugs.md` **BUG-005**
**Supersedes:** nothing yet — see [§17](#17-relationship-to-the-earlier-adrs).

---

## Context

ScrapeFlow's orchestration is hand-rolled: five polling loops plus Postgres state, with
idempotency guards, backoff schedules and crash recovery written by hand in every component
(`result_consumer.py`, `scheduler.py`, `webhook_loop.py`, `advisory.py`, `coordinator/`).

That approach has already cost a production incident. **Q8**: `job_runs.status` encodes both
*which stage* a run is in and *what that stage is doing*, so the LLM worker's `running` message
clobbered `processing` back to `running`, the next `completed` re-matched the scrape-completed
branch, and the system re-dispatched to the LLM subject in a tight loop — roughly 200 billable
LLM calls in five minutes. The fix was a `source ==` guard on two branches. The invariant that
fix depends on is *"every branch that reads `run.status` also reads `source`, forever, in every
code path anyone ever adds"* — not enforced by the type system, and not reliably caught in
review. Q8 is closed as do-not-fix precisely because the code holding it is deleted here.

Phase 4 adds a product layer — user-defined **Pipelines** (PRD-016) — that makes this worse
before it makes it better. Every step added to the fixed recipe multiplies the Q8 class of bug,
and a pipeline is by definition a variable number of steps.

**This ADR does three things:** records the engine decision and why; answers PRD-016's eleven
open questions; and defines the contract under which the existing NATS path (**v1**) and the
Temporal path (**v2**) run side by side.

### The one fact that reframes several answers

**Temporal Server does not run our logic, and its database is not an application database.**
Temporal is infrastructure: durable event history, timers, retries, task routing. Our workflow
and activity *code* runs in **our own worker pods** that connect to it. Its Postgres holds event
history and timer state; we never read or write it, and no user-facing question — "how many runs
has this user made", "list this user's pipelines" — is ever answered from it. Every such question
is answered from the **app** Postgres. Logic moves *out of the API pod* into worker pods; it does
not move "into the engine."

---

## Decisions

### 1. The engine is Temporal

Chosen over DBOS, Restate, Prefect, Windmill, and extending NATS+Postgres. The comparison table
in `workflows-scoping.md` §7 is the record; the deciding factors:

- **First-class Python *and* Go SDKs.** We run both — the http-worker is Go, everything else is
  Python. Prefect has no Go. This is the only hard technical filter, and it eliminates a
  candidate outright.
- **Durable timers, signals, and saga are all first-class.** Monitors (layer B) is built entirely
  from durable sleep plus wait-for-signal; Delivery sinks (layer C) is built from compensation.
  Both are marquee Temporal features rather than things we assemble.
- **Portfolio value.** This is a stated goal of the project, not a rationalisation. Temporal is
  the industry-standard name in this category.

Accepted cost: it is the heaviest dependency we will run on a single-node k3s homelab — server
components plus its own Postgres. Mitigated by using **standard visibility** (no
Elasticsearch/OpenSearch) initially and accepting **no Temporal HA** at homelab scale.

**Rejected — DBOS.** The lightest path (a library on the Postgres we already run, near-zero new
infra) and genuinely tempting. Rejected because `workflows-scoping.md` §7's "prototype on DBOS,
migrate to Temporal later" plan front-loads the cheap half and defers the expensive half: we
would write the workflow layer twice and still take the Temporal operational cost eventually.
Its Python-only strength also fails the Go filter for the http-worker's eventual activity
conversion.

**Rejected — Restate.** Light (single binary), rising, has both SDKs. The honest reason it loses
is the portfolio factor plus ecosystem maturity, not a technical deficiency. Worth revisiting
only if Temporal's operational weight proves unsustainable on this cluster.

**Rejected — extending NATS+Postgres.** This is the status quo that produced Q8. It means
re-solving retries, timers, idempotency and replay by hand, which is the definition of the
problem.

### 2. Topology

New infrastructure:

- **Temporal Server** + **Web UI**, plus a **separate Postgres instance** for Temporal
  persistence — see 2a.
- **Workflow-worker pod(s)** — hosts workflow and activity definitions (Python SDK).
- A **namespace-registration init job**, analogous to today's `nats-init-job.yaml`. This is where
  retention is set — see 2c.
- The three scrapers become **activity workers** (Go SDK for http-worker, Python for the others).
  **In the first increment, not eventually** — see [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected), which rejected the NATS bridge that
  would otherwise have deferred this. Each runs as two deployments of one image during
  coexistence: one bound to NATS for v1, one to a Temporal task queue for v2.

The API keeps auth, CRUD, and quota enforcement, and gains "start / signal / query workflow." It
loses all five background loops. That is what lifts the current single-replica + `Recreate`
constraint (`app/api.yaml`) and makes the API horizontally scalable — a concrete architectural
payoff, recorded here so it is not mistaken for incidental. **The workflow worker is horizontally
scalable too** (stateless task-queue polling), so this removes the bottleneck rather than moving
it. The playwright worker stays vertically constrained regardless — that is a headed-Chrome fact,
not something the engine change addresses.

#### 2a. Temporal persistence is a separate Postgres *instance*

**Decision: a second Postgres StatefulSet, not a second database on the existing one.**

A separate *database* on the shared instance would not buy the isolation the separation is for: a
history-write burst would still degrade the app DB through shared connections, shared buffers and
shared disk. The instance boundary is the one that actually holds.

Two further reasons, both about lifecycle rather than load:

- **Backup/restore discipline differs.** The Temporal DB holds in-flight execution state — losing
  it loses running work, not just history (see Consequences). It wants a different restore posture
  than app metadata.
- **Schema ownership differs, and this is the one most likely to bite.** The app DB's schema is
  Alembic's, auto-applied from `api/app/main.py` on API startup. Temporal's schema is owned by
  **`temporal-sql-tool`** and versioned on Temporal's release cadence — a server image bump can
  require a schema step that nothing in our deploy flow performs. Two migration mechanisms is
  already a cost; entangling them on one instance compounds it.

Cost is small: cloning the `infrastructure/postgres.yaml` pattern (`postgres:16` StatefulSet,
100m/256Mi requests, 10Gi PVC) is roughly **+100–250m CPU and +256–512Mi memory in requests**,
plus a PVC.

**Note that Temporal wants two databases *inside* that instance** — `temporal` and
`temporal_visibility` — even on standard visibility. Provisioning one and wondering why startup
fails is the predictable way to lose an hour here.

#### 2b. The Web UI is not ingress-exposed

**Decision: no ingress, no DNS record, no cert. Access via `kubectl port-forward` only.**

The governing fact is that **the Temporal Web UI is not a read-only dashboard.** It can terminate
and cancel workflows, send signals, and reset a run to a prior point. Exposing it publishes a
*write-capable control plane over production orchestration*, which is a materially higher bar than
the mlflow precedent on this cluster (a `basicAuth` Middleware in front of experiment tracking).
OSS Temporal UI also ships with **no authentication of its own**, so whatever gates it we supply.

Compounding it: [§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary)
settles on a **single namespace** with the API as the *only* tenant boundary, so every tenant's
runs share one engine-side listing — a surface with none of the 404-not-403 discipline applied
everywhere else, and one the API's ownership check does not reach, because the UI talks to
Temporal directly.

Not exposing it removes both problems outright instead of mitigating them, adds no components, and
is **fully reversible** — an ingress is purely additive later. The accepted cost is that the UI is
unavailable without cluster access, which is tolerable because its value peaks during incidents and
those are worked from a machine with `kubectl`.

**Deferred to post-Phase 4, tracked in "Deliberately not decided here":** if always-on access is
wanted later, the two candidates are **Traefik `basicAuth`** (the mlflow pattern already in the
infra repo — cheap, but one shared credential, no attribution of who terminated what) and
**Traefik `forwardAuth`** against an API admin endpoint (one identity system, real revocation and
attribution, but it must be built, it couples UI availability to API availability, and it depends
on Clerk's session **cookie** being scoped to a domain the Temporal host shares — a browser
navigating directly sends cookies, not the SPA's bearer token). Neither is worth building for a
single operator today.

#### 2c. Namespace retention is 30 days

**Decision: register the namespace with a 30-day retention period.**

Retention governs how long a **closed** workflow's event history survives before cleanup deletes
it. It buys post-hoc debugging — the per-run timeline that replaces `grep status=` log-spelunking —
and the ability to *reset* a run, which only works while its history exists. It costs disk in the
Temporal DB, scaling with runs × events-per-run × payload size in history.

Three things make 30 days a low-stakes pick rather than a load-bearing one:

- **It has no correctness role.** [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
  forbids answering any user-facing question from Temporal's DB — run counting moves onto a view
  over app Postgres. So retention cannot silently redefine "runs this month". It is purely an
  operator dial.
- **It is changeable after creation.** Not a one-way door. The namespace needs *a* number to exist,
  not the *right* number.
- **Volume is nowhere near the constraint.** The BUG-003 prod audit had 15 completed runs to
  examine in total. At that scale the difference between 7 and 30 days is megabytes.

30 rather than 7 because retroactive archaeology is worth protecting — the BUG-003 investigation
found the 40% bot-wall rate by auditing runs *after the fact*, and a 7-day window would have to be
lucky. 30 rather than 90 because 90 retains history for runs nobody will open. 30 also aligns with
the monthly quota window and is Temporal's default, so it matches every doc and default encountered
later.

**The coupling worth stating explicitly: retention is cheap only because
[§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity) holds.** Under
references-not-payloads a five-block run is plausibly tens of KB of history. The moment an activity
returns page *content*, one run goes from ~50 KB to ~4 MB (real pages measured at 291 KiB–4.1 MiB)
and retention becomes the binding constraint overnight. If this number ever needs revisiting
urgently, suspect §5 first.

**Archival** — shipping closed histories to MinIO before deletion — is the escape hatch that would
make a *shorter* retention safe. Deliberately not enabled: another moving part, for data currently
worth very little.

#### 2d. Capacity

For the record, since "heaviest dependency we run" invites the question. The node (`kimsufi-server`,
8 CPU / 32 GiB) currently sits at **28% CPU requests and 11% memory requests**. Temporal Server +
its Postgres + Web UI + a workflow worker is roughly **+1.5–2 CPU and +2–3 GiB in requests** —
landing near 50% CPU requests, with memory still under 20%. Requests are not the constraint.

**This figure already is the coexistence peak, which is the number that matters.** The 28% baseline
includes NATS, the coordinator and all three workers, so adding Temporal on top describes the
cluster from **the worker port** through the flow cutovers (drawn in `temporal-full-migration.md` §9a) — both orchestrators running, both
worker sets alive, five loops still serving in-flight v1 work. Nothing later in the sequence is
heavier; every subsequent step *removes* a component. Sizing to steady state would have understated
it, and the several weeks spent at the peak are exactly when a capacity surprise would land.

**CPU *limits* are.** The node is already at **162% limit overcommit**, and the playwright worker
alone is 500m request / 2000m limit for headed Chrome. A simultaneous headed render and a Temporal
history burst means CFS throttling on the history service, which surfaces as workflow task timeouts
and retries. **That looks exactly like a workflow bug and is not one** — recorded here so it is not
debugged twice.

### 3. OQ-1(a) — Run identity: pipeline runs get their own table, and quota counting stops naming a table

**Decision: a new `pipeline_runs` table with a `pipeline_run_blocks` child table. Pipeline runs
are never `job_runs` rows. Quota counting moves off tables and onto a database view.**

Three options existed:

| Option | Verdict |
|---|---|
| (a) Pipeline runs write `job_runs` rows (a third parent FK) | Rejected |
| (b) New `pipeline_runs` + `pipeline_run_blocks` | **Chosen** |
| (c) No app-side run record; read run state from Temporal | Rejected |

**(a) buys nothing and pollutes.** `job_runs` carries job-shaped columns — `content_hash`,
`diff_detected`, `diff_summary`, `nats_stream_seq`, a single `result_path` — and its
`chk_job_runs_single_parent` constraint is a two-way exclusive-or that would become three-way.
More decisively, R3's biggest win is **per-block** status and timing, which a single row cannot
express: a child table is required either way. Once you have the child table, reusing the parent
row buys only the quota query, which (b) solves better.

**(c) fails on enforcement.** Quota checks would require a live engine call on every trigger, and
Temporal's retention policy would silently determine what counts as a run this month. Listing,
admin views and cross-tenant 404 checks all need SQL.

**The load-bearing half of this decision is the quota fix, not the table choice.** Today
`quota.py` hardcodes `FROM job_runs` in both `_count_monthly_runs` and `_count_concurrent_jobs`.
Neither is a stored counter — both recount on every check. A new table is therefore *invisible to
the meters by construction*: a user who exhausts 500 monthly job runs could trigger unlimited
pipeline runs, not because anyone decided pipelines are free, but because the query never looked.

> **This is not a prediction. It has already happened, to crawls.** `JobRun(...)` is constructed
> in exactly three places — `routers/jobs.py:207`, `routers/batch.py:105`, `core/scheduler.py:80`
> — and **never for a crawl**. Crawl work lives in `crawl_pages`, with its own `status` and
> `result_path`. So both count meters are already blind to the crawl lane, and
> `routers/crawls.py` carries **no quota check at all** (zero references to `check_user_quota`).
> A 500-page crawl costs the user **zero** monthly runs and **zero** concurrent slots today.
> Pipelines would be the *third* lane to arrive invisible, not the first — which makes this a
> live gap rather than migration preparation.

So: **introduce a database view that is the single definition of "a run this user started," and
point the quota queries at it.** Adding Monitors later is one view change rather than an audit of
every call site. This survives the migration — when `job_runs` becomes a read-model mirror of
Temporal state, the view is unaffected.

**PM decision — PRD-016 OQ-4, review round 3 (2026-08-08). ✅ Confirmed by owner 2026-08-08.**
Whether crawls join the view was a **product** question, not an architectural one: it changes what
a shipped, live feature costs. The answer is **yes, and the view carries four lanes, not two** —
job `job_runs`, batch `job_runs`, **`crawl_pages`**, and `pipeline_runs`.

The view is **one row per countable unit**, and the two meters aggregate it differently:

- **`monthly_runs` counts rows.**
- **`concurrent_jobs` counts distinct concurrency *groups*** among active rows, where the group is
  **the submission**: the crawl for crawl pages, the **batch** for batch items, and the row itself
  for single job runs and pipeline runs.

That split is deliberate: cost and contention are different quantities. Per-page concurrency would
put every default crawl (`max_pages` 100) permanently over the default ceiling of 5 — a meter that
makes a shipped feature unrunnable is misconfigured, not strict — and enforcing it would need
dispatch throttling inside `coordinator/`, which [§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block)
deletes.

> **Amended 2026-08-17 (§8 review), two changes.** ✅ Owner's call: **the group is the submission
> on every lane**, so a batch of N is one slot, not N. The earlier text grouped only crawl pages
> and left batch items as individual rows — which does not match `batch.py:46-47`, where admission
> already checks the ceiling **once for the whole batch** and then inserts N rows. Admitting as 1
> and metering as 100 locks a user out of a 5-slot pool with a single call. Crawls were never the
> only place cost and contention disagree; under this rule they disagree everywhere, by design.
>
> Second: **"non-terminal" is replaced by "active", and active is per-lane.** For v1 lanes it means
> exactly what `quota.py:59` means today — `pending`, `running`, `processing` — and R5 forbids
> narrowing it. For pipeline runs it excludes a run parked on a durable timer, without which
> [§15](#15-oq-11--webhook-delivery-is-a-step-the-run-waits-for)'s ≈2.6 h webhook horizon would let
> one dead receiver hold a slot for hours. The view therefore carries a lane-aware predicate on
> this column; that cost is accepted in
> [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored).

**What the view fixes, and what it does not.** It fixes *which rows count*. It does **not** fix
*when they are counted*: both meters recount on every check, so two concurrent creations can each
read 499/500 and both proceed. That race is pre-existing and low-impact at current volume, but a
second lane and an engine that makes concurrent starts easy both push against it. Recorded so a
reader does not assume the view closed it — converting the meters to stored counters is a separate
change, deliberately not made here.

> **This is BUG-005's lesson generalised.** That bug was not "batch used the wrong table." It was
> that three separate contracts — two message schemas and the MinIO path convention — hardcoded
> the assumption that every run has a `job_id`. Batch was the first run that wasn't a job, and it
> broke in three places. Pipelines are the second and larger instance of the same shape. Every
> decision in this section exists to make the identity explicit rather than assumed.

**`storage_bytes_used` is not exempt, and an earlier draft of this section wrongly said it was.**
It is a stored counter rather than a recount, which is why it looks lane-agnostic — but it is
incremented from exactly **one** call site, `result_consumer.py:85`, and guarded by
`run.storage_accounted_at`, a **`job_runs` column**. The crawl coordinator writes page bytes to
MinIO (`coordinator/result_handler.py:150` sets `page.result_path`) and never touches storage
accounting at all. So crawl bytes are uncounted today for the same structural reason crawl runs
are: the accounting is keyed on a column only one lane has.

All **three** meters are therefore blind to the crawl lane, not two. The correction matters because
the exemption told an implementer that storage needed no thought — and pipeline artifacts written
by an activity rather than by `result_consumer.py` would be uncounted by exactly the same
mechanism. [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)'s
storage rule presumes something does the charging; on the v2 lane that something must be named,
not inherited.

> **✅ Named 2026-08-25 — and the storage meter's answer is *not* this section's view.**
> [§8d](#8d-who-charges-and-what-happens-at-the-wall--settled-2026-08-25) settles the charging
> component (an activity in the workflow worker) and the record (one shared per-object ledger, with
> the meter reading `user_id` and `bytes` only). **That partly supersedes this section for storage.**
> The view remains the answer for `monthly_runs` and `concurrent_jobs`, which count *runs* — and
> runs genuinely do live in a different table per lane, so a single definition of "a run this user
> started" is exactly right. Bytes do not: under §8d they all land in one table keyed on the owner,
> so the storage meter is lane-blind before any view is involved and a storage arm of the view
> would be a layer over a table that does not need one. **Do not build one.** The two are the same
> principle — *stop naming a table* — reaching different mechanisms because runs and bytes are
> shaped differently.

**The workflow ID is the correlation key** between an app record and its engine execution —
`pipeline-run-{pipeline_run_id}`, and `job-run-{run_id}` for migrated jobs. This replaces today's
`nats_stream_seq` correlation, and `nats_stream_seq` is dropped when v1 retires.

> **✅ Settled on review (2026-08-08).** These formats are final and carry **no user identity** —
> [§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary) previously
> claimed `user_id` was encoded in the workflow ID and has been reversed to match. The ID does
> exactly two jobs: correlate an app row with its engine execution (here), and give
> [§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee)'s mechanism 2
> its engine-level double-start guarantee. It is **not** a tenant-isolation mechanism; the API's
> ownership check is the only one.

### 4. OQ-1(b) — Block model: fixed typed catalog, JSON in Postgres, explicit named wiring

**Decision: a fixed catalog of typed blocks, stored as JSON in Postgres. Every block carries an
identifier that is stable across versions of its pipeline and names its input by explicit
reference to an earlier block. Data flow in layer A is a single chain; the stored shape is a
graph, so relaxing that is a validator change rather than a data migration. Run inputs are
config bindings, not graph edges.**

- **Fixed catalog, not a general DAG schema.** R2's catalog is closed, user-authored code is a
  non-goal, and R1's save-time validation requires knowing each type's declared consumes/produces.
  A general DAG schema would defer all of that to run time.
- **JSON in Postgres, not a DSL.** A DSL needs a grammar, a parser, versioning *of the grammar*,
  and its own error messages. At five block types that is pure cost. JSON validates against a
  schema and produces field-level errors for free.
- **Explicit named input references, not implicit previous-block wiring.** PRD-016 is internally
  inconsistent without it: R1's validation clause says a block's input must be producible by
  "anything before it," while Non-goals says chains are linear. Both hold only if a block can name
  an earlier block. **Under the linearity decision below, explicit wiring buys no capability that
  implicit wiring could not express today** — it is bought for forward-compatibility (see the two
  bullets below on execution order vs data flow) and because named references need identifiers, which
  OQ-2's pinning and R3's per-block history require regardless.
- **Block identifiers are stable across versions, which is stronger than immutable.** "Immutable
  once assigned" is satisfied by an edit that regenerates every identifier — nothing changed, they
  are merely all new — and under that reading both benefits evaporate: per-block history cannot be
  correlated across versions, and user story 3 ("edit my schema and re-run") produces run history
  that looks like a different pipeline. **The property is therefore an assertion about the edit
  operation, not about the stored row: an update carries block identifiers through, a new
  identifier means a new logical step, and an identifier absent from an update is a deletion.**
  This is what makes OQ-2's pinning meaningful and what lets per-block run history reference a
  block a later edit removed.
- **Execution order and data flow are different properties, and only one of them is linear by
  necessity.** Sequential execution — one block at a time, no parallelism — is what PRD-016's
  "parallel fan-out" non-goal forbids. A *data-flow* fan-out (two LLM blocks both consuming the
  Scrape block's page, executed one after the other) violates neither parallelism nor branching.
  An earlier draft of this section used "a single chain" for both meanings and so rejected, in its
  validator rule, the very example it cited to justify explicit wiring.
- **Layer A validates a single chain in both senses. ✅ Owner's call, 2026-08-10.** Data flow is a
  path: block *n* consumes block *n−1*. Data-flow fan-out is wanted, but **deferred to post-Phase
  4** rather than smuggled in here. The stored shape stays a graph, so lifting the restriction is
  a validator change against unchanged stored definitions — which is the entire
  forward-compatibility requirement, still satisfied at zero cost.
- **The consequence, recorded as a known exclusion.** PRD-016's Problem section lists *"run two
  extractions on one fetched page"* among the things a user cannot do today. **Layer A does not
  fix it** — a second extraction still means a second Scrape, hence a second render and a second
  hit on the target site. This belongs in PRD-016's exclusion list alongside R6's four
  divergences; a problem statement the design does not answer must be visible, not implied by the
  absence of a feature.
- **Per-type config schemas are the residual of the DSL cost, and are versioned like the
  validator, not like the data.** Rejecting a DSL avoids a grammar and a parser; it does not avoid
  needing a schema per block type, a validator over it, and a story for changing it. Config
  schemas live with the block-type definitions in the workflow worker, versioned with the code.
  **The obligation this creates: the validator must remain able to validate historical config
  shapes**, because §6 pins definitions and a pinned old version keeps the config shape it was
  saved under. A block type that gains a required field therefore needs a default for already-saved
  definitions, exactly as a database column would. Adding an optional field is free.

> **✅ Settled on review (2026-08-10).** Five corrections, none cosmetic. **(1) Linearity is now
> stated in both senses** — execution order *and* data flow — where the section previously used
> one phrase for both and contradicted its own motivating example; data-flow fan-out is deferred
> post-Phase 4 and the unfixed PRD problem is recorded as a known exclusion. **(2) Run inputs are
> config bindings, not graph edges** (below), resolving a direct conflict with
> [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)'s
> "inputs are MinIO references" — a URL is not one. **(3) Block identifiers must be stable across
> versions**, not merely immutable once assigned. **(4)** The block-state column is now named
> rather than asserted. **(5)** Config-schema versioning is acknowledged as the residual DSL cost
> with the historical-validation obligation stated.

**Run inputs are config bindings, not graph edges — and only declared fields are bindable.**
A block reading an earlier block's output and a value supplied at trigger time are different
things: the first is a data-flow edge carrying a **MinIO object reference**
([§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)), the second is a
**scalar substituted into a config field**. R1 already separates them — its validation list has
one rule for "a block whose input cannot be produced by anything before it" and a second for "a
block referencing a run input the pipeline does not declare." So:

- **Scrape consumes nothing. It is a source block**, and its `url` config field is *bound* to a
  run input rather than fed by an edge. R1's "a chain that does not start with a source block" is
  read against this: the source is Scrape, not a run-input node.
- **Each block type declares which of its config fields may be bound to a run input.** Today that
  is exactly one field: Scrape's `url`, which is R1's stated minimum. **✅ Owner's call,
  2026-08-10 — narrow, not "any config field."**
- **The reason is that R1's promise is save-time validation, and a wide binding rule dissolves
  it.** Binding the LLM block's extraction schema at run time means Save cannot check the schema —
  the user learns it is malformed *after* paying for a render, standing at the LLM block. Binding
  the Webhook URL turns "at most one Webhook block per pipeline" from a save-time rule into a
  per-run one. Widening later is additive (declare one more field bindable); narrowing later
  breaks saved pipelines.

**The halt-early obligation (from the PM's OQ-10 decision) is satisfied structurally.** Monitors
(B) will need a block that ends a run before its last block *with the run still reporting
success*. Two rules make that additive rather than breaking:

- **Run outcomes stay three** — `pipeline_runs.status` ∈ `completed`, `failed`, `cancelled`. A
  halted-early run is `completed`.
- **Block state is a separate vocabulary from run outcome, and includes `skipped` and `waiting`
  from day one.** The column is **`pipeline_run_blocks.status`**, and its vocabulary is `pending`,
  `running`, `completed`, `failed`, `skipped`, `waiting`. Nothing in R2's catalog produces
  `skipped`, but the column admits it now so that B is a new block type rather than a schema
  change plus a backfill. ⚠️ **`waiting` was added by the §15 review (2026-09-02) for the same
  reason, and it is not idle forward-compatibility — it is load-bearing on day one.** A block on
  a durable timer holds no worker, and [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)'s
  concurrency rule ("a run parked on a durable timer is not active") has **no other column to
  read**: a webhook mid-backoff and an LLM call in flight are otherwise the same row.
  See [15a](#15a-parked-runs-do-not-hold-a-slot-is-a-property-of-the-runs-state-not-of-the-webhook-block-type--and-no-column-expresses-it-today),
  which also settles that *which* blocks may enter it is decided by §5's content-producing /
  effect split, never by enumerating block types.
  **Naming the column matters:** an earlier draft asserted "the column admits it" without saying
  which column, and §3 introduces `pipeline_run_blocks` without enumerating its columns — between
  them, the one defence against a future backfill was a claim about a column no section defined.
  A `CHECK` constraint written from the states in use would produce exactly the migration this
  rule exists to avoid.
  **One value covers both cases on purpose:** a block skipped because an upstream gate halted the
  run (B) and a block skipped because its branch was not taken (conditionals,
  [§14](#14-oq-10-remaining-half--conditional-execution-gets-its-own-layer-a-prd-before-monitors))
  are the same fact from the run's perspective — it did not execute, and that is not a failure.
  If the two ever need distinguishing, that is a *reason* column beside the state, not a second
  state.

Overloading a single status vocabulary across two levels of a hierarchy is exactly what Q8 was.
Keeping block state and run outcome distinct is that lesson applied before the fact.

### 5. OQ-1(c) — Blocks pass references; artifacts are keyed on run identity

**Decision: block inputs and outputs are MinIO object references. Page content never enters
workflow history. The v2 artifact path is keyed on the pipeline run and block, not on a job.**

> **Knock-on from [§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)'s
> review (2026-08-10), to be carried into §5's own review.** "Inputs and outputs are references"
> is a statement about **data-flow edges between blocks**. It is not true of **run inputs**, which
> are scalars bound into a block's config (§4) — Scrape's URL is a string, not a MinIO object, and
> Scrape has no input edge at all. Run-input scalars *do* enter workflow history, which is
> harmless and intended: they are the run's arguments, they are bounded, and a run that could not
> see its own inputs could not replay. The rule to carry forward is narrower than it reads:
> **content is never a payload; arguments may be.**

Activity inputs and outputs are recorded in workflow history, which caps individual payload size
and bounds total history size. **The figures, confirmed against Temporal's documentation on
2026-08-10** (an earlier draft said only "low single-digit MB", which understated it):

| Limit | Warn | Error |
|---|---|---|
| Single payload / blob | **256 KiB** | **2 MiB** |
| gRPC message | — | 4 MiB |
| Event history, size | 10 MiB | 50 MiB |
| Event history, event count | 10,240 | 51,200 |

Set those against the BUG-003 audit, which measured genuine pages between **291 KiB and 4.1 MiB**:

- the **largest** measured page is **over twice the hard 2 MiB payload limit** — a content-passing
  model does not degrade on it, it fails outright;
- the **smallest** measured page already **exceeds the 256 KiB warn threshold**. Not the outliers —
  the whole measured range is above the line where Temporal starts complaining.

So the decision is not "content-passing fails on unusually large pages." It is that **essentially
every real page we have measured is at or past a payload threshold**, for reasons that have
nothing to do with scraping.

> ⚠️ **The escape hatch is real and taking it is the wrong move.** We self-host
> ([§2](#2-topology)), so unlike Temporal Cloud these limits *are* configurable
> (`blobSizeLimitError`, `historySizeLimitError`). Raising them is the obvious-looking fix and it
> silently undoes this section **and** [§2c](#2c-namespace-retention-is-30-days) together: content
> moves into history, and history is retained 30 days after completion. §2c's warning — "if this
> number ever needs urgent revisiting, suspect §5 first" — is describing exactly this failure,
> reached by config change rather than by code.

The subtler cost: **workflow history is retained after completion by design** — that retention is
what buys replay and resumption. Today's NATS stream is `--retention work`, so acked messages are
deleted and orchestration state is effectively free and self-cleaning. History is not. Keeping
payloads out of it is what keeps that bill proportionate, and this is **the largest new
operator-side cost in the migration**.

R2's "each block declares what it consumes and produces" is therefore read as declaring **types
of reference**, not types of payload.

**Not every block produces an object. ✅ Owner's call, 2026-08-10.** An earlier draft's "one
immutable object per block per run" was false of two of the five catalog types, and
[§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)'s
strict-path data flow makes that load-bearing rather than pedantic: block *n* consumes block
*n−1*, so every non-terminal block must emit something its successor can consume. R2's catalog
therefore splits in two:

| Kind | Blocks | Produces | Emits as its output |
|---|---|---|---|
| **Content-producing** | Scrape, Clean, LLM extract | a new object | a reference to that object |
| **Effect** | Validate, Webhook | nothing | **its own input reference, unchanged** |

- **Effect blocks are pass-through by contract, not by accident.** Validate asserts on its input
  and either continues or fails the run terminally; Webhook delivers. Neither transforms content,
  so neither writes an object. The alternative — writing a byte-identical copy so that every block
  has "its own" artifact — is storage spent to satisfy a naming convention, on every run.
- **Two blocks may therefore name one object**, which is the constraint that makes
  [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)'s
  garbage collection non-trivial: collecting "block *n*'s output" must not orphan a reference held
  by block *n+1*. Objects are collected by **run**, on the rules in §8 — never per block.

**This is what makes "the result" well defined, and R6 is why it had to be settled here.** R3 says
the final block's output is retrievable as *the* result. R6's acceptance-gate pipeline is
`scrape → LLM → webhook` — **it ends in an effect block.** Read naively, that pipeline has no
result at all, which is
[§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)'s
invisible-lane bug in a different costume. So, stated once:

> **The result of a run is the output of the last *content-producing* block in execution order.**
> An effect block that runs last does not change the answer — it delivers or asserts on the
> result; it does not replace it.

**⚠️ Amended 2026-08-17 (§8 review): this defines the result, and no longer defines what is
charged.** The original text made one object serve both purposes — *"that same object is the
artifact charged to `storage_bytes_used`"* — and that clause is **withdrawn**. §8 now charges
**every object the run currently holds**, so the result is simply the one that is never collected
and the one R3 returns; the others are charged while they exist and release their charge when
collected. The paragraph above originally carried a second argument, that the naive reading would
also charge zero storage against a job that charges the LLM output. **That comparison was factually
wrong** — the job path charges the *scraped page*, not the LLM output ([§8a](#8a-what-the-storage-rule-costs-on-the-job-path-found-in-review-2026-08-17)) —
and it is removed rather than repaired, because under the new rule the size of any single block's
output no longer determines what a run costs.

**The v2 path convention:**

```
pipelines/{pipeline_run_id}/{block_id}.{ext}   — one immutable object per content-producing block
```

- **The key is deterministic, and that is a guarantee rather than an incident.** An activity that
  fails after uploading and is retried by Temporal re-uploads to the *same* key, so a retry is
  idempotent at the storage layer. The contrast is BUG-005, where the key was derived from a value
  that was NULL for batch runs: a non-deterministic key collided across tenants, while a
  run-and-block-keyed one cannot collide at all.
- **The path carries no tenant segment, and does not need one** — `pipeline_run_id` is unique, so
  BUG-005's shared-object failure is unreachable here. But the reference stored in workflow history
  is a **bare path**, so anything that resolves one into bytes must ownership-check the run first.
  This is [§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary)'s
  "there is exactly one tenant boundary, and nothing at the engine backs it up," at the storage
  layer, where BUG-005 proved no 404 guard reaches.

Two deliberate departures from ADR-002 §4:

- **Keyed on the run, not on a definition.** `history/{job_id}/…` assumes a stable parent that a
  pipeline with run inputs does not have. This is the same assumption BUG-005 broke.
- **No `latest/` write.** Its semantics — "the newest result for this thing" — are job-shaped. A
  pipeline that takes a URL as a run input has no single "this thing," which is the same reason
  the cost gate cannot live in layer A (OQ-10). Writing a `latest/` object anyway would recreate
  BUG-005's shared-object collision exactly.

This **partially supersedes ADR-002 §4 for the v2 lane only**. v1 keeps its convention until
retired. Note that BUG-005's fix re-keys the v1 batch path on `run_id`, which converges the two
conventions rather than forking them further.

> **✅ Settled on review (2026-08-10).** The decision stands and is **more load-bearing than the
> section claimed**. Four changes: **(1)** the payload/history figures are now measured rather than
> hedged — at a **256 KiB warn / 2 MiB error** blob limit, the BUG-003 range means the *largest*
> real page is over twice the hard limit and the *smallest* is already past the warn line, so this
> is not an large-page edge case; plus the self-hosting escape hatch is named as a trap, because
> raising the limit undoes this section and §2c together. **(2)** The catalog splits into
> content-producing and effect blocks; "one object per block" was false of Validate and Webhook,
> and §4's strict-path data flow made that a correctness question rather than a wording one.
> **(3)** "The result" is pinned to the **last content-producing block** — without which **R6's
> own acceptance-gate pipeline**, which ends in a Webhook, has no retrievable result. *(Amended
> 2026-08-17: this originally also pinned "the charged artifact" to the same block. §8 now charges
> every object a run holds, so the two claims have been separated and only the result survives
> here.)* **(4)** Deterministic keys are stated as an idempotency guarantee, and the absence of a
> tenant segment is tied back to §12's single boundary.

### 6. OQ-2 — In-flight edits: definitions are pinned, and that is a different problem from code versioning

**Decision: a run executes the definition version it started with. Pipeline definitions are
immutable versioned rows; an edit creates a new version; a run records the version it pinned in
`pipeline_runs.pipeline_version_id`.**

**The reason is semantic, not mechanical.** A run that has already executed blocks 1–3 of the old
shape cannot meaningfully continue into a *different* block 2. If the edit deleted a block the run
has already executed, there is no answer to "which block is it on"; if it changed the LLM schema,
the run produces output half-conforming to each. This is true of any engine and would be true with
no engine at all.

> **⚠️ Corrected on review (2026-08-10) — the mechanical argument an earlier draft gave was
> wrong.** It claimed replay "reconstructs in-memory state by re-reading history against the
> current definition," making a mid-run edit a determinism violation. **Temporal's replay
> re-executes the workflow function against the recorded event history, and the workflow's input
> arguments are part of that history** (confirmed against Temporal's documentation). So a
> definition passed in as a workflow argument is *automatically* pinned — replay reuses the
> recorded argument and never reads the current row. The described failure is reachable only by a
> workflow body that loads the definition from Postgres mid-run, which the determinism rule at the
> end of this section already forbids. **Leaving the wrong reason in place was the actual risk:**
> it invites a correct rebuttal — *"we pass it as an argument, so replay is safe, so we may adopt
> edits"* — against a decision whose real basis is the paragraph above, which that rebuttal does
> not touch.

**The mechanism follows from the correction: the definition is a workflow input argument.** That
is what makes pinning free rather than something we enforce. It is also consistent with
[§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)'s rule as settled —
*content is never a payload; arguments may be* — and a definition is bounded by R1's
max-blocks-per-pipeline, so it is small against the 2 MiB payload limit. The standing prohibition
is narrower and sharper than "don't adopt edits": **the workflow body must never load the
definition itself.**

**Retention, naming, and deleting a pipeline.** R1's "deleting a pipeline must not destroy the
history of runs already executed from it" means version rows are retained as long as any run
references them. Two consequences that an earlier draft left unstated, both settled here:

- **A pipeline with a run in flight may be deleted, and the run finishes. ✅ Owner's call,
  2026-08-10.** The run already pinned its version, that version is retained, and the run is its
  own record. Cancelling a run is R3's explicit, separate operation — deleting the definition is
  not a back-door cancel. This mirrors the Q4 decision on jobs, where retiring a schedule and
  cancelling a run were deliberately split rather than folded into `DELETE`.
- **The run history holds the name, not the pipeline. ✅ Owner's call, 2026-08-10 (option C).**
  Deleting a pipeline that has **no runs** deletes it outright and frees the name immediately.
  Deleting one that **has runs** soft-deletes it; the retained rows keep holding the name, and
  reusing it returns **409**. The reason to hold a name is that run history refers to it — no
  history, nothing refers to it, nothing to hold. This lands close to the `api_keys` precedent
  (*"revoked keys still hold their name — names are identifiers, not recycled"*) without
  over-applying it: a revoked key holds its name for a different reason that always applies (the
  key string may be recorded elsewhere), whereas a never-run pipeline is referenced by nothing.
  The alternative — freeing the name on every delete — makes run history ambiguous by
  construction: two pipelines named "Price watch", possibly different URLs and different schemas,
  and no way to tell from a run which one produced it.

**Two versioning problems are easy to conflate, and only one is solved above.**

- **The user's definition** (data) — solved by pinning.
- **Our workflow code** (the interpreter) — *not* solved by pinning, and still requires
  Temporal's own versioning discipline. Changing how the engine interprets a Clean block affects
  in-flight runs regardless of which definition version they pinned.

Pinning means **the cook keeps the recipe card they started with**; it says nothing about swapping
the cook mid-dish. **Worth noticing: Temporal's current answer to the second problem has the same
shape as our answer to the first** — with pinned Worker Deployment Versions an execution runs
entirely on the worker version it started on. Pin the recipe, pin the interpreter. The mechanism
is **deliberately not chosen here** (see the deferral table), but it is not an open-ended question:
**Worker Versioning is GA and is Temporal's stated default**, patching is the older approach that
leaves conditional branches to clean up later, and the pre-2025 *experimental* Worker Versioning is
already withdrawn from the server — so the choice is between the current mechanism and patching,
not among three. It needs **server-side enablement** on our self-hosted deployment
([§2](#2-topology)), which makes it an infra task rather than a code flag.

The second brings a **new failure class**: workflow code must be deterministic. No I/O, no
`datetime.now()`, no `random()`, no direct database or MinIO access inside a workflow body —
those belong in activities. This is a standing review rule, not a one-time cleanup.

**Two obligations elsewhere in this ADR are this rule seen from the other end:**

- [§10](#10-oq-6--the-do-not-delete-list)'s do-not-delete list — the LLM cold-start
  `ensure_ready()` probe and the transient/terminal storage classifier — must be ported into
  **activities**. Both do I/O; this rule is precisely what forbids them in a workflow body. The two
  sections are read together by whoever does the port, and neither previously pointed at the other.
- [§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)'s
  config-schema obligation exists *because* of the pinning decided here: a pinned old version keeps
  the config shape it was saved under, so the validator must remain able to validate historical
  shapes, and a block type that gains a required field needs a default for definitions already
  saved.

> **✅ Settled on review (2026-08-10).** The decision stands; **its stated reason did not.** The
> replay argument was factually wrong (arguments live in history, so a definition passed in is
> pinned automatically) and, being wrong, invited the opposite conclusion from anyone who knew
> Temporal — the real basis is the semantic incoherence of continuing a run into a changed shape.
> Four things previously unstated are now decided: the definition travels as a **workflow input
> argument**; the pinned version is recorded in **`pipeline_runs.pipeline_version_id`**; a pipeline
> with a run in flight **may be deleted and the run finishes**; and **run history holds the name**
> — delete with no runs frees it, delete with runs soft-deletes and 409s on reuse.

### 7. OQ-3 — One lane: disjoint identity, plus an engine-level uniqueness guarantee

**Decision: "exactly one lane" is enforced by four stacked mechanisms, none of which is a
routing flag or a convention. They do not all cover the same cases — mechanisms 1 and 4 divide
between them, and which one applies depends on whether the work is a pipeline or a migrated job.**

1. **Disjoint identity spaces — pipelines only.** A pipeline run is a `pipeline_runs` row and
   never a `job_runs` row. v1 executors read `job_runs` and NATS subjects; v2 executors read
   Temporal task queues. No object exists that *could* be picked up by both.
   ⚠️ **This covers layer A and nothing else.** A **migrated job keeps its `job_runs` row** —
   [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
   makes that table a read-model mirror of Temporal state rather than replacing it, because R5
   forbids user-visible change and the job API, SPA, admin views and quota view all read it. So
   from **the job cutover** onward the same row is visible to both lanes **by requirement**, and
   mechanism 1 stops applying exactly when the problem becomes real
   ([§16](#16-the-v1v2-coexistence-contract) says the same thing from the sequence side).
2. **Workflow ID uniqueness, for flows that do migrate.** When jobs move to `JobWorkflow`, the
   workflow ID is derived from the run identifier (`job-run-{run_id}`). Temporal refuses to start
   a second execution with a workflow ID that is already **open** — the default Workflow ID
   Conflict Policy is `Fail` — so a concurrent double-start is impossible *at the engine* rather
   than prevented by a check we wrote.
   ⚠️ **Pin `WorkflowIdReusePolicy.REJECT_DUPLICATE`, or this is weaker than it reads.** The
   default reuse policy is **`ALLOW_DUPLICATE`**, which permits a *new* execution with the same ID
   once the previous one has **closed**. That yields "never two at once," not "this run executes
   once, ever" — and R5 asks for the second. A run identifier is genuinely single-use, so
   rejecting duplicates costs nothing. Note also what would silently destroy the guarantee:
   `TERMINATE_IF_RUNNING` converts a refused double-start into a kill-and-restart.
3. **`schedule_status` is the interlock for recurring work** (cutover gotcha #2). A job moved to
   a Temporal Schedule must be `paused` in v1 or it fires on both lanes. This is what Q4's
   deliberately tri-state flag is for.
4. **A lane marker on `job_runs`, from the job cutover. ✅ Owner's call, 2026-08-10.** A column
   written in the **same transaction as the row insert** (a later write leaves a window in which a
   v2 row is indistinguishable from a v1 row), and every v1 background query that dispatches work
   filters on it. This is mechanism 1's disjointness extended to the rows that cannot have it
   structurally. It is **job-cutover work, not day-one work** — §16's routing rule keeps jobs on v1
   until their flow is explicitly migrated, so layer A ships without it — but it is recorded here
   because §7 is what someone will consult *at* the job cutover. ⚠️ **Named, not numbered** — this
   obligation was written as "step 2" and the sequence has since been reordered under it
   ([§16 16a](#16a-steps-get-names-because-the-numbers-have-already-moved-once)).

**Why mechanism 4 is required and not belt-and-braces.** `_recover_stale_pending`
(`core/scheduler.py:131`) selects **every** `job_runs` row with `status = 'pending'` older than
`stale_pending_threshold_minutes` (default **10**) and re-publishes it to
`scrapeflow.jobs.run.http` / `.playwright`. It carries no lane filter and cannot know a workflow
owns the row. So: a job migrated to v2 whose workflow has not yet started — worker pod down,
task-queue backlog, Temporal unreachable — sits at `pending`, and ten minutes later **v1
dispatches it to a NATS worker**. When the workflow worker returns, the same URL is scraped a
second time and an LLM stage bills the user's key twice. Mechanism 2 does not intervene: v1 never
started a workflow, it published a message, so the engine had nothing to refuse.

Two things make it worse than a corner case. It fires **precisely when v2 looks stalled**, so the
operator's model is "nothing is running" while v1 quietly ran it; and it is **silent** — nothing
records that two lanes touched one run.

> ⚠️ **A documentation trap behind this.** `phase4-backlog.md` §3 lists `_recover_stale_pending`
> under "dissolved by Temporal — do NOT fix," because it exists only to police the hand-rolled
> scheduler. That is true of the **end state** and false of the **transition**: the loop is live
> for the whole coexistence period, which is exactly when this risk exists. *"Dissolved by the
> migration"* and *"safe during the migration"* are different claims, and §3 of the backlog only
> makes the first.

**The one v1 loop that is already safe is safe by accident, which is the argument for making it
deliberate.** `advisory.py:28` matches runs on `nats_stream_seq`, which is `NULL` for anything v1
never dispatched — a lane marker in disguise. It works, but the column it depends on is dropped
when v1 retires ([§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)),
so the protection is a side effect of something on its way out.

**Ordering matters for mechanism 3, and the safe order is the counter-intuitive one:** pause in
v1 → confirm no v1 dispatch is in flight → *then* create the Temporal Schedule. The reverse order
leaves a window in which both lanes are armed. A double scrape costs a double render; a double
LLM stage bills the user twice.

**Rollback has the mirror-image ordering, and it is not symmetric by intuition.** §16 claims every
step is reversible, which means the reverse move needs the same discipline: **pause the Temporal
Schedule → confirm no v2 execution is in flight → only then set `schedule_status` back to
`active`.** Un-pausing v1 first re-arms both lanes just as surely as creating the Schedule too
early does. Written down because whoever performs it is rolling back under pressure.

**One property helps, but only for the half that was never dangerous.** The NATS stream is
`--retention work`, so a message *acked* by v1 is deleted and there is no replayable backlog a v2
executor could later re-consume. The risk is the **unacked** message — in flight, or unprocessed
on the stream at the moment of cutover — which is still deliverable to a v1 worker. Retention says
nothing about those. The check that does is already written as §16's **deletion gate** (zero
unprocessed, zero outstanding acks, verified with `nats consumer info --json`); it is a **cutover
gate too**, not only a deletion gate, and the two sections should be read together.

> **✅ Settled on review (2026-08-10).** The section under-covered its hardest case. **(1)**
> Mechanism 1 is now explicitly **pipelines-only** — a migrated job keeps its `job_runs` row by
> requirement, so at the job cutover the covering set drops to mechanism 2 alone. **(2)** A **fourth
> mechanism** is added: a lane marker on `job_runs`, because `_recover_stale_pending` is a live v1
> dispatcher that re-publishes any stale `pending` row with no lane awareness — verified in code,
> 10-minute default threshold. **(3)** Mechanism 2 **over-claimed**: the default reuse policy
> allows a fresh execution once the previous one closes, so `REJECT_DUPLICATE` must be pinned for
> "once, ever" rather than "never two at once." **(4)** The `--retention work` reassurance
> addressed the safe half; the unacked-message case is now tied to §16's drain gate. **(5)** The
> rollback ordering for mechanism 3 is stated, since §16 claims reversibility at every step.

**Carried to [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)'s review — mechanism 1 is
true of rows, not of messages.** Under the draft's option (a) a v2 activity dispatched to the
existing NATS workers, so v2 work traversed v1 infrastructure by design. The return path was the
sharper half: the worker publishes to `scrapeflow.jobs.result`, where `result_consumer.py` is
subscribed, and its routing (`result_consumer.py:616`) branches on `run.job_id is not None` /
`run.batch_item_id` — a pipeline-originated result has neither. That is BUG-005's shape one lane
later.

> **✅ Resolved by the §9 reversal, 2026-08-23 — this hazard is moot.** Option (a) is rejected, so
> **v2 publishes no NATS results at all** and nothing v2-originated ever reaches
> `result_consumer.py`. The §9 review also found the failure was one step earlier and more final
> than described here: `_handle_result` resolves the run with `db.get(JobRun, run_id)`
> (`result_consumer.py:608`), so a pipeline-run id matches no row, and the handler **acks** —
> deleting the message on a work-queue stream — before the FK branch is ever reached. Mechanism
> 1's "rows, not messages" limitation stands as written; it simply no longer has a v2 message
> path to be limited against.

**⚠️ Mechanism 3 has an unowned behavioural gap: a scheduled job blocked by quota *waits* today,
and no Temporal Schedule overlap policy reproduces that.** Surfaced by the [§10](#10-oq-6--the-do-not-delete-list)
review 2026-08-26 and recorded here because it is a Schedule question, not an activity one.

`_dispatch_due_jobs` checks both count meters before creating a run (`scheduler.py:65-78`). On a
breach it logs and `continue`s — and, critically, **does not advance `next_run_at`**. The row stays
`due`, so the 60-second poll retries it until a slot frees. Nothing is dropped; it is a waiting
room, and a concurrency breach in particular clears in minutes.

Temporal Schedules offer `SKIP`, `BUFFER_ONE`, `BUFFER_ALL`, `CANCEL_OTHER` and
`TERMINATE_OTHER`. **None of them is this.** `SKIP` discards the firing permanently — a user who
is briefly at their concurrency ceiling silently loses that run, which R5 forbids as a
user-visible regression. `BUFFER_ONE` holds exactly one and drops the rest. And the closer
mismatch: overlap policies react to *a previous execution still running*, whereas this gate reacts
to *the account's meters*, which a Schedule cannot read.

[§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
makes the meters able to **count** every lane; it never says what a Schedule does when a meter says
no. So the admission check must live in the **workflow**, not in the Schedule: the Schedule fires
unconditionally, and the workflow's first step consults the counting view and either proceeds or
parks on a durable timer and re-checks — reproducing the waiting room, and composing with
[§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)'s
rule that a run parked on a timer holds no slot. **Left as a named open item rather than decided
here**, because it interacts with §8's headroom buffer (a storage breach does *not* clear on its
own, so parking forever is wrong for that meter and right for concurrency) and that pairing has
not been reviewed.

### 8. OQ-4 — Metering: one run is one unit, pools are shared, and storage is charged for what is stored

**Decision:**

**The unit, stated once for every lane** (PM, PRD-016 OQ-4 round 3 — ✅ owner-confirmed
2026-08-08): **one fetch of one target URL that is attempted.** Not one user action, and not one
step. So: job run = 1; batch of N = N; **crawl of N pages = N**; pipeline run = 1, because a
pipeline can only fetch once (below) and the non-Scrape blocks fetch nothing. **Admission checks
the declared ceiling; the meter charges actuals** — a crawl is pre-checked against `max_pages`
exactly as a batch is against `len(urls)`.

**"Attempted", not "produces one stored result" (corrected in review, 2026-08-17).** The earlier
wording described an outcome the meter never waits for. Every meter counts rows, and the row is
created when the work is *dispatched*, not when it succeeds — `JobRun` at submission,
`CrawlPage` at `dispatcher.py:103`. **A failed scrape already consumes a monthly run today**, and
that is the defensible behaviour: the fetch was attempted, the target was hit, the capacity was
spent. Defining the unit by its result would have made the meter unimplementable without a
second write-back on completion, for no user-visible gain.

**A retry is not a new unit.** A Temporal activity retry, a NATS redelivery and a
`_recover_stale_pending` re-publish are all the same attempted fetch. This is stated because it is
the first question the definition invites, and because getting it wrong reproduces Q5/Q6/Q7 on the
billing axis instead of the execution axis. The unit is one *logical* fetch; the retry budget is a
property of that unit, not a multiplier on it.

- **`monthly_runs_limit`: a pipeline run is one unit, regardless of block count.** Per-block
  metering makes the R6 gate pipeline cost 3 units where the identical job costs 1 — a direct
  violation of the PM's hard constraint (a), and it makes pipelines a *penalty* for expressing
  the same work differently. The arbitrage risk in the other direction is bounded structurally by
  R1's max-blocks-per-pipeline, and the genuinely expensive resource — LLM tokens — is billed to
  the user's own provider key regardless.

  **✅ The PM's multi-Scrape rider is resolved, 2026-08-17 (owner's call). A layer-A pipeline
  cannot fetch twice, and the reason is structural, not a cap.** The rider assumed "one run, one
  fetch" was a policy choice that a later feature could quietly invalidate. It is not: it follows
  from two [§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)
  rules acting together — layer A validates a **single chain in data flow**, where each block
  consumes the previous block's output, and **Scrape consumes nothing**. A Scrape block therefore
  has no valid position except first, and a second one is unsatisfiable at Save.

  ⚠️ **The earlier justification for the same conclusion was wrong and is withdrawn.** It read
  "R1 fixes one run to one URL". R1 does not: it makes the URL an *optional* run input, so nothing
  in R1 stops a pipeline from carrying two Scrape blocks with two URLs typed into their configs
  and never using a run input at all. The right answer was reached from the wrong premise, which
  is worth recording because the wrong premise is the plausible-sounding one.

  **Because the guarantee is emergent, it must be written down where the validator is built:
  a layer-A pipeline has exactly one starting block, and it is a Scrape block.** Nothing in §4
  says this in one sentence; it falls out of two rules stated pages apart. A validator implemented
  as "every block consumes the previous one, unless it declares no input" satisfies §4 as written
  and admits a second Scrape — at which point the metering rule silently under-charges. **The
  counting rule may not rest on a property no single document asserts.**

  **What would reopen this is a second starting block, not fan-out** — the rider named the wrong
  trigger. Fan-out (one output feeding several blocks, deferred post-Phase 4 by §4) still has one
  Scrape and still costs 1. Multiple *roots* — a pipeline that fetches two pages and merges them —
  is the change that makes a run cost 2, and it is not on any roadmap. The PM's preference stands
  for that day: **count executed Scrape blocks** rather than cap Scrape at one.
- **`concurrent_jobs_limit`: one shared pool, and the unit of contention is one submission.**
  The limit exists to protect worker capacity, and worker capacity is shared between lanes. A
  separate pipeline pool would let one user consume twice the capacity, which inverts the limit's
  purpose.

  **✅ Owner's call, 2026-08-17: one submission occupies one concurrency slot, on every lane.**
  A job run, a batch of any size, a crawl of any size and a pipeline run each hold exactly one.
  **Cost and contention therefore disagree by design and in general** — `monthly_runs` counts
  attempted fetches, `concurrent_jobs` counts submissions in flight. The previous text called the
  crawl case "the one place cost and contention deliberately disagree"; that was wrong on its own
  terms, because **batch already disagrees, in the opposite direction and by accident**:
  `batch.py:46-47` admits a batch by checking `concurrent_jobs` **once, as a single unit**, then
  inserts N `job_runs` rows, so a 100-URL batch is admitted as 1 and immediately meters as 100 —
  locking the user out of a 5-slot pool with one call. Making the rule uniform fixes that as a
  side effect rather than as a special case. ⚠️ **This is a live behaviour change on the batch
  path**, and the only one in this section; it is a loosening, so it cannot break an existing
  caller.

  **✅ Owner's call, 2026-08-17: a pipeline run waiting on a durable timer does not hold its slot;
  v1 lanes are unchanged.** §15 lets a Webhook block wait for real delivery on a ≈2.6 h horizon,
  and defended the resulting long-lived runs by asserting the ceiling "counts runs actively
  executing a block, not runs that exist". [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
  defines the counting view as *not yet finished*. **These are different definitions and §15's
  argument only survives under the first**, so the divergence is resolved explicitly rather than
  left for whoever writes the view.
  - **v1 (jobs, batches, crawls): unchanged.** `quota.py:59` counts `pending`, `running` and
    `processing`, and a `pending` row is doing nothing while still holding a slot. That is
    today's shipped behaviour; R5 forbids changing it, and narrowing it would be a silent
    loosening of a live limit.
  - **v2 (pipeline runs): a run parked on a durable timer is not active.** It occupies no worker,
    and charging for it would let one unreachable webhook receiver consume a user's pool for
    hours. Blocked-on-delivery is the *expected* state of a §15 run, not an anomaly.

  The cost is that the counting view is not lane-blind on this axis: it needs a per-lane predicate
  for "active". That is a real cost and it is accepted, because the alternative is either
  reopening §15 or changing v1 behaviour.

  ⚠️ **Amended by the §15 review, 2026-09-02 — this rule had no column to read.** "Parked on a
  durable timer" was not expressible: §4's block vocabulary had no waiting value and its run
  vocabulary is three terminal outcomes, so a webhook mid-backoff and an LLM call in flight are
  the same row. The v2 arm of the view is now **"a run with at least one block in `running`"**,
  against a vocabulary that gained `waiting`; and which blocks may enter `waiting` is decided by
  §5's content-producing / effect split rather than by naming the Webhook type, because every
  layer-C sink delivers to user-owned infrastructure and wants the same horizon. Full reasoning:
  [15a](#15a-parked-runs-do-not-hold-a-slot-is-a-property-of-the-runs-state-not-of-the-webhook-block-type--and-no-column-expresses-it-today).
- **`storage_bytes_used`: every stored object is charged, on every lane, for as long as it is
  stored.** **✅ Owner's call, 2026-08-17 — this reverses the previous rule and part of §5.**

  The meter measures **bytes on disk**, not "the result". If an object exists in MinIO on the
  user's behalf, it is charged; when it is deleted — by the user, or by intermediate-output
  collection — the charge is released. Storing something for the user and not charging for it is
  the defect, whichever lane does it.

  **Withdrawn:** §5's clause that *the charged artifact is the last content-producing block's
  output*, and this section's "every stored **final** artifact is charged". **Retained from §5,
  and still load-bearing:** that the same output is **the run's result** — what R3 returns, what
  the SPA shows, and what is never collected. The two ideas were fused and only one of them was
  about billing:

  | | definition | used for |
  |---|---|---|
  | **the result** | last content-producing block's output (§5, unchanged) | R3 display, permanence, what survives collection |
  | **what is charged** | every object currently stored for the run (new) | `storage_bytes_used` |

  This is simpler than what it replaces — it needs no notion of finality, so it is unaffected by
  fan-out, by effect blocks producing nothing, and by two blocks naming one object. It also makes
  intermediate-output collection **a user-visible benefit rather than housekeeping**: when a
  pipeline's intermediates are collected, the user's storage number actually falls.

  Applied to the other lanes: **every crawl page is charged** (a crawl has no intermediates, so
  nothing changes there), and **screenshots are chargeable** — they are stored on the user's
  behalf today and counted by nothing, which is BUG-004's other half and an argument for fixing it
  rather than deferring it further.

  The **call sites are not lane-agnostic and must be fixed** (see
  [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)).

#### 8a. What the storage rule costs on the job path (found in review, 2026-08-17)

The rule is a reversal, so the gap between it and live behaviour has to be stated. **Today an LLM
job stores the scraped page and the extracted JSON as separate objects, charges only the page, and
on delete subtracts only the JSON.** Traced end to end:

| step | code | effect |
|---|---|---|
| scrape completes | `result_consumer.py:411` | adds **HTML** size, sets `storage_accounted_at` |
| LLM completes | `result_consumer.py:485` → `:81` | tries to add **JSON** size, **skipped** — stamp already set |
| run finalised | `result_consumer.py:500` | `result_path` repointed at the **JSON** |
| worker wrote | `llm-worker/worker/storage.py:23` | JSON to a **new** key; the HTML is still there |

The short-circuit in `_try_increment_storage` is not itself wrong — it exists to make NATS
redelivery idempotent, which is necessary. **Its granularity is wrong: it is keyed on the run when
it should be keyed on the stored object.** A redelivery of the same result and a genuinely second
artifact are indistinguishable to it. Same pattern on the batch path (`:237` scrape, `:274` LLM).

Two consequences, both defects under the new rule and neither previously filed:

- **The counter is permanently inflated by every deleted LLM job.** Hard delete enumerates
  `JobRun.result_path` (`routers/jobs.py:391`, `admin.py:336`), stats *that* object — the JSON —
  and decrements by its size, while what was added was the HTML. `decrement_storage_bytes` clamps
  at zero, so it never goes negative; it also never returns to zero. Delete every job you own and
  your usage still reads non-zero.
- **The scraped page is never deleted.** Nothing enumerates it, so it outlives the run, the job
  and the user. Same shape as BUG-004's orphaned screenshots.

Neither file is touched by the migration, so **§10's do-not-delete reasoning does not apply and
backlog §3 does not cover these** — they are pre-migration fixes on live code. Filed as
**BUG-007** (`docs/project/open-bugs.md`), which carries the full trace; this section records only
why the metering decision depends on it.

**✅ The dual write is settled (owner's call, 2026-08-25): charge one copy.** Every worker writes
each result **twice** — `latest/{job_id}.{ext}` and `history/{job_id}/{ts}.{ext}` (ADR-002 §4) —
while `result_size` reports one copy, so MinIO holds 2× what the meter counts on every v1 lane.
Read literally, "charge for what is stored" charges both. It does not. The rule is:

> **Every `history/` object is charged, once. `latest/` is never charged, and is deleted with the
> artifact it mirrors.**

`latest/` is a convenience alias for bytes the user is already paying for, not a second artifact;
billing twice for one result is not defensible to a user. **`latest/` is kept as-is** — the v2
artifact path already drops it ([§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)),
so the discrepancy is v1-only with a known end date, and removing it early costs work for a
convenience that expires on its own.

**Settling it exposed a second defect: an LLM job leaves *four* objects, not two, and the delete
path can only ever remove three of them.** The scrape writes the job's own format while the LLM
always writes `.json`, so the two `latest/` keys differ by extension and neither overwrites the
other. For an `output_format=markdown` LLM job:

| object | charged today | deleted today |
|---|---|---|
| `history/{job}/{t1}.md` — the scraped page | ✅ (sets the stamp) | ❌ **never** — `result_path` was repointed away from it |
| `latest/{job}.md` | no | ✅ |
| `history/{job}/{t2}.json` — the extraction | ❌ skipped by the stamp | ✅ |
| `latest/{job}.json` | no | ❌ **never** |

Hard delete derives **one** filename from `job.output_format` (`routers/jobs.py:395`), so it
removes whichever `latest/` copy matches the job's declared format and orphans the other; *which*
one survives depends on the format, and an `output_format=json` job has only three objects because
the LLM's write lands on the scrape's key. **The assumption underneath is that a job has one
artifact in one format** — the same per-run granularity error as the stamp, in the deletion path
rather than the counting path. Both are fixed by the same thing: a per-object record
([§8d](#8d-who-charges-and-what-happens-at-the-wall--settled-2026-08-25)), which lets delete
*enumerate* what exists instead of deriving a filename and hoping. Carried into BUG-007.

#### 8b. Crawl conditions, and the mechanism the cutover promise needs

Storage is the axis where crawls are most dangerous: 10,000 pages at BUG-003's measured
291 KiB–4.1 MiB is **2.8–40 GB from a single API call** against a 5 GB wall. Two conditions the
PM attaches, both architectural obligations rather than product preferences:

- **Reclaim ships with counting.** Verified independently: **nothing frees crawl artifacts
  today.** `DELETE /crawls/{id}` (`routers/crawls.py:127`) is cancel-only and removes no objects;
  the admin user-delete (`admin.py:195`) and job hard-delete both enumerate `JobRun.result_path`
  only, so **deleting a user orphans their crawl artifacts in MinIO**. Charging against a hard
  wall with no way to free space is a support incident by construction.
- **Accounting starts at cutover.** No backfill, no reconciliation of history — and deleting a
  pre-cutover artifact must not decrement, or the counter goes negative.

**The second condition has no mechanism on two of the four lanes (found in review, 2026-08-17).**
"Do not decrement for something that was never incremented" requires knowing, at delete time,
whether a given object was counted. On the job path that knowledge exists —
`job_runs.storage_accounted_at` (`models/job_runs.py:35`) is exactly this marker. **`crawl_pages`
has no equivalent, and no size column either**, and nothing yet specifies one on `pipeline_runs`.
Without a per-object marker the cutover boundary is a date comparison against row timestamps,
which is wrong for any row created before cutover and deleted after. **Each lane that starts
counting needs its own accounted-at marker, added in the same change that starts counting it.**

> **✅ Resolved 2026-08-25 — and the resolution is that no lane gets its own marker.**
> [§8d](#8d-who-charges-and-what-happens-at-the-wall--settled-2026-08-25) settles accounting onto a
> single shared per-object ledger, so the ledger row *is* the accounted-at marker for every lane.
> `crawl_pages` never gains a column; `pipeline_runs` never specifies one. "Counting starts at
> cutover, no backfill" becomes true by construction rather than by a date comparison — a
> pre-cutover object has no ledger row, so nothing decrements for it. This paragraph is kept
> because the *requirement* it states is still the requirement; only the number of places that
> satisfy it changed from four to one.

#### 8c. Intermediate-output collection

Failure context is retained unconditionally (the PM's stated position): the failing block, its
input reference, and its error survive garbage collection, because "see why a step failed" is
R3's purpose.

Under the new storage rule the economics of this window change direction. Previously intermediates
were free to the user and a pure operator cost, so the window traded operator storage against
debuggability. **Now intermediates are charged while they exist**, so the window also bounds what
the user pays: a long window is a larger bill, a short one collects evidence they may still want.
It remains an operator dial, but it is now a dial with a **user-visible price**, which strengthens
rather than weakens the case for treating it as a promise.

**That window is a product-visible retention promise, not a free operator dial. ✅ Owner's call,
2026-08-10 (§5 review).** R3 promises a user can see which step failed *and what it returned*; for
**successful** intermediate blocks, that second half is exactly what garbage collection deletes.
Three rules keep the promise honest:

- **The run's result is not an intermediate and is never collected by this mechanism.** It is
  the last content-producing block's output
  ([§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)) — the run's
  *result*, which since the 2026-08-17 review is no longer the same claim as *the charged
  artifact*: the user pays for this object and every other object the run still holds. It is
  removed only when the user removes the run. **Collection operates per run, not per block** — an
  effect block passes its input reference through, so two blocks can name one object and per-block
  collection would orphan a live reference.
- **A collected output renders as collected.** The API and the SPA must distinguish *never
  produced* (an effect block), *collected* (retention elapsed) and *failed*. A collected
  intermediate that surfaces as a 404, an error or an empty result reads to the user as data loss.
- **What is retained is decoupled from Temporal's retention, and outlives it.** Per-block status
  and timing live in `pipeline_run_blocks` in the **app** database, so R3's "which step failed"
  survives indefinitely regardless of both the 30-day namespace retention
  ([§2c](#2c-namespace-retention-is-30-days)) and this window. Only *content* has a retention
  window. Stated because the natural assumption — that a run stops being inspectable when its
  workflow history expires — is wrong here, and designing to it would throw away observability the
  app database already provides for free.

The number itself is still an operator dial; what is decided is that it is chosen against a stated
promise rather than picked for storage cost alone.

**⚠️ "Per run, never per block" is safe only while no object is shared *between runs* (found in
review, 2026-08-17).** The rule was derived from a within-run hazard: an effect block passes its
input through, so two blocks in one run can name one object. The same hazard exists one level up,
and v1 already has it — on a content-hash match, `result_consumer.py:385` points the new run at
the **previous run's** object instead of writing a new one. Collecting per run would then delete
an object a later run still references. Pipelines do not have this yet, only because
change-detection was deferred to Monitors; **the deferral is what makes per-run collection safe,
not the design.** When Monitors bring "skip if unchanged" onto the pipeline lane, per-run
collection breaks exactly as per-block collection breaks now, and the rule becomes *collect an
object when no run references it*. Recorded here so that arrives as a known consequence rather
than as a data-loss bug. It interacts with the storage rule too: a shared object is stored once
and must be charged once, not once per referencing run.

#### 8d. Who charges, and what happens at the wall — settled 2026-08-25

Both gaps are now closed. Three owner's calls.

##### The accountant is the workflow worker, not the scraper workers

**✅ Storage accounting is performed by the workflow worker, as an activity, and the scraper
workers stay database-free.**

The constraint is older than this ADR: *light worker — NATS + MinIO only, no DB access; all
business logic in the API* (ADR-001). It is not a stylistic preference. The scraper workers are
the processes that run hostile pages through a real browser; keeping Postgres credentials out of
them is a containment boundary.

Two shapes were available, and they differ precisely on that boundary:

| shape | where it runs | verdict |
|---|---|---|
| the upload activity counts its own bytes | inside the **scraper** workers | **Rejected** — the process that wrote the object knows its size, but this hands a database to the pod that renders untrusted pages |
| a separate accounting step after the upload | inside the **workflow worker** | **Chosen** |

"Worker" is now ambiguous in this project and the ambiguity is what makes this look like a
conflict. After the migration there are two kinds: the **scraper workers** (Go http, Playwright,
LLM), which must stay DB-free, and the **workflow worker**, a new pod holding the orchestration
logic that leaves the API — the direct successor to `result_consumer.py`'s role, which has database
access by definition because orchestration *is* database work. Accounting in "a worker" does not
breach the rule; it depends entirely on which one.

**The boundary is enforced by task-queue routing, not by convention.** Activities are registered
against a Temporal task queue and the server dispatches only to pods listening on that queue. The
scrape activity is registered on the scraper queue, the accounting activity on the workflow-worker
queue; a scraper pod is never offered accounting work because it is not listening for it. The
no-DB rule stops being something an implementer has to remember and becomes a property of the
deployment.

##### A periodic sweep is the auditor, never the accountant

The alternative considered was doing all accounting in a scheduled batch job, which also satisfies
the no-DB constraint. **Rejected as the primary mechanism**, for three reasons:

- **It is a new hand-rolled loop.** `result_consumer.py` is a fragile polling loop because NATS
  offered no durability for "the work succeeded, now durably record it" — and Q8 was that loop
  failing in production. Temporal makes exactly that step a first-class retryable primitive.
  Writing a scheduler, failure handling and state for it by hand, inside the migration whose
  thesis is that this project has too many hand-rolled loops, inverts the point of the exercise.
- **A stale counter breaks the admission buffer below.** The buffer is a live guard and can only
  be as live as the number it reads; an hourly sweep lets a user run far over during the window
  when the guard matters most.
- **A sweep cannot attribute bytes without breaking a different boundary.** MinIO paths carry
  **no tenant segment** — `history/{job_id}/{ts}.{ext}` (this is the same absence
  [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity) ties to §12's single
  boundary) — so a sweep must join every object back to Postgres by `job_id`, a full listing that
  grows with total objects forever. The obvious shortcut, stamping the owner onto the object at
  upload, requires **telling the workers who the user is**. Verified: `user_id` appears in **zero**
  files across all four worker services today. Tenant identity has never been in those pods, and
  moving it there to save a join is a poor trade against
  [§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary), where the
  API's ownership check is already the *only* boundary.

A sweep over a ledger avoids all of that — but then something already wrote the ledger, and the
sweep is no longer primary. **So the sweep is retained for the two jobs it is genuinely good at:**
reconciliation (compare ledger totals against what MinIO actually holds, and alert on drift — it
is allowed to be slow and stale because it is hunting bugs, not enforcing a limit) and
**collection/refund** ([§8c](#8c-intermediate-output-collection), which is inherently periodic
because nothing watches a retention clock per object).

##### The record is one shared ledger, and the meter is lane-blind

**✅ A single shared per-object table, not a per-lane one.**

Storing this on the existing per-lane rows is impossible, not merely inelegant: `job_runs` has one
`result_path` and one `storage_accounted_at`, and an LLM job has two chargeable objects — which
*is* BUG-007. Adding columns cannot fix it, because a pipeline run produces one object per
content-producing block and the block count is not known when the schema is designed. A child
table is forced. The only real choice is one table or one per lane:

| | when lane #5 arrives | how you find out |
|---|---|---|
| per-lane tables (`job_run_artifacts`, `crawl_page_artifacts`, …) | the quota `UNION`, the delete path, the reconciler and collection each need an arm added | **you don't** — the query returns a well-formed, too-small number |
| **one shared table** | it writes rows into the same place | nothing to find out |

> **The per-lane failure is not hypothetical; it is P7.** `_count_monthly_runs` and
> `_count_concurrent_jobs` hardcode `FROM job_runs`, crawls write `crawl_pages`, and the result is
> that a 10,000-page crawl consumes zero of all three meters. Nothing errored. The number was
> simply missing a lane, for as long as crawls have existed, and it was found by reading code
> during this review rather than by using the system.

**The counter-argument, from this codebase, is real and is what the design has to survive.**
`webhook_deliveries` is already a shared-across-lanes table, guarded by
`num_nonnulls(job_id, batch_id, crawl_id) = 1` (`models/webhook_delivery.py:45`). A pipeline run
has none of those, so that shared table **structurally rejects the next lane** — which is why
[§15](#15-oq-11--webhook-delivery-is-a-step-the-run-waits-for)'s option (b) was never the free
reuse it appeared to be. A shared table with a closed enumeration of lanes baked into a `CHECK`
catches the same disease one level down.

**Hence the design rule that makes the shared table safe:**

> **The meter reads `user_id` and `bytes`. Nothing else.**

`user_id` is present on every lane already (jobs via `job.user_id`, crawls directly on `crawls`,
pipelines by construction), is never null, and never needs widening. If "how many bytes does this
user hold?" is answered from owner and size alone then **the meter is not lane-aware at all**, and
there is nothing about it for a future lane to forget. The producer link — *which run made this?*
— is still recorded, as nullable FKs so cascade delete works, but it is used only by **delete and
collection**, never by the meter. A future lane that forgets to widen that `CHECK` then fails
**loudly, on its first insert in dev**, while the meter, which never depended on it, stays correct.

That inverts today's failure mode. The meter is currently lane-aware and silently wrong; under
this rule it is lane-blind and structurally right, and the part that is allowed to be loud is the
part that is safe to be loud. It is
[§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)'s
"stop naming a table" applied to bytes rather than to runs.

Three things fall out without further design:

- **Delete stops guessing.** It enumerates ledger rows instead of deriving a filename from
  `output_format`, which kills the orphan class in §8a rather than the LLM instance of it.
- **Screenshots (BUG-004) need no separate mechanism** — a screenshot is a stored object, so it is
  a ledger row, so it is charged and deletable by the same path.
- **§8b's missing accounted-at marker resolves itself.** `crawl_pages` never gains one, because
  the ledger row *is* the marker, shared by every lane. "Counting starts at cutover, no backfill"
  becomes trivially true: a pre-cutover object has no ledger row, so nothing decrements for it and
  the counter cannot go negative.

##### At the wall: the run finishes, and a buffer stops the next one

**✅ A run that crosses the ceiling completes and is charged. A headroom buffer refuses to *start*
new runs near the limit.**

On v1, exceeding the wall is handled at `result_consumer.py:631` — the result is deleted and the
run fails. That is the wrong shape for a pipeline, because the ceiling is reached at the **last**
block, *after* the user's own LLM key has already been billed for the extraction: failing there
destroys paid work to reclaim a few KB. **Admission-time checking cannot substitute** — output
size is not knowable before the run. The buffer sidesteps that: it never needs to predict the
size, only to keep enough headroom that being wrong is survivable.

**⚠️ The buffer holds only while the most a single admitted run can add is smaller than the
buffer.** For pipelines that is comfortable — a run is a handful of objects. **It is false for
crawls**, where one submission is up to `max_pages` fetches: 10,000 pages at BUG-003's measured
291 KiB–4.1 MiB is 2.8–40 GB, which no fixed buffer survives. **Crawls must therefore check the
ceiling per page as they go, not once at admission** — recorded here because "we have a buffer"
will otherwise read as covering all four lanes when it covers two.

The buffer's *size* remains an operator dial and is not fixed here.

### 9. OQ-5 — Workers become Temporal activity workers directly; the NATS bridge is rejected

**Decision: the three workers gain a native Temporal activity entry point (option (b)) in the
first increment. Option (a) — activities dispatching through NATS to the unchanged workers — is
rejected. ✅ Owner's call, 2026-08-23, reversing this section's draft.**

The v1 lane is untouched by this: jobs, batches and crawls keep running on NATS until their flow
migrates ([§16](#16-the-v1v2-coexistence-contract)). What changes is that the pipeline lane never
acquires a NATS bridge at all.

**The draft chose option (a) and the reasoning inverted on inspection.** The original argument was
that R6 — reproduce today's `scrape → LLM → webhook` recipe as a pipeline — tests the *pipeline
model*, so rewriting three workers in the same increment would confound it: a failing gate could
mean the model is wrong or the rewrite is buggy. That argument is sound in form. It fails on its
premises, in four ways.

**1. "Rewriting three workers" overstates the change by an order of magnitude.** The workers split
cleanly into domain logic and transport, and only transport moves:

| file | lines | touching NATS |
|---|---|---|
| `http-worker/internal/worker/worker.go` | 411 | ~22% |
| `llm-worker/worker/main.py` | 164 | ~21% |
| `playwright-worker/worker/main.py` | 203 | ~18% |
| `playwright-worker/worker/worker.py` | 309 | ~10% |

Everything that is expensive to get right touches the transport **not at all**: `blocking.py`
(313 lines of tiered bot-wall detection, BUG-003), the Patchright/headed-Chrome stealth setup
(ADR-008), `formatter.go`, `robots.go`/`robots.py`, `llm.py`, the MinIO clients. These are plain
functions over inputs and outputs, and option (b) calls them from a different caller rather than
changing them.

The Go worker makes the point sharpest: `processJob(ctx context.Context, job *ScrapeMessage,
f *fetcher.Fetcher) (string, error)` (`worker.go:377`) **already has the shape Temporal wants** —
a Go activity is any `func(ctx, T) (R, error)`. The adapter is a few lines because the natural
shape of "do the work and return where you put it" is the same in both worlds.

**2. Roughly half of what option (a) preserves is compensation for NATS.** `ack_wait=120`, the
30-second `in_progress()` heartbeat, the `max_deliver` caps, the nak-with-backoff ladder — every
one exists because JetStream redelivers a message the worker has not acked in time. That is the Q6
incident, and the tuning was expensive. Under Temporal it is **deleted, not ported**: activity
heartbeating and the start-to-close timeout occupy that role. Preserving those settings is
preserving a dressing for a wound being removed. What genuinely carries forward is
[§10](#10-oq-6--the-do-not-delete-list)'s list — the cold-start probe, the transient/terminal
classifier, bot-wall detection — and **that list ports identically under either option.** Option
(a) does not avoid the work; it defers it while keeping the code that must be ported alive in two
places.

**3. Option (a)'s bridge does not exist, is not cheap, and one part of it is blocked.** The bridge
needs four things: dispatch (trivial — the payload already exists), **result correlation**, retry
neutralisation, and a lane marker. The second is the problem.

> **⚠️ The results subject cannot take another consumer, and there is a dead service in production
> proving it.** `SCRAPEFLOW` is `--retention work` (verified on the live stream, not from the
> manifest), subjects `scrapeflow.jobs.>`. A work-queue stream refuses a second consumer whose
> filter overlaps an existing one, and `api-result-consumer` already claims
> `scrapeflow.jobs.result` in full. The crawl coordinator attempts exactly the addition option (a)
> would need — `pull_subscribe(NATS_JOBS_RESULT_SUBJECT, durable="coordinator-result-consumer")`
> at `coordinator/result_handler.py:203` — and **that consumer has never existed on the stream**:
>
> ```
> $ nats consumer ls SCRAPEFLOW
>   api-result-consumer · go-worker · python-llm-worker · python-playwright-worker
> ```
>
> The failure is silent by construction. `result_handler_subscribed`, logged immediately after the
> subscribe, appears **zero** times in the coordinator's retained log, while `dispatch_loop_started`
> from the sibling task logs normally; `main.py:82` awaits both with
> `asyncio.gather(..., return_exceptions=True)`, which captures the exception and never re-raises.
> The pod reports itself started and healthy with half of itself dead. **No crawl has ever run in
> production** (`crawls` and `crawl_pages` are both empty), so nothing has surfaced it — the same
> shape as BUG-005: shipped, silently broken, never exercised. Filed as **BUG-008**
> (`docs/project/open-bugs.md`), and **deliberately not fixed on the NATS path** — the defect *is*
> the NATS integration, and [§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block)
> deletes the component that carries it.

So option (a) must invent a result path — a new subject, a second stream, or polling Postgres for
the row `result_consumer.py` writes. The last is the worst available answer and the most likely to
be reached for: it puts **`result_consumer.py` on the critical path of every pipeline run**, and
that component is the reason this ADR exists (Q8's hand-rolled state machine and its live feedback
loop). Adopting a durable execution engine and then routing its results through the thing it was
adopted to replace is a contradiction the section did not notice.

**And today the message would not merely be ignored — it would be destroyed.** `_handle_result`
(`result_consumer.py:608`) resolves the run with `db.get(JobRun, run_id)`. A pipeline-run id
resolves to nothing, so the handler logs *"Received result for unknown run, discarding"* and
**acks** — which on a work-queue stream deletes the message. The activity then waits out its
timeout while the scrape sits completed in MinIO. ([§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee)
predicted this one lane later as "neither FK set"; the real failure is a step earlier and final —
there is no row to route.)

**4. "Unchanged workers" and "NATS must not retry" cannot both hold.** The draft's own warning
required that, for workflow-originated work, the NATS layer must not retry — otherwise Temporal's
`RetryPolicy` and JetStream redelivery stack, which is R4's "retry lives in exactly one visible
layer" violated by the migration itself. But that retry lives **inside the workers**:
`msg.NakWithDelay` at `worker.go:316`/`:339`/`:360`, `msg.nak(delay=…)` at
`llm-worker/worker/worker.py:128`, and the same in the Playwright worker; and in **consumer
config** (`max_deliver`), which is a property of a consumer, not of a message. Neither can be
disabled per-message without either a flag the worker reads or a v2-only subject and consumer.
**Both are worker changes.** Option (a)'s defining property — workers completely untouched — does
not survive its own safety requirement.

**The gate is preserved, by a better mechanism.** The honest comparison was never *unchanged
workers* versus *rewritten workers*; it was **a brand-new bridge plus unchanged workers** versus
**a thin permanent adapter plus unchanged worker logic**. The bridge is the larger body of novel
code, it is novel in precisely the area that produced Q5, Q6, Q7 and Q8 (queue semantics, retry,
result correlation), and **all of it is deleted at the step that was always going to follow.**
Option (b) also admits a de-risking step option (a) cannot:

> **Pre-gate (required before R6): run the Scrape activity standalone against a URL and compare
> its output to a v1 job run of the same URL.** This separates "the adapter is wrong" from "the
> pipeline model is wrong" *before* the model is under test, which is the isolation option (a)
> claimed to provide. The bridge has no equivalent, because it has no simpler mode to run in.

**Costs, stated plainly.**

- **Each worker runs twice during coexistence** — one deployment bound to NATS for v1, one bound
  to a Temporal task queue for v2. **Two deployments of one image with a mode flag, not one
  process serving both**: retiring the v1 lane becomes deleting a deployment rather than unpicking
  a conditional, and the two scale and fail independently. Cost: three extra deployments for the
  duration.
- **Three integrations rather than one bridge.** Per worker: a dependency (`go.temporal.io/sdk`,
  `temporalio`), connection config (address, namespace, task queue) as env vars, an entry point,
  and a manifest. Two of the three are the same language and SDK, so this is realistically two
  distinct pieces of work and one repeat. Note the "one place" advantage of the bridge was
  illusory anyway — per point 4, retry neutralisation reaches into all three workers regardless.
- **⚠️ The Playwright worker's container contract must be preserved exactly.** Xvfb started first,
  then `exec python` as pid 1 — never `xvfb-run` as pid 1, which stays alive after the worker dies
  so k8s never restarts a dead container (ADR-008). A new entry point is a new opportunity to get
  this wrong, and the failure mode is a pod that looks healthy and consumes nothing. This is the
  single riskiest part of the port, and it is a container concern, not a Temporal one.
- **Ordering:** none of this can begin until the Temporal server is deployed — separate Postgres
  instance and `temporal-sql-tool` schema setup ([§2](#2-topology)). Equally true of option (a),
  so it does not separate them.

**Sequence.** Prove the pattern on the **Go http-worker** first: simplest, best-tested, and its
`NATS_MAX_DELIVER=3` already caps retry, so a like-for-like comparison against v1 is clean. Then
the LLM worker (it carries the cold-start and classifier logic that §10 requires be ported), then
Playwright (the container risk above). NATS and all four API loops are untouched throughout.

**What this decision dissolves elsewhere.** The stacked-retry hazard largely disappears — with no
NATS beneath the activity there is one retry layer, Temporal's. It does **not** disappear entirely:
§10's ported transient/terminal classifier must still decide, and its terminal verdicts must be
raised as **non-retryable application errors**, not swallowed into a retry loop inside the
activity, or R4 is violated again one level down. *(Corrected on the §10 review, 2026-08-26: this
originally read "expressed as non-retryable error types on the `RetryPolicy`", which is not
implementable — `non_retryable_error_types` is a **denylist** of type names, while the classifier
is a fail-closed **allowlist** that also reads exception attributes and, in Go, which step raised
the error. See [§10](#10-oq-6--the-do-not-delete-list).)* §7's carry-forward — that under option (a) v2 results land on `scrapeflow.jobs.result`
with neither FK set — is **moot**, since v2 publishes no NATS results. §16's sequence step
"workers to activity workers" moves from third to first.

### 10. OQ-6 — The do-not-delete list

Each item here was paid for with a production incident. **They fall into two groups with two
different failure modes, and conflating them was the original drafting error of this section:**

- **Group A — at risk of deletion.** Genuinely inside files the migration removes
  (`webhook_loop.py`, `result_consumer.py`). Instruction: *rescue before the file goes.*
- **Group B — at risk of silent semantic change.** These live in worker modules with **zero queue
  code** — verified: `llm.py`, both `errors.py`, `errors.go` and `blocking.py` contain no NATS
  calls at all (two comment references between them). Nothing points a delete at them; they port
  by being left alone. Their risk is the opposite one: they arrive intact and are then **overruled
  by the new retry owner**. Instruction: *port the file untouched, then re-establish the guarantee
  the old layer used to provide.*

#### Group B — ports intact; the guarantee must be re-established

| Behaviour | Where it lives now | What must hold after the port |
|---|---|---|
| **LLM cold-start handling** — `ensure_ready()` warm-up probe against `/models`, plus the request timeout | `llm-worker/worker/llm.py` (probe), `llm-worker/worker/config.py` (budgets) | Into the LLM activity. Temporal has no idea a scale-to-zero endpoint is cold; it would simply retry a timing-out activity and re-bill the user's key. See the two riders below |
| **Transient/terminal classification** for storage and provider faults, incl. the aiohttp-unreachable case | `llm-worker/worker/errors.py`, `playwright-worker/worker/errors.py`, `http-worker/internal/worker/errors.go` | **The classifier decides; Temporal retries.** The activity catches, calls `classify()`, and re-raises terminal failures as a non-retryable application error. **Fail-closed default preserved** (unknown → terminal). Not a `RetryPolicy` field — see below |
| **Bot-wall detection**, tiered, terminal, `blocked:<vendor>` | `playwright-worker/worker/blocking.py` | Stays in the **Playwright scrape activity**, unchanged semantics. Note the lane asymmetry below |

**⚠️ Rider 1 — the timeout numbers, because the ADR previously had them wrong.** There are two
budgets, not one, and the production value of the second is **not in this repo**:

| | setting | value |
|---|---|---|
| Warm-up budget (poll `/models` until awake) | `llm_warmup_max_wait_seconds` | **180s** |
| Request timeout (wait for the answer) | `llm_request_timeout_seconds` | **60s** in `config.py`; **180s in production**, set as `LLM_REQUEST_TIMEOUT_SECONDS` in the infra repo's `llm-worker.yaml` |

**The LLM activity's start-to-close timeout must exceed *warm-up budget + request budget*.** Against
production that is **≈360s**, not the 240s an implementer computes from the repo defaults alone.
Sizing it from `config.py` leaves the activity **two minutes short**, and it fails *only* on cold
starts — the exact case the row exists for. This is Q6 in a new costume — an outer layer less
patient than the work inside it — and it is exactly the composition rule R4 makes a hard
requirement.

**⚠️ Rider 2 — the heartbeat obligation survives; only its mechanism dies.** `ensure_ready`'s
docstring carries a caller obligation: *"Caller must ensure the NATS in-progress heartbeat is
running: this loop can outlast `ack_wait`."*
[§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)
deletes `ack_wait` and the 30s `in_progress()` call, which is correct — but the **duty** is not
deleted, only re-homed onto `activity.heartbeat()` plus a `heartbeat_timeout`. Both ways of
forgetting it fail: set a `heartbeat_timeout` and never heartbeat and the activity fails on
**every** cold start; set none and a dead worker's LLM job hangs for the full start-to-close.

#### Group A — rescue before the file is deleted

| Behaviour | Where it lives now | Where it goes |
|---|---|---|
| **SSRF re-validation on every delivery attempt** | `api/app/core/webhook_loop.py:90` | Inside the webhook activity — see [§15](#15-oq-11--webhook-delivery-is-a-step-the-run-waits-for). **Failure is terminal and immediate** — see below |
| **The webhook wire contract** — HMAC-SHA256 over the raw payload bytes; header `X-ScrapeFlow-Signature: sha256=<hex>`; success = `status_code < 300`; 10s per-attempt timeout; and the quirk that with no configured secret `secret_bytes = b""`, so **the header is always sent** | `api/app/core/webhook_loop.py:124-137` | Into the webhook activity, byte-identical. **The most externally visible thing in the migration**: receivers check that exact header name and that threshold. Changing either breaks every customer integration silently, with no failing test in this repo |
| **The webhook payload schema** — `event`, `job_id`, `run_id`, `result_path`, `diff_detected`, `diff_summary`, `timestamp`, plus `error` on failures | `api/app/core/webhooks.py:41-49` | Into whatever the v2 webhook activity sends. ⚠️ Two fields have no v2 source: a pipeline run has **no `job_id`**, and `diff_detected`/`diff_summary` have nothing to fill them while change detection is homeless (below). Both need recording as R6 divergences rather than discovering |
| **Content-hash dedup** (`xxhash`) — see the standalone note below | `api/app/core/result_consumer.py:49-56` (the hash) and `:375-392` (the dedup branch) | Nowhere yet. Must survive `result_consumer.py`'s deletion and wait for Monitors |

**Non-retryable is a decision the port has to make explicitly, in three places.** The classifier
answers *is this worth retrying*; Temporal owns *whether and when*. That keeps retry in exactly one
visible layer, satisfying R4 — but only if terminal outcomes are actually marked:

1. **Classifier terminals** — the mechanism in the Group B table.
2. **SSRF failures.** Today an SSRF block marks the delivery `exhausted` **immediately**, does
   **not** increment `attempts`, and logs a security event (`webhook_loop.py:99-110`). §15 makes
   the activity's retry policy the delivery loop, so a naive port turns "instantly dead" into
   "dead in ≈2.6 hours", re-resolving a hostname an attacker is actively rebinding, five times.
3. **Bot walls and robots.txt disallows.** ⚠️ **These never raise today.** `detect_block()`
   *returns* a verdict and the worker publishes `failed` (`playwright-worker/worker/worker.py:205`;
   robots at `:61-64`) — so the classifier has **never seen a block**. Rows 2 and 3 of this section
   have never met; the port is where they first touch. In an activity, returning a failure has to
   become raising one, and that raise must be non-retryable, or Temporal re-scrapes the same wall
   from the same IP on every attempt — three headed-Chrome renders and three lots of proxy
   bandwidth to be refused three times.

**⚠️ Why not `RetryPolicy` non-retryable error types.** An earlier draft of this section, and of
§9, said the classifier "becomes `RetryPolicy` non-retryable error types." **It cannot**, for three
independent reasons:

- **Direction.** Temporal offers only a **denylist** (`non_retryable_error_types`: retry
  everything except these named types). The classifier is an **allowlist** — retry only these,
  everything else terminal. There is no `retryable_error_types`, and a denylist cannot express an
  allowlist over an open set. So "non-retryable error types" and "fail-closed default preserved"
  were mutually exclusive claims in the same table cell.
- **Dynamism.** `classify()` reads attributes at runtime, not just types: an `APIStatusError` is
  transient at 429 and terminal at 418; an `S3Error` is transient with code `SlowDown` and terminal
  with `NoSuchBucket`. A list of type **names** cannot see inside the exception.
- **Structure (Go).** `classify` returns terminal for everything not wrapped in `*uploadError`
  (`errors.go:71-77`). That wrapper exists because in Go a dead **target site** and a dead
  **MinIO** raise the *same* type — the only disambiguator is which step raised it
  (`worker.go:393`). "Which step" is not a type name.

The correct shape is `ApplicationError(..., non_retryable=True)` in Python and
`temporal.NewNonRetryableApplicationError(...)` in Go. There is no second backoff, no second
attempt counter and no second ceiling, so this is **not** the in-activity retry loop §9 warned
against.

**⚠️ The lane asymmetry behind "the Playwright scrape activity".** §9 creates three activity
workers. Bot-wall detection exists only on the Playwright side; the Go worker has only
`fetcher.go:72`'s non-2xx check, so a wall served as a `200` passes through on the http lane. That
is the recorded position (BUG-003's audit found walls only at `engine=playwright`) and is not
reopened here — but it means **which engine the Scrape block routes to determines whether bot-wall
detection runs at all**, which the singular "the Scrape activity" hid.

#### The homeless pair: content-hash and `diff.py`

`temporal-full-migration.md` §4 assigns content dedup (`xxhash`) and `diff.py` to "a diff/dedup
activity, pure logic reused verbatim." The PM has since assigned **both halves of change detection
to Monitors (B)**, which is not yet specified. So these are **relocated, not deleted, and not yet
re-homed** — they must survive the deletion of `result_consumer.py` and wait for B.

**Two corrections to how that was stated.**

- **They are not equally at risk, and bundling them hides which one is.** `diff.py` is its own
  module whose only functional caller is `result_consumer.py` (`webhooks.py` imports just the
  `DiffResult` type) — deleting the caller leaves it orphaned but **intact**. The content-hash is
  `_compute_content_hash` at `result_consumer.py:49-56` plus the dedup branch at `:375-392`, both
  **inside** the deleted file. Only the second is actually at risk of the accidental loss this
  note warns about.
- **"Pure logic reused verbatim" is false of the dedup branch.** On a hash match it does two
  things beyond comparing: it **deletes the new `history/` object** and **repoints `result_path`
  at the previous run's object**. That is cross-run object sharing, and it is precisely what
  [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)
  recorded as breaking per-run collection: *"per-run GC is safe only while no object is shared
  between runs — v1 already shares them."* Porting it as pure logic carries the code and leaves
  the hazard behind.

#### Correctly absent — checked, and dissolved rather than lost

Recorded so this is not re-derived:

- **The terminal-status idempotency guard** (`result_consumer.py:541`). Exists only because
  JetStream redelivers; Temporal records an activity result once.
- **Schedule-drift prevention** (`croniter(cron, job.next_run_at)` rather than `now`,
  `scheduler.py:88`). Temporal Schedules compute the next fire from the spec, not from the actual
  fire time.
- **`ack_wait`, `max_deliver`, the nak backoff ladder.** Deleted with NATS per §9 — with the single
  exception in Rider 2, where the obligation outlives the mechanism.

**Two carry-forwards this section surfaced but does not own:**

- **A scheduled job blocked by quota waits; it is not skipped.** Recorded in
  [§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee), because it
  is a Temporal Schedule question rather than an activity one.
- **Sitemap discovery must port to `httpx`.** Recorded in
  [§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block), because it
  belongs to the crawl migration step.

### 11. OQ-7 — Run state to the SPA: mirror activity plus `pg_notify`

**Decision: preserve the existing contract. A status-mirror activity writes the app-side mirror
row and emits `pg_notify` at each stage, exactly as `result_consumer.py` does for the transitions
it owns. Do not stream Temporal events to the browser.**

- The SPA and its WebSocket path do not learn that Temporal exists — zero frontend change for the
  job path. *(Scoped on review: the pipeline lane still needs a channel, a listener, a subscriber
  map, a WebSocket route and a page — see [11e](#11e--what-preserve-the-contract-does-not-cover). And 11b adds a small,
  deliberate frontend change that applies to both lanes.)*
- The mirror row is needed anyway for listing, querying and admin views ([§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)),
  so the notify is nearly free on top of a write that has to happen regardless.
- Streaming engine events would put Temporal on the request path for a pure UI concern and couple
  the SPA's behaviour to engine retention settings.

**Pipelines get their own notify channel with a JSON payload — they do not reuse `job_status`.**
That channel's payload is a positional colon-delimited string (`job_id:run_id:status`) parsed by
`JobNotifier`; it cannot carry per-block detail, and widening it would break every existing
subscriber. Verified: `job_notifier.py:51` unpacks exactly three fields, and a fourth raises a
`ValueError` that is caught, logged as malformed, and **the update is dropped** — so widening
fails silently rather than loudly. `batch_status` already demonstrates the JSON-payload pattern to
follow. Overloading one status vocabulary to serve two different shapes of consumer is the Q8
mistake at a different altitude.

**Temporal Web UI is an operator tool** and is not part of any user-facing surface. Per
[§2b](#2b-the-web-ui-is-not-ingress-exposed) it is not exposed at all — `kubectl port-forward`
only — so nothing user-facing may depend on it being reachable.

The review of this section found no factual error in the decision above. What follows are four
gaps it left open, all closed by owner's call on 2026-08-26, plus the scope note.

#### 11a — Two writers, and the precedence rule is the thing to remember

**✅ Owner's call: keep today's arrangement. The workflow mirrors the transitions it owns; the API
keeps mirroring the transitions it owns, writing and notifying directly inside the request.**

The decision above says "exactly as `result_consumer.py` does today", which undersells the
contract being preserved: **`result_consumer.py` is not the only writer, and never has been.**
Four files emit `job_status`, and two of them are request handlers rather than the background
loop:

| Site | Fires when |
|---|---|
| `result_consumer.py:579` | a worker result arrives |
| `quota.py:260` | a run is failed by the quota path |
| `routers/jobs.py:419` | **a user cancels** — inside the request |
| `routers/admin.py:354` | **an admin cancels another user's job** — inside the request |

Routing cancellation through the workflow instead would make it *feel* broken. The PM's rule is
that **cancelling never aborts a block mid-execution** (PRD-016 review round 2), so a run cancelled
four minutes into an LLM block does not reach its next mirror point for four minutes. Today that
same click greys the page out instantly. R5 forbids exactly that kind of user-visible regression.

**⚠️ The cost of this choice, and the reason it is written here rather than left to the
implementer: two writers on one status column fail *silently* when the precedence rule is
forgotten.** The failure is a user cancelling a run, watching it cancel, and then watching it flip
back to `completed` — with the work done and charged. The single-writer alternative fails loudly
and harmlessly (a sluggish button), which is the better failure shape; it was rejected only
because matching today's behaviour exactly is a stated requirement.

> **The precedence rule: a cancellation written by the API wins. A mirror write must never move a
> run out of a terminal state it did not itself set.**

This rule already exists on the job path and must be re-established, not invented, for pipelines:
`result_consumer.py:613` checks `run.status == "cancelled"` **before anything else**, discards the
worker's result, and re-notifies. The v2 mirror activity needs the same guard against
`pipeline_runs.status`.

#### 11b — The socket must reconnect, because the runs this ADR creates are long

**✅ Owner's call: the client reconnects on any close that did not carry a terminal message.
The 300-second server timeout stays exactly as it is. A server keep-alive is optional and nothing
depends on it.**

The WebSocket route waits for the next transition with a **300-second timeout** (`jobs.py:745`),
then sends `{"type": "timeout"}` and closes. That has never mattered, because a job run takes
about 40 seconds. It matters now, because [§15](#15-oq-11--webhook-delivery-is-a-step-the-run-waits-for)
deliberately introduces runs that are **silent for hours**: a Webhook block waits for real
delivery across `BACKOFF_SECONDS` `[0, 30, 300, 1800, 7200]`, a reach of **≈2.6 hours**, during
which the run's status does not change. Monitors (layer B) extend that to durable sleeps measured
in days.

The frontend has no recovery: `JobDetail.tsx:81` is `ws.onclose = () => setWsLive(false)`, with no
reconnect and no polling fallback, and the react-query cache is invalidated only by a *terminal*
WebSocket message — which never arrives. So a pipeline parked at its webhook block goes stale at
6m30s and stays stale for the remaining two and a half hours, and the eventual completion notify
fires into an empty room because no subscriber is holding the queue.

**A server keep-alive was rejected as the primary fix**, for three reasons:

- It addresses **one** cause of a dead socket. Traefik's own idle cut, a rolling deploy of the API,
  a closed laptop lid and an ordinary network blip all close the connection anyway, and the page
  still sits stale.
- It makes **BUG-009 invisible**. If the listener connection has gone deaf, a keep-alive means the
  browser receives heartbeats forever while never receiving a status change; today's `timeout`
  message at least signals that something happened.
- It **cannot self-heal**, and reconnect can. On reconnect the route re-reads the row and sends the
  current status immediately, closing straight away if the run finished meanwhile
  (`jobs.py:734-739`) — so a reconnect **repairs every update missed while disconnected.** A
  heartbeat carries no state and can repair nothing.

Keeping the 300-second timeout alongside reconnect is deliberate: it stops being a defect and
becomes **a five-minute self-healing re-read of the row**, a slow safety net underneath the fast
push, at no server cost.

Two constraints on the implementation: reconnect **must back off** (a bare loop hammers an API that
is down), and it must **honour close code 4029** — `subscribe_job` refuses past
`ws_max_connections_per_user` and closes with it, so a reconnect loop that ignores 4029 spins
against a wall.

#### 11c — A failed mirror write fails the run

**✅ Owner's call: if the status-mirror activity exhausts its retries, the run fails. It is not
best-effort.**

This is the only activity in a pipeline whose failure has **no effect on the work itself** — the
scrape still scraped, the LLM key was still billed — which is precisely why it invites a
"best-effort, don't fail the run" treatment. That would be wrong here, because of how two
decisions in this ADR compose:

- this section: don't stream engine events, the app row is what the user sees; and
- [§2b](#2b-the-web-ui-is-not-ingress-exposed): the Temporal Web UI is not exposed at all.

Together they make **the mirror row the only window into a run that exists.** A run whose mirror
write failed completes perfectly, charges storage, fires its webhook — and shows as stuck at
`running` forever, indistinguishable from a genuinely hung run to anyone without cluster access.
Under [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)'s
decision that the app table is the read model, **a run whose state cannot be read is not a
successful run.**

#### 11d — The notify payload carries identifiers and status only

**✅ Owner's call: the payload carries identifiers and status values. Never error text, never
content, never anything user-supplied and unbounded. The browser fetches detail over HTTP.**

`pg_notify` caps a payload at **8000 bytes** (documented Postgres limit; not measured here). Over
that, the call **raises**. Today it is unreachable — the job payload is two UUIDs and a word, the
batch payload five integers and a URL — and this section's own JSON decision is what brings it
within reach.

The failure lands in the worst possible place. The notify runs **inside the transaction that
writes the row**, which is what makes it trustworthy (the browser cannot be told "done" about a
row that did not save). So an oversized payload does not merely lose the notification: **the
transaction rolls back and the block's status was never written.** The realistic trigger is a
failed LLM block's error string, which is whatever the provider's API returned and can run to
kilobytes of JSON.

This is [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)'s rule — blocks
pass references, never content — applied one layer down, to the notification channel.

**A second rule, same paragraph: the payload carries absolute state, never deltas.** Temporal runs
activities **at least once**, so a worker that crashes after committing but before reporting
success will run the mirror activity again. A duplicated `status = completed` is harmless; a
duplicated *"add one to the completed count"* is a counter that drifts. `batch_status` already
sends absolute totals (`result_consumer.py:345`), so the precedent is correct — but it is correct
by accident, and a per-block progress payload is exactly where someone would reach for an
increment.

#### 11e — What "preserve the contract" does not cover

The zero-frontend-change property is real and is **scoped to the job path.** The pipeline lane
still needs a channel, a third listener on the `JobNotifier` connection, a third subscriber map
and `subscribe_*` method, a WebSocket route, and a page. None of that is a decision — it is listed
so the section is not read as cheaper than it is. 11b's reconnect is a change to both lanes, and
is accepted as a cost.

**Filed against live code by this review: [BUG-009](../project/open-bugs.md) — `JobNotifier` never
reconnects.** It opens one Postgres connection at startup (`main.py:54`), registers its listeners,
and has no termination handler and no reconnect path. If that connection drops — a Postgres
restart, an upgrade, a failover, an idle cut — every WebSocket in that API process goes deaf
permanently and silently, until the API pod restarts. Pre-migration work on live code; this
section makes it more load-bearing by adding a second channel and longer-lived connections to the
same single connection.

### 12. OQ-8 — Tenant isolation: single namespace, and the API is the only boundary

**Decision: one Temporal namespace. The API is the enforcement boundary — the sole one. Tenant
identity is deliberately *not* encoded in the workflow ID.**

Namespace-per-user is rejected: namespace provisioning would become part of signup, per-namespace
configuration drifts, and Temporal namespaces are a much heavier boundary than the isolation we
need. Namespace-per-*tier* is a reasonable future step for noisy-neighbour isolation and is
explicitly left open, but buys nothing at current scale.

**The real boundary does not move.** Cross-tenant access returns 404 because the API checks
ownership of the `pipelines` / `pipeline_runs` row before it makes any engine call. The engine
never receives an unauthorised request because the API never issues one.

**Reversed on review (2026-08-08): an earlier draft of this section claimed that encoding
`user_id` in the workflow ID "adds a second, structural property" — operator queries scopable by
tenant, and signals that cannot reach another tenant's run by accident.** Both claims are
withdrawn, and the ID keeps the plain form
[§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
defines (`pipeline-run-{pipeline_run_id}`, `job-run-{run_id}`). Four reasons:

- **It would not be structural.** Temporal treats a workflow ID as an opaque string and never
  parses it. An embedded `user_id` protects nothing unless some other layer reads it back and
  validates it — which is *a check we wrote*, precisely what
  [§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee)'s mechanism 2
  is valuable for **not** being. Claiming both under one banner would attribute the uniqueness
  guarantee's strength to something that does not have it.
- **Tenant-scoped operator queries contradict [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table).**
  "List this user's runs" is exactly the class of question this ADR says is *never* answered from
  Temporal. `pipeline_runs` holds both `user_id` and the workflow ID, so the app database answers
  it better and hands back the precise IDs to look up.
- **The accidental-signal case is already closed upstream.** The API loads the run row, checks
  ownership, then derives the workflow ID *from that row*. Once ownership is proven the format is
  irrelevant. The only path an embedded `user_id` would rescue is one that builds an ID from
  user-supplied input without loading the row — a path that must not exist, and whose real defect
  would not be the ID format.
- **It keeps tenant identity out of engine-side data.** Workflow IDs travel into event history,
  task-queue metadata, logs and metrics, and into archival if that is ever enabled
  ([§2c](#2c-namespace-retention-is-30-days)). There is no reason to seed user identifiers across
  all of it for a property we just established is not real.

[§2b](#2b-the-web-ui-is-not-ingress-exposed) independently removed what little the operator-query
argument had left: with the UI unexposed, its audience is one operator who owns all the data, at a
workstation, during incidents — a filter convenience, not an isolation mechanism.

**The consequence to hold onto: there is exactly one tenant boundary, and it is the API's ownership
check.** Nothing structural backs it up at the engine, and this section previously implied
otherwise. Any future code path that reaches Temporal without first loading and ownership-checking
the app row is a tenant-isolation bug with no second line of defence.

Task queues are **shared** across tenants — per-tenant queues would require per-tenant workers.

### 13. OQ-9 — The crawl coordinator migrates, last, and a crawl is not a block

**Decision: yes, `coordinator/` migrates to a `CrawlWorkflow` and the service is deleted — but
after jobs and batches, and a Crawl block is *not* added to layer A's catalog.**

Reviewed 2026-08-28. The decision above is unchanged. Four things the section did not say are
now decided, and one of its clauses is withdrawn:

- the frontier work is a **rewrite, not a port** — most of the code this section proposes to
  migrate **has never executed** ([13a](#13a-this-is-a-rewrite-not-a-port-and-the-only-lane-with-no-v1-to-compare-against));
- **`crawl_queue` does not retire** — the frontier and the visited set stay in Postgres, because
  Temporal's measured limits cannot hold them at the page ceiling we already advertise
  ([13b](#13b-the-frontier-stays-in-postgres--the-measured-limits-decide-more-than-measure-later-allows));
- **`crawl_pages` is required, not optional** — three decisions taken *after* this section was
  drafted depend on it ([13c](#13c-crawl_pages-is-required-not-a-ui-convenience));
- **every URL entering the frontier is SSRF-checked** — the client swap this section already
  required is the smaller half of the problem in that file
  ([13d](#13d-ssrf-at-frontier-admission-and-the-sitemap-fetcher-changes-clients)).

A crawl is a fan-out over an unbounded, dynamically discovered set. That is a workflow shape
(child workflows, or a frontier held in workflow state), not a step in a linear chain. Modelling
it as a block would smuggle unbounded fan-out into a model whose non-goals explicitly exclude
fan-out, and would force the block model to express something no other block needs.

#### 13a. This is a rewrite, not a port, and the only lane with no v1 to compare against

The section was written as a migration of working code. It is not. The
[§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)
review established that `coordinator-result-consumer` has never existed — the work-queue stream
refuses a second consumer overlapping `api-result-consumer`'s claim on `scrapeflow.jobs.result`,
and `main.py:82`'s `asyncio.gather(..., return_exceptions=True)` swallows the exception, so the
pod is healthy with half of it dead (**BUG-008**, deliberately not fixed on the NATS path).

Traced through, that is a larger hole than "one consumer is missing". Of the crawl feature's
logic, **only the dispatch half has ever executed**:

| | has run in production | never run |
|---|---|---|
| `dispatcher.py` | dispatch loop, stalled-item recovery | — |
| `result_handler.py` | `check_completion`, `enqueue_crawl_webhook` (called from `dispatcher.py:156-158`) | `result_handler_loop`, `_process_crawl_result`, `_enqueue_url`, `_fetch_minio_bytes` |
| `link_extractor.py` | — | all of it (only caller is `result_handler.py:160`) |
| `sitemap.py` | — | all of it (only caller is `result_handler.py:179`) |

So **a crawl in production has never got past dispatching its seed page.** Nothing reads the
result, so no links are ever extracted, no sitemap is ever discovered, queue items never leave
`dispatched`, and `check_completion` therefore never fires. `crawls` and `crawl_pages` being empty
in production is not evidence the feature is unused — it is what this looks like.

The unit tests do not close the gap and were never meant to: `coordinator/tests/test_result_handler.py`
mocks the DB and MinIO and calls `_process_crawl_result` directly. `result_handler_loop` — where
the defect is — is never exercised.

**✅ Owner's call (2026-08-28): treat the frontier work as a rewrite, and keep it last in the
phase.** The consequence to state plainly, because "migrates last" otherwise reads as the cautious
option: §9's pre-gate — *run the activity standalone and diff it against a v1 run of the same URL* —
**does not exist for crawls, and cannot be made to exist.** There is no v1 crawl result to diff
against. Every other lane migrates with a reference implementation; this one migrates when the
least v1 machinery is left to build a reference from. The compensating gate has to be built rather
than borrowed, and that belongs in the crawl migration step's own plan.

Only two pieces are genuine ports: **link extraction** (`link_extractor.py`, pure function, moves
intact) and **sitemap discovery** (`sitemap.py`, which [13d](#13d-ssrf-at-frontier-admission-and-the-sitemap-fetcher-changes-clients)
modifies). Both are untested against reality, so "port" here means "port and then actually run".

#### 13b. The frontier stays in Postgres — the measured limits decide more than "measure later" allows

The draft deferred the frontier model — visited-set-in-workflow-state with `continue-as-new`
versus child-workflow-per-page — on the grounds that history size is the binding constraint and
should be measured rather than guessed. **The constraint is right; the deferral was too wide.**
[§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity) already measured the
limits, and `schemas/crawls.py:12-13` already publishes the ceilings, so the arithmetic is
available now:

| | |
|---|---|
| Temporal history ceiling | **51,200 events**, 50 MiB |
| `max_pages` ceiling (API-enforced, `le=10000`) | **10,000 pages** |
| ⇒ event budget per page | **≈5 events** |
| cheapest possible cost of one page | **3 events** (`ActivityTaskScheduled` / `Started` / `Completed`), before workflow-task overhead, timers or signals |

and for the visited set carried as state:

| | |
|---|---|
| 10,000 URLs at ~80 bytes | **≈800 KB** |
| §5's payload warn threshold | 256 KiB |
| §5's payload hard limit | 2 MiB |

Two conclusions follow without any new measurement. **`continue-as-new` is mandatory in both
candidate designs** — it is not a property that distinguishes one from the other, so the
either/or the draft posed was already false. And **the visited set cannot ride in a workflow
argument** at the ceiling we advertise: it is past the warn line before a single long URL is
involved.

**✅ Owner's call (2026-08-28): the frontier and the visited set stay in Postgres.** The clause
*"`crawl_queue` retires with the service"* is **withdrawn**. Temporal owns control flow, durability
and retry; Postgres keeps the set, as it does today.

Three consequences worth writing down:

1. **The workflow reaches the frontier through activities, never directly.**
   [§6](#6-oq-2--in-flight-edits-definitions-are-pinned-and-that-is-a-different-problem-from-code-versioning)'s
   determinism rule forbids a workflow body reading Postgres. This is the same rule that forbids
   loading a pipeline definition in a workflow body.
2. **The dedup mechanism is an index, not a data structure.** Today the visited set *is*
   `idx_crawl_queue_url UNIQUE (crawl_id, url)` plus `on_conflict_do_nothing`
   (`result_handler.py:103`). Whatever the table becomes must keep that property; there is no
   separate "seen" collection to port.
3. **What stays genuinely open is narrower**: the table's shape, and whether one table still
   carries both the queue and the seen-set. Decide that at build time. The *location* is settled.

#### 13c. `crawl_pages` is required, not a UI convenience

The draft said `crawl_pages` *"may be kept as a per-page result mirror for the UI."* That sentence
is untouched original text from `eb78146` (2026-08-04) and predates every decision that now leans
on the table:

| date | decision | what it needs from `crawl_pages` |
|---|---|---|
| 2026-08-08 | **P7** — crawls are metered **per page** | a per-page row to meter |
| 2026-08-17 | **§8 reversal** — every stored object is charged | a producer for each charged object |
| 2026-08-25 | **§8d** — one shared storage ledger, producer linked by nullable FK | the FK target for a crawl artifact |

Plus one that predates the ADR entirely: **the artifact's name is the page row's id.**
`dispatcher.py:120` puts `crawl_page_id` into the message's `job_id` field precisely so ADR-002
§4's path convention resolves. And P7's reclaim half — *nothing frees crawl artifacts today* —
needs to enumerate a crawl's objects; `crawl_pages.result_path` is the only enumeration that
exists.

**✅ Owner's call (2026-08-28): `crawl_pages` survives the migration. It is the metering unit, the
storage ledger's producer link, and the reclaim anchor.**

Note also that the table having **no size column** — recorded as a gap by the
[§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)
review — is now **correct** rather than missing, because §8d put the byte count on the ledger row
and gave no lane its own marker.

⚠️ **Open, deferred past Phase 4 (product, not architecture):** how a crawl is presented in the
dashboard. A crawl is not a job and does not fit the job list; it likely wants its own page. Not a
blocker for the migration — recorded so the table's survival is not mistaken for the UI question
being answered.

#### 13d. SSRF at frontier admission, and the sitemap fetcher changes clients

**⚠️ The port must switch the sitemap fetcher to `httpx`. This is a change, not a copy.**
Recorded here by the [§10](#10-oq-6--the-do-not-delete-list) review 2026-08-26, because it belongs
to this migration step and was surviving only as a paragraph in `CLAUDE.md`.

`coordinator/coordinator/sitemap.py:11` fetches `robots.txt` and sitemap XML with **aiohttp**,
from **user-supplied target sites**. Every other untrusted-target fetch in the platform already
uses `httpx` — `playwright-worker/worker/robots.py:10` is the direct sibling. That matters because
of **BUG-006**: `coordinator/` has no lockfile and has never been scanned by Dependabot, and the
live aiohttp advisory's *visible* alert is the unreachable copy (aiohttp parsing MinIO responses)
while **this** is the reachable one.

**Do not close BUG-006 as dissolved by the migration.** The service is deleted, but sitemap
discovery is *ported into a `CrawlWorkflow` activity* and carries the exposure with it unless the
port changes the client. Because this is the only do-not-delete item that must be **modified**
rather than copied, it is the easiest one to get wrong — a faithful port is the failure mode here,
not the success one.

**🔴 Found in review 2026-08-28: the client swap is the smaller half of what is wrong in that
file.** The same 58 lines contain an unvalidated fetch whose targets are chosen by the site being
crawled.

The platform's rule today is **check the URL once, at the front door**. `validate_no_ssrf` runs
exactly twice, both at creation, on the seed URL and the webhook URL (`routers/crawls.py:34-36`).
The coordinator validates nothing, and **no worker validates anything** — there is no SSRF check,
IP-range test or `getaddrinfo` call anywhere in `http-worker/`, `playwright-worker/` or
`llm-worker/`. Everything discovered after creation is admitted unchecked.

For links that is *mostly* survivable by accident: `link_extractor.py:33` discards anything not on
the seed's own origin. **Sitemap URLs get no such restriction.** `sitemap.py:39` takes them
verbatim from the target's `robots.txt` body and `:45` fetches them; `result_handler.py:180-185`
then enqueues them into `crawl_queue` with **no origin filter** — the include/exclude path filters
apply only to extracted links. Two ways of discovering a URL, one guarded, one not.

The chain, end to end:

```
user submits   https://evil.example/            → SSRF-checked, public, allowed
evil.example/robots.txt says
    Sitemap: http://169.254.169.254/latest/meta-data/...
coordinator fetches it                           → unchecked  (sitemap.py:45)
coordinator enqueues it                          → unchecked  (result_handler.py:183)
a worker scrapes it and uploads the body         → unchecked  (no worker validates)
user reads it via GET /crawls/{id}/pages         → the response comes back out
```

The last step is what makes this a **read** primitive rather than a blind fetch: the response body
is persisted to the tenant's bucket and served through the normal API.

**It has never fired, for exactly one reason: sitemap discovery is only reachable from
`_process_crawl_result`, which per [13a](#13a-this-is-a-rewrite-not-a-port-and-the-only-lane-with-no-v1-to-compare-against)
has never run.** The migration is what switches it on. This is the mirror image of the usual
"latent bug" note — the code is not latent because nobody hit the case, it is latent because the
component is dead, and reviving it is precisely what this section proposes.

**✅ Owner's call (2026-08-28): every URL entering the frontier is SSRF-checked, at admission —
not only the seed.** The check belongs at the point where a URL joins the queue, which is the one
place all three discovery routes (seed, extracted link, sitemap entry) converge. A rejected URL is
**skipped, and the crawl continues** — it is not a crawl failure, because the user did not choose
that URL and cannot fix it. This mirrors [§10](#10-oq-6--the-do-not-delete-list)'s rule that an
SSRF refusal is terminal and never retried: re-resolving a hostname an attacker is actively
rebinding is the failure mode there too.

⚠️ **Still open, and needed before the crawl step is built: are sitemap entries restricted to the
seed's origin, the way extracted links already are?** The SSRF check stops the internal-address
case; it does not stop a `robots.txt` pointing the crawl at an unrelated public site, which is a
quota and attribution question rather than a security one. Legitimate sitemaps do occasionally
cross subdomains, so this is a real trade-off and not a free tightening.

**Related, and deliberately not decided here: a worker-side SSRF check.** The correct security
position is to validate at the point of use — the worker is what opens the socket, and a check
there covers every lane including ones not yet built. It is **not** a substitute for the admission
check (a worker cannot tell "the user typed a bad URL", which should 400 at creation, from "a
crawl discovered one", which should skip a page and continue), and it is a larger piece of work
than it looks: two implementations in two languages that must not drift — the risk
`playwright-worker/worker/robots.py`'s *"mirrors the Go worker's internal/robots package"* already
carries — a DNS-rebinding window that a naive check narrows without closing, and a new terminal
failure class that must be wired into the retry classifier at the same time. **Filed separately as
a cross-lane item; it is not the crawl migration's to carry.**

### 14. OQ-10 (remaining half) — Conditional execution gets its own layer-A PRD, before Monitors

The PM resolved change detection: both halves go to **Monitors (B)**, because *"the previous run
of this same thing"* is undefinable in layer A once R1's run inputs exist. That decision handed
back the sequencing of conditional execution.

**Decision: conditional execution is specified in a follow-up layer-A PRD, written before
PRD-018 (Monitors). It is not absorbed into B.**

If B absorbs it, the reviewer of B's PRD is designing layer A's block model again under a
different heading. That is the "unplanned layer-A work" OQ-10 was raised to prevent — absorbing
it does not prevent the work, it only renames it and hides it inside a PRD nobody will read as a
layer-A change.

The cost of a separate PRD is low precisely because [§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)
already left the room: explicit named wiring, block identifiers stable across versions,
graph-shaped storage with linearity enforced in the validator, and a `skipped` block state. The follow-up PRD is
additive to the model rather than a re-specification of it. That is the payoff of the
forward-compatibility constraint, and it is worth stating that the constraint has now been *spent*
on something concrete rather than held as a vague intention. **That claim is true of the wiring
and only of the wiring** — [14c](#14c-the-cheap-part-is-the-wiring-the-hard-part-is-what-a-condition-is-allowed-to-say)
records the part of the follow-up PRD none of the four choices helps with.

#### 14a. The order is two orders, and the section stated them in one notation

The ordering line below reads `A ships → C → conditional PRD → B`. That chain mixes a **shipped
product**, a **document** and a **layer** in one sequence of arrows, and a sequencing decision has
exactly one job: distinguishing when something is *written* from when it is *built*. Stated
separately:

| | when |
|---|---|
| **Conditional PRD is written** | before PRD-018 is written. It may be written while layer A is still being built — nothing forbids it, and writing it early is what tests whether the four forward-compatibility choices actually suffice. |
| **Conditional execution is built** | **after layer A ships** — PRD-016's Non-goals already say so ("conditional execution is the *first* thing to add after it ships"), and this section is where that belongs, since §14 is the sequencing decision and Non-goals is a list of exclusions. |
| **…and before B is built** | forced, not chosen: the PM made the cost gate a **launch requirement** of Monitors rather than a backlog item, and the gate consumes this primitive. |
| **Layer C (PRD-017)** | independent of the whole chain — see [14b](#14b-layer-c-is-two-things-and-only-one-of-them-is-a-block-type) for what "independent" was and was not verified against. |

**Why building it after A rather than inside A is right, stated because the section asserted the
order without defending it:** the gate's purpose is to skip the LLM call and the webhook when the
page is byte-identical to last time. That saves nothing unless the same pipeline runs against the
same URL repeatedly, and **layer A has no scheduling** — recurrence, durable sleeps and timers are
all Monitors. A layer-A pipeline runs when a user triggers it. So building conditionals into layer
A means building a capability whose only known consumer does not exist yet, and the accepted cost
of shipping without it (one LLM call and one stored artifact per unchanged repeat run, versus the
job path, which does hash-check) is near zero in layer A by construction. It is B that makes the
absence hurt, which is exactly why B is where the PM made it a launch requirement.

#### 14b. Layer C is two things, and only one of them is a block type

The ordering line waves C through on the grounds that it *"adds block types without extending what
a pipeline can express."* C is **two** deliverables, and that sentence covers one:

| part of layer C | is it a block type? |
|---|---|
| sink blocks — S3, database, Sheet, email | ✅ yes |
| **saga rollback** — one delivery fails partway, so the ones that already succeeded are cleanly undone | ❌ no. Nobody authors a rollback block. |

**The sink half checks out, and for a reason worth recording** rather than leaving as an
assumption: [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity) split the
catalog into content-producing and **effect** blocks, and effect blocks pass their input reference
through unchanged. So "one result, several destinations" is expressible as a chain of effect blocks
under §4's single-chain data flow, and **C does not need the data-flow fan-out §4 deferred past
Phase 4.** That is the substantive content of the parallelism claim, and it holds.

**The rollback half was not examined at all.** It is not a new block; it is a new *run shape* —
the run turning around and executing compensating work the user never wrote down:

```
layer A today          Scrape → Clean → LLM → Webhook → done      (forward only)

layer C with rollback  Scrape → Clean → LLM → S3 ✅ → BigQuery ✅ → Email ❌
                                                  undo BigQuery ← undo S3
```

Where that lands on layer A is a concrete, checkable question: **what is the S3 block's status
after it has been compensated?** `pipeline_run_blocks.status` was fixed in §4 as `pending`,
`running`, `completed`, `failed`, `skipped`. It is not `completed` (the object is gone), not
`failed` (it worked — BigQuery failed), and not `skipped` (it ran). **There is no value for
"succeeded, then undone."**

⚠️ **This is the same trap §4 spent effort avoiding, aimed at the nearer layer.** `skipped` was
admitted on day one, though nothing in R2's catalog produces it, expressly *"so that B is a new
block type rather than a schema change plus a backfill."* The identical exposure for C went
unnoticed because the sentence clearing C to run in parallel only ever looked at the sinks — and C
is the layer that may start **soonest**.

✅ **Obligation, not a decision made here:** PRD-017 must settle how a compensated block is
recorded — a sixth status value, a reason column beside the state (§4's stated preference when two
cases share one fact), or rows that are not `pipeline_run_blocks` rows at all — **before C starts
building, not during.** Whether that requires a layer-A schema change is the question; if it does,
it is cheapest on the same day-one logic already applied to `skipped`. Until it is answered,
"C may proceed in parallel" is a claim about the sinks only.

#### 14c. The cheap part is the wiring; the hard part is what a condition is allowed to say

The four choices that make the follow-up PRD additive — named input references, identifiers stable
across versions, graph-shaped storage behind a linear validator, and `skipped` — share a property:
**every one of them is about how blocks are connected.** None of them says anything about the
question a conditional block exists to ask, which is *"if **what**?"*

That question runs straight into a hard rule of PRD-016's Non-goals: **user-authored code as a
block is excluded**, and *"an expression evaluator would cross the line."* A condition is, on its
face, an expression. So the first and hardest job of the follow-up PRD is to let a user say *"if
the price changed"* without handing them a programming language — and the "cost is low" claim
above buys nothing towards it.

✅ **The precedent to follow is the Validate block, and it should be named in the follow-up PRD's
brief rather than rediscovered.** PRD-016 already fought this and won: Validate rules are a fixed
declarative vocabulary, not expressions, for three reasons — the expression-evaluator non-goal, the
fact that validation is terminal and bills the user before it fires, and, decisively for this
section, **replay determinism**.

⚠️ **The determinism constraint binds a conditional harder than it binds Validate**, because
branching is decided in the workflow body itself (`workflows-scoping.md` §4A: *"branching is an
`if` in workflow code"*), and the workflow body is precisely the code Temporal replays.
[§6](#6-oq-2--in-flight-edits-definitions-are-pinned-and-that-is-a-different-problem-from-code-versioning)'s
determinism rule therefore applies to the condition itself, not merely to the blocks around it. A
condition that can return a different answer on replay does not fail when it is written — it fails
after an unrelated pod restart:

```
16:58  run starts; condition "hour < 17" is TRUE  → LLM block runs, user is billed
17:03  workflow worker pod restarts (routine deploy)
17:03  Temporal replays the history; the same condition now evaluates FALSE
       → recorded history and re-executed code disagree; the run is corrupt
       → nothing was wrong at 16:58, and nothing logs a cause at 17:03
```

One thing the follow-up PRD does **not** need to re-litigate, recorded so it is not mistaken for an
open problem: the cost gate's comparand is supplied by the **monitor**, not by layer A, so the
conditional block will need one more config field bindable at trigger time. §4 already settled that
**widening the bindable-field list is additive** ("declare one more field bindable"), and only
narrowing it breaks saved pipelines.

#### 14d. The deliverable this section creates has no name, and that is the failure this section is about

§14's argument is that absorbing the work into B *"renames it and hides it inside a PRD nobody will
read as a layer-A change."* Measured against the doc set as it stands, the separate PRD is only
partly better off:

| | PRD-017 (C) | PRD-018 (B) | the conditional PRD |
|---|---|---|---|
| has a number | ✅ | ✅ | ❌ |
| has a row in `phase4-backlog.md` §2 | ✅ | ✅ | ❌ — one line of prose in the artifact chain |
| records what it **owes** | ✅ multi-sink fan-out with rollback | ✅ does not ship without the cost gate | ❌ nothing recorded |

The one deliverable this section *creates*, and the only one that **blocks** another PRD, is the
only one in the chain with no number, no row and no stated obligations. **A nameless PRD is only
marginally more visible than an absorbed one**, which is the property §14 exists to buy.

✅ **Obligation:** give it a number and a `phase4-backlog.md` §2 row alongside PRD-017 and PRD-018,
carrying the three commitments this review names — the Validate-precedent brief and the replay
constraint ([14c](#14c-the-cheap-part-is-the-wiring-the-hard-part-is-what-a-condition-is-allowed-to-say)),
and the halt-early block B cannot build for itself (§4). ⚠️ **The number is a genuine choice, not
bookkeeping:** creation order gives PRD-019, which reads as *after* PRD-018 in every index while
being required *before* it. Left open.

⚠️ **`workflows-scoping.md` is now stale in three places, and its status banner flags only two of
its own.** Found while checking this section's ordering claims:

| passage | still says | actually decided |
|---|---|---|
| §4A, layer A's block catalog | `scrape / clean / LLM / validate / `**`branch`**` / deliver` | branch is **not** in layer A — this section is why |
| §5, phased roadmap | Phase 1 = A · Phase 2 = C · Phase 3 = B | there is now a **conditional step between C and B** that the table has no row for |
| §6, worker integration | *"**Recommendation: (a) for Phase 1**"* — activities dispatch to the existing workers over NATS | ⚠️ **reversed 2026-08-23** by [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected): option (a) is rejected **and blocked** — a work-queue stream refuses the second consumer it requires |

The third is unrelated to §14 and is the urgent one: it reads as a live recommendation to build the
design that was rejected, in the same class as the known staleness in
`temporal-full-migration.md`. Same disposition — 🔴 markers now, one redraw after the review
closes.

**Resulting order** (specification and implementation separated per [14a](#14a-the-order-is-two-orders-and-the-section-stated-them-in-one-notation)):

```
build layer A (PRD-016, no conditionals)
      │
      ├─► PRD-017 (C — Delivery sinks): unblocked, may proceed in parallel
      │        ⚠️ subject to 14b — the sinks are clear, the rollback is unverified
      │
      └─► write conditional-execution PRD  (layer A; may begin during A's build)
                 │
                 └─► build conditional execution
                            │
                            └─► PRD-018 (B — Monitors), which cannot ship without the cost gate
```

> **✅ Settled on review (2026-09-01).** The decision is upheld unchanged — a separate layer-A PRD,
> written before PRD-018, not absorbed into B. Four gaps closed, none of them cosmetic.
> **(1)** The ordering line stated a sequencing decision in a notation that could not distinguish
> *writing* a PRD from *building* the feature; the two orders are now separated, the
> build-after-A answer is moved here from PRD-016's Non-goals, and the reason it is right — the
> cost gate has no consumer until something makes pipelines recur, and only Monitors does — is
> stated rather than assumed. **(2)** "C adds block types without extending what a pipeline can
> express" covered the sinks and not **saga rollback**, which is a run shape rather than a block;
> the sink half is now *verified* through §5's effect-block pass-through (so C needs none of the
> deferred data-flow fan-out), and the open half is named: `pipeline_run_blocks.status` has no
> value for "succeeded, then undone", which is the schema-change-plus-backfill trap §4 avoided
> for B, aimed at the layer that may start soonest. **(3)** The "cost is low" list is entirely
> about wiring; the follow-up PRD's hardest question is the **condition vocabulary**, which runs
> into the expression-evaluator non-goal and, harder, into replay determinism — the **Validate
> block is the precedent** and belongs in the brief. **(4)** The deliverable this section creates
> has no number, no backlog row and no recorded obligations, while both of its siblings have all
> three — the visibility this section's own argument is made of. Also flagged: `workflows-scoping.md`
> contradicts this section in two places and the reversed §9 in a third.

### 15. OQ-11 — Webhook delivery is a step the run waits for

**Decision: option (c). The Webhook block waits for real delivery on its own durable horizon.
There is no `webhook_deliveries` row for the v2 lane.**

*(Reviewed 2026-09-02. The decision is upheld. Two of its supporting statements could not be
built as written — the concurrency rule it leans on has no column to live in (15a), and the
mechanism it names cannot produce the schedule it promises (15b) — and four things it did not
say are settled in 15c–15f.)*

- **The activity's retry policy *is* the delivery loop.** Workflow history is the attempt record.
  A parallel table would be a second source of truth for "did it deliver." ⚠️ *Amended by 15b:*
  the retry policy carries the retries, but it cannot reproduce today's interval list, so the
  word "exactly" below is withdrawn. ⚠️ *Scoped by 15e:* "the Web UI shows it" was in this bullet
  and is withdrawn — [§2b](#2b-the-web-ui-is-not-ingress-exposed) does not expose the Web UI, so
  the operator-facing half of this argument is a `kubectl port-forward`, not a page.
- **The horizon is matched to today's reach** so no capability is lost. `BACKOFF_SECONDS`
  (`[0, 30, 300, 1800, 7200]`) across `webhook_max_attempts = 5` gives a total reach of
  **≈2.6 hours**. ✅ *Verified on review, including against production* — the infra repo sets
  `WEBHOOK_MAX_ATTEMPTS: "5"` in `app/api.yaml`, matching `settings.py`, so the ≈2.6 h figure is
  the deployed one. (Checked deliberately: the §10 review found `llm_request_timeout_seconds`
  overridden in production and the repo default therefore misleading. That trap does not repeat
  here.) A block horizon in that range reproduces today's "a receiver down for two hours still
  gets its delivery" — **approximately, not exactly; see 15b.**
- **The PRD's objection to (c) dissolves.** It worried that runs waiting hours on dead receivers
  would consume a user's concurrency budget. But that ceiling protects **worker capacity**, and a
  workflow sleeping on a durable timer occupies none — it is not resident anywhere. Hence
  [§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)'s
  rule: the ceiling counts runs **actively executing a block**. The collision was an artefact of
  counting the wrong thing. *(Scoped on review, 2026-08-17: this holds for the **pipeline lane**,
  which is all this section governs. §3's counting view defines active as "not yet finished", and
  **v1 keeps that definition** — `quota.py:59` counts `pending` rows that occupy no worker either,
  and R5 forbids changing it. §8 carries the per-lane split; the argument above is not a claim
  about jobs.)* ⚠️ **This bullet is true and unbuildable as stated — see 15a**, which supplies the
  thing it is missing.
- **The PM's one-Webhook-block cap bounds this to at most one open delivery per run**, which
  removes the pathological case entirely. *(Per run. It is not a bound on how many runs a user may
  have parked at once — see 15a.)*

**Rejected — (b), "succeeds once durably queued."** It makes "the block succeeded" mean *queued*
rather than *delivered*, which is the quiet lie R3's per-block status exists to eliminate. It is
also not the cheap reuse it appears to be: `webhook_deliveries` carries two CHECK constraints —
`num_nonnulls(job_id, batch_id, crawl_id) = 1` and `num_nonnulls(run_id, crawl_id) = 1`, with
`run_id` an FK into `job_runs` — so a pipeline delivery row is **rejected by the database**. (b)
requires a migration loosening both constraints before it can be considered free. ✅ *Verified on
review against `models/webhook_delivery.py`: both constraints and the FK are as described.*

**Rejected — (a), "fail the run, retries bounded by the block budget."** Consistent and simple,
but it silently loses today's multi-hour reach.

**SSRF re-validation stays inside the activity, on every attempt** — DNS rebinding is why it is
per-attempt rather than per-creation, and that reason is unchanged by the transport. Two riders
added on review. **It must be raised as non-retryable** — this is the second of §10's three
non-retryable obligations, and it lands here: an SSRF refusal that Temporal is free to retry
becomes ≈2.6 hours of re-resolving a hostname an attacker is actively rebinding, which is the
attacker's best case and the exact inverse of today's behaviour. And **the consequence is
larger on this lane than on the job lane**: today an SSRF block marks the delivery `exhausted`,
does not count as an attempt, and never touches the job's outcome; under (c) it is a terminal
block failure, so **the run fails** — because a URL the user saved weeks ago resolves somewhere
new today. That is inside the R6 divergence recorded below, but it is different in kind from a
receiver being down, and it is the one failure mode a user cannot fix by bringing their server
back up.

**Recorded against R6:** an undelivered webhook fails a pipeline run where it never fails a job.
That is a known exclusion, already in PRD-016.

**One consequence for v1:** because layer A writes no `webhook_deliveries` rows, the existing
`idx_webhook_deliveries_dedup` unique index on `(run_id, event)` keeps its assumption true and the
existing machinery stays correct for the job lane. The PM's one-block cap makes this durable
rather than coincidental. ✅ *Verified: the index is created in migration 3.18
(`8f4b6eb47abb`), `UNIQUE (run_id, event) WHERE run_id IS NOT NULL`.*

#### 15a. "Parked runs do not hold a slot" is a property of the run's **state**, not of the Webhook block type — and no column expresses it today

**✅ Owner's call: pre-admit a `waiting` value to `pipeline_run_blocks.status` now, and let
[§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)'s existing
content-producing / effect split decide which blocks may enter it.**

The third bullet above is this section's answer to the PM's only objection, and §8 made it an
owner's call on 2026-08-17: *a pipeline run parked on a durable timer is not active.* That
decision has to execute as a **SQL predicate**, because the concurrency check runs in the API
when a run is started. So the question the sections never asked is: *what does that query read?*

Two runs, in the app database, at the same instant:

```
run A — inside a 4-minute LLM call
  pipeline_runs        in flight
  pipeline_run_blocks  [Scrape ✅][Clean ✅][LLM  running][Webhook pending]

run B — 40 minutes into a 2.6-hour webhook backoff
  pipeline_runs        in flight
  pipeline_run_blocks  [Scrape ✅][Clean ✅][LLM ✅][Webhook  running]
```

They are indistinguishable. One is holding a worker and a paid API key; the other is a timer.
And the vocabulary could not be stretched to separate them. As
[§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring) stood
before this review, **block state** was `pending`/`running`/`completed`/`failed`/`skipped` — no
waiting value — and **run outcome** is `completed`/`failed`/`cancelled`, three terminal values
with no in-flight value enumerated at all. So the concrete failure is the objection §15 declares
dissolved, arriving anyway:

> A user has 5 concurrent slots. Five runs are parked on webhooks pointing at a receiver that
> went down. The counting view sees five blocks at `running`, refuses the sixth run, and **the
> user is locked out for two and a half hours by five timers.**

**Why the obvious fix is wrong, and this is the owner's finding:** the tempting predicate is
*"a `running` Webhook block does not count"* — special-case the one block type that waits. That
survives exactly until layer C, whose sinks deliver to **the user's own S3, the user's own
database, the user's own mail server** — infrastructure precisely as unavailable as their webhook
receiver, wanting precisely the same long horizon. The rule then reads:

```
run A   [ S3 sink  running ]   parked 40 min, holding nothing  →  counts
run B   [ Webhook  running ]   parked 40 min, holding nothing  →  does not count
```

Same situation, opposite answer, no explanation a user could be given — and it fails in the
worse direction, with the sink locking the user out of their own quota. **The distinction is not
which block it is. It is whether the run is holding worker capacity at this instant**, which
flips back and forth *inside* a single block: holding during each 10-second POST, holding nothing
across each backoff sleep.

Two mechanisms, and the decision takes both:

- **The category rule decides who may wait.** §5 already split the catalog into
  **content-producing** blocks (Scrape, Clean, LLM — they write an object) and **effect** blocks
  (Validate, Webhook, and every layer-C sink — they do not). That line already sits where "heavy"
  sits. No block type is ever enumerated; a new sink inherits the behaviour by being an effect
  block, which is the property this section needs and the type-aware predicate cannot provide.
- **The state value records it.** `waiting` is added to `pipeline_run_blocks.status` **now**, for
  the reason §4 pre-admitted `skipped`: Monitors (B) will park a run on a durable sleep that
  belongs to **no block at all** — between blocks, or before the first — which the category rule
  has nothing to say about. Adding a value today is a line in a `CHECK` constraint; adding it once
  pipeline runs exist is a migration plus a backfill, the specific outcome §4 spent effort
  avoiding. **This is the third missing value found in the same vocabulary in three review
  sessions** (`skipped`, pre-admitted by §4; "succeeded then undone", found by the §14 review for
  saga rollback; `waiting`, here) — and the last two share a cause: the vocabulary describes a
  block's *progress*, while both callers need what the run is *doing with resources*.

The counting view's v2 arm therefore reads **"a run with at least one block in `running`"**, and
the per-lane predicate §8 already accepted stays as scoped there.

⚠️ **One accepted interaction.** Whoever writes `waiting` writes it at every timer boundary, and
[§11c](#11-oq-7--run-state-to-the-spa-mirror-activity-plus-pg_notify) decided **a failed mirror
write fails the run**. So a delivery that would have succeeded can fail because a status write
failed during its backoff. Accepted rather than special-cased: §11c's reasoning — a run whose
state cannot be read is not a successful run — does not weaken because the state in question is
`waiting`.

#### 15b. The mechanism cannot produce the schedule: "reproduces today's behaviour exactly" is withdrawn

**✅ Owner's call: keep the retry policy; drop "exactly"; record the drift.**

Two of this section's statements are incompatible. The first bullet says the delivery loop **is
the activity's retry policy**. The second says the horizon reproduces today's behaviour
**exactly**. Today's backoff is an explicit list, and it is not a curve:

```
today          30s  →  300s  →  1800s  →  7200s
ratio                 ×10      ×6        ×4        ← not geometric
```

A Temporal retry policy takes four numbers — first interval, multiplier, ceiling, maximum
attempts. **There is no way to hand it a list.** The closest fit, and what should be configured:

```
initial 30s · multiplier ×10 · ceiling 7200s · maximum_attempts 5

              attempt 1   attempt 2   attempt 3   attempt 4   attempt 5
today            0s         30s        5m 30s     35m 30s     2h 35m
retry policy     0s         30s        5m 30s     55m 30s     2h 55m
                 same       same       same       ✗ +20m      ✗ +20m
```

Same number of POSTs; a horizon ~13% longer; attempts 1–3 identical and 4–5 displaced. The
alternative — writing the exact list as explicit durable sleeps in the workflow body around a
**non-retrying** activity — is byte-exact and **contradicts the first bullet**, because it
rebuilds the delivery loop in the workflow, which is the thing this section says it is not doing.

So: **the retry policy carries it, the attempt count is preserved, the intermediate timing
drifts, and the word "exactly" is withdrawn.** The drift is recorded here rather than left to be
found during the R6 comparison, where an unqualified "exactly" would read as a failure of the
gate. A receiver doing its own idempotency bookkeeping sees the same five deliveries; it sees two
of them at different times.

#### 15c. The horizon lives in one of four nested timeouts, set in three different files

**✅ Owner's call: the ladder below is part of this decision, not an implementation detail.**

Today there is effectively one number — `timeout=10.0` on the POST — plus a loop we wrote.
Temporal replaces the loop with configuration, and the horizon becomes one of **four nested
limits, each measuring a different span, where the smallest silently wins**:

| | measures | from → to | value | set in |
|---|---|---|---|---|
| 1 · POST timeout | one HTTP request | connection opened → response | **10s** (today's) | the activity's own code |
| 2 · `start_to_close` | **one attempt** | worker picks the task up → worker returns | **~20s** | the workflow, at the call site |
| 3 · `schedule_to_close` | **all attempts + all sleeping between them** | first scheduled → finally succeeded or gave up | **≥ 2.6 h** ← *the horizon* | the workflow, one line later |
| 4 · the run's time budget (R4) | the whole pipeline run | trigger → finish | **> 2.6 h + every other block** | pipeline / operator config |

**Each must be strictly greater than the one nested inside it.** Two failures, both silent:

- **The natural one.** You want 2.6 hours, so you set it on `start_to_close` — the timeout most
  people reach for. A single POST to a receiver that accepts the connection and never answers is
  now allowed to hang for two and a half hours. One attempt happens instead of five; the retry
  schedule never runs.
- **The Q6 one.** Someone sets a reasonable-sounding operational limit — *"no pipeline run may
  exceed one hour"* — in a different file, for a different reason, knowing nothing about
  webhooks. Four attempts happen instead of five and the reach becomes ~56 minutes. Nothing
  errors. This is Q6's exact shape: NATS `ack_wait` at 30s under an LLM call taking 60s+, where
  the symptom (an infinite re-scrape loop) pointed nowhere near the setting. R4 already requires
  time budgets to compose; **this is the first concrete place where they must.**

Note also that `maximum_attempts = 5` and `schedule_to_close` are **two stop conditions**, and
whichever hits first wins. `maximum_attempts` is what normally ends it (it is what preserves
today's attempt count); `schedule_to_close` is the outer wall. *(Temporal's fifth timeout,
`schedule_to_start`, bounds queue wait and is deliberately left unset.)*

#### 15d. Cancellation now takes up to 2.6 hours, and a cancelled run still delivers

**✅ Owner's call: the API sends a cancel to the workflow in addition to writing the row, and the
workflow re-checks at every backoff boundary as the fallback.**

Two decisions collide, neither written knowing about the other:

- The PM's rule (2026-08-04): **cancellation never aborts a block mid-execution.**
- This section: **one block can now run for 2.6 hours.**

That rule was priced when every block lasted seconds to minutes. §15 introduces the first block
measured in hours, and nobody re-checked what the rule then means:

```
14:00  Webhook block starts; receiver is down; retries begin
14:05  user clicks Cancel
       → the API writes cancelled and notifies; the page greys out instantly   (§11a)
       → the workflow is not told. The block is mid-execution.
14:35  attempt 4  ← POSTing on behalf of a run cancelled thirty minutes ago
16:35  attempt 5 succeeds
       → the customer's system is told the run finished
```

Today this cannot happen, and not by luck: the delivery row is created **when the run completes**,
so a cancelled run has no delivery. Note also that [§11a](#11-oq-7--run-state-to-the-spa-mirror-activity-plus-pg_notify)'s
precedence rule protects the **status column** — it says nothing about the outbound HTTP request,
which is the part the user's customer actually sees.

Temporal cancels natively at await points, and a durable sleep is an await point — so the
interruption is free **if the workflow is told**. §11a's decision is that cancellation is written
straight to the database so the button stays instant, which means it is not told. Hence both
halves:

| | mechanism | worst case after Cancel |
|---|---|---|
| primary | the cancel handler writes the row (unchanged, instant UI) **and** sends a cancel to Temporal, best-effort | seconds |
| fallback | after each backoff sleep the workflow re-reads `pipeline_runs.status` through an activity and stops if it is `cancelled` | up to 2 hours — the last backoff step is 7200s |

The row write happens first and the signal is best-effort, so §11a's instant-UI property is
untouched and a Temporal outage degrades to the fallback rather than failing the cancel. The
fallback alone is not sufficient — it turns 2.6 hours into 2 hours — but it is the safety net,
and it costs one activity call per boundary.

**This is not webhook-specific work.** Monitors will park runs on multi-day sleeps, where "cancel
a run that is asleep" is the identical problem. Building it here builds it there.

#### 15e. "No `webhook_deliveries` row for v2" removes admin capability and blinds three meters

**✅ Owner's call: the two admin endpoints become job-lane-only, deliberately and permanently. The
three counters must be renamed or scoped — they may not be left reading a table one lane no longer
writes to.**

The section treats the table as a *record*, and on that it is right. But the table also backs
live API surface:

```
GET  /admin/webhooks/deliveries              list, filter by status   (admin.py:367)
POST /admin/webhooks/deliveries/{id}/retry   attempts = 0, re-queue   (admin.py:387)
```

**What sunsets, and why that is acceptable.** Manual retry mattered *because failure was
invisible*: a delivery exhausts at 3 a.m. into a table nobody watches, so an admin re-firing it is
the only recovery, and it works a week later. Under (c) there is nothing to re-fire — the workflow
closed at 2.6 hours — but there is also nothing hidden: **the run itself failed**, in the user's
own run list, and the user can re-trigger the pipeline. Visibility moves from a hidden admin table
to the person who cares. The endpoints are **not deleted** — they keep serving the job lane for as
long as v1 exists — so the accurate statement is **"job lane only, by design"**, recorded here so
a later reader does not file the gap as a bug and close it by widening the table.

**What may not simply be left.** Three meters read `webhook_deliveries` with **no lane filter**:

```
webhook_deliveries_pending          admin.py:_build_operational_stats   ← rendered on the Usage page
webhook_deliveries_exhausted        admin.py:_build_operational_stats
webhook_delivery_success_rate_7d    admin.py:_build_historical_stats
```

After pipelines ship, the dashboard reports **webhook success rate 100%** while every pipeline
delivery in the system is failing. Nothing errors; the number is well-formed and wrong. ⚠️ **That
is the shape of this project's recurring defect** — a meter keyed on a table a new lane does not
write to. It is BUG-005 (batch runs invisible because `job_id` is NULL), it is P7 (crawls
invisible because every meter reads `job_runs`), and it is why §3 moved run counting onto a view
instead of naming a table. Declining to build pipeline delivery stats is a legitimate choice;
leaving a meter that lies is not, and the fix is naming, not features: scope them to the job lane
in their own identifiers, or return them absent for the pipeline lane.

Also corrected here: the first bullet's *"and the Web UI shows it"* is withdrawn. §2b decided the
Temporal Web UI is not exposed — `kubectl port-forward` only — so what replaces an admin HTTP
endpoint requires cluster credentials. To be fair to the section, per-attempt **detail** is
parity, not a loss: today's row also keeps only `attempts` and `last_error`, never every error.
The real differences are **retention** (a row lives until its parent is deleted; Temporal history
is kept 30 days per §2c) and **reach**.

#### 15f. The failure-notification obligation was handed to a PRD whose obligation list does not contain it

**✅ Owner's call: it goes on the conditional-execution PRD's backlog row, with the three
obligations §14d already owes it.**

Today **any** failure notifies — a dead scrape, a bad LLM key, anything
(`result_consumer.py:563–575`) — because delivery is triggered by the run's *outcome*, not by a
position in the recipe. In a pipeline the Webhook block is a step in a chain: an earlier terminal
failure stops the chain, the block never runs, **and nobody is told.**

PRD-016 records this and passes it on — it is settled "when conditional execution is settled,"
as either an on-failure branch or a run-level notification. Then [§14](#14-oq-10-remaining-half--conditional-execution-gets-its-own-layer-a-prd-before-monitors),
reviewed 2026-09-01, **created that PRD and enumerated what it owes** — the Validate-block
precedent, the replay-determinism constraint, the halt-early block Monitors cannot build for
itself — and **failure notification is not on the list.** The obligation was handed to a document
whose obligations were written down a week later without it. §15 owns webhook semantics and is
the last section that passes over it before the review closes.

The sharpest form of the gap is a configuration, not a behaviour. `webhook_events` is a live
user-facing setting validated against `{job.completed, job.failed, crawl.completed,
batch.completed}`, so a user may save:

```json
{ "webhook_events": ["job.failed"] }        "only tell me when it breaks"
```

**That job has no expressible pipeline equivalent in layer A at all** — the only notification
mechanism sits at the end of the success path. Not a divergence in outcome: an entire
configuration that cannot be migrated, which is a stronger statement than the R6 exclusion
already recorded and belongs beside it.

### 16. The v1/v2 coexistence contract

**Definitions.** **v1** = NATS JetStream + `result_consumer.py` + `scheduler.py` +
`webhook_loop.py` + `advisory.py` + `coordinator/`. **v2** = Temporal (server, workflow workers,
and eventually activity workers).

**Routing rule.** New **pipelines** are v2-only from the first day they exist — there is no v1
pipeline implementation, so there is nothing to route between. Existing **jobs, batches and
crawls** stay on v1 until their flow is explicitly migrated.

This is worth stating plainly because it changes the risk profile of the first increment: layer A
**adds a lane** rather than splitting an existing one. R5's "exactly one lane" and OQ-3's
structural enforcement are trivially satisfied for pipelines, and become a real problem only at
**the job cutover**, when jobs move to `JobWorkflow` and the same unit of work has two possible
executors. ⚠️ The same fact cuts the other way on rollback, which the section originally missed —
see [16d](#16d-every-step-is-reversible-is-false-for-the-increment-that-ships-first).

**Cutover obligations** (from `phase4-backlog.md` §2, restated as contract terms):

1. A unit of work executes on **exactly one lane**, enforced per [§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee).
2. A recurring job moved to a Temporal Schedule is **paused in v1 first**, in that order, with
   the pause verified before the Schedule is created. ⚠️ The flag that does this is
   **user-facing and user-writable**, and one live admin meter counts it — obligations and
   consequences in [16c](#16c-obligation-2-borrows-a-user-facing-switch-and-one-live-meter-reads-it).
3. NATS workers stay alive until v1 is drained — but under the reversed
   [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)
   they do so **as a second deployment of the same image**, bound to NATS, alongside a
   Temporal-bound one. Removing v1's executors is deleting that deployment, and it happens after,
   not during, the flow migration.

**Sequence** (`temporal-full-migration.md` §9 is the detailed version; **reordered by the §9
review, 2026-08-23**; **named rather than numbered by the §16 review, 2026-09-03** —
[16a](#16a-steps-get-names-because-the-numbers-have-already-moved-once)):

| step | what it does |
|---|---|
| **Engine up** | Temporal server, its own Postgres, one workflow-worker pod. NATS untouched. |
| **Worker port** | the three workers gain Temporal activity entry points, **Go http-worker first**. |
| **Pipeline lane** | pipelines run end-to-end on v2; this is where the **R6** acceptance gate is run. |
| **Job cutover** | jobs move onto `JobWorkflow`. |
| **Batch and crawl cutover** | batches onto `BatchWorkflow`, crawls onto `CrawlWorkflow` ([§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block)). |
| **Schedule and webhook cutover** | Temporal Schedules replace `scheduler.py`; delivery becomes an activity. `webhook_loop.py` and `advisory.py` go. |
| **Consumer deletion** | `result_consumer.py` is deleted once no flow routes through it. |
| **NATS removal** | the stream, the consumers and the client dependencies go. |
| **API thinning** | the single-replica / `Recreate` constraint is lifted; horizontal scaling and rolling deploys. |

The **worker port** moves ahead of the **job cutover**: with option (a) rejected there is no
bridge to carry pipelines in the meantime, so the activity workers *are* the first increment's
executors. ⚠️ The **pipeline lane** step also has prerequisites that are not on this list at all —
[16e](#16e-the-sequence-begins-after-three-items-this-adr-moved-in-front-of-it).

**The shape of coexistence is drawn in `temporal-full-migration.md` §9a**, not here: it changes at
four of these steps, so it is sequence material rather than a decision. Two things it shows that
this contract only states in words — the API keeps `replicas: 1` and all four loops until the
**schedule and webhook cutover** (the thinning is the *last* payoff, not the first), and the three
workers serve **both lanes at once**. *(The diagram predates the §9 reversal and needs redrawing:
serving both lanes is now two deployments of one image rather than one process reading NATS for
both, and the retry-stacking hazard it illustrates is largely gone with the bridge.)*

**Reversibility.** Reversibility is a property of **migrated** flows, not of the plan as a whole.
A flow that was cut over falls back to its v1 path until it is fixed; a flow that has no v1
implementation — which is every pipeline, by the routing rule above — falls back to being
**switched off**. Both are acceptable; they are not the same promise, and
[16d](#16d-every-step-is-reversible-is-false-for-the-increment-that-ships-first) is why the
distinction is written down rather than left to be discovered during a rollback.

**Drain gate.** The same check fires at **two** points, not one
([16b](#16b-the-drain-gate-is-described-here-as-a-deletion-gate-and-the-section-that-depends-on-it-says-otherwise)):

- **at every flow cutover**, before routing that flow to v2 — because an already-published,
  unacked NATS message is still deliverable to a v1 worker no matter what the routing switch says;
- **at deletion**, before a v1 component is removed — its flow fully drained *and* its consumers
  clean.

Both read the same three numbers: the flow is drained, and its NATS consumers report **zero
unprocessed messages and zero outstanding acks**. Verify consumer state with
`nats consumer info --json` — the table output omits `Max Deliver` when it is `-1`, so it cannot
distinguish a capped consumer from an uncapped one.

**What explicitly does not change:** the scraping muscle (Patchright/headed-Chrome stealth per
ADR-008, the Go fetcher, LLM call logic, formatters, robots handling), MinIO result storage for
the v1 lane, Clerk auth, Redis rate limiting, Fernet secret encryption, the cross-tenant = 404
invariant, and the bulk of the existing CRUD test suite.

#### 16a. Steps get names, because the numbers have already moved once

The section said *"a real problem only at **migration step 2**, when jobs move to `JobWorkflow`"*
and then, twenty lines later, listed a sequence in which step 2 was the **worker port** and jobs
were step 4. Both were written in this document; the second is correct.

The cause is mechanical. The §9 review (2026-08-23) reordered the sequence and updated **the list**
without updating **the references into it**, and "step 2" is not prose — three sections use it as
an address:

| who | what they mean | where "step 2" pointed after the reorder |
|---|---|---|
| [§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee), five times | build the lane marker on `job_runs` | the **worker port**, which has no `job_runs` rows to mark |
| [§2d](#2d-capacity) | "both orchestrators running at steps 2–3" | worker port + pipeline lane, not job cutover + batches |
| `temporal-full-migration.md` §9 | its own 7-item list, **not renumbered at all** | its step 2 is still the *rejected* NATS bridge |

⚠️ **The failure is silent and lands on the one obligation that is a schema change.** §7's
mechanism 4 — a lane marker written in the same transaction as the row insert — is required from
the moment one `job_runs` row is visible to both lanes, which is the **job cutover**. An
implementer who builds it "at step 2" builds it during the worker port, where it is inert and
untestable; one who reads §16's list instead concludes step 2 is already done and arrives at the
job cutover believing the marker was handled. Neither produces an error.

✅ **Owner's call: the sequence is named, not numbered, and every cross-reference names a step.**
Names do not renumber when a step is inserted, moved or split, and this sequence has already been
reordered once by a review and will be again. The three references above are corrected in place;
`temporal-full-migration.md` is on the post-review redraw list and inherits the names there.

Also corrected while counting: the section said the shape *"changes at four of the seven steps"*
while listing **nine**. The seven was the migration document's list, quoted from before the
reorder.

#### 16b. The drain gate is described here as a deletion gate, and the section that depends on it says otherwise

This is not a disagreement between two sections; it is one section that was never amended.
[§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee) already
records the requirement, in these words:

> The check that does is already written as §16's **deletion gate** … it is a **cutover gate too**,
> not only a deletion gate, and the two sections should be read together.

§16 said *"A v1 component is deleted when its flow is fully drained and its NATS consumers report
zero unprocessed messages and zero outstanding acks"* — one firing point, at deletion. So the
document that an implementer works from carried the deletion-only version, while the section that
depends on the gate asserted §16 said something it did not.

**What the gate is actually for.** `--retention work` deletes a message once it is **acked**, so
there is no replayable backlog. The risk is the message that is *not yet acked* at the instant a
flow is routed to v2:

```
10:00:00  user submits a job → API writes the job_runs row → publishes to NATS
10:00:01  the message sits on the stream, unacked
10:00:02  ✂  the job flow is routed to v2
10:00:03  Temporal starts JobWorkflow for that run
10:00:05  the v1 NATS worker — still alive, per obligation 3 — consumes the message
          it was already handed, and scrapes

          → the target site is scraped twice
          → the user's own LLM key is billed twice
          → the v1 result returns to result_consumer.py, which resolves the run
            by id and overwrites the state of the run Temporal is still executing
```

**None of §7's four mechanisms reaches this**, and the reasons are worth stating because each one
looks like it should:

| mechanism | why it misses |
|---|---|
| 1 · disjoint identity | operates on **rows**; this is a message already in flight |
| 2 · workflow-ID uniqueness | v1 started no workflow — it consumed a message |
| 3 · `schedule_status` interlock | this is a one-off submission, not a schedule |
| 4 · lane marker on `job_runs` | workers hold **no DB access at all** (ADR-001's light-worker rule, still true in [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)) — a v1 worker cannot read the marker and cannot know it has been sidelined |

Mechanism 4 stops the *dispatcher* re-publishing. Nothing stops a message already handed out. The
only thing that does is draining before flipping.

✅ **Owner's call: the gate fires at every flow cutover as well as at deletion**, stated in the
Drain gate paragraph above rather than left as a cross-reference. ⚠️ **The residual is the
`result_consumer.py` write**, not the wasted scrape: a late v1 result does not merely duplicate
work, it lands on a live v2 run. [§11a](#11a--two-writers-and-the-precedence-rule-is-the-thing-to-remember)'s
precedence rule guards a **cancellation** written by the API and `result_consumer.py:613` guards
`cancelled` specifically; neither refuses a stale v1 result for a run the other lane now owns.
Recorded here rather than solved: with the gate observed the case does not arise, and the lane
marker read by the *consumer* (not the worker) would close it if a belt-and-braces guard is later
wanted.

#### 16c. Obligation 2 borrows a user-facing switch, and one live meter reads it

Obligation 2 pauses a recurring job in v1 before creating its Temporal Schedule. The order is
right — a missed firing is cheaper than a double one — and
[§7](#7-oq-3--one-lane-disjoint-identity-plus-an-engine-level-uniqueness-guarantee) already
records the mirror ordering for rollback. What neither section noticed is **what
`schedule_status` is**.

It is not an internal flag. Q4 made it a deliberately tri-state, user-writable field with its own
endpoint, and two live consumers read it:

```
schemas/jobs.py:114     JobPatch.schedule_status        ← the user can write it
routers/jobs.py:465     generic setattr loop            ← no lane awareness, no guard
admin.py:494            COUNT(*) WHERE schedule_status = 'active'
schemas/admin.py:61     active_recurring_jobs
UsageStats.tsx:67       <Stat label="Recurring jobs" …> ← rendered today
```

**Consequence 1 — a live meter reports the migration as a decline.** As recurring jobs move to
Temporal Schedules, `active_recurring_jobs` counts down toward zero and `next_run` goes null,
while every one of those jobs is firing normally on v2. Nothing errors; the number is well-formed
and wrong. ⚠️ This is the **fourth** instance of one defect in this ADR — batch invisible because
`job_id` is NULL (BUG-005), crawls invisible because every meter reads `job_runs` (P7), webhook
success rate blind to the pipeline lane ([15e](#15e-no-webhook_deliveries-row-for-v2-removes-admin-capability-and-blinds-three-meters)) —
and the first one **caused by an instruction in this ADR** rather than found in existing code.

**Consequence 2 — the interlock is user-reversible in one request.** `PATCH /jobs/{id}` with
`{"schedule_status": "active"}` is a documented, owner-scoped call that succeeds. A user who
believes their schedule was paused in error re-arms **both lanes**, which is the double scrape and
double LLM bill R5 requires be *structurally* prevented rather than avoided by convention.
Obligation 1 gets structural treatment; obligation 2 rests on a switch the user owns.

*(One mitigation already present, and it is luck rather than design: `schedule_status` is absent
from `JobResponse`, so the user cannot read the flag back and has nothing prompting them to flip
it.)*

✅ **Owner's call: both are recorded as migration obligations of the schedule and webhook cutover,
and neither is fixed on the v1 path.** The meter fix is **naming, not features** — the same
resolution as [15e](#15e-no-webhook_deliveries-row-for-v2-removes-admin-capability-and-blinds-three-meters):
the tile counts *v1 recurring jobs*, and a lane-aware count is what the migrated state needs.
The interlock is the sharper one, and the honest statement is that `schedule_status` is doing two
unrelated jobs — *the user's intent* and *which engine owns this schedule* — which is the same
overloading Q8 came from and the same shape as `nats_stream_seq` being "a lane marker in
disguise". ⚠️ **A dormant meter is not a correct one:** there are no scheduled jobs in production
today, so the tile reads 0 before and after and the defect is invisible for exactly the reason
[13d](#13d-ssrf-at-frontier-admission-and-the-sitemap-fetcher-changes-clients)'s was — the feature
is dormant, not safe. Monitors (layer B) is *entirely* about recurrence, so the population this
breaks arrives with it.

#### 16d. "Every step is reversible" is false for the increment that ships first

The section's safety net was: *"a misbehaving flow falls back to the v1 path until fixed."* Its
own opening paragraph says pipelines have **no v1 implementation**. Both cannot hold.

```
Worker port              broken? → delete the Temporal-bound deployment;
                                   the NATS-bound one keeps serving        ✅ v1 fallback

Job cutover              broken? → route jobs back to the v1 path          ✅ v1 fallback

Pipeline lane (R6)       broken? → there is no v1 pipeline.
                                   The fallback is switching the feature off  ❌
```

The section noticed the **favourable** half of "layer A adds a lane" — no old lane means nothing
to double-execute, so the top cutover risk is near zero at the first increment — and then stated a
blanket reversibility promise that the *same fact* makes false. The two halves point opposite ways
and only one was written down:

| | double-execution risk | rollback target |
|---|---|---|
| **adding** a lane (pipelines) | ~none | ~none |
| **moving** a lane (jobs, batches, crawls) | high | the v1 path |

✅ **Owner's call: the claim is narrowed, not dropped** — reversibility is a property of migrated
flows; new flows fall back to being switched off. Recorded because the consequence is real for
planning: the R6 gate is run on a lane with no fallback, which is an argument for §9's standalone
pre-gate (run the Scrape activity alone and diff it against a v1 run of the same URL) being a
requirement rather than a nicety, and it is the same exposure
[13a](#13a-this-is-a-rewrite-not-a-port-and-the-only-lane-with-no-v1-to-compare-against)
identified for crawls, arriving one lane earlier than that section expected.

#### 16e. The sequence begins after three items this ADR moved in front of it

The sequence opens at **engine up**. Three items sit before it and are not on the list:

| | what | why the pipeline lane needs it |
|---|---|---|
| **P6** | BUG-005 — batch broken on all three paths | not a dependency; a live silent defect that the queue puts first |
| **P8** | the shared per-object storage ledger ([§8d](#8d-who-charges-and-what-happens-at-the-wall--settled-2026-08-25)) | **a dependency.** The v2 charging activity writes ledger rows; without the table a pipeline run stores objects and charges nothing |
| **P7** | crawls join the run-counting view | same table, and the view is what lets *any* new lane be counted |

Without the counting view, pipeline runs consume none of the three meters **by construction** —
which is P7's own bug, reproduced on a brand-new lane by a plan that was written to prevent it.

The cause is dating: the sequence was last touched by the §9 review on **2026-08-23**, and
[§8d](#8d-who-charges-and-what-happens-at-the-wall--settled-2026-08-25) moved the ledger to
pre-migration on **2026-08-25** — two days later, in a section that recorded the knock-on as
*"filed as P8 in `phase4-backlog.md` §1"* and did not carry it back into the contract that states
the order of work.

✅ **Owner's call: the pre-migration queue is named as the sequence's entry condition** —
**P6 → P8 → P7 + BUG-007**, then **engine up**. It is not restated here item by item;
`phase4-backlog.md` §1 stays the single source of truth for its contents, and this section owns
only the fact that the sequence does not start until it is empty.

### 17. Relationship to the earlier ADRs

ADR-009 **will** supersede parts of ADR-001, ADR-002, ADR-004, ADR-005 and ADR-006 — but **not
yet**. Those contracts remain authoritative for the flows still served by v1, and marking them
superseded now would mislead anyone maintaining the live system.

That deferral is this ADR's own call, not a rule inherited from anywhere. The ADR index says
**how** a supersession notice is written (status header, plus inline `⚠` markers at the sections
that changed); it says nothing about **when**, because every previous supersession here replaced a
contract that stopped being true the moment the new one was accepted. This one does not: the whole
point of [§16](#16-the-v1v2-coexistence-contract)'s strangler-fig sequence is that v1 keeps serving
real traffic for the entire migration, and a `Superseded` header reads as *do not implement against
this* to the person maintaining the Go worker next month. There is no status value for *"replaced
on the new lane, still binding on the old one"*, so the stamp waits.

**What is superseded, and at which named step.** [§16](#16-the-v1v2-coexistence-contract) named its
sequence rather than numbering it; this table names a step per row for the same reason
([16a](#16a-steps-get-names-because-the-numbers-have-already-moved-once)). "Component deletion" is
not usable as a trigger here — three of these contracts die at a **cutover**, before anything is
deleted ([17c](#17c-when-the-corresponding-v1-component-is-deleted-is-an-address-two-of-these-documents-do-not-have)).

| document | section | verdict | notice added at |
|---|---|---|---|
| **ADR-001** | §2 Subjects · §3 Message Schemas · §8 MinIO paths | already superseded **by ADR-002** in 2026-04-02 — not ADR-009's to supersede ([17a](#17a-the-adr-001-entry-lists-the-sections-adr-002-already-superseded)) | — |
| | §4 Worker Responsibilities | **split.** The retry row and the status-update row are replaced; **"Worker dependencies: NATS + MinIO only. No database access." survives permanently** | NATS removal, as a **partial** notice that names the surviving rule |
| | §5 Acknowledgment Timing | superseded — ack timing is on [§10](#10-oq-6--the-do-not-delete-list)'s correctly-dissolved list | NATS removal |
| | §6 Retry Policy | superseded — and ⚠️ already false of live code ([17e](#17e-two-of-the-contracts-held-authoritative-here-are-already-false-of-live-code)) | NATS removal |
| | §7 Cancellation | superseded — [§15d](#15d-cancellation-now-takes-up-to-26-hours-and-a-cancelled-run-still-delivers) has the API send a cancel **to the workflow**, where §7 says no signal is ever sent | schedule and webhook cutover |
| **ADR-002** | §1 Stream Subject Change · §2 Updated Subjects · §5 Pull Consumer | superseded | NATS removal |
| | §3 Updated Message Schemas | superseded | last flow cutover — **batch and crawl cutover** |
| | §4 MinIO Path Convention | **partially superseded already**, v2 lane only, by [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity) — the live exception | job cutover for the rest |
| | §6 Unchanged from ADR-001 | superseded — and ⚠️ its retry row is already false ([17e](#17e-two-of-the-contracts-held-authoritative-here-are-already-false-of-live-code)) | NATS removal |
| **ADR-004** | all — fat message schema v2 | superseded. ⚠️ It belongs to the **stream**, not to a component, so it stops being used when the last flow stops publishing | **batch and crawl cutover** |
| **ADR-005** | §1 dedicated coordinator process | superseded — [§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block) replaces it with `CrawlWorkflow` | batch and crawl cutover |
| | §2 Postgres `crawl_queue` | ✅ **upheld by name** — the draft's clause that it retires was withdrawn on review | — |
| | §3 `crawls` / `crawl_pages` | ✅ **upheld by name** — `crawl_pages` is *required*, not optional | — |
| | §4 crawl NATS subjects | superseded | NATS removal |
| **ADR-006** | §1 `batches` / `batch_items` · §2 nullable `job_id` + `batch_item_id` | ✅ **upheld** — cited as correct in BUG-005's analysis; P6 changes the artifact path, not the data model | — |
| | §3 Result consumer routing | superseded | consumer deletion |
| | §4 Workers are unchanged | superseded — [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected) gives every worker a Temporal entry point | worker port |

**Not affected.** **ADR-003** (job/run split) — `job_runs` survives as the job lane's own table and
as [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)'s
read-model mirror. **ADR-007** (Fernet secret storage) and **ADR-008** (Patchright/headed-Chrome
stealth) are both on §16's *what explicitly does not change* list; ADR-008 in particular is the
behaviour the transport change must leave untouched.

#### 17a. The ADR-001 entry lists the sections ADR-002 already superseded

The section said ADR-009 will supersede ADR-001 *"§2 subjects, §3 schemas, §8 MinIO paths."* Those
are, word for word, the three sections **ADR-002 superseded on 2026-04-02**. ADR-001's own header
notice says so:

> ADR-002 supersedes §2 (Subjects), §3 (Message Schemas), and §8 (MinIO Path Convention) of this
> document. Sections §4 (Worker Responsibilities), §5 (Acknowledgment Timing), §6 (Retry Policy),
> and §7 (Cancellation) remain authoritative.

So the entry was **a copy of the 2026-04-02 notice, not an assessment of what this ADR does**, and
it fails in both directions at once.

**It points at the wrong document.** Those decisions moved to ADR-002 four months before this ADR
was drafted. If the migration deletes the subject names it deletes **ADR-002 §2**; re-superseding
ADR-001 §2 is a no-op on a section that is already history, and it leaves the live one unstamped.

**It misses every section of ADR-001 that is still authoritative** — which is where the content is.
Two are deleted outright (§5 ack timing, §6 retry), one is **contradicted by name** (§7 says *"No
cancellation signal is sent to NATS or the worker"* and *"the API result consumer is the single
enforcement point"*, while [§15d](#15d-cancellation-now-takes-up-to-26-hours-and-a-cancelled-run-still-delivers)
has the API signal the workflow directly), and §4 is **split**:

```
ADR-001 §4, Worker Responsibilities

  Retry logic                    │ NATS JetStream (via MaxDeliver)   ← deleted
  Update job status in Postgres  │ API (result consumer)             ← rewritten by §11
  Fetch URL / write MinIO / publish result                           ← unchanged

  "Worker dependencies: NATS + MinIO only. No database access."      ← SURVIVES, permanently
```

That last line is the most load-bearing sentence in the entire ADR set for this migration, and
this ADR leans on it three times: [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)
keeps it under the activity-worker port,
[§8d](#8d-who-charges-and-what-happens-at-the-wall--settled-2026-08-25) enforces it *structurally*
through task-queue routing rather than convention, and
[16b](#16b-the-drain-gate-is-described-here-as-a-deletion-gate-and-the-section-that-depends-on-it-says-otherwise)'s
unfixed residual risk **exists because of it** — a v1 worker cannot read the lane marker, because
it cannot read the database at all.

⚠️ **So the section most needing an explicit "this part survives" was the one section §17 did not
name**, and a blanket "ADR-001 is superseded" stamp would assert the opposite of the rule three
other sections depend on.

✅ **Owner's call: the list is rebuilt per section, and §4 gets a partial notice that names the
surviving rule rather than a section-level one.** The table above is that list.

#### 17b. ADR-002 has no §8 — and the wrong address is in six files

The departure recorded in [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)
is real and correctly reasoned. Its address is not: **ADR-002 has six sections, and its MinIO Path
Convention is §4.** §8 was *ADR-001's* number for that decision, and the number was carried along
when ownership moved to ADR-002 in 2026-04-02 — the same document-level slip as
[16a](#16a-steps-get-names-because-the-numbers-have-already-moved-once), one layer up.

The correct spelling exists in the repo and lost:

```
ADR-001:164   "⚠ Superseded by ADR-002 §4."          ← right, written 2026-04-02
ADR-003:65    "See ADR-002 §4 for the full …"        ← right
… and 16 occurrences of "ADR-002 §8" across six files, including this ADR (×4),
  open-bugs.md (×3) — which is the document P6 will be implemented from — README.md,
  phase4-backlog.md and CLAUDE.md
```

✅ **Owner's call: corrected to §4 everywhere it is a live instruction** — this ADR, the ADR index,
`CLAUDE.md`, `open-bugs.md`, `phase4-backlog.md`. The occurrences inside the session handoff's
**archived** session blocks are left as written: they are a record of what past sessions said, and
rewriting them would be editing history rather than fixing an address.

#### 17c. "When the corresponding v1 component is deleted" is an address two of these documents do not have

The trigger assumed each ADR maps to one component whose deletion stamps it. Against §16's named
steps, none of them cleanly does:

| document | when it actually stops being true | is that a component deletion? |
|---|---|---|
| ADR-001 §5 / §6 | per flow, finishing at **NATS removal** | spread across cutovers, not one event |
| **ADR-001 §4's light-worker rule** | **never** | ❌ **there is no event at all** — and a stamp would assert the opposite of what [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected) and [§8d](#8d-who-charges-and-what-happens-at-the-wall--settled-2026-08-25) rely on |
| ADR-002 | subjects and pull consumer at **NATS removal**; the result-consumer clause at **consumer deletion**; the MinIO convention when the v1 lane retires | ❌ three different steps |
| ADR-004 | when the last flow stops publishing — the **batch and crawl cutover** | ❌ it belongs to the stream, not a component, and it dies **before** either deletion step |

§16 had just finished converting addresses into names because the numbers had moved once already;
this section reintroduced an unnamed one twelve lines later.

✅ **Owner's call: every row of the scope table names a step**, and where a contract survives, the
"notice added at" cell is empty rather than deferred to an event that will never arrive.

#### 17d. "For as long as v1 serves traffic" is one global switch on a per-flow migration

After the **job cutover**, ADR-002 is authoritative for batch and crawl and **not** for jobs. The
whole of §16 is per-flow; this deferral was all-or-nothing, so the two documents describe different
migrations in the same breath. It is the lane-blindness this ADR keeps finding —
[15e](#15e-no-webhook_deliveries-row-for-v2-removes-admin-capability-and-blinds-three-meters)'s webhook meters
and [16c](#16c-obligation-2-borrows-a-user-facing-switch-and-one-live-meter-reads-it)'s recurring-jobs
tile are the same shape — with the difference that here it is prose rather than a query, and it
fails by being **read** wrongly rather than by returning a wrong number.

✅ **Owner's call: authority is per flow, and the scope table is what states it.** Same fix as
[17c](#17c-when-the-corresponding-v1-component-is-deleted-is-an-address-two-of-these-documents-do-not-have);
recorded separately because the two were separate mistakes with one repair.

#### 17e. Two of the contracts held authoritative here are already false of live code

This section's stated reason for deferring is that a supersession notice *"would mislead anyone
maintaining the live system."* Two of the contracts it protects already mislead that person:

```
ADR-001 §6   "NATS JetStream handles retries automatically via MaxDeliver …
              No application-level retry loop is needed in the worker."

ADR-002 §6   "NATS-managed retries │ MaxDeliver controls retry count;
              no application-level retry loop"          ← re-affirmed as Unchanged
```

Every worker has had one since the Q5 / UF-003 work:

| worker | where |
|---|---|
| LLM | `llm-worker/worker/worker.py:107` reads `msg.metadata.num_delivered`, caps at `llm_max_delivery_attempts:122`, `msg.nak(delay=…)` at `:128` |
| Playwright | `playwright-worker/worker/worker.py:259`, `:281` — same shape |
| Go http | `http-worker/internal/worker/worker.go:308` `retryDelay(attempt, …)` → `NakWithDelay` at `:316` |

The retry **decision** and the backoff ladder are in worker code; `max_deliver` is only the
consumer-side backstop. This predates ADR-009 and is not caused by it — but §17 is the section that
asserts these documents are currently authoritative, so it is the section that has to qualify the
claim rather than restate it.

⚠️ It also sharpens what the migration inherits: [§10](#10-oq-6--the-do-not-delete-list)'s ported
classifier is not moving retry *into* the workers, it is moving a retry decision that **already
lives there** onto a different engine. The rule *the classifier decides; Temporal retries* is
continuity, not a new hazard.

✅ **Owner's call: recorded here, not fixed on the v1 documents.** ADRs are immutable once accepted
and are not edited to match drifted code — that is the index's first rule. The correction rides
with the supersession notice at NATS removal, and until then this row is the record that the
divergence is known.

#### 17f. ADR-005 and ADR-006 are not mentioned anywhere in this ADR

Zero occurrences in 3,300 lines, while this ADR decides the fate of both lanes they describe.

**ADR-005 (crawl BFS coordinator)** is a textbook partial supersession whose four sections
[§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block) has *already*
decided — including two it **upholds by name**, which is exactly the fact an index reader needs,
since 13b's finding was that the draft wrongly assumed the frontier table retires. Leaving that
unrecorded means the next reader re-derives a withdrawn clause.

**ADR-006 (batch data model)** loses only its result-consumer routing section. Its data model is
cited as **correct** in BUG-005's root cause — *`job_id` is NULL for batch runs (correct, per
ADR-006)* — and P6 changes the artifact path convention, not the tables.

The heading *"Relationship to ADR-001/002/004"* is itself the defect: a reader concludes 003, 005,
006, 007 and 008 are unaffected, and for 005 that is wrong.

✅ **Owner's call: both are brought into scope, and the section is renamed *Relationship to the
earlier ADRs* so the list is not fixed in the heading.** The three genuinely unaffected ADRs are
now stated as unaffected rather than left to inference.

> **✅ Settled on review (2026-09-04).** The deferral stands and is now stated as this ADR's own
> call rather than attributed to an index rule that does not exist. Six corrections, none of them
> to the decision: the ADR-001 entry named the three sections **ADR-002** superseded in 2026-04-02
> and missed all four live ones, including the light-worker rule that **survives** and that §9, §8d
> and 16b all depend on (17a); the recorded §5 departure cited **ADR-002 §8, which is not a section
> that exists** — the convention is §4 — an address wrong in six files (17b); the "component
> deletion" trigger does not resolve for ADR-004 or for ADR-001 §4, so the scope table now **names
> a step per row** (17c) and states authority **per flow** rather than globally (17d); ADR-001 §6
> and ADR-002 §6 are **already false of live code** and are recorded as a known divergence rather
> than protected as accurate (17e); and **ADR-005 and ADR-006 appeared nowhere in this ADR** though
> §13 decides all four of ADR-005's sections — two of them upheld by name — so both are in scope
> and the section is renamed (17f).

---

## Consequences

**Gained**

- Retry, timers, and crash recovery become declarative configuration instead of five hand-rolled
  loops. The Q8 class of bug — an overloaded status value producing an unintended transition —
  cannot occur, because the engine owns the state machine.
- The ack_wait/redelivery class of bug (Q6) disappears with NATS.
- Per-block status and timing, which is R3's largest observability gain over today's opaque
  `job_runs.status`.
- The API becomes stateless and horizontally scalable once the background loops leave it.
- A per-workflow timeline in the Temporal Web UI replaces `grep status=` log-spelunking — a real
  debugging upgrade for exactly the Q8 class of incident.
- Temporal's time-skipping test framework makes long sleeps and monitors testable in
  milliseconds, which is what makes layer B testable at all.

**Paid**

- The heaviest infrastructure dependency in the project: Temporal Server plus its own Postgres,
  on a single-node cluster with no HA.
- **The Temporal database becomes critical in-flight state** and needs backup/restore discipline
  that did not previously exist. Losing it loses running work, not just history.
- **Workflow history retention is a new, ongoing storage cost** that scales with completed runs
  and does not self-clean the way `--retention work` does. [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity)'s
  references-not-payloads rule is what keeps it proportionate; retention is set at **30 days**
  ([§2c](#2c-namespace-retention-is-30-days)) and archival stays off.
- **A second migration mechanism.** Temporal's schema is `temporal-sql-tool`'s, on Temporal's
  release cadence — separate from Alembic, and not applied by anything in the current deploy flow
  ([§2a](#2a-temporal-persistence-is-a-separate-postgres-instance)).
- **Determinism bugs are a new failure class.** Non-deterministic workflow code breaks replay,
  and it fails *after* a restart rather than at the point of the mistake.
- Two SDKs (Go and Python) means a small duplication in activity-worker setup.
- Two orchestration systems run concurrently for the whole coexistence period, but they no longer
  sit on top of each other: with the NATS bridge rejected ([§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)), each unit of work is driven
  by exactly one of them, and the retry-layering hazard is confined to §10's ported classifier
  raising **non-retryable application errors** for its terminal verdicts rather than running a
  retry loop of its own.

**Risk explicitly named**

The end state is clean, and that is precisely what makes jumping to it directly tempting. The
strangler-fig sequence exists to prevent that, and the deletion gate exists to make "we are
finished with v1" a measured fact rather than an assumption.

---

## Deliberately not decided here

| Item | Why deferred, and to what |
|---|---|
| Crawl frontier model (visited-set + `continue-as-new` vs child-workflow-per-page) | Binding constraint is history size; decide against measurements at the crawl migration step ([§13](#13-oq-9--the-crawl-coordinator-migrates-last-and-a-crawl-is-not-a-block)) |
| Temporal **archival** (closed histories → MinIO) | Retention itself is now decided — 30 days ([§2c](#2c-namespace-retention-is-30-days)). Archival stays off: another moving part, for data currently worth little. Revisit if history growth ever forces a *shorter* retention |
| **Web UI ingress exposure** (post-Phase 4) | Not exposed for now — `kubectl port-forward` only ([§2b](#2b-the-web-ui-is-not-ingress-exposed)). If always-on access is wanted later, choose between Traefik `basicAuth` (the mlflow pattern; cheap, one shared credential, no attribution) and `forwardAuth` against an API admin endpoint (one identity system; must be built, couples UI to API availability, depends on Clerk's session cookie domain). Owner's call, 2026-08-08 |
| Namespace-per-tier | Buys nothing at current scale; revisit under noisy-neighbour pressure ([§12](#12-oq-8--tenant-isolation-single-namespace-and-the-api-is-the-only-boundary)) |
| **Mechanism for versioning our *workflow code*** (Worker Versioning vs `patched`) | Distinct from §6's pinning of *user definitions*, which is decided. Not open-ended: **Worker Versioning is GA and Temporal's stated default**; patching is the older approach leaving branches to clean up; the pre-2025 experimental variant is already withdrawn from the server. Decide at the first workflow-code deploy that must survive in-flight runs — it needs **server-side enablement** on the self-hosted deployment ([§2](#2-topology)), so it is an infra task, not a code flag ([§6](#6-oq-2--in-flight-edits-definitions-are-pinned-and-that-is-a-different-problem-from-code-versioning)) |
| Conditional execution's design | Its own layer-A PRD, before PRD-018 ([§14](#14-oq-10-remaining-half--conditional-execution-gets-its-own-layer-a-prd-before-monitors)) |
| **Data-flow fan-out** (one block's output consumed by two later blocks — "two extractions on one fetched page") | **Wanted, deferred to post-Phase 4. Owner's call, 2026-08-10.** Layer A validates data flow as a path ([§4](#4-oq-1b--block-model-fixed-typed-catalog-json-in-postgres-explicit-named-wiring)). The stored shape is already a graph, so this is a validator change against unchanged definitions. **Cheaper than it looked, after the §8 review (2026-08-17):** it was thought to make §8's "final artifact" ambiguous and to force the multi-Scrape rider — neither now applies. §8 charges every stored object, so there is no "final artifact" for fan-out to make ambiguous, and the rider is closed on structural grounds with **multiple roots, not fan-out**, named as the trigger that would reopen it. A fanning graph still has one Scrape and still costs one run. Distinct from *parallel* fan-out, which stays a PRD-016 non-goal |
| Run-failure notification (R6's fourth exclusion) | The PM left it unassigned on purpose: it is either an on-failure branch or a run-level setting, and that depends on the conditional-execution decision above |
| Whether `webhook_deliveries` / `crawl_pages` survive as v1-only audit mirrors | Decide at the step that retires each flow, not now |
| **The headroom buffer's size**, and **per-page ceiling checks for crawls** | The wall policy is decided ([§8d](#8d-who-charges-and-what-happens-at-the-wall--settled-2026-08-25)): a run that crosses the ceiling finishes and is charged, and a buffer refuses to *start* new runs near the limit. The **number** is an operator dial and wants real pipeline data. Separately, the buffer only holds while the most a single admitted run can add is smaller than the buffer — true for jobs and pipelines, **false for crawls**, where one submission is up to `max_pages` fetches (2.8–40 GB at BUG-003's measured page range). Crawls need the ceiling checked **per page as they go**; the mechanism belongs to P7's implementation, not here |
| Retention window for intermediate block outputs — **the number only** | Needs a real number from the first pipelines ([§8](#8-oq-4--metering-one-run-is-one-unit-pools-are-shared-and-storage-is-charged-for-what-is-stored)). **No longer a free dial** (§5 review, 2026-08-10): it is chosen against a stated promise, since it bounds how long R3's "what did this step return" works. The rules around it — result never collected, collection per run not per block, collected renders as *collected* — are decided. **The trade-off changed direction in the §8 review (2026-08-17):** intermediates are now charged while they exist, so the window is no longer operator-storage vs debuggability but **the user's bill vs debuggability**, and collection is a visible refund. It also becomes a *correctness* deadline once Monitors ship: per-run collection is unsafe as soon as an object is shared between runs |

---

## Implementation reference

- **PRD-016** — the product spec this design serves. R6 is the acceptance gate: reproduce
  `scrape → LLM → webhook` as a pipeline before designing any block outside R2's catalog.
- **`temporal-full-migration.md`** — component-by-component change inventory and the
  strangler-fig sequence.
- **`phase4-backlog.md`** §3 — bugs the migration dissolves; do not design fixes for them.
  **§1 P6 / BUG-005** — the batch identity failure that grounds [§3](#3-oq-1a--run-identity-pipeline-runs-get-their-own-table-and-quota-counting-stops-naming-a-table)
  and [§5](#5-oq-1c--blocks-pass-references-artifacts-are-keyed-on-run-identity).
- **`open-questions.md` Q5–Q8** — the incidents behind [§9](#9-oq-5--workers-become-temporal-activity-workers-directly-the-nats-bridge-is-rejected)'s
  retry-layering warning and [§10](#10-oq-6--the-do-not-delete-list)'s port list.
- **ADR-008** — the scraping behaviour that must survive the transport change untouched.
