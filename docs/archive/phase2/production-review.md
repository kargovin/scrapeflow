# Phase 2 — Production Readiness Review

> **Reviewed:** 2026-04-14
> **Reviewer:** Tech Lead
> **Verdict:** Not ready to ship. Two security bugs and two reliability bugs must be fixed before production deployment. All other items are optional hardening.

---

## Summary

| Severity | Count | Must fix before prod? |
|----------|-------|-----------------------|
| CRITICAL | 2 | Yes |
| HIGH | 2 | Yes |
| MEDIUM | 5 | Recommended |
| LOW | 3 | Optional / Phase 3 |

**Test coverage:** Excellent — all 26 Phase 2 steps have test coverage. No gaps.

---

## CRITICAL — Must fix before any production deployment

### C1: Fernet key encoding inconsistency

**Risk:** Encryption/decryption mismatch — LLM keys encrypted by one code path may be unreadable by another.

`main.py:65` encodes the key to bytes before passing to Fernet:
```python
fernet = Fernet(settings.llm_key_encryption_key.encode())
```

But `api/app/routers/users.py:102`, `api/app/routers/jobs.py:125,337,389`, and `api/app/core/encryption.py:9` do NOT:
```python
f = Fernet(settings.llm_key_encryption_key)  # string, not bytes
```

`llm-worker/worker/llm.py:17` uses `.encode()` — matches `main.py` but not the API routes.

**Fix:** Standardize all call sites. Fernet expects bytes. Either always call `.encode()` at the call site, or store the key as bytes in settings. The unused `get_fernet()` helper in `api/app/core/encryption.py` could be the single canonical factory — use it everywhere.

---

### C2: Missing LLM key ownership check in PATCH /jobs

**Risk:** User A can patch their job to reference User B's LLM key (if they know the UUID), potentially triggering LLM calls billed to User B's API key.

`api/app/routers/jobs.py:342-351` — PATCH handler accepts `llm_config.llm_key_id` but does not verify the key belongs to the authenticated user.

Compare to `POST /jobs` (`jobs.py:103-111`) which correctly does:
```python
user_llm_key = await db.get(UserLLMKey, key_id)
if user_llm_key.user_id != user.id:
    raise HTTPException(403)
```

**Fix:** Add the same ownership check in the PATCH handler when `llm_config` is being set.

---

## HIGH — Fix before production deployment

### H1: Go HTTP worker busy-loops on NATS disconnect

**File:** `http-worker/internal/worker/worker.go:111-114`

```go
msgs, err := sub.Fetch(available, nats.MaxWait(5*time.Second))
if err != nil {
    continue  // ← no sleep; spins at 100% CPU when NATS is down
}
```

**Fix:** Add a `time.Sleep` (e.g. 2–5s, or exponential backoff up to 30s) before `continue` on fetch errors.

---

### H2: Webhook delivery loop busy-loops on DB error

**File:** `api/app/core/webhook_loop.py:40-65`

The outer exception handler catches DB query failures and immediately re-enters the loop with no sleep. If Postgres is down, the loop burns 100% CPU.

**Fix:** Add `await asyncio.sleep(backoff)` (e.g. starting at 2s, capped at 60s) in the exception handler before continuing.

---

## MEDIUM — Recommended before production

### M1: All four Dockerfiles run as root

None of the four Dockerfiles (`api/Dockerfile`, `http-worker/Dockerfile`, `playwright-worker/Dockerfile`, `llm-worker/Dockerfile`) have a `USER` directive. All containers run as root.

**Fix:** Add a non-root user to each:
```dockerfile
RUN adduser -D -u 1000 appuser
USER appuser
```

For the Go worker (Alpine), use `adduser -S -u 1000 appuser`.

---

### M2: Worker Dockerfiles ship dev dependencies to production

`playwright-worker/Dockerfile:10` and `llm-worker/Dockerfile:9` copy the `tests/` directory into the image. Both use `pip install -e .` (editable/dev install).

Neither is a multi-stage build.

**Fix:** Make both multi-stage. Install only runtime dependencies in the final stage. Drop `tests/` from the final image.

---

### M3: docker-compose.yml hardcodes Postgres and MinIO credentials

`docker/docker-compose.yml:8` (`POSTGRES_PASSWORD: scrapeflow`) and lines 51-52 (MinIO) are hardcoded in the compose file. The API service uses `env_file`, but the infrastructure services do not.

**Fix:** Move credentials to `.env` and reference them via `${VAR}` substitution in docker-compose.yml.

---

### M4: Python workers have weak fixed backoff on NATS fetch errors

`playwright-worker/worker/main.py:217-224` and `llm-worker/worker/main.py:92-108` sleep a fixed 1 second on any fetch exception. If NATS is down for minutes, they retry every second.

**Fix:** Implement simple exponential backoff (2s → 4s → 8s → … capped at 60s), reset on success.

---

### M5: Scheduler NATS publishes are not individually caught

`api/app/core/scheduler.py:84,86,131,133` — NATS publish calls inside the scheduler loop are covered only by the outer `except Exception` at line 40-41, which just logs and continues. If a publish fails after the DB commit, the `job_runs` row exists but no NATS message was sent. The stale-pending recovery will catch this after 10 minutes, but the window is silent.

**Fix:** Wrap each publish in its own try/except with a specific log message identifying it as a publish failure (distinct from a DB failure), so it's visible in logs before the 10-minute recovery fires.

---

## LOW — Defer to Phase 3 / operational setup

### L1: No healthchecks for worker services in docker-compose.yml

`playwright-worker`, `llm-worker`, and `http-worker` have no `healthcheck` sections. Infrastructure services (postgres, redis, nats, minio) all have them.

Workers don't expose HTTP so a healthcheck requires a custom probe (e.g. a pid file or a lightweight `/health` endpoint). Deferred to Phase 3 k8s work where liveness probes are defined in the manifest.

---

### L2: No resource limits in docker-compose.yml

No `deploy.resources.limits` on any service. Playwright in particular needs an explicit memory ceiling (Chromium uses 400–600 MB per instance).

Deferred to Phase 3 k8s manifests where resource requests/limits are set in Deployment specs. For Docker Compose, acceptable for homelab use.

---

### L3: Health endpoints expose service topology

`GET /health/ready` returns connectivity status for all backing services. This is intentional for k8s probes but leaks internal topology to unauthenticated callers.

Deferred to Phase 3 — acceptable on a Traefik-gated homelab deployment where the API is not directly internet-exposed.

---

## Items confirmed OK

- **Auth gates:** All user routes require `get_current_user`; all admin routes require `get_current_admin_user`. No unprotected routes found.
- **SSRF protection:** `validate_no_ssrf()` called at job creation for URL, webhook_url, and llm base_url. Also called in PATCH for webhook_url.
- **HMAC webhook signing:** Implemented correctly in `api/app/core/webhook_loop.py:84-96` — SHA-256 HMAC with decrypted secret, `X-ScrapeFlow-Signature: sha256=<hex>` header.
- **Ack-after-MinIO-write:** All three workers (Go, Playwright, LLM) ack after result is published, not before.
- **FOR UPDATE SKIP LOCKED:** Scheduler (`scheduler.py:57,114`) and webhook loop both use skip-locked for safe multi-instance operation.
- **Result consumer:** Correctly acks cancelled/unknown runs to prevent infinite redelivery.
- **Test coverage:** All Phase 2 routes, admin endpoints, LLM key routes, PATCH /jobs, scheduler, webhook delivery, and MaxDeliver advisory are covered by tests.
- **NATS stream subjects:** docker-compose nats-init uses `scrapeflow.jobs.>` wildcard, correctly covering all Phase 2 subjects.

---

## Recommended fix order

1. **C1** — Fernet encoding (data correctness risk)
2. **C2** — PATCH llm_key_id ownership (security)
3. **H1** — Go worker busy-loop (ops reliability)
4. **H2** — Webhook loop busy-loop (ops reliability)
5. **M1** — Non-root users in Dockerfiles
6. **M2** — Worker Dockerfiles: multi-stage + drop test deps
7. **M3** — docker-compose credential externalization
8. **M4** — Python worker backoff
9. **M5** — Scheduler publish error granularity
