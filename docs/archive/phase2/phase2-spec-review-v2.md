# Phase 2 Engineering Spec v2 — Architect Review

**Reviewer:** Senior Software Architect
**Spec reviewed:** `phase2-engineering-spec-v2.md`
**Review date:** 2026-04-02
**Verdict:** **Substantially improved — v2 correctly addresses all 23 issues from the v1 review.** Four new defects were introduced during the fixes: two are silent logic bugs (P1) and two are implementation pitfalls (P2/P3). These must be resolved before implementation begins.

---

## Original Issues — Status

All 23 issues from the v1 review are addressed:

| # | Original Issue | v2 Status |
|---|----------------|-----------|
| 1 | `asyncio.TaskGroup` hangs FastAPI | ✅ Fixed — `create_task()` + explicit shutdown in §6.4 |
| 2 | Webhook secret race condition | ✅ Fixed — secret generated before NATS publish in §5.3 |
| 3 | Cancellation enforcement unspecified | ✅ Fixed — `DELETE /jobs/{id}` defined in §5.5, result consumer guard in §7 |
| 4 | `nats_stream_seq` column missing | ✅ Fixed — migration 2.8 added |
| 5 | Diff on raw HTML for LLM jobs | ✅ Fixed — diff deferred to post-LLM handler in §7 |
| 6 | Go worker AckWait timeout under load | ✅ Fixed — fetch-only-available-slots pattern in §4.1 |
| 7 | Scheduler split-brain | ✅ Fixed — DB commit before NATS publish, stale-pending recovery in §6.1 |
| 8 | Scrape vs LLM result indistinguishable | ✅ Fixed — `job_run.status` discriminator documented in §7 |
| 9 | MaxDeliver advisory misses `processing` | ✅ Fixed — `'processing'` added to status filter in §6.3 |
| 10 | LLM worker no timeout | ✅ Fixed — `httpx.Timeout` at transport layer in §4.3 |
| 11 | Double webhook POST per change | ✅ Fixed — Option A (single event + diff embedded) in §7 |
| 12 | `down -v` destroys all volumes | ✅ Fixed — idempotent `stream edit \|\| stream add` in §3.1 + §8.1 |
| 13 | `cleanup_old_runs.py` undefined | ✅ Fixed — full script defined in §8.3 |
| 14 | `result_path` ambiguity (`latest/` vs `history/`) | ✅ Fixed — `history/` path explicitly specified in §2.3 and §3.3 |
| 15 | `DELETE /jobs/{id}` unspecified for Phase 2 | ✅ Fixed — §5.5 updated |
| 16 | `PATCH /jobs/{id}` scope underspecified | ✅ Fixed — mutable/immutable field table in §5.6 |
| 17 | `call_anthropic()` undefined | ✅ Fixed — full tool-use implementation in §4.3 |
| 18 | No LLM content truncation | ✅ Fixed — `LLM_MAX_CONTENT_CHARS` truncation in §4.3 |
| 19 | SSRF not re-checked at webhook send | ✅ Acknowledged as known gap, deferred to Phase 3 in §6.2 |
| 20 | Redundant BRIN + B-tree index | ✅ Fixed — BRIN removed in §2.3 |
| 21 | `job_runs.status` no CHECK constraint | ✅ Fixed — CHECK constraint added in §2.3 |
| 22 | `wait_strategy` not validated at API boundary | ✅ Fixed — `WaitStrategy` enum in §5.3 |
| 23 | Migration workflow inverted | ✅ Fixed — autogenerate workflow documented in §2 |

---

## New Defects in v2

### Issue A — P1: `cleanup_old_runs.py` — `continue` doesn't exclude the run from DB deletion

**Location:** §8.3

The MinIO failure handler uses `continue` to skip to the next loop iteration, but `run_ids` is built from the full batch **after** the loop completes. The DB delete runs against all IDs regardless:

```python
for run in runs:
    if run.result_path and run.result_path.startswith("history/"):
        try:
            await minio.remove_object(BUCKET, run.result_path)
        except Exception:
            log.exception("MinIO delete failed — skipping DB delete for this run", ...)
            continue  # ← exits the for-loop iteration, but does NOT remove run from run_ids

# run_ids is built from ALL runs — the failed MinIO deletes are still included
run_ids = [r.id for r in runs]
await db.execute("DELETE FROM webhook_deliveries WHERE run_id = ANY(:ids)", {"ids": run_ids})
await db.execute("DELETE FROM job_runs WHERE id = ANY(:ids) ...", {"ids": run_ids})
```

The comment says "leave DB row intact — will retry next night" but the code deletes it anyway. The MinIO object is now orphaned with no reference in the DB.

**Fix:** Build the IDs list inside the loop, appending only on successful MinIO delete:

```python
successful_ids = []
for run in runs:
    if run.result_path and run.result_path.startswith("history/"):
        try:
            await minio.remove_object(BUCKET, run.result_path)
        except Exception:
            log.exception("MinIO delete failed — skipping DB delete for this run", ...)
            continue  # now correctly skips appending to successful_ids
    successful_ids.append(run.id)

run_ids = successful_ids  # only successfully-cleaned runs
await db.execute("DELETE FROM webhook_deliveries WHERE run_id = ANY(:ids)", {"ids": run_ids})
await db.execute("DELETE FROM job_runs WHERE id = ANY(:ids) AND created_at < :cutoff",
                 {"ids": run_ids, "cutoff": cutoff})
```

---

### Issue B — P1: Result consumer — missing `return` after `llm_key is None`

**Location:** §7, "On `status: completed` from a scrape worker"

When `llm_key is None`, the code sets `status='failed'` but has no `return`. Execution falls through to the diff computation and webhook creation steps:

```python
if job.llm_config:
    await db.execute(UPDATE job_runs SET status='processing' WHERE id = run_id)
    llm_key = await db.get(UserLLMKey, job.llm_config['llm_key_id'])
    if llm_key is None:
        await db.execute(UPDATE job_runs SET status='failed',
                          error='LLM key not found or deleted' WHERE id = run_id)
        # ← MISSING return — falls through to steps 3 and 4
    else:
        await js.publish(...)
        return  # only the else-branch returns

# step 3: compute text diff on a 'failed' run  ← wrong
# step 4: create webhook delivery with event="job.completed" for a failed run  ← wrong
```

**Fix:** Add `return` after the failure path, and fire a `job.failed` webhook if applicable:

```python
if llm_key is None:
    await db.execute(UPDATE job_runs SET status='failed',
                      error='LLM key not found or deleted' WHERE id = run_id)
    if job.webhook_url:
        await create_webhook_delivery(db, job, run_id, event="job.failed", minio_path=None)
    return  # ← required
```

---

### Issue C — P2: `transaction = False` in §2.6 is not a valid Alembic construct

**Location:** §2.6

The spec shows:

```python
# Cannot run inside a transaction — set at module level, not inside upgrade()
transaction = False

def upgrade() -> None:
    op.execute("ALTER TYPE jobstatus ADD VALUE 'processing' AFTER 'running'")
```

Alembic migration files do not support a module-level `transaction` variable. This attribute is silently ignored — Alembic will wrap the migration in a transaction as normal, and `ALTER TYPE ADD VALUE` inside a transaction will raise:

```
ProgrammingError: ALTER TYPE ... cannot run inside a transaction block
```

The `env.py` note (`transaction_per_migration = False`) is a real Alembic option, but it is a **global** flag in `context.configure()` — it cannot be set per-migration from the migration file itself.

**Fix:** Use the COMMIT/BEGIN trick inside `upgrade()`, which works with all Alembic versions including the asyncpg setup this project uses:

```python
import sqlalchemy as sa

def upgrade() -> None:
    # ALTER TYPE ADD VALUE must run outside a transaction in PostgreSQL.
    # Explicitly end the transaction Alembic opened, execute, then reopen.
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'processing' AFTER 'running'"))
    op.execute(sa.text("BEGIN"))

def downgrade() -> None:
    pass  # Postgres has no ALTER TYPE DROP VALUE
```

Remove the `transaction = False` module-level line and the `env.py` instruction — they don't work and will mislead the implementing engineer.

---

### Issue D — P3: `BACKOFF_SECONDS` hardcoded to 5 entries; `WEBHOOK_MAX_ATTEMPTS` is configurable

**Location:** §6.2

```python
BACKOFF_SECONDS = [0, 30, 300, 1800, 7200]  # attempts 1–5
...
next_attempt = datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[attempts])
```

With the default `WEBHOOK_MAX_ATTEMPTS = 5`, this works — `attempts` reaches at most 4 before the `exhausted` branch fires. But if an operator sets `WEBHOOK_MAX_ATTEMPTS = 8`, then on the 6th failure `BACKOFF_SECONDS[5]` raises `IndexError` at runtime. There is no documentation pairing these two settings.

**Fix — either cap the index or validate at startup:**

Option A (cap the index):
```python
backoff_idx = min(attempts, len(BACKOFF_SECONDS) - 1)
next_attempt = datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[backoff_idx])
```

Option B (assert at startup):
```python
assert settings.webhook_max_attempts <= len(BACKOFF_SECONDS), \
    "WEBHOOK_MAX_ATTEMPTS exceeds BACKOFF_SECONDS table length"
```

---

## Minor Observations (No Spec Change Required)

- **§4.1 and §4.2 worker code examples omit `nats_stream_seq`** from the `status: "running"` publish call. §3.3 documents it correctly — add a callout in each worker section explicitly pointing to §3.3 so engineers don't miss it during implementation.

- **§8.1 nats-init shell operator precedence** — `(stream_info && stream_edit) || stream_add` runs `stream_add` if `stream_edit` fails for any reason, not just "stream doesn't exist." If `stream_edit` is unsupported and fails, `stream_add` also fails (stream already exists). Consider `if/else` shell logic instead of `&&/||` for robustness.

---

## Pre-Implementation Checklist

- [ ] Fix `cleanup_old_runs.py` — build `successful_ids` from MinIO successes only, not all batch rows (Issue A)
- [ ] Add `return` after `llm_key is None` failure path in result consumer; fire `job.failed` webhook (Issue B)
- [ ] Replace `transaction = False` in §2.6 migration with `COMMIT`/`BEGIN` trick inside `upgrade()` (Issue C)
- [ ] Cap `BACKOFF_SECONDS` index access or validate `WEBHOOK_MAX_ATTEMPTS <= len(BACKOFF_SECONDS)` (Issue D)
