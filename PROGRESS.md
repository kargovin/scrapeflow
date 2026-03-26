# ScrapeFlow - Build Progress

## Phase 1 — MVP (API layer)

| Step | Description | Status |
|------|-------------|--------|
| 1 | Project scaffold — directory structure, pyproject.toml, .env.example, .gitignore | ✅ Done |
| 2 | Docker Compose — Postgres, Redis, NATS JetStream, MinIO, FastAPI container | ✅ Done |
| 3 | FastAPI skeleton — SQLAlchemy async, Alembic setup, Redis/MinIO/NATS clients wired up | ✅ Done |
|   | 3a. SQLAlchemy async engine + session factory + `get_db` dependency | ✅ Done |
|   | 3b. Alembic init — `alembic.ini`, `env.py` wired to async engine | ✅ Done |
|   | 3c. Redis connection pool + `get_redis` dependency | ✅ Done |
|   | 3d. MinIO client + bucket auto-create on startup + `get_minio` dependency | ✅ Done |
|   | 3e. NATS + JetStream connection on startup, graceful shutdown | ✅ Done |
|   | 3f. Wire all clients into `lifespan` in `main.py` | ✅ Done |
|   | 3g. Test setup (`conftest.py`) + tests for health, DB, Redis, MinIO, NATS | ✅ Done |
| 4 | Database schema + migrations — `users`, `api_keys`, `jobs` tables | ✅ Done |
|   | 4a. `User` model — id, clerk_id, email, created_at | ✅ Done |
|   | 4b. `ApiKey` model — id, user_id (FK), key_hash, name, created_at, last_used_at, revoked | ✅ Done |
|   | 4c. `Job` model — id, user_id (FK), url, status, output_format, result_path, created_at, updated_at | ✅ Done |
|   | 4d. Generate + apply Alembic migration | ✅ Done |
| 5 | Clerk auth middleware — JWT verification, user sync to local DB, API key auth | ✅ Done |
|   | 5a. Clerk JWT verification middleware | ✅ Done |
|   | 5b. User sync — upsert Clerk user into local `users` table on first login | ✅ Done |
|   | 5c. `get_current_user` dependency — extracts verified user for route handlers | ✅ Done |
|   | 5d. API key auth — generate/hash keys, verify as alternative to JWT | ✅ Done |
|   | 5e. `/me` endpoint + end-to-end auth tests (unauthenticated 401, authenticated 200, user created in DB) | ✅ Done |
| 6 | Job CRUD API — `POST /jobs`, `GET /jobs/{id}`, `GET /jobs`, `DELETE /jobs/{id}` | ✅ Done |
|   | 6a. Pydantic schemas — `JobCreate` (request), `JobResponse` (response) | ✅ Done |
|   | 6b. `POST /jobs` — insert row with `status=pending`, publish fat message `{job_id, url, output_format}` to `scrapeflow.jobs.run` | ✅ Done |
|   | 6c. `GET /jobs/{id}` — fetch single job with ownership check (404 if not found or wrong user) | ✅ Done |
|   | 6d. `GET /jobs` — list jobs for current user with `limit`/`offset` pagination (default limit=50, max=200) | ✅ Done |
|   | 6e. `DELETE /jobs/{id}` — set `status=cancelled` (ownership check; no-op if already terminal state) | ✅ Done |
|   | 6f. Wire `jobs` router into `main.py` | ✅ Done |
|   | 6g. NATS result consumer — background task in `lifespan`; subscribes to `scrapeflow.jobs.result`, updates job `status`/`result_path`/`error` in DB; discards results for `status=cancelled` jobs | ✅ Done |
|   | 6h. Tests — create job, get job (own/other user returns 404), list jobs (pagination), cancel job, unauthenticated 401, result consumer updates DB correctly | ✅ Done |
| 7 | Rate limiting — Redis-backed per-user quotas | ✅ Done |
|   | 7a. Rate limit config in `settings.py` — requests per window, window seconds | ✅ Done |
|   | 7b. Redis-backed rate limiter utility — fixed window counter (`INCR` + `EXPIRE`) per user | ✅ Done |
|   | 7c. Wire as FastAPI dependency on `POST /jobs` — returns `429 Too Many Requests` when exceeded | ✅ Done |
|   | 7d. Tests — under limit passes, at limit passes, over limit returns 429, independent counters per user | ✅ Done |
| 8 | Go HTTP scraper worker — consumes `scrapeflow.jobs.run`, fetches URL, writes result to MinIO, publishes `{job_id, minio_path, status, error?}` to `scrapeflow.jobs.result`, acks after MinIO write | 🔜 Next |
|   | 8a. NATS stream init — Docker Compose init container (`natsio/nats-box`) creates `SCRAPEFLOW` stream before API starts | ✅ Done |

## Phase 2 — Core features [LATER]
- Playwright worker (opt-in JS rendering per job)
- LLM processing (user provides own API key + output schema)
- Change detection (recurring jobs, diff on result)
- Webhook delivery (exponential backoff retry)
- Admin panel API

## Phase 3 — Production hardening [LATER]
- Proxy rotation (pluggable provider config)
- robots.txt compliance toggle
- Billing/quotas
- Admin SPA (React)
- MCP server (scrape_url, get_result, list_jobs)
- K8s manifests for k3s (namespace: scrapeflow, scrapeflow.govindappa.com)

## Gotchas
- SQLAlchemy async does **not** support lazy loading. Always use `selectinload()` or `joinedload()` when a query needs to traverse a relationship.
- NATS result consumer (`app/core/result_consumer.py`) creates its own DB sessions via `AsyncSessionLocal` directly — it cannot use the `get_db()` FastAPI dependency since it runs outside the request/response cycle.
- NATS subject names and stream name live in `app/constants.py`, **not** `settings.py` — they are part of the worker contract (ADR-001) and must not vary between environments.
- Static routes must be registered **before** parameterized routes in the same router (e.g. `GET /jobs` before `GET /jobs/{job_id}`) or the parameterized route will swallow requests meant for the static one.
- The `nats:2.10-alpine` image contains only `nats-server` — it does not include the `nats` CLI. Use `natsio/nats-box` for the init container.
- Shared pytest fixtures (e.g. `mock_clerk_auth`, `db_user`) must live in `conftest.py` to be visible across test files. Fixtures defined in a regular test file are only available within that file.

## Notes
- Auth: Clerk (OAuth + JWT)
- Local dev: Docker Compose
- Production: k3s homelab, FluxCD GitOps, infra repo at `govindappa-k8s-config`
- Domain: scrapeflow.govindappa.com
