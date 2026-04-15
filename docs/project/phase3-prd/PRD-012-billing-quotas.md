# PRD-012 — Billing and Per-user Quotas

**Priority:** P3
**Source:** CLAUDE.md Phase 3 + PHASE3_DEFERRED.md
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

ScrapeFlow has no enforcement mechanism to prevent a single user from consuming unlimited resources. Without enforcement, any multi-user deployment risks uncontrolled consumption exhausting worker capacity, MinIO storage, and LLM API budgets. Phase 3 ships quota enforcement; billing integration (Stripe) is explicitly Phase 4.

---

## Goals

1. Per-user configurable quota: maximum concurrent jobs, maximum runs per month, maximum MinIO storage.
2. 429 enforcement at job submission time and scheduler dispatch time when quota is exceeded.
3. Usage tracking: count of runs and storage bytes consumed per user, per month.
4. Admin can view and adjust quotas via the Admin SPA (PRD-011).
5. Users can view their own quota status via a new API endpoint.

---

## Non-goals

- Stripe or payment integration (Phase 4)
- Free tier / paid tier product distinctions (Phase 4)
- Automated quota upgrades (Phase 4)
- Overage charges (Phase 4)
- Per-endpoint or per-feature quotas (one global "runs per month" limit is sufficient for Phase 3)

---

## User stories

**As an admin**, I want to set a monthly run limit and concurrent job limit per user so one user can't exhaust the platform's resources.

**As a user**, I want to call `GET /me/quota` to see how many runs I've used this month and how many I have remaining.

**As a user who has hit their quota**, I want to receive a `429 Too Many Requests` response with a clear message telling me when my quota resets, not a confusing generic error.

---

## Requirements

### Quota dimensions

Three enforced limits per user:

| Dimension | Enforcement point | Default |
|-----------|------------------|---------|
| `monthly_runs` | `POST /jobs` trigger, scheduler dispatch | 500 runs/month |
| `concurrent_jobs` | `POST /jobs` trigger (count active runs) | 5 concurrent |
| `storage_bytes` | Before each result write to MinIO | 5GB |

Defaults are platform-wide env variables (`DEFAULT_QUOTA_MONTHLY_RUNS`, etc.) and can be overridden per user.

### Data model

New table `user_quotas` (or quota columns on `users` — Architect decides):
- `user_id: UUID FK`
- `monthly_runs_limit: int` (null = use platform default)
- `concurrent_jobs_limit: int` (null = use platform default)
- `storage_bytes_limit: bigint` (null = use platform default)

Usage tracking (options — Architect decides):
- Option A: Aggregate counters on `user_quotas` (reset monthly via cron)
- Option B: Compute on-demand from `job_runs` and MinIO object sizes
- Option C: Redis counters (fast, but non-durable; reconcile with DB on recovery)

### Enforcement

**`POST /jobs` and batch dispatch:**
1. Check `concurrent_jobs`: count `job_runs` with `status IN ('queued', 'running')` for this user; reject if >= limit
2. Check `monthly_runs`: count `job_runs` with `created_at >= first of current month` for this user; reject if >= limit

**Scheduler dispatch:**
- Before dispatching a scheduled run, re-check `monthly_runs` and `concurrent_jobs`
- If quota exceeded: skip the run, set `next_run_at` to next schedule time, log the skip

**MinIO write (worker result path):**
- Before writing result to MinIO, compute total storage used by user (sum of object sizes under `user/{user_id}/` prefix)
- If total + result_size > limit: fail the run with `error = "storage_quota_exceeded"`

### User-facing endpoint

`GET /me/quota`

Response:
```json
{
  "monthly_runs": {"used": 47, "limit": 500, "resets_at": "2026-05-01T00:00:00Z"},
  "concurrent_jobs": {"active": 2, "limit": 5},
  "storage_bytes": {"used": 1073741824, "limit": 5368709120}
}
```

### 429 response format

```json
{
  "error": "quota_exceeded",
  "quota_type": "monthly_runs",
  "message": "Monthly run limit reached (500/500). Quota resets on 2026-05-01.",
  "resets_at": "2026-05-01T00:00:00Z"
}
```

### Admin quota management

Via Admin SPA (PRD-011): admin can view and edit a user's per-quota overrides. Requires `PATCH /admin/users/{id}/quota` endpoint (new, or extended from existing `PATCH /admin/users/{id}`).

---

## Success criteria

- [ ] A user who has reached `monthly_runs_limit` receives 429 with `quota_exceeded` at `POST /jobs`
- [ ] Scheduler skips a run for a quota-exceeded user and logs the skip; does not error
- [ ] `GET /me/quota` returns accurate counts (verified against direct job_runs count)
- [ ] Admin can update a user's quota via the Admin SPA and see the new limit take effect immediately
- [ ] A user with 5 concurrent active runs cannot submit a 6th job
- [ ] MinIO storage enforcement blocks a result write that would exceed the storage quota
- [ ] Monthly run counter resets at the start of each calendar month (verified by cron or test fixture)

---

## Open questions for Architect

1. MinIO storage accounting: querying total storage per user requires listing all objects under the user's prefix. At scale this is slow. Should we maintain a `storage_bytes_used` counter in Postgres (updated on each write/delete) or query MinIO on-demand? The Postgres counter approach risks drift if objects are deleted outside the API.
2. Should the monthly run counter be a Redis counter (reset by cron) or computed from `job_runs` (always accurate but slower)? Redis is faster but adds a reconciliation concern on Redis restart.
3. Batch scraping (PRD-006) deducts N credits for N URLs at submission time. If the API checks quota at submission but the batch runs over multiple days, the "concurrent jobs" check needs to count batch items — should batch items count as concurrent jobs, or only as monthly run deductions?
