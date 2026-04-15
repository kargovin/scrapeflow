# PRD-002 — Rate Limiting: Sliding Window

**Priority:** P1
**Source:** PHASE3_DEFERRED.md, CLAUDE.md decisions table
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

The current rate limiter uses a Redis fixed window counter (`INCR` + `EXPIRE`). A user who knows the window boundary can fire `N` requests at `T-1s` (end of window) and another `N` requests at `T+1s` (start of next window), consuming `2N` requests in 2 seconds — double the configured quota. This is a documented limitation explicitly deferred from Phase 2.

Before billing and per-user quotas ship (PRD-012), the rate limiter must be correct, or quota enforcement is meaningless.

---

## Goals

1. Replace the fixed window counter with a sliding window algorithm that enforces the quota accurately regardless of when within a window requests arrive.
2. Keep the implementation at 3–5 Redis operations per request (same order of magnitude as current implementation).
3. No change to the external API (rate limit headers, 429 response format stay the same).

---

## Non-goals

- Distributed rate limiting across multiple API replicas (single-node homelab; one API pod)
- Per-endpoint rate limits (per-user global limit is sufficient for Phase 3)
- Token bucket or leaky bucket algorithms (sliding window is sufficient and simpler to reason about)

---

## User stories

**As a platform operator**, I want a user configured for 100 requests/hour to be unable to burst 200 requests in a 2-second span by straddling a window boundary.

**As an API user**, I want the `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers to accurately reflect how many requests I have left in the current sliding window.

---

## Requirements

### Algorithm: Sliding window log (Redis sorted set)

Use a Redis sorted set per user:
- Key: `rate_limit:{user_id}`
- Members: request timestamps (Unix float, used as both member and score)
- On each request:
  1. `ZREMRANGEBYSCORE` — remove entries older than `now - window_seconds`
  2. `ZCARD` — count remaining entries
  3. If count >= limit: return 429
  4. `ZADD` — add current timestamp
  5. `EXPIRE` — reset TTL to `window_seconds * 2`

This is 4–5 Redis commands, all fast O(log N) operations.

### Quota configuration

- Window size and limit remain env-configurable (no change to settings schema)
- Default: 1000 requests per hour (same as Phase 2 default)

### Response contract (no change)

- `429 Too Many Requests` with `Retry-After` header
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers on every response

### Migration

- No database migration required (Redis-only change)
- The old `rate_limit:{user_id}` string keys are replaced by sorted set keys with the same name — old string keys expire naturally within one window period after deploy

---

## Success criteria

- [ ] A user configured for 10 requests/min cannot send more than 10 requests regardless of where within a minute they arrive
- [ ] A burst of 10 at `T=59s` + 10 at `T=61s` is correctly rejected: the 11th–20th requests in the second burst hit 429 for approximately 59 seconds after the first burst
- [ ] `X-RateLimit-Remaining` accurately decrements on each request
- [ ] Rate limiter adds < 5ms p99 latency overhead (baseline the before/after)
- [ ] Unit tests cover the boundary condition (burst across window)

---

## Open questions for Architect

1. Lua script atomicity: the 4–5 Redis commands should be wrapped in a Lua script to prevent race conditions under concurrent requests from the same user. Is this acceptable complexity for Phase 3 or should a Redis pipeline suffice?
2. The sorted set member must be unique per request — using `timestamp` as member causes collisions if two requests arrive in the same millisecond. Should we use `timestamp:uuid` composite members, or a monotonic counter?
