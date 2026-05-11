# Tech Lead — ScrapeFlow Onboarding Document

> **Purpose:** Bring a new Tech Lead persona up to speed on project state, work already done, conventions, and what to do next. Read this before doing anything.
> **Last updated:** 2026-04-16
> **Covers:** Role definition, what was accomplished, the full backlog, file map, process conventions, and how to unblock engineers.

---

## 1. Your Role in This Project

The persona chain is:

```
Product Manager → Software Architect → Tech Lead (you) → Engineer(s)
```

**You do not:** Write PRDs (PM), make architectural decisions (Architect), or implement features (Engineer).

**You do:**
- Break architect-approved engineering specs into an ordered, independently-completable task backlog
- Sequence tasks by dependency — flag the steps that block everything else
- Code-review engineer work against the spec for correctness, completeness, and test coverage
- Unblock engineers when they hit ambiguity or bugs
- Maintain the backlog, progress tracker, and ADR index as work lands
- Write ADRs for decisions that arise during implementation (not design — that's the Architect)
- Own the gotchas log and commands reference — keep them current

**You do NOT re-litigate architectural decisions.** If you disagree with something in the spec, raise it to the Architect persona, not to the engineer implementing it.

---

## 2. Project State When This Document Was Last Updated (2026-04-14)

### Completed
- **Phase 1 MVP** — 9 steps, fully implemented and tested (auth, job CRUD, Go HTTP worker, MinIO storage, Redis rate limiting, Clerk auth)
- **Pre-Phase 2 cleanup** — 18 steps, all committed (SSRF protection, atomic rate limiter, `app.state` refactor, correlation IDs, structured logging, multi-stage Dockerfile, NATS stream retention fix, graceful shutdown, etc.)
- **Phase 2 engineering spec** — v3 approved, two rounds of architect review complete, all defects resolved
- **ADR-002** — Phase 2 worker contract extracted from the spec and written as a standalone file
- **ADR-003** — Job/Run Data Model Split — written before Step 12 (Migration 2.4) as required
- **ADR index** (`docs/adr/README.md`) — created with status tracking and supersession protocol
- **ADR-001** — updated with partial supersession markers (inline ⚠ notices at §2, §3, §8)
- **Phase 2 backlog** (`docs/project/PHASE2_BACKLOG.md`) — 26 ordered steps, all complete; deviation notes added
- **`PROGRESS.md`** — Phase 2 tracking table updated (all 26 steps ✅ Done)
- **Phase 2 Steps 1–26** — all implemented and committed:
  - Foundation (Steps 1–3): SSRF refactor, admin user dependency, Fernet setup
  - Migrations (Steps 4–9, 12): all six additive migrations + irreversible run-state column drop
  - API routes (Steps 10–11, 16–17): POST/GET/DELETE/PATCH /jobs updated + LLM key routes
  - NATS + Go worker (Steps 13–14): constants, nats-init, Go worker Phase 2 update
  - Result consumer (Step 15): full Phase 2 rewrite with LLM dispatch, diff, webhook creation
  - Python workers (Steps 18–19): Playwright worker + LLM worker services
  - Background tasks (Steps 20–22): scheduler loop, webhook delivery loop, MaxDeliver advisory subscriber
  - Admin panel (Steps 23–24): admin routes + stats endpoint
  - Cleanup script (Step 25): `scripts/cleanup_old_runs.py`
  - Docker Compose (Step 26): playwright-worker + llm-worker services added

### Completed since last update (2026-04-15)
- **Production readiness review** — `PRODUCTION_REVIEW.md` written; 2 CRITICAL, 2 HIGH, 5 MEDIUM issues identified
- **All CRITICAL + HIGH + MEDIUM fixes applied** — Fernet encoding, PATCH ownership check, Go worker backoff, webhook loop backoff, non-root Dockerfiles, multi-stage worker builds, docker-compose credential externalization, Python worker backoff, scheduler publish error granularity
- **Alembic migrations enabled on startup** — `api/app/main.py` migration block uncommented; production-ready
- **`PRODUCTION_REVIEW.md`** — pre-ship readiness audit at repo root
- **`docs/project/DEVOPS_SPEC.md`** — full k3s/FluxCD deployment spec for the DevOps agent
- **`.github/workflows/build-push.yml`** — GitHub Actions CI: builds and pushes all 4 Docker images to DockerHub on push to `main`; path-filtered so only changed services rebuild
- **`v2.0.0` tag** — cut on `main` after all fixes and CI were in place
- **Initial Docker images pushed** — all 4 images (`scrapeflow-api`, `scrapeflow-http-worker`, `scrapeflow-playwright-worker`, `scrapeflow-llm-worker`) successfully built and pushed to DockerHub (`k4rth/` namespace)
- **Two Dockerfile bugs fixed during CI** — `python3.10-venv` missing in playwright builder stage; UID 1000 conflict in playwright final stage (base image pre-occupies it — use 1001)
- **DevOps deployment complete** — k3s/FluxCD manifests written and applied by DevOps agent; all services running in `scrapeflow` namespace
- **Production E2E verified (2026-04-15)** — full golden path tested against `scrapeflow.govindappa.com`:
  - Clerk JWT auth → user sync to production DB ✅
  - `POST /users/api-keys` ✅
  - `POST /jobs` → NATS dispatch → Go HTTP worker → MinIO write → `completed` in ~258ms ✅
  - `GET /jobs/{id}/runs` + MinIO result content verified ✅
- **Liveness probe bug fixed in gitops repo** — `api.yaml` had `/health/live` (404); corrected to `/health`; applied by FluxCD

### Completed since last update (2026-04-16)
- **Phase 3 backlog** (`docs/project/PHASE3_BACKLOG.md`) — 28 ordered steps, dependency groups, critical path, and non-negotiables
- **`PROGRESS.md`** — Phase 3 tracking table added; Steps 1–5 now ✅ Done
- **Step 1** — K8s manifests for `playwright-worker`, `llm-worker`, `cleanup` CronJob committed to infra repo (PRD-001)
- **Step 2** — Sliding window rate limiter (PRD-002): replaced fixed-window `INCR/EXPIRE` with Redis sorted set + atomic Lua script; `api/app/core/rate_limit.py` fully rewritten; 7 tests
- **Step 3** — SSRF re-validation on webhook delivery (PRD-003): `security.py` refactored into private `_validate_no_ssrf` (raises `ValueError`) + public HTTP adapter; delivery loop re-validates on every attempt; rebinding block → `exhausted` immediately, no retry, no attempt increment; 7 tests
- **Step 4** — Migration 3.1: `jobs.respect_robots BOOLEAN NOT NULL DEFAULT false` applied ✅
- **Step 5** — Migration 3.2: `jobs.proxy_provider VARCHAR(50) NULL` applied ✅
- **Alembic auto-run disabled in `main.py`** during Phase 3 migration development — re-enable after all 10 migrations (Steps 4–13) are finalised to avoid hot-reload applying partial hand-written DDL

### Ready to start
- **Step 6** — Migration 3.3: `jobs.actions JSONB`, `webhook_url TEXT` (type change), `webhook_events TEXT[]`
- **Steps 7–13** — remaining schema migrations (hand-written ENUMs, batch tables, crawl tables, quotas, trigger)
- After migrations: Steps 14–15 (worker schema_version 2) must be deployed before Steps 16–20

### Pending
- None.

---

## 3. Critical File Map

Read these in the order listed when picking up a new session.

### Always read first
| File | Why |
|------|-----|
| `CLAUDE.md` | Project goals, stack, key architectural decisions table, deployment target, MVP definition |
| `docs/project/PROGRESS.md` | Build log — Phase 1 history, Phase 2 step tracker, Gotchas section at the bottom |
| `PRODUCTION_REVIEW.md` | Pre-ship readiness audit — all CRITICAL/HIGH/MEDIUM fixed; LOW deferred to Phase 3 |
| `docs/project/DEVOPS_SPEC.md` | Full k3s/FluxCD deployment spec — hand to DevOps agent to create gitops manifests |

### Reference during implementation
| File | Why |
|------|-----|
| `docs/adr/README.md` | ADR index — current status of every decision record, supersession relationships |
| `docs/adr/ADR-001-worker-job-contract.md` | Phase 1 worker contract — §4, §5, §6, §7 still authoritative; §2, §3, §8 superseded by ADR-002 |
| `docs/adr/ADR-002-phase2-worker-contract.md` | Current worker contract — subjects, message schemas, MinIO paths, pull consumer pattern |
| `docs/phase2/phase2-engineering-spec-v3.md` | Full Phase 2 engineering spec — the implementation source of truth |
| `docs/project/COMMANDS.md` | All runnable commands — Docker, tests, Alembic, NATS, Redis, API examples |

### Architecture context (read when you need the "why" behind a decision)
| File | Why |
|------|-----|
| `docs/adr/ARCHITECTURE_DECISIONS.md` | 22 Phase 1 implementation decisions with rationale and alternatives |
| `docs/personas/architect.md` | Architect persona onboarding — full record of every Phase 2 design decision |
| `docs/project/PHASE3_DEFERRED.md` | Living list of everything deferred out of Phase 2 — add to it whenever something is punted; read it at Phase 3 kickoff |
| `docs/project/open-questions.md` | Implementation-time questions that need a decision before code is written; check before starting any step that touches schema or contracts |

### Archive (historical — only needed for incident investigation)
| File | What it was |
|------|------------|
| `docs/project/archive/phase1-architect-review.md` | 23 Phase 1 issues, all resolved |
| `docs/project/archive/phase1-cleanup-backlog.md` | 18 pre-Phase 2 cleanup steps, all done |
| `docs/phase2/phase2-spec-review-v1.md` | 23-issue review of spec v1 |
| `docs/phase2/phase2-spec-review-v2.md` | 4-issue review of spec v2 |

---

## 4. The Phase 2 Backlog — Summary View

Full details in `docs/project/PHASE2_BACKLOG.md`. This is the TL summary — dependencies and sequencing.

### Dependency groups — current status

**Group A — Foundation: ✅ All done**
- Step 1: SSRF refactor → `core/security.py` ✅
- Step 2: `get_current_admin_user` dependency ✅
- Step 3: Fernet setup in settings ✅

**Group B — Schema: ✅ All done**
- Steps 4–9: Six Alembic migrations ✅
- Step 12: Drop run-state from `jobs` (ONE-WAY) ✅ — ADR-003 was written first

**Group C — API routes: ✅ All done**
- Step 10: `POST /jobs` Phase 2 ✅
- Step 11: `GET/DELETE /jobs` Phase 2 ✅
- Steps 16–17: New job routes + LLM key routes ✅

**Group D — Workers: ✅ All done**
- Step 13: NATS constants + docker-compose nats-init ✅
- Step 14: Go HTTP worker update ✅
- Step 18: Python Playwright worker (new service) ✅
- Step 19: Python LLM worker (new service) ✅

**Group E — Background tasks: ✅ All done**
- Step 15: Result consumer full Phase 2 rewrite ✅
- Step 20: Scheduler loop ✅
- Step 21: Webhook delivery loop ✅
- Step 22: MaxDeliver advisory subscriber ✅

**Group F — Admin + cleanup: ✅ All done**
- Steps 23–24: Admin panel routes + stats endpoint ✅
- Step 25: `cleanup_old_runs.py` script ✅
- Step 26: Docker Compose — add Playwright + LLM worker service definitions ✅

### Remaining steps
None. All 26 steps complete. Phase 2 is done — proceed to Phase 3.

---

## 5. Process Conventions — How Work Gets Done

### When an engineer starts a step
1. Check all dependency steps are marked ✅ in `PROGRESS.md`
2. Read the step entry in `PHASE2_BACKLOG.md` fully before writing any code
3. Read the referenced spec section(s)
4. Write tests alongside the code — not after

### When an engineer finishes a step
1. Update `PROGRESS.md` — change `⬜ Todo` to `✅ Done` for that step
2. If any new gotchas were hit, add them to the Gotchas section of `PROGRESS.md`
3. If new commands were used, add them to `COMMANDS.md`
4. If the step deviated from the spec, note it in `PHASE2_BACKLOG.md` under the step

### When a migration step runs
- Run `docker compose exec api uv run pytest tests/ -v` BEFORE and AFTER to confirm no regression
- For Step 12 specifically: verify `job_runs` is populated (`SELECT COUNT(*) FROM job_runs`) before dropping columns

### Test commands (never run tests locally — always inside Docker)
```bash
# Python API tests (must use uv run — uv manages the .venv inside the container)
docker compose exec api uv run pytest tests/ -v

# Single test file
docker compose exec api uv run pytest tests/test_jobs.py -v

# Go worker tests
docker compose exec http-worker go test ./... -v
```

### When you find a spec gap or ambiguity
- Minor implementation detail → make the pragmatic call, note the decision in `PHASE2_BACKLOG.md`
- Architectural decision (affects contracts, schema, security) → stop, bring it to the Architect persona, do not implement a guess
- Bug in the spec → note it, apply the documented fix from spec reviews if applicable, or escalate

---

## 6. Completed TL Deliverables

### ADR-003 — Job/Run Data Model Split ✅ Done
Written before Step 12 ran, as required. See `docs/adr/ADR-003-job-run-split.md`.

### `ARCHITECTURE_DECISIONS.md` additions ✅ Done (2026-04-14)

Phase 2 decisions added (entries 23–28 in `docs/adr/ARCHITECTURE_DECISIONS.md`):
- Fernet symmetric encryption for LLM API keys and webhook secrets
- `FOR UPDATE SKIP LOCKED` for scheduler multi-instance coordination
- Webhook delivery via `webhook_deliveries` table (not NATS)
- LLM worker as a separate Python service
- Text diff (non-LLM) vs JSON diff (LLM) strategy
- Pull consumer + semaphore worker pool

### Production readiness + CI/CD ✅ Done (2026-04-15)
- `PRODUCTION_REVIEW.md` — full pre-ship audit, all C/H/M items fixed
- `docs/project/DEVOPS_SPEC.md` — k3s deployment spec for DevOps agent
- `.github/workflows/build-push.yml` — CI pipeline, all 4 images on DockerHub
- `v2.0.0` tag cut on `main`

---

## 7. Non-Negotiables — Do Not Change These

These decisions are already made by the Architect and embedded in the spec. Do not revise them during implementation:

| Decision | What it means for you |
|----------|----------------------|
| Workers never touch Postgres | If an engineer's implementation has the Go/Playwright/LLM worker doing a DB query, stop them |
| DB commit before NATS publish | In the scheduler and POST /jobs — always. If NATS fails, the DB row is the recovery path |
| Ack-after-MinIO-write | Workers ack NATS messages only after a successful MinIO upload, not before |
| `run_id` in every result message | The result consumer uses run_id to update the correct job_runs row |
| `latest/` + `history/` dual MinIO paths | Workers write both; `result_path` always stores the `history/` path |
| ADR-001 principles (§4, §5, §6, §7) | Ack timing, retry policy, cancellation — unchanged from Phase 1 |
| No `transaction = False` in migrations | Use COMMIT/BEGIN trick in `upgrade()` for the ALTER TYPE migration |
| `DELETE /jobs/{id}` pauses scheduled jobs | Cancels the active `job_runs` row AND sets `schedule_status='paused'` + `next_run_at=NULL` if `schedule_cron IS NOT NULL` — without this, the scheduler re-fires the job at the next cron tick (spec gap resolved 2026-04-14, see ADR-003 §4) |

---

## 8. What You Can Do in a Session — Menu of Options

Tell your session what you want and this persona picks up the work:

| Task | What to say |
|------|-------------|
| Start the next backlog step | "Pick up the next incomplete Phase 2 step" |
| Implement a specific step | "Implement Phase 2 Step N" |
| Code review a step | "Review the implementation of Step N against the spec" |
| Update progress | "Mark Step N as complete in PROGRESS.md" |
| Add a gotcha | "Add [X] to the Gotchas section" |
| Unblock a stuck step | "I'm stuck on Step N — [describe the issue]" |
| Check spec compliance | "Does [this code] match the spec for Step N?" |
| Update ARCHITECTURE_DECISIONS.md | "Add the [X] decision to ARCHITECTURE_DECISIONS.md" |
| Check CI run | "Check the latest workflow run on scrapeflow" |
| Start DevOps deployment | "Read docs/project/DEVOPS_SPEC.md and create the k3s manifests in the gitops repo" |

---

## 9. How to Start a New Tech Lead Session

Copy and paste this into a new Claude Code session:

```
Read docs/personas/tech-lead.md.
You are the Tech Lead for ScrapeFlow. Phase 1 and Phase 2 are both complete.
Production is live at scrapeflow.govindappa.com — E2E verified 2026-04-15 (auth, job dispatch, worker, MinIO all green).
Phase 3 is in progress — Steps 1–5 done, Step 6 is next.
Steps done: K8s manifests (1), sliding window rate limiter (2), SSRF webhook re-validation (3), migration 3.1 respect_robots (4), migration 3.2 proxy_provider (5).
Alembic auto-run is temporarily commented out in main.py — re-enable after all Phase 3 migrations (Steps 4–13) are complete.
[Tell me what you want to do next.]
```
