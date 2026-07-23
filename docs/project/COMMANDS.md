# ScrapeFlow - Command Reference

> Run all Docker Compose commands from the `docker/` directory.

---

## Docker Compose

```bash
# Start all services (detached)
docker compose up -d

# Start and rebuild images
docker compose up -d --build

# Rebuild and restart a single service
docker compose up -d --build api

# Stop all services (keeps containers and volumes)
docker compose stop

# Stop and remove containers (keeps volumes/data)
docker compose down

# Stop and remove containers + volumes (wipes all data)
docker compose down -v

# Restart a single service
docker compose restart api

# See status of all containers
docker compose ps

# Resource usage (CPU/mem)
docker compose stats
```

---

## Docker Logs

```bash
# Logs for all services
docker compose logs

# Follow logs for a specific service
docker compose logs -f api

# Last 50 lines
docker compose logs --tail=50 api
```

---

## Docker Exec

```bash
# Shell into the API container
docker compose exec api bash

# Postgres shell
docker compose exec postgres psql -U scrapeflow -d scrapeflow

# Redis CLI
docker compose exec redis redis-cli ping
docker compose exec redis redis-cli
```

---

## Tests

> The API container uses `uv` to manage its virtualenv — always prefix with `uv run`.

```bash
# Run all tests
docker compose exec api uv run pytest tests/ -v

# Run a specific test file
docker compose exec api uv run pytest tests/test_health.py -v

# Run a specific test
docker compose exec api uv run pytest tests/test_health.py::test_health -v
```

---

## Alembic (Database Migrations)

> Migrations auto-run on API startup (`main.py`). Manual commands are useful for dev and rollbacks.

```bash
# Check current migration state (what's applied to DB)
docker compose exec api uv run alembic current

# Auto-generate a new migration from model changes
docker compose exec api uv run alembic revision --autogenerate -m "describe change here"

# Apply all pending migrations
docker compose exec api uv run alembic upgrade head

# Roll back one migration
docker compose exec api uv run alembic downgrade -1

# Roll back all migrations
docker compose exec api uv run alembic downgrade base

# View migration history
docker compose exec api uv run alembic history

# Upgrade to a specific revision ID
docker compose exec api uv run alembic upgrade <revision_id>
```

---

## Postgres (Quick Queries)

```bash
# Connect to DB
docker compose exec postgres psql -U scrapeflow -d scrapeflow

# Inside psql:
\dt          # list tables
\l           # list databases
\d users     # describe a table
SELECT 1;    # test connection
\q           # quit

# One-liners
docker compose exec postgres psql -U scrapeflow -d scrapeflow -c "SELECT * FROM users;"
docker compose exec postgres psql -U scrapeflow -d scrapeflow -c "\dt"
```

---

## Migration workflow (when models change)

```bash
# 1. Rebuild container to pick up new model files
docker compose up -d --build api

# 2. Generate migration from model changes
docker compose exec api uv run alembic revision --autogenerate -m "describe change"

# 3. Copy migration file from container to host (so it gets committed to git)
docker compose cp api:/app/migrations/versions/. ../api/migrations/versions/

# 4. Review the generated migration file, then apply
docker compose exec api uv run alembic upgrade head

# 5. Verify tables exist
docker compose exec postgres psql -U scrapeflow -d scrapeflow -c "\dt"
```

---

## MinIO

```bash
# Health check
curl http://localhost:9000/minio/health/live

# Console UI (browser)
# http://localhost:9001
# login: scrapeflow / scrapeflow_secret
```

---

## NATS

```bash
# Check JetStream info
curl http://localhost:8222/jsz

# Check server info
curl http://localhost:8222/varz

# Check client port is open (use full path to avoid NordVPN alias)
/bin/nc -z localhost 4222 && echo "NATS is up"

# List all streams
docker compose exec nats nats stream ls --server nats://localhost:4222

# View stream details (SCRAPEFLOW stream)
docker compose exec nats nats stream info SCRAPEFLOW --server nats://localhost:4222

# View consumer details (result consumer)
docker compose exec nats nats consumer info SCRAPEFLOW api-result-consumer --server nats://localhost:4222
```

---

## Health / dependency checks

Two endpoints, deliberately different questions. Both are unauthenticated.

```bash
# Liveness — process is up
curl http://localhost:8000/health

# Serving readiness — DB / Redis / NATS only. This is the k8s readinessProbe:
# a 503 here pulls the pod out of the Service.
curl -i http://localhost:8000/health/ready

# Full dependency report — the above plus MinIO. Diagnostics only; nothing routes
# on it. First thing to check when jobs run but results don't appear.
curl -s http://localhost:8000/health/deps | jq

# In production
curl -s https://scrapeflow.govindappa.com/health/deps | jq
```

MinIO is on `/deps` and **not** on `/ready` on purpose: it is needed to store and
fetch scrape output, not to serve `/jobs`, auth, or the admin panel. Gating the
probe on it would turn a partial outage into a total one — including the dashboard
you would use to diagnose it.

---

## Job API

> Requires auth — pass `X-API-Key: <key>` or `Authorization: Bearer <jwt>` on all requests.

```bash
# Create a job
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sf_<your_key>" \
  -d '{"url": "https://example.com", "output_format": "html"}'

# Get a specific job
curl http://localhost:8000/jobs/<job_id> \
  -H "X-API-Key: sf_<your_key>"

# List jobs (with pagination)
curl "http://localhost:8000/jobs?limit=20&offset=0" \
  -H "X-API-Key: sf_<your_key>"

# Cancel a job
curl -X DELETE http://localhost:8000/jobs/<job_id> \
  -H "X-API-Key: sf_<your_key>"
```

---

## Go HTTP Worker

> Run all commands from the `http-worker/` directory.

```bash
# Build the worker binary
go build ./cmd/worker/

# Run unit tests only (no external services needed)
go test ./...

# Run unit tests with verbose output
go test -v ./...

# Run integration tests (requires Docker Compose services running)
go test -tags integration ./...

# Check for compilation errors across all packages
go vet ./...

# Tidy dependencies
go mod tidy
```

---

## Redis (Rate Limiting)

> Phase 3 uses a sliding-window limiter. Keys are Redis sorted sets at `rate:user:<user_id>`.

```bash
# Inspect a user's rate limit sorted set (member count = requests in current window)
docker compose exec redis redis-cli ZCARD "rate:user:<user_id>"

# List all rate limit keys
docker compose exec redis redis-cli KEYS "rate:user:*"

# View all entries with timestamps (score = epoch ms)
docker compose exec redis redis-cli ZRANGE "rate:user:<user_id>" 0 -1 WITHSCORES

# Manually clear a user's rate limit (useful in dev/testing)
docker compose exec redis redis-cli DEL "rate:user:<user_id>"
```
