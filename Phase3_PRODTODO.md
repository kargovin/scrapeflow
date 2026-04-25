## Purpose: Findings from me after phase 3 implementation

Status legend: [ ] open  [~] partial  [?] investigation/design question  [x] done

Items 1–19: post-Phase 3 findings (original list)
Items 20–34: codebase production audit (April 2026)

---

### Bugs (must fix before production)

- [ ] **3** — Uncomment Alembic auto-migration in `api/app/main.py` before merging `develop` → `main`
  - The migration block at `main.py:42-47` is still commented out. It was disabled during Phase 3 to prevent hot-reload from applying partial migrations before hand-edited SQL (CHECK constraints, triggers, ENUMs) was saved. That risk is gone now — re-enable it.

- [ ] **14** — Batch jobs are not dispatched to the LLM worker
  - When a batch item result arrives, `_handle_batch_result` updates `batch_items`/`batches` and fires the batch webhook but never checks if the batch was created with an LLM schema. The LLM extraction path only exists in `_handle_job_result`. Batch items with an output format of `json` (LLM) silently return raw HTML.

- [ ] **15** — `decrement_storage_bytes` is never called; storage quota leaks on delete
  - `quota.py` defines `decrement_storage_bytes()` but there are zero call sites outside the file itself. Every job/run delete (via `DELETE /jobs/{id}` or the cleanup CronJob `cleanup_old_runs.py`) removes the MinIO object without decrementing `user_quotas.storage_bytes_used`. Over time a user's recorded usage grows unboundedly even if their actual stored data shrinks.

- [ ] **17** — Dedup bug: deletes the `history/` object that `job_runs.result_path` points to
  - Workers write two MinIO paths: `latest/{job_id}.ext` (overwritten each run) and `history/{job_id}/{ts}.ext` (immutable). We store the `history/` path in `job_runs.result_path`. When dedup detects identical content (`result_consumer.py:247`), it deletes the new `history/` object to save space — but that is the exact path we just wrote into `result_path` for this run. Any future `GET /jobs/{id}/result` call will get a 404 from MinIO. Fix: on dedup hit, either point `result_path` to the previous run's path (the canonical copy), or delete the `latest/` copy instead.

- [ ] **20** — Batch counter race condition (`result_consumer.py:140,177,186`) — **critical**
  - `_handle_batch_result` does: `batch = db.get(...)` → increment `batch.completed` in Python → commit. If two items complete simultaneously, both read `completed=N`, both write `N+1` instead of `N+2`. The final count is wrong and the `batch.completed + batch.failed == batch.total` check may never fire — the batch can stay permanently in `running` state, never firing the completion webhook.
  - Fix: Use atomic SQL `UPDATE batches SET completed = completed + 1 WHERE id = :id` (or `SELECT FOR UPDATE` to lock the row for the duration of the handler).

- [ ] **21** — `zip(strict=False)` silently truncates batch dispatch (`batch.py:89,100`) — **critical**
  - Two `zip(items, runs, strict=False)` loops are used when associating dispatched NATS messages with their DB `job_runs` rows. If the two lists diverge due to any construction bug, `strict=False` silently processes fewer items — a batch of 10 URLs could dispatch only 7 with no error logged.
  - Fix: Change both to `zip(items, runs, strict=True)`.

- [ ] **22** — Batch webhooks are always unsigned (`webhook_loop.py:111-114`) — **critical**
  - `_attempt_delivery` fetches the `Job` to read `webhook_secret`. Batch deliveries have `job_id=None`, so `job` is `None`, `secret_bytes` stays `b""`, and the HMAC is computed over an empty key. Any receiver implementing signature verification will always reject batch webhooks.
  - Fix: Add a `webhook_secret` column to the `Batch` model and fetch it when `delivery.batch_id` is set.

- [ ] **23** — Crawl page status filter has no whitelist (`crawls.py:115-116`)
  - `page_status: str | None` from the query string is used directly in `.where(CrawlPage.status == page_status)` with no validation. An invalid value like `?status=typo` returns an empty 200 response instead of a 422, silently masking client bugs.
  - Fix: `if page_status not in {None, "pending", "processing", "completed", "failed"}: raise HTTPException(422)`.

- [ ] **24** — MinIO object orphaned when LLM key is deleted mid-schedule (`result_consumer.py:259-268`)
  - When a scheduled job's result arrives but the user has since deleted their LLM API key, the result consumer marks the run `failed` and leaves the already-uploaded MinIO object in place. `storage_bytes_used` was incremented on upload but is never decremented on this failure path — the object is unreachable but counts against quota forever.
  - Fix: Delete the MinIO object before marking the run failed (same pattern as `_handle_storage_quota_exceeded`).

- [ ] **25** — Admin user delete leaks MinIO objects (`admin.py:183`)
  - `db.delete(user)` cascades through Postgres FKs (jobs → job_runs) but MinIO objects are not touched. A deleted user's entire scrape history stays in object storage. There is also no decrement of platform-wide storage accounting.
  - Fix: Before deleting, iterate the user's `job_runs.result_path` values and bulk-delete the corresponding MinIO objects.

- [ ] **29** — PATCH job upserts secrets before all validations pass (`jobs.py:495-547`)
  - The `PATCH /jobs/{id}` handler upserts `job_secrets` rows (proxy URL, cookies) mid-function, before the check that rejects patches on actively `processing` runs (line ~586). If validation fails after the upsert, the new secret is committed but the job fields are not updated — the DB is left in an inconsistent state.
  - Fix: Move all `job_secrets` writes after the validation gate, or wrap the entire handler in a single transaction with rollback on any exception.

- [ ] **30** — `increment_storage_bytes` not atomic with run status update (`result_consumer.py:272,291,323,463`)
  - The storage quota increment and the job/batch run status write are separate SQL statements within the same SQLAlchemy session, but the quota increment is not guarded — if it raises, the run is still committed as `completed`. This permanently breaks quota accounting for that run.
  - Fix: Treat the quota increment as part of the same transaction unit; if the increment fails, do not mark the run completed.

---

### Partially Addressed (recent commit `6e6cc8b` — "fix ws/webhook gaps")

- [~] **7** — WebSocket update not sent when a job is cancelled
  - Before the fix commit, `result_consumer.py:167` returned early for cancelled runs without emitting `pg_notify`, so the WebSocket subscriber never received the terminal status. The fix commit restructured result_consumer and added more `pg_notify` sites (lines 106, 123, 153, 202, 393, 431). Whether the `cancelled` case is now fully covered needs explicit verification.

- [~] **8** — `pg_notify` missing for several status transitions
  - Before the fix, we only notified on: storage-limit exceeded (batch + job), batch completed/failed, job completed, and job running. The full status lifecycle is `pending → running → processing → completed / failed / cancelled`. The jobs router (the only code that sets `cancelled`) still has zero `pg_notify` calls — so a cancel action does not push a WebSocket update to the browser. The `processing` transition (LLM in-flight) is also not notified.

---

### Feature Not Built

- [ ] **2** — WebSocket live updates not wired into the Admin SPA
  - The API exposes `GET /jobs/{id}/watch` and `GET /batch/{id}/watch` WebSocket endpoints (Step 26). The Admin SPA (Step 28) was built without using them — job and batch status in the UI is static until the user refreshes. Wire the WS endpoints into the React job/batch detail views.

---

### Housekeeping / Audit

- [ ] **5** — Stale comment in result_consumer near webhook delivery
  - Originally flagged at line 131-132. The file was restructured in `6e6cc8b` so the line number has shifted. Find and clean up the comment.

- [ ] **6** — Verify admin users cannot access other users' jobs
  - The standard job routes return 404 for cross-tenant access (correct). Admin routes in `routers/admin.py` query across all users by design. Check that admin-scoped routes are properly guarded by the `is_admin` dependency and that no admin route accidentally exposes per-user write operations (cancel, delete) across tenants.

- [ ] **11** — `SCHEDULE_MIN_INTERVAL_MINUTES` missing from the k8s scheduler Job YAML
  - The env var controls the minimum cron interval the scheduler will accept. It is set in `docker-compose.yml` but was not carried over to the k8s manifest in `govindappa-k8s-config`. Without it the scheduler falls back to its default, which may not match the production policy.

- [ ] **12** — `batches.status` and `batch_items.status` have no DB-level constraint
  - Both columns are `VARCHAR(20)` (deliberately not an ENUM, because status values may grow — see Migration 3.5 notes). But that means any string is valid at the DB layer. Add a `CHECK (status IN (...))` constraint so a typo in application code can't silently write a garbage status.

- [ ] **18** — Audit all `create_webhook_delivery` call sites
  - Step 20 added a `webhook_events` filter that gates every `create_webhook_delivery` call. Verify that every event type (`job.completed`, `job.failed`, `batch.completed`, `crawl.completed`) actually passes the correct `event_name` string and that no new event was added after the filter was introduced without a matching guard.

- [ ] **19** — `_attempt_delivery` in `webhook_loop.py` does a redundant DB round-trip
  - The caller already has the `WebhookDelivery` ORM object. `_attempt_delivery` currently receives a delivery ID and re-fetches it from DB. Pass the object directly to eliminate the extra query on every delivery attempt (including retries with exponential backoff, where this multiplies).

- [ ] **26** — `_resolve_credentials` is copy-pasted into two modules (`jobs.py:71`, `scheduler.py:32`)
  - Identical function bodies in the router and the scheduler. A circular import prevented sharing at the time (see Phase 3 Step 17 notes). Adding a new secret type (e.g. a new proxy provider) requires editing both files — they will inevitably diverge.
  - Fix: Extract to `app.core.credentials` and import from both; the circular import can be avoided by not importing router-level symbols into the core module.

- [ ] **27** — Stale-pending recovery threshold hardcoded to 10 minutes (`scheduler.py:170`)
  - `timedelta(minutes=10)` is baked into code. HTTP jobs typically finish in seconds; Playwright + LLM jobs can take minutes. A k8s deployment cannot tune this without a code change.
  - Fix: Move to `settings.stale_pending_threshold_minutes` with a default of 10.

- [ ] **28** — Admin stats `list_objects` call has no timeout (`admin.py:581`)
  - The cold-cache path iterates all MinIO objects with no timeout or iteration cap. If MinIO is slow or unresponsive, the admin stats endpoint blocks indefinitely. The Redis cache TTL mitigates repeated calls but the first cold call is unprotected.
  - Fix: Add an `asyncio.wait_for` wrapper or a max-bytes iteration guard around the `list_objects` loop.

- [ ] **31** — Coordinator fires crawl completion webhook with no retry (`coordinator/bfs.py`)
  - The `crawl.completed` webhook is a direct HTTP POST with no retry on failure. A transient 5xx from the user's endpoint loses the event forever. Every other webhook type goes through `WebhookDelivery` + exponential backoff in `webhook_loop.py`.
  - Fix: Write a `WebhookDelivery` row (with `batch_id=None`, `job_id=None`, `crawl_id=crawl.id`) and let the existing retry loop handle delivery.

- [ ] **32** — No test for batch item storage quota exceeded
  - The quota-exceeded branch in the batch result path (`_handle_storage_quota_exceeded` called from `_handle_batch_result`) has no test coverage. The scenario where a batch item result arrives but the user is over quota is untested.
  - Fix: Add `test_batch_item_storage_quota_exceeded` to `tests/test_batch.py`.

- [ ] **33** — No test for LLM key deleted between job dispatch and result arrival
  - The `result_consumer.py:259-268` path (LLM key not found → run marked failed) is not tested. This is also the source of the MinIO orphan bug in TODO-24.
  - Fix: Add `test_result_consumer_llm_key_deleted` to `tests/test_jobs.py`.

- [ ] **34** — Cron schedule validation assumes server timezone, not user timezone (`jobs.py:100-112`)
  - `validate_cron_min_interval()` calls `croniter` without a timezone argument, so "every day at 9am" is interpreted as 9am in whatever timezone the API container runs (UTC in production). Users in other timezones get unexpected execution times with no error or warning.
  - Fix: Accept an optional `timezone` field on `JobCreate`/`JobPatch` and pass it to `croniter(..., hash_type=...)`, or at minimum document the UTC assumption in the API response.

---

### Investigation / Design Questions

- [?] **1** — Why did we write a custom robots.txt fetcher and parser instead of using an existing package?
  - The Go worker has `internal/robots` and the Playwright worker has `worker/robots.py`, both hand-rolled. Packages like `reppy` (Python) or `temoto/robotstxt` (Go) handle edge cases (wildcard rules, crawl-delay, sitemaps). Worth understanding whether the custom implementations cover the same spec surface, or whether we should swap them out.

- [?] **4** — Should we install `hiredis` for redis-py?
  - `hiredis` is a C extension that replaces redis-py's pure-Python RESP parser with a much faster one. The redis-py docs recommend it for high-throughput workloads. Our usage (rate limiting, job notifier) is moderate, but it is a one-line dependency change with no API difference. Low risk, easy win.

- [?] **9** — Why is the `JobNotifier` connection synchronous?
  - `JobNotifier` uses a dedicated `asyncpg.connect()` (async) for the `LISTEN` loop. If the question is about the `pg_notify` emit side — those are issued via SQLAlchemy's async session using `text("SELECT pg_notify(...)")`. Clarify what "sync" refers to before deciding if a change is needed.

- [?] **10** — How does the `.env` file path stay stable inside Docker?
  - See `api/settings.py:8`. Docker Compose mounts the repo root into the container; the working directory is set to `/app` and `.env` is at `/app/.env` (same relative path as on the host). Pydantic-settings resolves it relative to CWD at import time — stable because CWD is fixed by the image entrypoint.

- [?] **13** — Can `_handle_batch_result` receive a `worker_status` other than `completed`/`failed`?
  - Workers publish `running` before they start and `completed`/`failed` when done. The result consumer routes `running` messages to update `job_runs.status` and then returns — they never reach `_handle_batch_result`. So in practice only `completed`/`failed` arrive there. But this is an implicit contract; adding an explicit guard (or an assertion) would make it explicit.

- [?] **16** — Should we compute the content hash even when `schedule_cron` is not set?
  - Currently yes — `result_consumer.py:284` computes the hash for all regular job runs regardless of schedule. This is intentional: a user might add a cron schedule via PATCH after the first run, and we want a hash baseline to compare against. The cost is one extra MinIO read per run, which is acceptable.
