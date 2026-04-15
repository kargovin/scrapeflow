# PRD-004 — robots.txt Compliance

**Priority:** P1
**Source:** CLAUDE.md Phase 3 + PHASE3_DEFERRED.md
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

ScrapeFlow workers currently make no `robots.txt` check before scraping. For a single-user homelab scraping known internal or consented sites, this is fine. As the platform is used more broadly — or as a portfolio piece reviewers will inspect — ignoring `robots.txt` is an ethical and potential legal liability.

The feature needs to be per-job (not global), defaulting to **respect** robots.txt, so that users who are scraping their own property or sites that explicitly permit scraping can opt out of the check.

---

## Goals

1. Add a per-job toggle: `respect_robots` (boolean, default `true`).
2. When enabled, workers fetch, parse, and enforce `robots.txt` before scraping.
3. Cache parsed `robots.txt` per domain to avoid re-fetching on every job run.
4. Return a clear error to the caller when a URL is disallowed by robots.txt.

---

## Non-goals

- Crawl-delay directive enforcement (Phase 4 at earliest)
- `Sitemap:` directive parsing (handled in PRD-007 Site Crawl)
- Sitemaps-based URL discovery from robots.txt
- User-agent spoofing to bypass robots.txt (this feature is for compliance, not evasion)

---

## User stories

**As a user scraping a public website**, I want ScrapeFlow to respect the site's `robots.txt` by default so I'm not inadvertently violating their terms.

**As a user scraping my own internal site**, I want to set `respect_robots: false` on a job so the worker doesn't fail on a missing or restrictive robots.txt.

**As a platform operator**, I want robots.txt results cached in Redis per domain so we don't hit the target server's `/robots.txt` endpoint on every single job run.

---

## Requirements

### New job field

- `respect_robots: boolean` — default `true`
- Added to `jobs` table via Alembic migration
- Included in the fat NATS dispatch message so workers receive it without DB access
- Exposed on `POST /jobs` request body and `GET /jobs/{id}` response

### Worker behavior when `respect_robots = true`

1. Parse the URL to extract `scheme://hostname`
2. Check Redis cache: key `robots:{hostname}`, TTL 1 hour
3. Cache miss: fetch `{hostname}/robots.txt` with a 5-second timeout; parse and store in Redis
4. Check if the target URL path is allowed for user-agent `*` (and `ScrapeFlow` if listed)
5. If disallowed: publish a failure result to `scrapeflow.jobs.result` with `error = "robots_txt_disallowed"`; do not attempt the scrape
6. If allowed or robots.txt fetch fails (404, timeout): proceed with the scrape

### robots.txt fetch failure handling

- 404 → treat as "no restrictions" (proceed)
- 5xx or timeout → treat as "no restrictions" (proceed; do not block the user's job because the target server is down)
- Malformed robots.txt → treat as "no restrictions" (proceed)

### Caching

- Redis key: `robots:{hostname}`
- TTL: 1 hour (configurable via env `ROBOTS_CACHE_TTL_SECONDS`, default 3600)
- Value: serialized parsed ruleset (not raw text — store parsed allow/disallow lists)
- Both Go HTTP worker and Playwright worker must implement this cache (shared Redis)

### Error surfaced to user

When a job fails robots.txt check:
- `job_run.status = "failed"`
- `job_run.error = "robots_txt_disallowed: {url} is blocked by robots.txt for this host"`
- 200 on `GET /jobs/{job_id}/runs/{run_id}` (not a 4xx — the job ran, it just failed a pre-flight)

---

## Success criteria

- [ ] A job targeting a URL disallowed by robots.txt (with `respect_robots: true`) fails with `robots_txt_disallowed` error
- [ ] A job with `respect_robots: false` targeting the same URL succeeds
- [ ] `GET /jobs/{id}` shows `respect_robots` in the response
- [ ] Redis is populated with the cached robots.txt ruleset after the first run; second run does not re-fetch
- [ ] 404 on `/robots.txt` does not block the scrape
- [ ] Tests cover: disallowed path, allowed path, 404 robots, timeout robots, `respect_robots=false` bypass

---

## Open questions for Architect

1. The Go HTTP worker and Python Playwright worker need the same robots.txt parsing logic. Should this be implemented separately in each (Go parser + Python parser) or should the API handle robots.txt pre-flight and include the result in the fat message?
2. Should the `robots.txt` cache key also incorporate the user-agent string? `robots.txt` can specify different rules per user-agent, and if we identify ourselves as `ScrapeFlow`, the cache key should reflect that.
