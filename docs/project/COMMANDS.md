# ScrapeFlow - Command Reference

> This file is not committed to git. It's a local reference for commonly used commands.

---

## Docker Compose

> Run all commands from the `docker/` directory.

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

```bash
# Run all tests
docker compose exec api python -m pytest tests/ -v

# Run a specific test file
docker compose exec api python -m pytest tests/test_health.py -v

# Run a specific test
docker compose exec api python -m pytest tests/test_health.py::test_health -v
```

---

## Alembic (Database Migrations)

> Run from the `docker/` directory via `docker compose exec api`.

```bash
# Check current migration state (what's applied to DB)
docker compose exec api alembic current

# Auto-generate a new migration from model changes
docker compose exec api alembic revision --autogenerate -m "describe change here"

# Apply all pending migrations
docker compose exec api alembic upgrade head

# Roll back one migration
docker compose exec api alembic downgrade -1

# Roll back all migrations
docker compose exec api alembic downgrade base

# View migration history
docker compose exec api alembic history

# Upgrade to a specific revision ID
docker compose exec api alembic upgrade <revision_id>
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
docker compose exec api alembic revision --autogenerate -m "describe change"

# 3. Copy migration file from container to host (so it gets committed to git)
docker compose cp api:/app/migrations/versions/. ../api/migrations/versions/

# 4. Review the generated migration file, then apply
docker compose exec api alembic upgrade head

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

# Run integration tests for a specific package
go test -tags integration ./internal/storage/...

# Run integration tests with verbose output
go test -tags integration -v ./...

# Check for compilation errors across all packages (without producing a binary)
go vet ./...

# Tidy dependencies (add missing, remove unused) — also regenerates go.sum
go mod tidy

# Download all dependencies and generate go.sum from scratch
# (run this after cloning or if go.sum is missing)
go mod download
```

---

## Redis (Rate Limiting)

```bash
# Inspect rate limit counter for a user (replace <user_id> and <window>)
# window = epoch_seconds // 60  (e.g. for 60s window)
docker compose exec redis redis-cli GET "scrapeflow:rl:<user_id>:<window>"

# List all rate limit keys
docker compose exec redis redis-cli KEYS "scrapeflow:rl:*"

# Check TTL remaining on a key
docker compose exec redis redis-cli TTL "scrapeflow:rl:<user_id>:<window>"

# Manually clear a user's rate limit (useful in dev/testing)
docker compose exec redis redis-cli DEL "scrapeflow:rl:<user_id>:<window>"
```
