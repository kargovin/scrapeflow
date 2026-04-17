# ScrapeFlow - Build Progress

<details open>
<summary> <h2 style='display:inline'> Phase 1 — MVP (API layer) </h3> </summary>

| Step | Description                                                                                                                                                                                        | Status  |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| 1    | Project scaffold — directory structure, pyproject.toml, .env.example, .gitignore                                                                                                                   | ✅ Done |
| 2    | Docker Compose — Postgres, Redis, NATS JetStream, MinIO, FastAPI container                                                                                                                         | ✅ Done |
| 3    | FastAPI skeleton — SQLAlchemy async, Alembic setup, Redis/MinIO/NATS clients wired up                                                                                                              | ✅ Done |
|      | 3a. SQLAlchemy async engine + session factory + `get_db` dependency                                                                                                                                | ✅ Done |
|      | 3b. Alembic init — `alembic.ini`, `env.py` wired to async engine                                                                                                                                   | ✅ Done |
|      | 3c. Redis connection pool + `get_redis` dependency                                                                                                                                                 | ✅ Done |
|      | 3d. MinIO client + bucket auto-create on startup + `get_minio` dependency                                                                                                                          | ✅ Done |
|      | 3e. NATS + JetStream connection on startup, graceful shutdown                                                                                                                                      | ✅ Done |
|      | 3f. Wire all clients into `lifespan` in `main.py`                                                                                                                                                  | ✅ Done |
|      | 3g. Test setup (`conftest.py`) + tests for health, DB, Redis, MinIO, NATS                                                                                                                          | ✅ Done |
| 4    | Database schema + migrations — `users`, `api_keys`, `jobs` tables                                                                                                                                  | ✅ Done |
|      | 4a. `User` model — id, clerk_id, email, created_at                                                                                                                                                 | ✅ Done |
|      | 4b. `ApiKey` model — id, user_id (FK), key_hash, name, created_at, last_used_at, revoked                                                                                                           | ✅ Done |
|      | 4c. `Job` model — id, user_id (FK), url, status, output_format, result_path, created_at, updated_at                                                                                                | ✅ Done |
|      | 4d. Generate + apply Alembic migration                                                                                                                                                             | ✅ Done |
| 5    | Clerk auth middleware — JWT verification, user sync to local DB, API key auth                                                                                                                      | ✅ Done |
|      | 5a. Clerk JWT verification middleware                                                                                                                                                              | ✅ Done |
|      | 5b. User sync — upsert Clerk user into local `users` table on first login                                                                                                                          | ✅ Done |
|      | 5c. `get_current_user` dependency — extracts verified user for route handlers                                                                                                                      | ✅ Done |
|      | 5d. API key auth — generate/hash keys, verify as alternative to JWT                                                                                                                                | ✅ Done |
|      | 5e. `/me` endpoint + end-to-end auth tests (unauthenticated 401, authenticated 200, user created in DB)                                                                                            | ✅ Done |
| 6    | Job CRUD API — `POST /jobs`, `GET /jobs/{id}`, `GET /jobs`, `DELETE /jobs/{id}`                                                                                                                    | ✅ Done |
|      | 6a. Pydantic schemas — `JobCreate` (request), `JobResponse` (response)                                                                                                                             | ✅ Done |
|      | 6b. `POST /jobs` — insert row with `status=pending`, publish fat message `{job_id, url, output_format}` to `scrapeflow.jobs.run`                                                                   | ✅ Done |
|      | 6c. `GET /jobs/{id}` — fetch single job with ownership check (404 if not found or wrong user)                                                                                                      | ✅ Done |
|      | 6d. `GET /jobs` — list jobs for current user with `limit`/`offset` pagination (default limit=50, max=200)                                                                                          | ✅ Done |
|      | 6e. `DELETE /jobs/{id}` — set `status=cancelled` (ownership check; no-op if already terminal state)                                                                                                | ✅ Done |
|      | 6f. Wire `jobs` router into `main.py`                                                                                                                                                              | ✅ Done |
|      | 6g. NATS result consumer — background task in `lifespan`; subscribes to `scrapeflow.jobs.result`, updates job `status`/`result_path`/`error` in DB; discards results for `status=cancelled` jobs   | ✅ Done |
|      | 6h. Tests — create job, get job (own/other user returns 404), list jobs (pagination), cancel job, unauthenticated 401, result consumer updates DB correctly                                        | ✅ Done |
| 7    | Rate limiting — Redis-backed per-user quotas                                                                                                                                                       | ✅ Done |
|      | 7a. Rate limit config in `settings.py` — requests per window, window seconds                                                                                                                       | ✅ Done |
|      | 7b. Redis-backed rate limiter utility — fixed window counter (`INCR` + `EXPIRE`) per user                                                                                                          | ✅ Done |
|      | 7c. Wire as FastAPI dependency on `POST /jobs` — returns `429 Too Many Requests` when exceeded                                                                                                     | ✅ Done |
|      | 7d. Tests — under limit passes, at limit passes, over limit returns 429, independent counters per user                                                                                             | ✅ Done |
| 8    | Go HTTP scraper worker — consumes `scrapeflow.jobs.run`, fetches URL, writes result to MinIO, publishes `{job_id, minio_path, status, error?}` to `scrapeflow.jobs.result`, acks after MinIO write | ✅ Done |
|      | 8a. NATS stream init — Docker Compose init container (`natsio/nats-box`) creates `SCRAPEFLOW` stream bPhaseefore API starts                                                                        | ✅ Done |
|      | 8b. Go module scaffold — `go.mod`, `go.sum`, directory layout (`cmd/worker/`, `internal/`)                                                                                                         | ✅ Done |
|      | 8c. Config — read NATS URL, MinIO endpoint/credentials, bucket name from env vars into a `Config` struct                                                                                           | ✅ Done |
|      | 8d. NATS consumer — connect to JetStream, pull-subscribe to `scrapeflow.jobs.run`, parse `{job_id, url, output_format}` message                                                                    | ✅ Done |
|      | 8e. HTTP fetcher — fetch URL with timeout, return raw HTML bytes and final URL (after redirects)                                                                                                   | ✅ Done |
|      | 8f. Output formatter — convert raw HTML → Markdown (html-to-markdown) or JSON `{url, title, text}`; raw HTML is pass-through                                                                       | ✅ Done |
|      | 8g. MinIO writer — upload result bytes to `scrapeflow-results/{job_id}.{ext}`, return the object path                                                                                              | ✅ Done |
|      | 8h. Result publisher — publish `{job_id, status, minio_path}` or `{job_id, status, error}` to `scrapeflow.jobs.result`; ack NATS message only after MinIO write succeeds                           | ✅ Done |
|      | 8i. Wire it all — `main.go` ties config → NATS → dispatch loop → fetch → format → upload → publish → ack                                                                                           | ✅ Done |
|      | 8j. Dockerfile + Docker Compose — multi-stage Go build, add `worker` service depending on `nats-init` and `minio`                                                                                  | ✅ Done |
| 9    | API key management routes + dev tooling                                                                                                                                                            | ✅ Done |
|      | 9a. `POST /users/api-keys` — generate `sf_...` key, store hash, return raw key once                                                                                                                | ✅ Done |
|      | 9b. `GET /users/api-keys` — list active (non-revoked) keys for current user                                                                                                                        | ✅ Done |
|      | 9c. `DELETE /users/api-keys/{id}` — revoke key (sets `revoked=True`; 404 for missing or cross-user)                                                                                                | ✅ Done |
|      | 9d. `scripts/dev_token.sh` — `--api-key sf_...` or `--clerk sk_test_...` modes for local API testing                                                                                               | ✅ Done |
|      | 9e. Fix Clerk JWT `authorized_parties=None` — was `[]` which blocked all non-browser JWTs                                                                                                          | ✅ Done |

</details>

<details open>
<summary> <h2 style='display:inline'> Phase 2 — Core features </h2> </summary>

> Full task breakdown: `docs/project/PHASE2_BACKLOG.md` (26 steps)
> Engineering spec: `docs/phase2/phase2-engineering-spec-v3.md`

| Step | Description                                                           | Status  |
| ---- | --------------------------------------------------------------------- | ------- |
| 1    | Refactor `_validate_no_ssrf()` to `core/security.py`                  | ✅ Done |
| 2    | Add `get_current_admin_user` dependency                               | ✅ Done |
| 3    | Fernet encryption setup in settings + dependencies                    | ✅ Done |
| 4    | Migration 2.1: Add `is_admin` to `users`                              | ✅ Done |
| 5    | Migration 2.2: Add Phase 2 fields to `jobs`                           | ✅ Done |
| 6    | Migration 2.3: Create `job_runs` table + data migration               | ✅ Done |
| 7    | Migration 2.5: Create `user_llm_keys` table                           | ✅ Done |
| 8    | Migration 2.6: Add `processing` status to `JobStatus` enum            | ✅ Done |
| 9    | Migrations 2.7 + 2.8: `webhook_deliveries` + `nats_stream_seq`        | ✅ Done |
| 10   | Update `POST /jobs` for Phase 2                                       | ✅ Done |
| 11   | Update `GET /jobs`, `GET /jobs/{id}`, `DELETE /jobs/{id}` for Phase 2 | ✅ Done |
| 12   | Migration 2.4: Drop run-state columns from `jobs` ⚠ requires ADR-003  | ✅ Done |
| 13   | Update NATS constants + docker-compose nats-init                      | ✅ Done |
| 14   | Update Go HTTP worker for Phase 2                                     | ✅ Done |
| 15   | Update result consumer for Phase 2                                    | ✅ Done |
| 16   | New job routes: PATCH, GET runs, webhook-secret rotate                | ✅ Done |
| 17   | LLM key management routes                                             | ✅ Done |
| 18   | Python Playwright worker (new service)                                | ✅ Done |
| 19   | Python LLM worker (new service)                                       | ✅ Done |
| 20   | Scheduler loop background task                                        | ✅ Done |
| 21   | Webhook delivery loop background task                                 | ✅ Done |
| 22   | MaxDeliver advisory subscriber                                        | ✅ Done |
| 23   | Admin panel API routes                                                | ✅ Done |
| 24   | Admin stats endpoint                                                  | ✅ Done |
| 25   | `scripts/cleanup_old_runs.py`                                         | ✅ Done |
| 26   | Docker Compose: add Playwright + LLM worker services                  | ✅ Done |

</details>

<details open>
<summary> <h2 style='display:inline'>
Phase 3 — Production hardening
</h2></summary>

> Full task breakdown: `docs/project/PHASE3_BACKLOG.md` (28 steps)
> Engineering spec: `docs/phase3/phase3-engineering-spec.md`

| Step | Description                                                                                  | Status  |
| ---- | -------------------------------------------------------------------------------------------- | ------- |
| 1    | K8s manifests: playwright-worker, llm-worker, cleanup CronJob (PRD-001)                     | ✅ Done |
| 2    | Sliding window rate limiter (PRD-002)                                                        | ✅ Done |
| 3    | SSRF re-validation on webhook delivery (PRD-003)                                             | ✅ Done |
| 4    | Migration 3.1: `jobs.respect_robots`                                                         | ✅ Done |
| 5    | Migration 3.2: `jobs.proxy_provider`                                                         | ✅ Done |
| 6    | Migration 3.3: `jobs.playwright_actions`, `webhook_url TEXT`, `webhook_events`               | ✅ Done |
| 7    | Migration 3.4: `job_secrets` table + `job_secret_type` ENUM                                 | ✅ Done |
| 8    | Migration 3.5: `batches` + `batch_items` tables                                              | ✅ Done |
| 9    | Migration 3.6: `job_runs` nullable `job_id`, `batch_item_id`, check constraint, `content_hash` ⚠ | ✅ Done |
| 10   | Migration 3.7: `crawls`, `crawl_pages`, `crawl_queue` tables                                | ✅ Done |
| 11   | Migration 3.8: `user_quotas` table                                                           | ✅ Done |
| 12   | Migration 3.9: `jobs.updated_at` DB trigger ⚠ hand-written                                  | ⬜ Todo |
| 13   | Migration 3.10: `api_keys (user_id, name)` uniqueness constraint                            | ⬜ Todo |
| 14   | Go HTTP worker: schema_version 2 struct + proxy routing + robots.txt                        | ⬜ Todo |
| 15   | Playwright worker: schema_version 2 + proxy + cookies + actions + robots.txt                | ⬜ Todo |
| 16   | PRD-004: robots.txt — API integration                                                        | ⬜ Todo |
| 17   | PRD-005: proxy rotation — API integration                                                    | ⬜ Todo |
| 18   | PRD-008: authenticated scraping — API integration                                            | ⬜ Todo |
| 19   | PRD-009: page actions — API integration                                                      | ⬜ Todo |
| 20   | PRD-013: webhook event filter                                                                | ⬜ Todo |
| 21   | PRD-006: batch scraping — API + result consumer                                              | ⬜ Todo |
| 22   | PRD-007: site crawl — API routes                                                             | ⬜ Todo |
| 23   | PRD-007: coordinator service + Docker Compose                                                | ⬜ Todo |
| 24   | PRD-010: MCP server                                                                          | ⬜ Todo |
| 25   | PRD-012: billing/quotas — enforcement + admin endpoint                                       | ⬜ Todo |
| 26   | PRD-014: WebSocket real-time job tracking                                                    | ⬜ Todo |
| 27   | PRD-015: content deduplication                                                               | ⬜ Todo |
| 28   | PRD-011: Admin SPA + CI build + permanent delete                                             | ⬜ Todo |

</details>

<details> <summary> <h2 style='display:inline'>
Gotchas
</h2></summary>

- SQLAlchemy async does **not** support lazy loading. Always use `selectinload()` or `joinedload()` when a query needs to traverse a relationship.
- NATS result consumer (`app/core/result_consumer.py`) creates its own DB sessions via `AsyncSessionLocal` directly — it cannot use the `get_db()` FastAPI dependency since it runs outside the request/response cycle.
- NATS subject names and stream name live in `app/constants.py`, **not** `settings.py` — they are part of the worker contract (ADR-001) and must not vary between environments.
- Static routes must be registered **before** parameterized routes in the same router (e.g. `GET /jobs` before `GET /jobs/{job_id}`) or the parameterized route will swallow requests meant for the static one.
- The `nats:2.10-alpine` image contains only `nats-server` — it does not include the `nats` CLI. Use `natsio/nats-box` for the init container.
- Shared pytest fixtures (e.g. `mock_clerk_auth`, `db_user`) must live in `conftest.py` to be visible across test files. Fixtures defined in a regular test file are only available within that file.
- Clerk JWT `authorized_parties=[]` is **not** the same as `None` — an empty list causes the SDK to reject all tokens (including Clerk dashboard-issued ones). Use `None` in dev to skip the check; set to explicit domain list in production.
- Go worker does not need Postgres — only NATS + MinIO. If you see a Postgres dependency in the worker, something is wrong architecturally.
- Go Dockerfile must copy both `go.mod` and `go.sum` before `go mod download` to get proper layer caching. Copying only `go.mod` causes full re-download on every code change.
- Bot-protected sites (Amazon, Cloudflare-backed) will return 503/CAPTCHA pages to the plain HTTP worker — this is expected behaviour, not a bug. Playwright worker (Phase 2) addresses this.
- Go worker Alpine runtime image needs `RUN apk add --no-cache ca-certificates` — Alpine ships without CA certificates, so all HTTPS fetches fail with "x509: certificate signed by unknown authority" without it.
- NATS durable consumer filter subjects are stored persistently on disk. If the worker subject changes (e.g. Phase 1 `scrapeflow.jobs.run` → Phase 2 `scrapeflow.jobs.run.http`), the old consumer must be manually deleted (`nats consumer delete SCRAPEFLOW <name>`) so the worker can recreate it with the correct filter. PullSubscribe does not update an existing consumer's config.
</details>

<details>
<summary>
<h2 style='display:inline'>Notes</h2>
</summary>

- Auth: Clerk (OAuth + JWT)
- Local dev: Docker Compose
- Production: k3s homelab, FluxCD GitOps, infra repo at `govindappa-k8s-config`
- Domain: scrapeflow.govindappa.com

</details>
