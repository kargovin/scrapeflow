# ScrapeFlow — Open Questions

> Items raised during implementation that need a decision before code is written.
> Each entry includes the context, the options, and a recommendation so the discussion starts with something concrete.

---

## Q1 — Should `(user_id, name)` be unique on `api_keys`?

**Raised during:** Phase 1 — `POST /api-keys` implementation
**File:** `api/app/routers/users.py`, `api/app/models/api_key.py`

### Context

`POST /api-keys` creates a new named API key every time it is called. A user can currently create two keys both named `"my key"` — there is no uniqueness enforcement on the name within a user's keyspace.

The model already supports multiple named keys per user (GitHub-style): `name`, `revoked`, and `last_used_at` fields all point to this intent. The question is whether the `name` field should also be unique within a user's keys.

### Options

| Option | Behaviour | Trade-off |
|--------|-----------|-----------|
| **A — unique `(user_id, name)`** | DB constraint prevents duplicate names per user; `POST` returns `409` if name already exists | Better UX, simpler key management, requires catching `IntegrityError` in route handler |
| **B — allow duplicate names** | Users can have multiple keys with the same name | Confusing — two keys called "CI" with no way to distinguish them |
| **C — unique, but soft** | Enforce uniqueness only on non-revoked keys | Allows reuse of names after revocation; more complex constraint logic |

### Recommendation

**Option A.** The `name` field only carries value if it uniquely identifies a key. A DB-level `UniqueConstraint("user_id", "name")` is the right enforcement point — it's race-safe and gives a clear error. Route handler catches `IntegrityError` and returns `409 Conflict`.

If there is a use case for reusing names after revocation, revisit with Option C, but that should be a deliberate call.

### What needs to happen

- Add `UniqueConstraint("user_id", "name", name="uq_api_keys_user_name")` to `ApiKey.__table_args__`
- New Alembic migration
- `POST /api-keys` catches `IntegrityError` → `409 Conflict`
- Test: duplicate name returns 409, different name succeeds

---

## Q2 — `jobs.updated_at` exists but is never updated

**Raised during:** Phase 2 Step 5 — reviewing `job.py` model additions
**File:** `api/app/models/job.py`

### Context

`updated_at` was added to the `jobs` model in Phase 1 with `onupdate=lambda: datetime.now(UTC)`. SQLAlchemy's `onupdate` fires when a column value is changed via the ORM. `result_consumer.py` does mutate job fields (`job.status`, `job.result_path`, `job.error`) so `onupdate` fires there. The open question is whether all other mutation paths (cancel route, Phase 2 status transitions) also touch a field, or if some updates bypass ORM assignment and go through `db.execute(update(...))` — in which case `onupdate` would silently not fire.

### Options

| Option | Behaviour |
|--------|-----------|
| **A — remove it** | Drop the column; no misleading stale data |
| **B — keep, wire it up** | Ensure every route that mutates a job (cancel, result consumer updates) sets at least one field so `onupdate` fires, or explicitly assign `job.updated_at` |
| **C — DB trigger** | Let Postgres maintain it — more reliable than ORM-level `onupdate` |

### Recommendation

**Option B** if the column is useful for the admin panel or change detection. **Option A** if it's never queried — dead columns are a maintenance burden. Decide before Step 12 (the irreversible migration) so it can be cleaned up in the same window if needed.

---

## Q3 — `jobs.webhook_url` column type should be `Text`

**Raised during:** Phase 2 Step 10 — testing webhook URL creation
**File:** `api/app/models/job.py`

### Context

`jobs.webhook_url` is declared as `Mapped[str | None] = mapped_column(nullable=True)` with no explicit SA column type. SQLAlchemy infers `String` (unbounded `VARCHAR`) from the Python type annotation. The `url` column on the same model uses `Text` explicitly. URLs can be arbitrarily long and `Text` is the correct Postgres type for unbounded string storage — consistent with `url`, `webhook_url`, `error`, and `result_path` on the same table.

### What needs to happen

- Change `jobs.webhook_url` column type to `Text` in `api/app/models/job.py`
- New Alembic migration: `ALTER TABLE jobs ALTER COLUMN webhook_url TYPE TEXT`
- Low risk — `VARCHAR` and `TEXT` are functionally equivalent in Postgres; this is a type annotation cleanup

---

## Q4 — `DELETE /jobs/{id}` does not disable scheduled jobs

**Raised during:** Phase 2 Step 11 — implementing Phase 2 cancellation

**File:** `api/app/routers/jobs.py`

### Context

`DELETE /jobs/{id}` now cancels active `job_runs` rows (status = pending/running/processing). This stops the current scrape, but it does not prevent the scheduler (Step 20) from firing a new `job_run` at the next cron tick for a scheduled job. After Step 12 drops `jobs.status`, there is no job-level flag to mark a scheduled job as disabled.

### Options

| Option | Behaviour |
|--------|-----------|
| **A — `is_active` flag on `jobs`** | `DELETE /jobs/{id}` sets `is_active = False`; scheduler skips jobs where `is_active = False` |
| **B — Separate disable endpoint** | `DELETE` only cancels the active run; a new `PATCH /jobs/{id}` with `{"is_active": false}` disables future scheduling |
| **C — DELETE means hard delete** | `DELETE /jobs/{id}` removes the job row (CASCADE deletes runs); no disable concept needed |

### Recommendation

Decide before Step 20 (scheduler loop). **Option A** is simplest — a boolean flag is enough for the MVP. **Option B** is more RESTful but adds a route. **Option C** is clean but loses history.

---

## Q5 — LLM worker timeout vs scale-to-zero cold starts

**Raised during:** Phase 4 — evaluating self-hosted LLM cloud providers (2026-05-15)
**Files:** `llm-worker/worker/config.py`, `llm-worker/worker/llm.py`, `llm-worker/worker/worker.py`

### Context

Self-hosted LLM endpoints with scale-to-zero pricing have a cold-start window of **90–120 seconds** for the first request after idle. The current LLM worker can't survive this:

1. `Settings.llm_request_timeout_seconds` defaults to **60s** (`config.py:21`), used as the httpx `read` timeout in `_make_timeout()` (`llm.py:21-27`). A 90–120s cold start raises `httpx.ReadTimeout` before the response arrives.
2. The exception falls into the catch-all at `worker.py:100-113`. The worker publishes `status="failed"` and **acks the message** — see the comment at line 111: *"Re-delivery won't recover a bad LLM key or a missing MinIO object."*
3. The job is permanently `failed`. NATS never redelivers. The user sees a timeout error in the UI.

The ack-on-failure choice is correct for **poison-pill failures** (bad encrypted key, schema mismatch, missing MinIO object) — retrying those just burns money. But it conflates them with **transient failures** (cold start, 5xx, rate limit) that *would* recover on retry.

The naïve assumption that "NATS will retry on timeout" is incorrect here — the worker preempts NATS by acking on every exception path.

### Options

| Option | Behaviour | Trade-off |
|--------|-----------|-----------|
| **A — Just bump the timeouts** | Raise `LLM_REQUEST_TIMEOUT_SECONDS` to 180; raise consumer `ack_wait` to ~200s (see Q6) | Simplest; one worker slot is held for the full cold-start duration whether it succeeds fast or not; a truly hung worker takes 3+ min to redeliver |
| **B — Classify exceptions; nak on transient errors** | Catch `httpx.TimeoutException`, `httpx.ConnectError`, 429, 5xx separately → don't publish `failed`, don't ack → NATS redelivers up to MaxDeliver; auth/validation errors still ack+fail | Closest to "what should happen"; needs care to avoid double-publishing `failed` (which the API I-2 terminal guard would lock in, blocking the legitimate retry's `completed`) |
| **C — Pre-warm probe** | Cheap `/health` GET before the real call; on wake-detected, extend timeout *just for this request* | Costs ~100-200ms per warm call; turns cold-start into an expected long wait, not an error |
| **D — Keepalive cron** | Separate process pings the endpoint every ~5 min to prevent scale-down | Defeats the scale-to-zero cost savings during business hours; a "partial-zero" schedule (e.g. only 2am-6am) preserves most savings |

### Recommendation

**A + B together** as the baseline. (A) gives an honest worst-case timeout that fits cold starts. (B) makes NATS the retry mechanism for transient failures, which is what queue-based workers are for. (C) is a good follow-up once (A+B) is in place. (D) is an operational decision, orthogonal to worker code.

Also worth thinking about: with `llm_max_workers=3`, three near-simultaneous cold-start jobs would block the worker entirely for 2 minutes. Consider raising the concurrency cap — it's async tasks, not threads, so cost is low.

### What needs to happen

- Decide on (A) vs (A+B) — affects whether the worker needs an exception-classification layer
- Coordinate with **Q6** — bumping httpx timeouts is meaningless if `ack_wait` is still 30s
- If choosing (B), document the contract: which exception types are transient vs terminal, and ensure the result publish is gated on the *final* outcome (after retries are exhausted) so the API I-2 guard doesn't lock in a premature `failed`
- Decide whether to raise `LLM_MAX_WORKERS` to compensate for cold-start slot occupancy

### Possible solution (not final)

A **pre-warm health-check probe** in front of the LLM call:

- New helper, e.g. `ensure_ready()`, that polls the provider's health endpoint **before** dispatching the real LLM call
- Two-tier timeout: a **short per-request** timeout (e.g. ~5s) on each probe + a **long overall** timeout (e.g. ~180s) on the polling loop itself. Each individual probe fails fast; the loop keeps retrying until the endpoint signals ready or the overall budget is exhausted
- Only after `ensure_ready()` returns success do we run the LLM call — at that point the endpoint is warm and the normal httpx read timeout (60s) should be sufficient
- On overall-timeout exhaustion → treat as a terminal failure (real outage, not cold start)

Open concerns to think through before committing:

- **Health endpoint availability is provider-specific.** Anthropic and OpenAI don't expose a public health endpoint, so this technique only helps for self-hosted / cloud-operator providers (Modal, Replicate, RunPod, vLLM, etc.) — which is exactly the scale-to-zero scenario this is solving for, so that may be fine. But the `provider` field on `JobMessage` would need to gate whether `ensure_ready()` is called.
- **Race window between probe and real call.** A scale-to-zero endpoint could scale back down between a successful probe and the actual LLM call. The probe doesn't *reserve* capacity. Mitigated by the fact that real calls happen within milliseconds of a successful probe.
- **Cost on the warm path.** Adds one extra round-trip per job even when the endpoint is hot. Could be avoided with an in-memory "last-seen-healthy" timestamp that skips the probe if the endpoint was warm in the last N seconds.
- **Pairs with Q6's heartbeat.** Without `msg.in_progress()` (Q6), the polling loop itself would exceed `ack_wait` and trigger duplicate delivery — so this solution only works if Q6 is solved first.

---

## Q6 — NATS `ack_wait` is not configured on the LLM worker consumer

**Raised during:** Phase 4 — investigating Q5 cold-start handling (2026-05-15)
**File:** `llm-worker/worker/main.py`

### Context

`main.py:68-72` creates the pull subscription with only `durable` and `stream` arguments:

```python
psub = await js.pull_subscribe(
    LLM_SUBJECT,
    durable=DURABLE_NAME,
    stream=STREAM_NAME,
)
```

No `ack_wait` is passed, so the JetStream consumer is created with the **default of 30 seconds**. The worker also never calls `msg.in_progress()` to extend the lease during long-running calls.

This means: if an LLM call legitimately takes longer than 30 seconds (which a warm Anthropic or OpenAI call regularly can, never mind cold start), NATS thinks the consumer died and **silently redelivers the message to another instance — or to the same one on its next fetch**. Both deliveries then execute the full pipeline:

- Both decrypt the user's API key and call the LLM provider — the user is charged twice
- Both upload to MinIO — `history/{job_id}/{ts}.json` paths differ by timestamp, so neither is lost, but storage doubles
- Both publish `completed` results — the API-side I-2 terminal guard from the production review (`docs/archive/phase3/production-review.md`) blocks the second from corrupting state, but the *work* has already been done and paid for

This bug is **latent today** — it's silent unless someone correlates LLM provider billing against ScrapeFlow job counts. It becomes impossible to ignore once Q5's cold-start scenario is in play.

### Options

| Option | Behaviour | Trade-off |
|--------|-----------|-----------|
| **A — Static `ack_wait`** | Pass `config=ConsumerConfig(ack_wait=…)` (or equivalent) when creating the durable; pick a value comfortably above the worst-case LLM call duration | Simple; tradeoff is that a *truly* hung worker takes the full `ack_wait` to redeliver, which delays user-visible recovery |
| **B — Heartbeat with `msg.in_progress()`** | Keep `ack_wait` short (e.g. 30-60s); spawn a background task per message that calls `msg.in_progress()` every ~10s while work is in flight | Decouples ack_wait from LLM call duration; a stuck worker stops sending heartbeats and NATS redelivers quickly; more code |
| **C — Both** | Static `ack_wait` of ~60s as a floor, plus heartbeats for long calls | Belt-and-braces; matches how production systems usually handle this |

### Recommendation

**Option C.** A short static `ack_wait` keeps recovery fast for genuinely-dead workers; heartbeats handle the case where a single LLM call legitimately needs minutes. The pattern generalises — same logic will apply when Phase 4 considers other long-running operations.

Note: the existing durable consumer was created in production with the default 30s `ack_wait`. Changing the static value requires either deleting and recreating the consumer, or using JetStream's `update_consumer` API. This is operational work, not just a code change.

### What needs to happen

- Decide whether the existing 30s `ack_wait` is already causing duplicate work in production (worth a quick audit of LLM provider invoices vs run counts before fixing)
- Decide on Option A, B, or C
- Coordinate with **Q5** — these two questions resolve together; fixing one without the other leaves a half-broken pipeline
- If recreating the consumer, plan the cutover (drain old consumer, recreate, restart worker) — durable consumers are stateful infra

### Possible solution (not final)

Spawn a **per-message heartbeat task** that keeps the JetStream lease alive while work is in flight:

- When `handle_message` starts, kick off a background `asyncio.Task` that calls `await msg.in_progress()` on a fixed cadence (e.g. every 10s — comfortably below any reasonable `ack_wait`)
- Cancel the heartbeat task **in every exit path** — success, exception, timeout — before the function returns. A `try/finally` or context manager around the body is the safest pattern; a leaked heartbeat task would keep the message alive indefinitely
- Keep a short static `ack_wait` (e.g. 60s) as a floor — if the worker crashes hard, heartbeats stop, NATS notices within `ack_wait`, redelivery happens promptly

Open concerns to think through before committing:

- **Heartbeat cadence vs `ack_wait`.** Cadence must be meaningfully smaller than `ack_wait` so a single missed heartbeat doesn't trigger redelivery. ~10s cadence + ~60s `ack_wait` gives a 6x safety margin.
- **Cancellation reliability.** Python asyncio task cancellation is cooperative — if the heartbeat task is awaiting `in_progress()` when cancelled, that's fine; but the surrounding logic must `await task` (or `task.cancel(); await asyncio.gather(task, return_exceptions=True)`) to ensure it actually exited before `handle_message` returns.
- **Pairs with Q5's polling loop.** This heartbeat is what lets Q5's `ensure_ready()` polling loop run for ~180s without NATS giving up on the message. Without heartbeats, the polling loop just shifts the duplicate-work bug from "long LLM call" to "long warmup poll".
- **Existing consumer state.** The current durable consumer was created with default `ack_wait`. Setting a new static floor requires updating or recreating the consumer — operational work, not just a code change.

---

## Q7 — LLM worker has no retry on transient call failures

**Raised during:** Phase 4 — discussion of Modal vLLM instance death failure mode (2026-05-18)
**Files:** `llm-worker/worker/llm.py`, `llm-worker/worker/worker.py`

### Context

When the LLM call in `worker.py:73-80` raises any exception, the worker publishes `status="failed"` and acks (`worker.py:100-113`). There is no retry. This means a single transient failure — TCP reset because the Modal container died mid-stream, a 503 from a self-hosted endpoint under brief load, a connection timeout — produces a permanently failed job.

The worker is **not retry-naked** — both SDK constructors at `llm.py:36` and `llm.py:55-59` rely on the Anthropic/OpenAI SDK default of `max_retries=2`, which covers 429 and most 5xx automatically. But those retries:

- Happen **inside** the httpx `read` timeout — if collective retry+backoff time exceeds the timeout, the worker sees a single timeout exception with no signal that retries occurred
- Don't reliably cover **connection errors** (the exact failure mode of instance death mid-call) in older SDK versions
- Behave unevenly when the OpenAI SDK is pointed at non-OpenAI endpoints (like Modal vLLM) — error-shape detection isn't guaranteed for self-hosted backends

So the practical position today is: **we have a little retry, in places we can't see, that we don't control, that doesn't cover the failure mode we care about most** (instance death + cold-start follow-on).

### Three layers of possible retry

| Layer | Where it lives | Granularity | Cost of one retry |
|---|---|---|---|
| **SDK** | Inside `AsyncAnthropic` / `AsyncOpenAI` clients | Single HTTP round-trip | Cheap — same connection, same warm endpoint |
| **Worker** | Loop around `call_llm` in `worker.py` | The full LLM call | Cheap-ish — same worker process, same MinIO-read content, possible `ensure_ready` re-probe |
| **NATS** | Q5 Option B — classify exception, nak instead of ack | The full message lifecycle | Expensive — fresh worker, fresh decrypt, fresh MinIO read, possible cold start if Modal scaled down between attempts |

Each layer covers a different failure granularity:
- SDK retry: flaky network round-trip, transient server hiccup
- Worker retry: the LLM call as a whole failed but the worker process is fine
- NATS retry: the worker itself died or hit a state it can't recover from

A healthy retry strategy uses **all three layers in concert**, with each layer's budget bounded so they compose to a sane total — not a multiplicative storm.

### Options

| Option | Behaviour | Trade-off |
|--------|-----------|-----------|
| **A — SDK retries only (status quo)** | Rely on Anthropic/OpenAI SDK defaults; no worker-level retry | Zero code change; doesn't cover instance death or cold-start follow-on; invisible to logs |
| **B — SDK + Worker retry** | Explicitly configure SDK `max_retries`; wrap `call_llm` in a small in-worker retry loop with exception classification | Catches the cases SDK doesn't; same warm endpoint; doesn't help if the worker itself dies |
| **C — SDK + Worker + NATS (full stack)** | All of B, plus Q5 Option B for the final fallback | Most resilient; risks retry-amplification if budgets aren't bounded; requires Q5+Q6 to be solved first |
| **D — Worker only, no SDK retry** | Set SDK `max_retries=0`; do everything in the worker | Single source of retry logic — easier to reason about, log, and bound; loses "free" coverage for 429 backoff timing |

### Recommendation

**Option B** for the immediate next iteration, with **Option C** as a documented next step once Q5 and Q6 are landed. (Option D is appealing for observability but probably overkill for a portfolio project — the SDK retries are well-tuned and cheap.)

The reason to prefer in-worker retry over jumping straight to NATS-level retry: the endpoint is *warm right now* after a successful `ensure_ready()` probe (Q5). An in-worker retry hits the warm endpoint immediately. A NATS redelivery happens after `ack_wait` expires, during which Modal might have scaled back to zero — paying cold-start *again*. NATS retry is for "the worker died", not "the call failed once".

### Critical correctness concerns

- **Exception classification is the whole game.** Misclassifying a poison-pill failure as transient creates retry storms charged to the user. The standard table for LLM providers:
  - **Retry**: `httpx.ReadTimeout`, `httpx.ConnectError`, `httpx.RemoteProtocolError`, HTTP 429, HTTP 5xx
  - **Don't retry**: 401/403 (key issue), 400/422 (schema/prompt issue), `json.JSONDecodeError` (model confusion — same garbage twice), schema-validation failures
- **Idempotency**: LLM calls are not idempotent. If the first request reached the server, did inference, and the response was lost in transit, retrying produces a second billed inference. For Modal vLLM (user-controlled, GPU-seconds billing), the cost is bounded. For Anthropic/OpenAI, retries cost real money — bound the budget tightly (2-3 max).
- **Premature `failed` publish**: today the worker publishes `failed` *before* acking. If Q5 Option B is ever adopted and the publish-failed-then-nak pattern is reused, the API I-2 terminal guard will lock the run as failed before the retry's `completed` can land. Whatever retry layer is added must defer the result publish until the *final* attempt fails.
- **Retry budget vs `ack_wait`**: total time spent on retries + backoffs must fit within Q6's `ack_wait` + heartbeat budget. SDK retries (~30s tail) + worker retries (~60s tail) + cold-start probe (~120s tail) is a lot of time to keep a message in-flight. The numbers need to add up.

### What needs to happen

- Decide on Option A, B, C, or D — and which exception types each layer is responsible for
- Build the **exception classification table** explicitly; review it against actual production failure logs (any LLM failures since deploy) to make sure no class is mis-categorised
- Set explicit `max_retries` on both SDK constructors (don't leave it implicit)
- If choosing B or C, decide:
  - Worker-level retry count (recommend 2)
  - Backoff curve (exponential with jitter is standard)
  - Whether to call `ensure_ready()` again between worker-level retries (recommend yes — single-instance Modal could have died and re-cold-started between attempts)
- Confirm `LLM_REQUEST_TIMEOUT_SECONDS` and `ack_wait` (Q5+Q6) are large enough to accommodate the chosen retry budget; if not, revisit those values

### Possible solution (not final)

Layer-1 + layer-2 retry, classified by exception type:

- **SDK layer**: pass `max_retries=2` explicitly to both `AsyncAnthropic` and `AsyncOpenAI` constructors. Make it configurable via `Settings`. This makes the existing implicit behaviour visible and tunable.
- **Worker layer**: wrap the `call_llm` invocation in a retry loop bounded to ~2 attempts. Catch a *whitelist* of transient exception types; re-raise immediately on anything else (auth errors, schema errors, JSON decode errors). Between attempts: short jittered backoff (e.g. 1s + jitter), then call `ensure_ready()` again before the next attempt — instance might have died.
- **Result publishing**: only publish `failed` after the *final* worker-level attempt fails. The intermediate failures stay in logs, not in the result stream.

Open concerns to think through before committing:

- **Classification table is provider-specific.** The list of retry-worthy exception types differs between Anthropic SDK, OpenAI SDK, and Modal-fronted vLLM responses. Need to test each combination, ideally with a fault-injection harness.
- **Worker slot held for the whole retry budget.** With `LLM_MAX_WORKERS=3`, a 3-attempt retry budget on each of 3 jobs means all workers are blocked simultaneously. Consider raising `LLM_MAX_WORKERS` if retry budgets are large (async tasks, not threads — cost is low).
- **Pairs with Q5 and Q6.** Retry budgets are bounded by `ack_wait` + heartbeat. Adopting Q7 without Q6 risks every retry-extended call being silently redelivered to another worker mid-retry, doubling cost.
- **Observability.** Today retry failures are invisible (they happen inside the SDK and produce a single exception at the worker boundary). A worker-level retry loop is the right place to emit structured logs per attempt — "attempt 1 failed with ReadTimeout, retrying after 1.3s" — which is what oncall needs when failure rates spike.

---

## Q8 — `job_runs.status` values are overloaded across pipeline stages

**Raised during:** post-Phase-3 usage — traced an infinite-loop bug in `result_consumer.py`
**File:** `api/app/core/result_consumer.py`, `api/app/models/job_run.py`

### Context

The `job_runs.status` column takes the values `pending`, `running`, `processing`, `completed`, `failed`, `cancelled`. In the current design these values represent both **which stage** a run is in and **what that stage is doing**:

| Status | Meaning (as designed) |
|--------|-----------------------|
| `pending` | Enqueued, no worker has picked it up |
| `running` | Scrape worker is actively working |
| `processing` | Scrape done, LLM stage is in flight |
| `completed` | Terminal — LLM stage (or scrape, if no LLM) succeeded |
| `failed` | Terminal — any stage failed |
| `cancelled` | Terminal — user cancelled |

Both the scrape worker and the LLM worker publish a `running` `ResultMessage` when they start (ADR-002 §3). The value string `"running"` is the same on both — the caller has to consult the `source` field to know which stage's `running` this is. The `completed` string is similarly overloaded.

This overloading has already caused a production bug: the API's regular and batch result handlers had un-source-guarded `if worker_status == "running": run.status = "running"` branches. When the LLM stage's `running` arrived, it clobbered `run.status = "processing"` back to `"running"`. The next `completed` from the LLM worker then matched the scrape-completed branch (`worker_status == "completed" and run.status == "running"`), which re-dispatched a new message to `scrapeflow.jobs.llm` — closing a tight feedback loop that burned ~200 LLM API calls in 5 minutes before the worker was stopped.

The immediate fix (applied) added `if source == "scrape":` guards on the `running` transition in both `_handle_job_result` and `_handle_batch_result`. That stops the current loop, but leaves the state-value overloading in place — future stages (webhook delivery, post-processing, notification) would multiply the same class of bug and require the same class of guard on every branch.

### Options

| Option | Behaviour | Trade-off |
|--------|-----------|-----------|
| **A — leave as-is with source guards (current)** | Every branch that reads `run.status` also reads `source`. Every new stage adds a discriminator check to every existing branch | Minimal code churn. Fragile — one missed source-check anywhere reopens the same class of bug. Doesn't scale beyond 2 stages |
| **B — distinct status values per stage** | `pending` → `scrape_running` → `llm_pending` → `llm_running` → `completed`. Each transition matches exactly one `(worker_status, source, current_status)` triple; the state machine is total | Migration for existing rows. Frontend, WebSocket status stream, admin views, and `/jobs/{id}` response all need to map new values back to a user-facing bucket. Larger diff |
| **C — two rows per pipeline** | Separate `job_runs` for the scrape stage and the LLM stage, linked by parent-run FK. Each row's `status` only describes its own stage | Deepest refactor. Aligns with how Temporal/Airflow/Prefect model task-level vs workflow-level state. Adds a query cost — `GET /jobs/{id}` has to aggregate across rows. Breaks the "one run = one row" mental model that the result-path stall detection and I-2 terminal guard were built around |

### Recommendation

**Option B** as a Phase 4 refactor. It's the smallest change that eliminates the *class* of bug entirely: a total state machine has no ambiguous transitions, so no branch can ever accidentally match the "wrong" stage's message. Option C is the direction production systems eventually converge to, but it's overkill for the current pipeline depth (2 stages) and would touch too many surfaces at once.

Explicit stage-in-value naming also improves observability — `docker compose logs api | grep status=llm_running` becomes precise instead of ambiguous.

Rejected — Option A: the fix works today but the invariant it depends on ("every branch that reads `run.status` also reads `source`, forever, in every code path anyone ever adds") is not something the type system enforces and not something reviewers will reliably catch. The Phase 3 I-1 discriminator was added specifically because this class of bug was foreseen; missing the `running` transition despite that awareness is the empirical proof that source-checks are not a sufficient defence.

Rejected — Option C: worth revisiting *only if* Phase 4 adds a third or fourth stage (post-processing, notification, aggregation). At two stages the row-per-stage cost outweighs the benefit.

### Dependencies and interactions

- **Pairs with Q5, Q6, Q7.** Any state-machine refactor should land *after* the ack_wait/heartbeat and retry work — otherwise the retry layer would need to be rebuilt against the new state values immediately after being built against the old ones.
- **I-2 terminal guard.** `if run.status in ("completed", "failed"): return` continues to work under Option B — those two terminal values do not need to be stage-specific. The guard only needs to widen if Option B ever adds intermediate terminal states.
- **Frontend impact.** The WebSocket `job_status` pg_notify payload publishes `run.status` verbatim. Option B needs a status-to-display-bucket mapping in the frontend, or a compatibility layer in the notify payload.
- **Historical data.** Migration must set a sensible status for in-flight rows at cutover — `running` maps to `scrape_running`, `processing` maps to `llm_running` — otherwise stall detection and admin views break during rollout.

### What needs to happen

- Decide A vs B vs C
- If B: enumerate new status values, write the transition table, update `_handle_job_result` and `_handle_batch_result` to a total state machine (every `(worker_status, source, current_status)` triple maps to exactly one branch or an explicit ignore)
- Alembic migration: rename existing values (`running` → `scrape_running`, `processing` → `llm_running`) with backfill
- Update frontend status display, WebSocket payload documentation, admin view filters
- Add a *catch-all* `else: logger.warning("unhandled_state_transition", ...)` at the bottom of both handlers — a defense-in-depth measure that would have surfaced the current bug in Phase 3 testing
- Test the state machine with property-based tests: for every `(worker_status, source, current_status)` triple, assert the resulting `run.status` is deterministic and monotonic (never regresses)

### Possible solution (not final)

State-value naming proposal for Option B:

- `pending` → unchanged (pre-scrape queued)
- `scrape_running` (was `running`)
- `scrape_completed_awaiting_llm` (was `processing`) — long name, but unambiguous
- `llm_running`
- `completed`, `failed`, `cancelled` — unchanged (terminal, stage-agnostic)

Transition table:

| Current | worker_status | source | → New |
|---------|---------------|--------|-------|
| pending | running | scrape | scrape_running |
| scrape_running | completed | scrape | (branch A: no `llm_config` → completed) / (branch B: `llm_config` → scrape_completed_awaiting_llm + dispatch LLM) |
| scrape_completed_awaiting_llm | running | llm | llm_running |
| llm_running | completed | llm | completed |
| any non-terminal | failed | any | failed |

Any other triple is a warning-and-ignore.

---
