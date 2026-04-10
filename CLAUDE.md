# ScrapeFlow - Apify Clone

> **Status: Phase 2 — in progress. Phase 1 MVP is complete. Phase 2 core features are actively being built.**

## Goal

A self-hosted, multi-tenant web scraping platform. Primary use case: structured data extraction and change detection to feed ML/data pipelines. Built as a production-grade portfolio project.

---

## Architecture

### Core stack
- **API**: FastAPI (Python)
- **Workers**: Go (background scrape workers)
- **Queue**: NATS JetStream
- **DB**: PostgreSQL (metadata), MinIO (object storage / raw output)
- **Cache / rate limiting**: Redis
- **Gateway**: Traefik
- **Auth**: Clerk (OAuth — Google, GitHub; JWT issued by Clerk, verified by API)
- **[LATER] MCP server**: LLM-callable interface (scrape_url, get_result, list_jobs)

### Worker contract
See `docs/adr/README.md` for the full ADR index and current status of each decision record.
- **ADR-001** — Phase 1 worker contract (partially superseded)
- **ADR-002** — Phase 2 worker contract (current authoritative reference for NATS subjects, message schemas, MinIO path convention)

When ADR-001 and ADR-002 conflict, **ADR-002 takes precedence**.

### Deployment
- **Local dev**: Docker Compose (Postgres, Redis, NATS, MinIO)
- **Production**: k3s homelab — namespace `scrapeflow`, domain `scrapeflow.govindappa.com`
  - Traefik ingress, ExternalDNS (Cloudflare), cert-manager (letsencrypt-prod)
  - GitOps via FluxCD — infra repo at `/home/karthik/Documents/govindappa/govindappa-k8s-config`

---

## Components

### Phase 1 — MVP (building now)
- **Auth**: Clerk OAuth login/signup, JWT verification middleware, user sync to local DB, API key management
- **Job CRUD**: create scrape job (URL + options), get status, list jobs, cancel job
- **HTTP scraper worker**: plain HTTP requests, returns raw HTML / cleaned Markdown / JSON
- **Output storage**: raw results stored in MinIO, metadata in Postgres
- **Rate limiting**: Redis-backed per-user quotas
- **Docker Compose**: full local dev stack

### Phase 2 — Core features [IN PROGRESS]
- **Playwright worker**: opt-in JS rendering for dynamic/SPA sites, configurable per job
- **LLM processing**: user provides their own Anthropic/OpenAI API key + output schema; worker extracts structured data
- **Change detection**: recurring/scheduled jobs, diff detection, notify on change
- **Webhook delivery**: configurable webhooks with exponential backoff retry
- **Admin panel API**: manage users, view all jobs, usage stats

### Phase 3 — Production hardening [LATER]
- **Proxy rotation**: pluggable proxy provider config (Bright Data, Oxylabs, etc.)
- **robots.txt compliance**: respect/ignore toggle per job
- **Billing/quotas**: per-user job limits, usage tracking
- **Admin SPA**: React dashboard for user and job management
- **MCP server**: expose scrape_url, get_result, list_jobs as LLM-callable tools
- **K8s manifests**: production deployment manifests for k3s, added to infra repo

### Phase 3 — Build Process
Phase 3 simulates how a larger engineering organization works by dividing the build process across distinct Claude personas. Each persona owns a specific part of the process and produces defined outputs before handing off to the next.

| Persona | Responsibilities | Outputs |
|---------|-----------------|---------|
| **Program Manager** | Defines scope, priorities, success criteria, and stakeholder requirements for each feature | PRD per feature, prioritized backlog |
| **Software Architect** | Translates PRDs into technical design decisions, system contracts, and ADRs | Design docs, ADRs, updated engineering spec |
| **Tech Lead** | Breaks the engineering spec into an ordered implementation backlog with dependencies and sequencing | Task breakdown, sprint plan, dependency graph |
| **Engineer(s)** | Implements tasks from the backlog, writes tests, raises blockers to Tech Lead | Code, tests, implementation notes |

Each persona operates with only the outputs from the persona before them — the Engineer does not read the PRD; the Architect does not second-guess the PM's priorities. This mirrors how information flows in real organizations and surfaces the communication gaps between roles.

---

## Key decisions

| Concern | Decision | Rationale |
|---|---|---|
| Auth provider | Clerk | Handles OAuth, JWT, user mgmt out of the box |
| Tenancy | Multi-tenant | Each user has isolated jobs/data |
| Scraping engine | HTTP first, Playwright opt-in later | Most structured data sites are server-rendered |
| LLM output | User provides own API key + schema | Avoids shared LLM cost; users control their models |
| Proxy rotation | Skip for MVP | Low volume personal use; add as pluggable provider later |
| Change detection | Yes, Phase 2 | Key feature for ML data pipeline use cases |
| Output formats | Raw HTML, cleaned Markdown, JSON | Feed directly into ML pipelines |
| Worker design | Light worker — NATS + MinIO only, no DB access | Keeps worker DB-ignorant; all business logic in API |
| Job dispatch message | Fat message `{job_id, url, output_format}` | Worker needs no DB lookup to execute the scrape |
| Worker→API result | Worker publishes to `scrapeflow.jobs.result`; API background consumer updates DB | Decoupled; worker never touches Postgres |
| Cancellation | API sets `status=cancelled`; result consumer discards worker results for cancelled jobs | Worker is unaware of cancellations; API enforces correctness |
| NATS stream creation | Outside API/worker (init container / infra); API asserts stream exists at startup | API has no infra concerns |
| Cross-tenant access | 404 (not 403) for jobs belonging to other users | 403 leaks resource existence; 404 is safer for multi-tenant |
| NATS subject constants | `app/constants.py` (not `settings.py`) | Subject names are part of the worker contract, not env-configurable |
| Rate limiting | Fixed window counter (Redis `INCR` + `EXPIRE`) per user; sliding window planned for Phase 2 | Simple, 2–3 Redis ops; adequate for MVP quotas |
| Cancellation (Phase 2) | Cancel active `job_runs` rows (not `jobs.status`); result consumer discards by checking `run.status == "cancelled"` | `jobs` no longer has a `status` column after migration 2.4 |
| MinIO path convention | Dual write: `latest/{job_id}.{ext}` (overwritten) + `history/{job_id}/{unix_ts}.{ext}` (immutable); `job_runs.result_path` always stores the `history/` path | history path enables per-run diff; latest path for convenience access |
| Worker routing (Phase 2) | Subject-based: `scrapeflow.jobs.run.http` for Go worker, `scrapeflow.jobs.run.playwright` for Playwright worker | Workers subscribe to their own subject; wrong engine never receives the message |
| `nats_stream_seq` | Stored on `job_runs` from the worker's "running" result message | MaxDeliver advisory carries only stream seq — used to identify stalled runs (Step 22) |

---

## MVP definition

> "Submit a URL via API → get back raw or cleaned data (HTML/Markdown/JSON) → check job status → usable in an ML pipeline"
