# PRD-015 — Content Deduplication

**Priority:** P3
**Source:** NEW — identified from crawl4ai (xxhash content fingerprinting + head fingerprinting)
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

Scheduled jobs re-scrape and re-store content even when the target page hasn't changed. For a change-detection use case (already in Phase 2), this wastes MinIO storage and produces redundant run records that dilute the change history. crawl4ai uses xxhash content fingerprinting to detect when a page hasn't changed and skip reprocessing — a direct analog to what ScrapeFlow needs for its scheduled jobs.

Note: Phase 2 change detection already detects *changes* and notifies. This PRD is about the complementary case: detecting *no change* and skipping the MinIO write + result storage entirely.

---

## Goals

1. When a scheduled job runs and the scraped content is identical to the previous run's content, skip writing to MinIO and mark the run as `unchanged` rather than `completed`.
2. Deduplication is opt-in at the job level — users who always want a stored result can disable it.
3. The "latest" MinIO path remains valid (pointing to the most recent actual content write, even if that was several runs ago).

---

## Non-goals

- Cross-user deduplication (User A's content is never shared with User B, even if the URL and content are identical)
- Fuzzy/semantic similarity (exact hash match only in Phase 3)
- Retroactive deduplication of existing stored content
- Deduplication for non-scheduled (one-time) jobs — dedup only applies to re-runs of the same job

---

## User stories

**As a user** running a daily scheduled scrape of a news page, I want the job to skip storage if the page hasn't changed since yesterday — so my run history only shows runs where something actually changed.

**As a user** reviewing my job's run history, I want to see `unchanged` runs clearly distinguished from `completed` runs so I know which ones have new content.

**As a platform operator**, I want reduced MinIO write volume for unchanged scheduled jobs — the same content shouldn't be written 30 times a month.

---

## Requirements

### New job field: `skip_unchanged`

- `skip_unchanged: boolean` — default `false` (existing behavior unchanged for all current jobs)
- When `true`: compare content hash of this run against the previous run's hash; skip storage if identical

### Content hashing

- Hash algorithm: xxhash (fast, non-cryptographic — same as crawl4ai; appropriate for change detection, not security)
- Hash computed over: the raw extracted content (before any LLM processing), normalized for whitespace (to avoid false positives from timestamp injections)
- Hash stored on `job_runs`: new `content_hash: str | null` column

### Deduplication logic (in result consumer)

When the API result consumer receives a completed run result and `skip_unchanged = true`:

1. Look up the most recent previous run for this job with `status = 'completed'` and a non-null `content_hash`
2. Compare hashes
3. **If identical:** Set `job_run.status = 'unchanged'`, skip MinIO write, skip webhook delivery (or deliver with `event: job.unchanged` if user subscribed to it — see PRD-013)
4. **If different (or no previous run):** Proceed normally (write to MinIO, update `latest/` path, fire webhook as today)

### `unchanged` status

New terminal status value: `unchanged`
- Terminal (counts against monthly run quota — the worker still did the work)
- Does NOT fire a `job.completed` webhook
- Does NOT write to MinIO
- Does appear in `GET /jobs/{id}/runs` history with `status: unchanged`

### `latest/` path behavior

The `latest/{job_id}.{ext}` MinIO path is not updated on `unchanged` runs — it continues pointing to the most recent actual content write. This is correct behavior: the latest path always holds the current authoritative content.

### Worker changes

None — the worker still fetches the page and returns the content. The hash comparison and skip logic lives entirely in the API result consumer. This is consistent with the "worker is DB-ignorant" principle.

Hash computation: the result consumer computes the xxhash on the content it receives in the NATS result message, before deciding whether to write to MinIO.

---

## Success criteria

- [ ] A scheduled job with `skip_unchanged: true` that re-scrapes an unchanged page shows `status: unchanged` in the run history
- [ ] An `unchanged` run does not create a new MinIO object (verified by MinIO object count)
- [ ] The `latest/` path continues to serve the previous run's content after an `unchanged` run
- [ ] A job with `skip_unchanged: false` (default) always writes to MinIO regardless of content similarity
- [ ] A first-run (no previous hash) always writes to MinIO even with `skip_unchanged: true`
- [ ] `content_hash` is stored on `job_runs` for all completed runs (regardless of `skip_unchanged` setting)
- [ ] `unchanged` runs are visible in run history with correct status; `completed` runs count is not inflated

---

## Open questions for Architect

1. `xxhash` is not in the current Python dependencies. Should we use `xxhash` (pip package) or `hashlib.md5` (stdlib) for simplicity? The choice of hash algorithm matters only for collision resistance — md5 is fine for change detection.
2. Should `content_hash` be stored on all `job_runs` (including non-scheduled, non-dedup jobs), or only when `skip_unchanged = true`? Storing it universally enables future dedup and change detection features without a schema migration.
3. How should whitespace normalization work? The goal is to avoid treating `"content \n"` and `"content\n"` as different. Should we strip trailing whitespace per line, or normalize to a canonical form (e.g. single newline between paragraphs)?
