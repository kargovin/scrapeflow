# Usage Findings

> **Consolidated Phase 4 view: [`phase4-backlog.md`](./phase4-backlog.md).**

Things noticed while using the app that are worth fixing or considering.
Captured here before being formalised into Phase 4 backlog items.

---

## UF-001 — `/health/ready` missing MinIO check ✅ FIXED (2026-07-23)

`GET /health/ready` checks DB, Redis, and NATS but not MinIO. All scrape results
are written to MinIO — if it is down, jobs will complete in the worker but the
result consumer will fail to store output. The readiness endpoint would return
`200 ok` while the platform is silently broken for every job.

**Original suggestion** was to add the check to `ReadinessResponse` and include it
in the `degraded` set. **That was rejected on inspection** — `/health/ready` is
wired as the k8s **readinessProbe** (infra `clusters/k3s-server/scrapeflow/app/api.yaml`)
and the API is single-replica, so a MinIO outage would have pulled the only endpoint
out of the Service and 503'd the *entire* API: `/jobs`, auth and the admin panel
included. That converts a partial outage into a total one, and removes the dashboard
you'd use to diagnose it.

**Shipped instead — split the endpoints**, because probe and diagnostic are
different questions that were being conflated:

- `GET /health/ready` — **serving readiness**: DB, Redis, NATS. Unchanged. Still the
  probe. MinIO deliberately absent.
- `GET /health/deps` — **full dependency report**: the above plus MinIO. 503 +
  `status: degraded` when anything is down, but nothing routes on it.

MinIO check is `bucket_exists(settings.minio_bucket)` — exercises the bucket results
are actually written to and needs no account-wide listing permission — wrapped in a
3s `asyncio.wait_for` so a hung object store can't hang the endpoint. It is the only
dependency check that makes a live network round-trip.

Curl recipes in `COMMANDS.md` → "Health / dependency checks". 6 new tests → 249.

---

## UF-002 — Per-user proxy pool (replace platform default)

The current `DEFAULT_PROXY_URL` env var is a single platform-wide proxy shared across
all users. At scale this is a liability — one user's scraping behaviour can get the IP
banned, affecting everyone.

**Decision:** Replace with a per-user proxy model. Each user stores one or more named
proxy endpoints (a `user_proxies` table). Jobs reference a proxy by name or fall back
to the user's default. No platform-level default.

**Rotation strategy:** Provider-side. Users store a single rotating gateway URL
(e.g. Bright Data's `zproxy.lum-superproxy.io`) — the provider handles per-request IP
rotation. The API stays simple: store one encrypted URL, forward it at dispatch. No
client-side round-robin needed.

**Impact:** New `user_proxies` table, updated secrets model, dispatch changes, frontend
proxy management UI. Phase 4 candidate.

---

## UF-003 — MinIO failures are handled inconsistently across the write path

Surfaced while closing UF-001. UF-001 was about *observing* a MinIO outage from
outside; this is about how the system *reacts* to one on the actual job path. Three
distinct behaviours, none of them right, split across the workers and the consumer.

### 3a — playwright + Go workers ack on a MinIO write failure (the Q5 bug, unfixed on two of three workers)

`playwright-worker/worker/worker.py:250` catches every exception from the render/upload
block → logs `job_failed` → publishes `failed` → **acks**. The Go worker has the same
shape: `Upload()` errors propagate to the same terminal path
(`http-worker/internal/worker/worker.go:356`).

Two problems:

1. **A MinIO write fault is indistinguishable from a scrape failure.** MinIO's error
   string lands in the same `error` field the user reads, under the same `job_failed`
   log event. There is no `storage_unavailable` signal to alert or grep on.
2. **It acks — so a transient MinIO blip permanently fails the job**, *after* the
   expensive work is already done (a headed-Chrome render; for the LLM path, a call
   already billed to the user's own key). The worker preempts the queue's retry by
   acking. This is **exactly the Q5 ack-on-failure failure mode.**

The Q5 fix was applied to **only the LLM worker.** `llm-worker/worker/errors.py:66`
already carries a `_TRANSIENT_S3_CODES` set and naks with backoff on a MinIO backend
fault. The playwright and Go workers never got that treatment — same bug, same
infrastructure, one of three workers fixed.

**This is the part that survives Temporal as *domain knowledge*.** The nak/backoff
plumbing is deleted with NATS, but the transient-vs-terminal S3 classification is the
same knowledge §3 of the backlog already says must port into the activity's
`RetryPolicy` `non_retryable_error_types`. Right now that knowledge lives in one worker
out of three, so the migration would carry over only a third of it. Propagating it to
all three workers pre-migration both fixes a live ack-on-failure bug and consolidates
the knowledge into one place before it gets ported once.

### 3b — result consumer swallows MinIO errors with no log line

Two helpers fail open **silently** (contrast `delete_minio_object` ten lines away in
the same file, which *does* log a warning):

- `_compute_content_hash` (`api/app/core/result_consumer.py:48`) — `except Exception:
  return None`. Fail-open for dedup is the correct *behaviour*, but with MinIO down every
  run gets no hash, change detection silently degrades to "everything looks changed,"
  and nothing says so. Same class as BUG-003: the dedup baseline quietly stops meaning
  what you think it means.
- `stat_minio_size` (`api/app/core/storage.py:18`) — `except Exception: return 0`. Size
  0 trips the `result_size > 0` guard at all six call sites, so **both** the quota check
  and `_try_increment_storage` are skipped. The user's storage is under-counted for that
  run **permanently** — the message acks, `storage_accounted_at` is never set, and no
  redelivery corrects it. Quota drifts with no trace.

**`result_consumer.py` is deleted by the Temporal migration** (§3), so don't invest in
structured logging there — a bare `logger.warning` in the two helpers is the ceiling.
`stat_minio_size` lives in `storage.py`, a helper more likely to survive, and the quota
under-count is money-adjacent state, so its log line is the one worth adding regardless.

**Recommendation / priority:** 3a is the substantive item and a better use of a session
than P4 (Dependabot) — it fixes a live ack-on-failure bug *and* consolidates the S3
classification the migration needs. 3b is a cheap log-line pass with a deliberately low
ceiling given the deletion. Added to backlog §1 as **P3b**.
