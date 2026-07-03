# Usage Findings

Things noticed while using the app that are worth fixing or considering.
Captured here before being formalised into Phase 4 backlog items.

---

## UF-001 — `/health/ready` missing MinIO check

`GET /health/ready` checks DB, Redis, and NATS but not MinIO. All scrape results
are written to MinIO — if it is down, jobs will complete in the worker but the
result consumer will fail to store output. The readiness endpoint would return
`200 ok` while the platform is silently broken for every job.

**Suggestion:** Add a MinIO ping (e.g. `bucket_exists` or a lightweight `stat`) to
`ReadinessResponse` and include it in the `degraded` check.

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
