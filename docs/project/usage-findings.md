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
