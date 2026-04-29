# Phase 3 — Production Review

> **Purpose:** Pre-ship readiness audit combining post-Phase-3 self-review (original `Phase3_PRODTODO.md`) with a full architectural, security, and quality analysis of the entire codebase.
> **Date:** 2026-04-24
> **Branch:** `develop`
> **Reviewer:** Tech Lead audit pass

Status legend: `[ ]` open · `[~]` partial · `[x]` done

---

## Severity Key

| Level | Meaning |
|-------|---------|
| **CRITICAL** | Data loss, security breach, or permanent corruption |
| **HIGH** | Production stability, silent data integrity failure |
| **MEDIUM** | Correctness gap, poor resilience, or noticeable UX breakage |
| **LOW** | Code quality, tech debt, missing polish |

---

## Part 1 — NEW findings from full codebase audit (2026-04-24)

These items were **not** in `Phase3_PRODTODO.md`. Numbered from 35 onward to avoid collision.

---

### Security

---

#### [x] 35 — `authorized_parties=None` in both JWT paths — **CRITICAL**

- **File:** `api/app/auth/jwt.py:39`, `api/app/auth/dependencies.py:77`
- **Issue:** Both `verify_request` (REST path) and `auth_from_token` (WebSocket path) pass `authorized_parties=None` to the Clerk SDK. A JWT issued by Clerk for a **completely different application** that shares the same Clerk instance will be accepted. A TODO comment at `jwt.py:35` acknowledges this but was never converted to config.
- **Fix:** Add `CLERK_AUTHORIZED_PARTIES` env var (comma-separated list); load in `settings.py`; pass as `authorized_parties=[settings.clerk_authorized_parties]` in both auth paths. Minimum value for production: `["https://scrapeflow.govindappa.com"]`.

---

#### [x] 36 — `execute_js` Playwright action executes arbitrary user JavaScript — **HIGH**

- **File:** `playwright-worker/worker/actions.py:87`
- **Issue:** `page.evaluate(action["script"])` runs any JavaScript string that arrives in the NATS message payload. The CSP header injected at step 6 of `worker.py` restricts `connect-src` only — image `src`, form `action`, `fetch` via `no-cors`, and WebSocket connections to the same origin are still available as exfiltration channels. A malicious user who controls the script field can exfiltrate page content to an attacker-controlled domain.
- **Context:** The script field is validated to have `type == "execute_js"` at the API layer (`schemas/jobs.py:86`), but the script *content* is never inspected. Any authenticated user who creates a Playwright job with actions can run arbitrary JS inside the worker's browser.
- **Fix (short term):** Fixed in `worker.py` — swapped `page.set_extra_http_headers` (which sets *request* headers, a no-op for CSP) for `page.route("**", _inject_csp)` which injects CSP into the *response* that Chromium actually enforces. Directives now cover `connect-src`, `img-src`, `form-action`, and `frame-src`. Non-document routes call `route.fallback()` to pass through to the image-abort handler. **Fix (long term):** Deferred to `docs/project/PHASE3_DEFERRED.md` — options are drop `execute_js`, sandbox it in an isolated context, or replace freeform scripts with a parameterised allowlist.

---

#### [x] 37 — `_validate_no_ssrf` imported as internal symbol — **MEDIUM**

- **File:** `api/app/core/webhook_loop.py:23`
- **Issue:** `from app.core.security import _validate_no_ssrf` imports a private (underscore-prefixed) function, bypassing the public `validate_no_ssrf` adapter. This tightly couples the webhook loop to the module's internal API and would be silently broken if `_validate_no_ssrf` were renamed or refactored.
- **Fix:** Add a dedicated `validate_no_ssrf_internal(url: str) -> None` public function (raises `ValueError`, no HTTP context) in `security.py`; remove the underscore export.

---

#### [x] 38 — Default proxy URL transmitted as plaintext in NATS messages — **MEDIUM**

- **File:** `api/app/core/scheduler.py:127`, `api/app/routers/jobs.py:288`
- **Issue:** `settings.default_proxy_url` is injected into the NATS fat-message `credentials.proxy_url` field as a plaintext string. Any party with NATS read access (e.g., a compromised worker container) can read the platform-level proxy credentials. Per-job proxy secrets in `job_secrets` are encrypted at rest but are *decrypted before being placed in the NATS message*.
- **Fix:** Introduced a dedicated `CREDENTIALS_ENCRYPTION_KEY` (separate from `LLM_KEY_ENCRYPTION_KEY`) using Fernet symmetric encryption. Per-job secrets (`job_secrets` rows) are forwarded as ciphertext directly — no decrypt/re-encrypt round-trip. `default_proxy_url` is encrypted at dispatch time. Field renamed `credentials.proxy_url` → `credentials.encrypted_proxy_url` (and `cookies` → `encrypted_cookies`) in the NATS schema; workers decrypt before use. `docker-compose.yml` updated to inject the key via `env_file: ../.env` on playwright-worker and http-worker.
- **⚠ K8s TODO (before production deploy):** Add `CREDENTIALS_ENCRYPTION_KEY` to the k8s Secret and to the `env` array of the API, playwright-worker, and http-worker Deployments in `govindappa-k8s-config`. Generate the key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and store it in the sealed secret alongside `LLM_KEY_ENCRYPTION_KEY`.

---

#### [x] 39 — No rate limiting on WebSocket endpoints — **MEDIUM**

- **File:** `api/app/routers/jobs.py:697`, `api/app/routers/batch.py:226`
- **Issue:** `GET /jobs/{id}/watch` and `GET /batch/{id}/watch` require auth but skip `check_rate_limit`. An authenticated user can open thousands of concurrent WebSocket connections, exhausting file descriptors and asyncpg listener queues.
- **Fix:** Per-user concurrent connection cap in `JobNotifier`. `subscribe_job` and `subscribe_batch` now accept `user_id: str`; a shared `_user_connection_counts` dict tracks open connections across both channels. On subscribe, if `count >= settings.ws_max_connections_per_user` (default 25) a `WebSocketConnectionLimitExceeded` exception is raised — caught by both WS handlers and closed with code 4029. Counter decrements in `finally` so it's exact even under abrupt disconnects. 2 new tests — 231 WS tests passing.

---

### Architecture & Design

---

#### [x] 40 — Coordinator hardcodes NATS subjects — **HIGH**

- **File:** `coordinator/coordinator/bfs.py:23-24`, `coordinator/coordinator/result_handler.py:32`
- **Issue:** NATS subject strings are copy-pasted as literals (`"scrapeflow.jobs.run.http"`, `"scrapeflow.jobs.run.playwright"`, `"scrapeflow.jobs.result"`) rather than imported from a shared constants file. The API's authoritative source is `api/app/constants.py`. If a subject name ever changes, the coordinator silently breaks with no compile-time error.
- **Fix:** Extract a shared `coordinator/coordinator/constants.py` mirroring the relevant NATS subjects from `api/app/constants.py`. Import from there in `bfs.py` and `result_handler.py`.

---

#### [x] 41 — `result_consumer.py` is a 506-line orchestration monolith — **MEDIUM**

- **File:** `api/app/core/result_consumer.py`
- **Issue:** One file owns: storage quota enforcement, content deduplication, LLM dispatch, text/JSON diff, batch routing, pg_notify emission, webhook scheduling, and MinIO object lifecycle. Any bug in one concern is surrounded by other concerns — hard to test in isolation, hard to reason about. Six private async functions share the same `db` session passed by reference, with unclear ownership of commit.
- **Fix:** No immediate refactor needed (it works), but flag for Phase 4. At minimum, extract `_handle_storage_quota_exceeded` into `quota.py` and consolidate the MinIO deletion pattern into a single utility.

---

### Performance

---

#### [ ] 42 — `_build_operational_stats` runs 6+ sequential DB queries — **MEDIUM** *(deferred to Phase 4)*

- **File:** `api/app/routers/admin.py:348-450`
- **Issue:** `admin_get_stats` executes 6 sequential `SELECT` queries (jobs_running, jobs_pending, jobs_by_engine, wh_pending, wh_exhausted, active_recurring, next_run) plus 3–7 more in `_build_historical_stats`. All are independent and could run concurrently.
- **Fix:** Collapse scalar counts into a single CTE query (one round trip); `asyncio.gather()` with separate sessions for the grouped queries (`engine_stmt`, `status_stmt`, `top_stmt`). Admin-only endpoint — pool pressure is acceptable.

---

#### [ ] 43 — Content hash recomputed by re-reading freshly-written MinIO object — **LOW** *(deferred to Phase 4)*

- **File:** `api/app/core/result_consumer.py:44-53`
- **Issue:** `_compute_content_hash` fetches the object from MinIO that the worker *just wrote*. This is an extra network round trip (read after write). The worker already has the bytes in memory at upload time.
- **Fix (long term):** Have workers compute and publish the hash in the result message (it's a one-line `xxhash.xxh64` call). The result consumer can then skip the MinIO re-read for dedup. This is a schema_version 3 change to the worker contract.

---

#### [x] 44 — `JobNotifier` subscriber queues are unbounded — **LOW**

- **File:** `api/app/core/job_notifier.py:25-26`
- **Issue:** `asyncio.Queue()` is created with no `maxsize`. A slow WebSocket consumer (or a runaway scheduled job publishing rapid-fire `pg_notify` events) can grow the queue unboundedly, causing API memory growth under load.
- **Fix:** `asyncio.Queue(maxsize=100)`. Catch `asyncio.QueueFull` in `_on_job_notify`/`_on_batch_notify` and drop/log; the WebSocket handler can timeout and reconnect.

---

### Code Quality

---

#### [x] 45 — `validate_cron_min_interval` uses naive (non-UTC) datetime — **MEDIUM**

- **File:** `api/app/routers/jobs.py:101`
- **Issue:** `datetime.now()` (no timezone) is passed as the croniter base. The rest of the codebase uses `datetime.now(UTC)` consistently. On servers in a non-UTC timezone (or near DST transitions), the minimum-interval validation can silently pass for a schedule that would fail in UTC.
- **Fix:** Replace `datetime.now()` with `datetime.now(UTC)` on line 101.

---

#### [x] 46 — `import os` placed at module bottom — **LOW**

- **File:** `api/app/main.py:155`
- **Issue:** `import os` appears after all other code, violating PEP 8 E402. Ruff's `E402` is in the ignore list (`E501`) but `E402` is not, so this should have been caught — possibly because it's inside an `if` guard. Makes the import order unpredictable.
- **Fix:** Move `import os` to the top of the file with other stdlib imports.

---

#### [x] 47 — `email.ilike` search has no index — **LOW**

- **File:** `api/app/routers/admin.py:100`
- **Issue:** `User.email.ilike(f"%{email}%")` uses a leading wildcard, preventing B-tree index usage. On a large user table this is a sequential scan.
- **Fix:** Migration 3.17 — `CREATE EXTENSION IF NOT EXISTS pg_trgm` + GIN index `idx_users_email_trgm ON users USING gin (email gin_trgm_ops)`. Index declared in `User.__table_args__` to keep model and DB in sync.

---

#### [x] 48 — WebSocket handlers have no connection timeout — **MEDIUM**

- **File:** `api/app/routers/jobs.py:741-763`, `api/app/routers/batch.py:260-281`
- **Issue:** Both WebSocket handlers block indefinitely waiting on `queue.get()`. If a job is permanently stuck in a non-terminal state (NATS down after `running` is published but before the result arrives), the WebSocket connection never closes. Under load, this can exhaust server file descriptors.
- **Fix:** Wrap `queue.get()` in `asyncio.wait_for(queue.get(), timeout=300)`. On `asyncio.TimeoutError`, send a `{"type": "timeout"}` message and close.

---

### Error Handling & Resilience

---

#### [x] 49 — `_dispatch_batch` in coordinator has no FOR UPDATE SKIP LOCKED — **HIGH**

- **File:** `coordinator/coordinator/bfs.py:111-117`
- **Issue:** The BFS dispatch query selects `pending` items with `ORDER BY created_at LIMIT batch_size` but has no `FOR UPDATE SKIP LOCKED`. If multiple coordinator replicas are deployed (or the coordinator restarts while items are selected), two replicas can select the same items concurrently, dispatching duplicate NATS messages for the same crawl page.
- **Fix:** Add `.with_for_update(skip_locked=True)` to the pending items query, matching the same pattern used by `scheduler_loop` and `webhook_delivery_loop`.

---

#### [x] 50 — `_check_completion` runs inside `_dispatch_batch` after-commit but in a new session without FOR UPDATE — **MEDIUM**

- **File:** `coordinator/coordinator/bfs.py:183-190`
- **Issue:** After committing the dispatch transaction, a second session opens to check crawl completion. Between the two sessions, another dispatcher could add new queue items. The completion check could fire prematurely, marking the crawl `completed` while items are still in-flight.
- **Fix:** Add a small delay and re-check, or move completion detection entirely into `result_handler_loop` when the last queue item is marked terminal.

---

#### [x] 51 — `result_handler_loop` does not handle coordinator restart mid-crawl cleanly — **MEDIUM**

- **File:** `coordinator/coordinator/result_handler.py:146-192`
- **Issue:** On coordinator restart, `reenqueue_stalled` resets `dispatched` items to `pending`. But `running` items (where the worker published a "running" result) are not reset — they remain in `running` status in `crawl_queue`. If the worker then publishes "completed", the coordinator's `result_handler` updates `crawl_queue` via `CrawlQueueItem.crawl_page_id == crawl_page_id`, but if the `crawl_page_id` was already reset by `reenqueue_stalled`, the update hits zero rows and the completion is silently lost.
- **Fix:** Include `running` items (not just `dispatched`) in `reenqueue_stalled`'s reset condition, or treat them differently.

---

#### [x] 52 — `_process_crawl_result` is not idempotent on NATS redelivery — **MEDIUM**

- **File:** `coordinator/coordinator/result_handler.py`
- **Issue:** If the coordinator commits a terminal result to the DB but crashes before calling `msg.ack()`, NATS JetStream redelivers the message. `_process_crawl_result` runs again for the same `crawl_page_id` — `crawl.total_completed` (or `total_failed`) is incremented a second time, corrupting the crawl counters that surface in `GET /crawls/{id}`.
- **Fix:** Added an early-return guard after the `"running"` branch: if `page.status` is already `"completed"` or `"failed"`, return immediately. The `"running"` branch is safe to re-apply (idempotent by nature). 2 new tests — 48 coordinator tests passing.

---

## Part 2 — Original Phase3_PRODTODO findings (preserved and re-triaged)

Items are renumbered into the global list. Original item number shown in parentheses.

---

### CRITICAL — Merge blocker

---

#### [x] 1 (orig #20) — Batch counter race condition in `_handle_batch_result` — **CRITICAL**

- **File:** `api/app/core/result_consumer.py:140,177,186`
- **Issue:** `batch.completed += 1` / `batch.failed += 1` read-modify-write in Python. Concurrent item completions both read `completed=N`, both write `N+1`. Batch can permanently freeze in `running`, completion webhook never fires.
- **Fix:** Replace with `UPDATE batches SET completed = completed + 1 WHERE id = :id RETURNING completed, failed, total` (or `SELECT FOR UPDATE` to lock the row).

---

#### [x] 2 (orig #21) — `zip(strict=False)` silently truncates batch dispatch — **CRITICAL**

- **File:** `api/app/routers/batch.py:89,100`
- **Issue:** If `items` and `runs` diverge (construction bug, flush partial failure), `zip(strict=False)` silently processes fewer items. A batch of 10 URLs could dispatch 7 with no error.
- **Fix:** Change both `zip(items, runs, strict=False)` to `zip(items, runs, strict=True)`.

---

#### [x] 3 (orig #22) — Batch webhooks always unsigned (HMAC over empty key) — **CRITICAL**

- **File:** `api/app/core/webhook_loop.py:111-114`
- **Issue:** `job = await db.get(Job, delivery.job_id)` — batch deliveries have `job_id=None`, so `job` is `None`, `secret_bytes = b""`, HMAC is computed over an empty key. Every batch webhook receiver that verifies signatures will reject.
- **Fix:** Add `webhook_secret` column to `Batch` model. In `create_batch_webhook_delivery`, generate and store an encrypted secret. In `_attempt_delivery`, fetch from `Batch` when `delivery.batch_id` is set.

---

#### [x] 4 (orig #3) — Alembic auto-migration still commented out — **CRITICAL**

- **File:** `api/app/main.py:42-47`
- **Issue:** The auto-migration block is commented out. Deploying `develop → main` without re-enabling it means Phase 3 migrations will not run automatically in production. Manual migration would be required before every deploy.
- **Fix:** Uncomment the migration block. Phase 3 migrations are all finalised.

---

#### [x] 5 (orig #17) — Dedup deletes the `history/` object that `result_path` points to — **CRITICAL**

- **File:** `api/app/core/result_consumer.py:241-248`
- **Issue:** When content hash matches a previous run, the consumer deletes the new `history/` object to save space — but that path was already written into `job_runs.result_path` for this run. Any future `GET /jobs/{id}/result` returns MinIO 404.
- **Fix (option A):** On dedup hit, set `run.result_path = prev.result_path` (point to the previous run's canonical copy) and delete the new object. **Fix (option B):** Delete the `latest/` overwrite copy instead (cheaper; `latest/` is volatile by design).

---

### HIGH — Fix before next production deploy

---

#### [x] 6 (orig #14) — Batch jobs with LLM output silently return raw HTML — **HIGH**

- **File:** `api/app/core/result_consumer.py` — `_handle_batch_result`
- **Issue:** `_handle_batch_result` never checks `batch.llm_config` or the item's output format. A batch created with `output_format=json` + LLM config silently stores raw HTML results.
- **Fix:** After `worker_status == "completed"`, mirror the LLM dispatch logic from `_handle_scrape_completed` — look up the batch's LLM config and dispatch to the LLM subject if present.

---

#### [x] 7 (orig #15) — `decrement_storage_bytes` never called on delete — **HIGH**

- **File:** `api/app/core/quota.py:192-206` — zero call sites outside this file
- **Issue:** Every job/run delete (`DELETE /jobs/{id}`, admin hard-delete, `cleanup_old_runs.py`) removes MinIO objects without decrementing `user_quotas.storage_bytes_used`. Storage quota grows unboundedly even as actual data shrinks. Note: `cleanup_old_runs.py` already has inline decrement SQL — that path is covered. The gap is `cancel_job` (permanent delete) and `admin_delete_or_cancel_job` (hard_delete).
- **Fix:** Both paths now stat before delete and call `decrement_storage_bytes` on success. Admin hard-delete also gained MinIO cleanup (previously left orphaned objects). 2 new tests in `test_quota.py` — 226 passing.

---

#### [x] 8 (orig #29) — PATCH job upserts secrets before all validations pass — **HIGH**

- **File:** `api/app/routers/jobs.py:495-547`
- **Issue:** `proxy_url` and `cookies` secret rows are upserted to `job_secrets` (lines 496–547) before the `processing` check (line 584). If a later validation fails (e.g., job is currently processing), the secrets are committed but job fields are not updated — inconsistent DB state.
- **Fix:** Move all `job_secrets` writes after the `processing` guard and all validations, or wrap the entire handler in a single transaction with explicit rollback.

---

#### [x] 9 (orig #30) — `increment_storage_bytes` not atomic with run status update — **HIGH**

- **File:** `api/app/core/result_consumer.py:272,291,323,463`
- **Issue:** Storage quota increment and run status write are separate SQL statements in the same session. If the increment fails (DB error, constraint violation), the run is still committed as `completed`. Quota accounting is permanently wrong for that run.
- **Fix:** Added `_try_increment_storage()` helper using `begin_nested()` (PostgreSQL SAVEPOINT) — increment failure rolls back only the savepoint, outer transaction stays clean. All 6 call sites now explicitly mark the run `failed` with `error="storage_accounting_failed"` and ack the NATS message on failure (no indefinite redelivery). Batch paths also update the `failed` counter instead of `completed`.

---

#### [x] 10 (orig #24) — MinIO object orphaned + quota leaks when LLM key deleted mid-schedule — **HIGH**

- **File:** `api/app/core/result_consumer.py:259-268`
- **Issue:** When a scheduled job result arrives and the LLM key has since been deleted, the consumer marks the run `failed` but leaves the already-uploaded MinIO object (and its `storage_bytes_used` increment) in place.
- **Fix:** `_handle_scrape_completed` and `_handle_batch_result` now both call `minio.remove_object()` on the already-written path before marking the run `failed`. Storage is never incremented for objects that are immediately deleted. `_handle_batch_result` gained a `minio: Minio` parameter.

---

#### [x] 11 (orig #25) — Admin user delete leaks MinIO objects — **HIGH**

- **File:** `api/app/routers/admin.py:183`
- **Issue:** `db.delete(user)` cascades through Postgres FKs but MinIO objects are never touched. A deleted user's entire scrape history stays in object storage with no decremented quota accounting.
- **Fix:** `admin_delete_user` now accepts `minio: Minio = Depends(get_minio)`, queries all `job_runs.result_path` owned by the user (both job and batch paths via `COALESCE(Job.user_id, Batch.user_id)`), removes each object, decrements `storage_bytes_used` by total freed bytes, then cascades the DB delete.

---

#### [x] 12 (orig #8) — `cancelled` status never emits `pg_notify` from the router — **HIGH**

- **File:** `api/app/routers/jobs.py:430-437`, `api/app/routers/admin.py:283-287`
- **Issue:** The `cancel_job` route sets `run.status = "cancelled"` and commits but issues no `pg_notify`. WebSocket subscribers waiting on `queue.get()` never receive the terminal status.
- **Note:** `result_consumer.py` does emit `pg_notify` when it discards a cancelled run result, so the WebSocket *eventually* gets notified when the worker's result arrives. But if the worker result never arrives (e.g., NATS is down), the WebSocket hangs forever.
- **Fix:** Add `await db.execute(text("SELECT pg_notify('job_status', :p)"), {"p": f"{run.job_id}:{run.id}:cancelled"})` after the cancel commit in both routes.

---

#### [x] 13 (orig #49) — Coordinator `_dispatch_batch` has no `FOR UPDATE SKIP LOCKED` — **HIGH**

- **File:** `coordinator/coordinator/bfs.py:111-117`
- *(See item #49 in Part 1 — combined here for completeness)*

---

### MEDIUM — Fix before Phase 4 kickoff

---

#### [x] 14 (orig #23) — Crawl page status filter accepts any string — **MEDIUM**

- **File:** `api/app/routers/crawls.py:115-116`
- **Issue:** `page_status` is used directly in `.where(CrawlPage.status == page_status)` with no whitelist check. A typo like `?status=typo` returns an empty 200 instead of 422, silently masking client bugs.
- **Fix:** Add `if page_status not in {None, "pending", "processing", "completed", "failed"}: raise HTTPException(422, ...)` before the `if page_status` block.

---

#### [x] 15 (orig #31) — Coordinator fires crawl completion webhook with no retry — **MEDIUM**

- **File:** `coordinator/coordinator/bfs.py:30-52`
- **Issue:** `_fire_crawl_webhook` is a direct HTTP POST — fire-and-forget with no retry. A transient 5xx or network error loses the event forever. All other webhook types use `WebhookDelivery` + exponential backoff.
- **Fix:** Migration 3.15 adds `webhook_deliveries.crawl_id` FK, makes `run_id` nullable, drops old CHECK `num_nonnulls(job_id, batch_id) = 1` and replaces with two constraints: `num_nonnulls(job_id, batch_id, crawl_id) = 1` and `num_nonnulls(run_id, crawl_id) = 1`. `_fire_crawl_webhook` replaced with synchronous `_enqueue_crawl_webhook(db, crawl)` that inserts a `WebhookDelivery` row in the same transaction as the crawl completion status update. `WebhookDelivery` model added to `coordinator/coordinator/models.py`. 2 new tests in `test_bfs.py` — 46 coordinator tests passing.

---

#### [x] 16 (orig #2) — WebSocket not wired into Admin SPA — **MEDIUM**

- **File:** `frontend/src/pages/JobDetail.tsx`, `frontend/src/pages/Jobs.tsx`
- **Issue:** The Admin SPA was built without using the `/jobs/{id}/watch` or `/batch/{id}/watch` WebSocket endpoints. Job and batch status in the UI is static until page refresh.
- **Fix:** Wire `useEffect` + WebSocket client in `JobDetail.tsx` to subscribe to live status updates. Close the socket on component unmount.

---

#### [x] 17 (orig #26) — `_resolve_credentials` copy-pasted in two modules — **MEDIUM**

- **File:** `api/app/routers/jobs.py:71`, `api/app/core/scheduler.py:32`
- **Issue:** Identical function bodies. Adding a new secret type requires editing both files; they will inevitably diverge.
- **Fix:** Extract to `app.core.credentials` (no router imports); import from both.

---

#### [x] 18 (orig #27) — Stale-pending recovery threshold hardcoded — **MEDIUM**

- **File:** `api/app/core/scheduler.py:170`
- **Issue:** `timedelta(minutes=10)` is hardcoded. Playwright + LLM jobs can legitimately take several minutes; 10-minute threshold may trigger spurious re-publishes.
- **Fix:** Move to `settings.stale_pending_threshold_minutes` with a default of 10.

---

#### [x] 19 (orig #28) — Admin stats `list_objects` has no timeout or iteration cap — **MEDIUM**

- **File:** `api/app/routers/admin.py:581`
- **Issue:** The cold-cache MinIO enumeration iterates all objects with no timeout. A slow or unresponsive MinIO will block the admin stats endpoint indefinitely.
- **Fix:** Wrap in `asyncio.wait_for(..., timeout=10.0)`; on `TimeoutError`, return `minio_bytes=0` and log a warning.

---

#### [x] 20 (orig #12) — `batches.status` / `batch_items.status` have no DB CHECK constraint — **MEDIUM**

- **File:** Migration `8d05cd602f03`
- **Issue:** Both are `VARCHAR(20)` with no CHECK. A typo in application code can silently write a garbage status that never appears in any query filter.
- **Fix:** Add a CHECK constraint via a new migration: `CHECK (status IN ('queued','running','completed','partial_failure','cancelled','failed'))` for batches; similar for batch_items.

---

#### [ ] 21 (orig #11) — `SCHEDULE_MIN_INTERVAL_MINUTES` missing from k8s API manifest — **MEDIUM**

- **File:** `govindappa-k8s-config` — `scrapeflow/api.yaml`
- **Issue:** The env var is set in `docker-compose.yml` but was not carried to the k8s manifest. Without it, the scheduler uses the default (5 minutes), which may not match the production policy.
- **Fix:** Add `SCHEDULE_MIN_INTERVAL_MINUTES` to the API Deployment's `env` array in the gitops repo.

---

#### [x] 22 (orig #35) — `authorized_parties=None` needs env var config — **MEDIUM**

- *(See item #35 in Part 1 — CRITICAL when in production)*

---

#### [x] 23 (orig #45) — `validate_cron_min_interval` uses naive datetime — **MEDIUM**

- *(See item #45 in Part 1)*

---

#### [x] 24 (orig #48) — WebSocket handlers have no connection timeout — **MEDIUM**

- *(See item #48 in Part 1)*

---

### LOW — Housekeeping / Tech debt

---

#### [x] 25 (orig #5) — Stale comment in `result_consumer.py` near webhook delivery — **LOW**

- **File:** `api/app/core/result_consumer.py` — line shifted after `6e6cc8b` restructure
- **Fix:** `grep -n "# Stale\|# TODO\|# FIXME" api/app/core/result_consumer.py` and remove outdated comments.

---

#### [x] 26 (orig #6) — Verify admin routes cannot be used for cross-tenant writes — **LOW**

- **File:** `api/app/routers/admin.py`
- **Issue:** Admin cancel (`admin_delete_or_cancel_job`) operates on any job regardless of owner. This is correct by design (admin powers), but verify no admin route exposes per-user write operations (PATCH, secret rotation) without owner consent.
- **Fix:** Audit: confirm no admin route modifies a user's data in a way that would surprise that user (e.g., changing their webhook URL).

---

#### [x] 27 (orig #18) — Audit all `create_webhook_delivery` call sites for correct `event_name` — **LOW**

- **File:** `api/app/core/result_consumer.py`, `api/app/core/webhooks.py`
- **Fix:** Grep all call sites; verify each passes a string in `_VALID_WEBHOOK_EVENTS = {"job.completed", "job.failed", "crawl.completed", "batch.completed"}`. Any event added after the filter was introduced without a matching guard would silently bypass filtering.
- **Result:** All 7 `create_webhook_delivery` call sites in `result_consumer.py` pass `"job.completed"` or `"job.failed"` and are each guarded by the matching `job.webhook_events` check. `create_batch_webhook_delivery` passes `"batch.completed"` (no `webhook_events` filter on `Batch` by design). Coordinator's `enqueue_crawl_webhook` writes directly to DB (separate service) with hardcoded `"crawl.completed"` — bypasses `create_webhook_delivery` intentionally. Two design notes filed in `docs/project/PHASE3_DEFERRED.md`: batch/crawl missing `webhook_events` filter field; coordinator direct-write bypass.

---

#### [x] 28 (orig #19) — `_attempt_delivery` does redundant DB round-trip per delivery — **LOW**

- **File:** `api/app/core/webhook_loop.py:85`
- **Issue:** Caller has the `WebhookDelivery` object; `_attempt_delivery` re-fetches by ID. Extra query on every retry.
- **Result:** Re-fetch is load-bearing — not a cosmetic round-trip. PostgreSQL `FOR UPDATE` locks are transaction-scoped: committing within a single session releases all 50 locks at once, not just the processed row. Any single-session redesign would still need a per-delivery status re-check (`db.refresh`), which is the same round trip count. The existing comment at line 56 already documents the intent. Current design accepted as-is.

---

#### [x] 29 (orig #32) — No test for batch item storage quota exceeded — **LOW**

- **File:** `api/tests/test_batch.py`
- **Fix:** Added `test_result_consumer_batch_item_storage_quota_exceeded` — mocks `stat_object` to return an oversized object; asserts `run.status == "failed"`, `item.status == "failed"`, `item.error == "storage_quota_exceeded"`, and `batch.failed == 1`. 15 batch tests passing.

---

#### [x] 30 (orig #33) — No test for LLM key deleted between dispatch and result — **LOW**

- **File:** `api/tests/test_jobs.py`
- **Fix:** Add `test_result_consumer_llm_key_deleted` — dispatch a job with LLM config, delete the key, publish a "completed" result; assert run is `failed` with `error="LLM key not found or deleted"`.
- **Result:** `test_result_consumer_llm_key_deleted_cleans_minio` already exists at `test_jobs.py:418`; asserts run is `failed` with the expected error message.

---

#### [x] 31 (orig #34) — Cron schedule validation assumes server timezone — **LOW**

- **File:** `api/app/schemas/jobs.py` — `_MutableJobFields.schedule_cron`
- **Fix:** Added `Field(description=...)` documenting the UTC evaluation assumption; visible in OpenAPI docs and generated SDKs. Long-term timezone field deferred to Phase 4 (new `Job` column, croniter `pytz` base, migration).

---

#### [x] 32 (orig #40) — Coordinator hardcodes NATS subjects — **LOW**

- *(See item #40 in Part 1 — LOW when coordinator is single-process; HIGH if multi-replica)*

---

#### [x] 33 (orig #41) — `result_consumer.py` complexity hotspot — **LOW**

- *(See item #41 in Part 1 — flag for Phase 4 refactor)*
- **Result:** Part 1 item #41 marked done — `_handle_storage_quota_exceeded` extracted to `quota.py`, MinIO helpers extracted to `storage.py`. Remaining complexity flagged for Phase 4 refactor.

---

#### [ ] 34 (orig #42) — Admin stats runs 6+ sequential DB queries — **LOW** *(deferred to Phase 4)*

- *(See item #42 in Part 1)*

---

#### [x] 35 (orig #44) — `JobNotifier` subscriber queues are unbounded — **LOW**

- *(See item #44 in Part 1)*

---

#### [x] 36 (orig #46) — `import os` at module bottom in `main.py` — **LOW**

- *(See item #46 in Part 1)*

---

#### [x] 37 (orig #47) — `email.ilike` search has no index — **LOW**

- *(See item #47 in Part 1)*

---

### Partially Addressed (post `6e6cc8b`)

---

#### [x] 38 (orig #7) — WebSocket update not sent when job is cancelled — **HIGH**

- **Status:** Fixed. `cancel_job` and `admin_delete_or_cancel_job` now emit `pg_notify('job_status', ...)` before commit (item #12). WebSocket subscribers receive `cancelled` immediately regardless of whether the worker result arrives.

---

#### [~] 39 (orig #8) — `pg_notify` missing for `processing` status transition — **MEDIUM**

- **Status:** The `result_consumer` now emits notify for most transitions. `processing` (LLM in-flight) is still not explicitly notified — the WebSocket jumps from `running` to `completed` skipping `processing`.
- **Fix (optional):** Emit `pg_notify('job_status', f'{job_id}:{run_id}:processing')` in `_handle_scrape_completed` when dispatching to LLM worker.

---

### Investigation / Design Questions (carry-over)

---

#### [?] 40 (orig #1) — Custom robots.txt parsers vs established packages

- **Go:** `internal/robots` hand-rolled. Reference: `temoto/robotstxt` (Go).
- **Python:** `worker/robots.py` hand-rolled. Reference: `reppy` or `robotexclusionrulesparser`.
- **Recommendation:** Evaluate edge cases (wildcards, `Allow:` precedence, `Crawl-delay`). If the hand-rolled versions handle Google's extended spec, keep them. If not, swap.

---

#### [?] 41 (orig #4) — Should `hiredis` be installed for redis-py?

- `hiredis` is a C extension that speeds up RESP parsing. One-line dependency addition, no API change. Low risk. Recommend adding for production.

---

#### [?] 42 (orig #9) — `JobNotifier` uses blocking `asyncpg.connect()` at startup?

- `asyncpg.connect()` is `async` — the call is awaited correctly at `job_notifier.start()`. Not a blocking issue. Close this.

---

#### [?] 43 (orig #13) — Can `_handle_batch_result` receive `worker_status` other than `completed`/`failed`?

- Only `running`, `completed`, `failed` arrive in practice. The `running` branch returns early. `_handle_batch_result` never sees `processing` (LLM paths). Close this — add an explicit `assert worker_status in {"running", "completed", "failed"}` if defensive coding is preferred.

---

#### [?] 44 (orig #16) — Should content hash be computed when `schedule_cron` is not set?

- Intentional — establishes a baseline for users who add scheduling later via PATCH. Design is correct. Close this.

---

## Scorecard

| Area | Score | Notes |
|------|-------|-------|
| Architecture | 7/10 | Clean worker isolation; result_consumer is the one overloaded file |
| Code Quality | 7/10 | Consistent style; `_resolve_credentials` duplication; naive datetime in cron validator |
| Security | 6/10 | `authorized_parties=None`, `execute_js` JS execution, unsigned batch webhooks |
| Performance | 7/10 | Good use of Lua atomics and SKIP LOCKED; stats endpoint is N+1; no coordinator locking |
| Error Handling | 7/10 | Good backoff patterns; WS timeout gap; quota not atomic with status |
| Testability | 7/10 | 218 tests; gaps in quota-delete and LLM-key-deleted paths |
| Dependencies | 8/10 | No version pinning; `hiredis` easy win; no concerning licenses |

**Overall: 7/10** — Solid Phase 3 implementation. The CRITICAL items are all in batch/dedup code that was added late. Security gaps (`authorized_parties`, `execute_js`) need production attention before opening to public users.

---

## Top 3 to fix first

| Priority | Item | Why |
|----------|------|-----|
| **1** | #4 — Uncomment Alembic auto-migration | Blocks every production deploy without manual intervention |
| **2** | #1 + #2 + #3 (batch counter race, zip strict, unsigned webhook) | Three CRITICAL bugs in the same feature; fix together in one pass of `result_consumer.py`, `batch.py`, `webhook_loop.py` |
| **3** | #35 — Set `authorized_parties` from env var | Currently accepts JWTs from any Clerk app; trivial to fix, significant security gap |

---

## Filing guide for engineers

When picking up an item:
1. Confirm it is still open (check git log since this document was written)
2. Write the test first — most of these items are bugs that are provably reproducible
3. Mark `[x]` in this document and commit the change alongside the fix
4. Note any deviations in the PR description

Items marked `[?]` are design questions — bring to Tech Lead before implementing.
