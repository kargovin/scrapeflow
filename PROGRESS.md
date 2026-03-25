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
| 5 | Clerk auth middleware — JWT verification, user sync to local DB, API key auth | 🔜 Next |
| 6 | Job CRUD API — `POST /jobs`, `GET /jobs/{id}`, `GET /jobs`, `DELETE /jobs/{id}` | ⏳ Pending |
| 7 | Rate limiting — Redis-backed per-user quotas | ⏳ Pending |
| 8 | Go HTTP scraper worker — reads from NATS, fetches URL, stores result in MinIO, updates job status | ⏳ Pending |

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

## Notes
- Auth: Clerk (OAuth + JWT)
- Local dev: Docker Compose
- Production: k3s homelab, FluxCD GitOps, infra repo at `govindappa-k8s-config`
- Domain: scrapeflow.govindappa.com
