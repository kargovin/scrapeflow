# ScrapeFlow — Documentation Index

All project documentation lives here. Nothing is deleted — completed work is kept as a historical record of how the project evolved through phases.

---

## Architecture decisions

| Location | Contents |
|----------|----------|
| [`adr/README.md`](adr/README.md) | ADR index — status, supersession chain, which phase each decision belongs to |
| [`adr/ADR-001`](adr/ADR-001-worker-job-contract.md) | Phase 1 worker/job NATS contract (partially superseded by ADR-002) |
| [`adr/ADR-002`](adr/ADR-002-phase2-worker-contract.md) | Phase 2 worker contract — authoritative NATS subjects, message schemas, MinIO paths |
| [`adr/ADR-003`](adr/ADR-003-job-run-split.md) | `job_runs` split from `jobs` |
| [`adr/ADR-004`](adr/ADR-004-phase3-fat-message-schema.md) | Phase 3 fat message schema_version 2 |
| [`adr/ADR-005`](adr/ADR-005-site-crawl-bfs-coordinator.md) | Site crawl BFS coordinator design |
| [`adr/ADR-006`](adr/ADR-006-batch-scraping-data-model.md) | Batch scraping data model |
| [`adr/ADR-007`](adr/ADR-007-job-secrets-storage.md) | Job secrets storage |

---

## Phase 2

| File | Contents |
|------|----------|
| [`phase2/phase2-engineering-spec-v3.md`](phase2/phase2-engineering-spec-v3.md) | Authoritative Phase 2 engineering spec (v3 supersedes v1/v2) |
| [`phase2/phase2-engineering-spec-v2.md`](phase2/phase2-engineering-spec-v2.md) | Historical v2 spec |
| [`phase2/phase2-engineering-spec-v1.md`](phase2/phase2-engineering-spec-v1.md) | Historical v1 spec |
| [`phase2/phase2-concepts.md`](phase2/phase2-concepts.md) | Conceptual background written before the spec |
| [`phase2/phase2-spec-review-v1.md`](phase2/phase2-spec-review-v1.md) | Architect review of v1 spec |
| [`phase2/phase2-spec-review-v2.md`](phase2/phase2-spec-review-v2.md) | Architect review of v2 spec |
| [`phase2/production-review.md`](phase2/production-review.md) | Phase 2 production readiness review — all findings resolved before Phase 3 |

---

## Phase 3

| File | Contents |
|------|----------|
| [`phase3/phase3-engineering-spec.md`](phase3/phase3-engineering-spec.md) | Phase 3 engineering spec — all 28 steps, service contracts, migration plan |
| [`phase3/production-review.md`](phase3/production-review.md) | Phase 3 production readiness review — all findings resolved or deferred to Phase 4 |
| [`phase3/idempotency-checks.md`](phase3/idempotency-checks.md) | NATS redelivery idempotency audit — 7 findings, all fixed (Migration 3.18 + terminal guards + source discriminator) |
| [`phase3/service-failure-recovery.md`](phase3/service-failure-recovery.md) | Service failure & recovery audit — coordinator + API consumer + scheduler findings, all fixed |
| [`phase3/prod-todo.md`](phase3/prod-todo.md) | Early post-implementation production findings log (pre-review); superseded by `production-review.md` |

---

## Project management

| File | Contents |
|------|----------|
| [`project/PROGRESS.md`](project/PROGRESS.md) | Build log — step-by-step tracking across all phases, gotchas section |
| [`project/PHASE3_BACKLOG.md`](project/PHASE3_BACKLOG.md) | Phase 3 ordered implementation backlog (28 steps) |
| [`project/PHASE2_BACKLOG.md`](project/PHASE2_BACKLOG.md) | Phase 2 ordered implementation backlog (historical) |
| [`project/PHASE3_ADDITIONS.md`](project/PHASE3_ADDITIONS.md) | Phase 3 scope additions tracked mid-sprint |
| [`project/PHASE3_DEFERRED.md`](project/PHASE3_DEFERRED.md) | Items deferred out of Phase 3 scope |
| [`project/COMMANDS.md`](project/COMMANDS.md) | All runnable commands — Docker Compose, tests, Alembic, NATS, Redis, API curl |
| [`project/DEVOPS_SPEC.md`](project/DEVOPS_SPEC.md) | k3s deployment spec — namespaces, Flux, ingress, sealed secrets |
| [`project/open-questions.md`](project/open-questions.md) | Open design questions across phases |
| [`project/usages.md`](project/usages.md) | Quick reference — how Redis and MinIO are used in the project |
| [`project/phase3-prd/`](project/phase3-prd/) | Phase 3 PRDs (PRD-001 through PRD-015) — one file per feature |

### Archive

| File | Contents |
|------|----------|
| [`project/archive/phase1-architect-review.md`](project/archive/phase1-architect-review.md) | Phase 1 architect review — 23 issues, all resolved before Phase 2 |
| [`project/archive/phase1-cleanup-backlog.md`](project/archive/phase1-cleanup-backlog.md) | Pre-Phase 2 cleanup backlog — 18 steps, all complete |

---

## Guides & research

| File | Contents |
|------|----------|
| [`guides/playwright-primer.md`](guides/playwright-primer.md) | Playwright concepts primer written before the Playwright worker was built |
| [`guides/litellm-provider-routing.md`](guides/litellm-provider-routing.md) | LiteLLM provider routing reference |
| [`guides/modal-llm-inference.md`](guides/modal-llm-inference.md) | Modal.com LLM inference exploration |
| [`guides/competitor-research.md`](guides/competitor-research.md) | crawl4ai and firecrawl research notes — informed Phase 3 PRD backlog |

---

## Personas (Phase 3 process)

Phase 3 was built using a multi-persona process (PM → Architect → Tech Lead → Engineer). Each persona's standing instructions live here.

| File | Role |
|------|------|
| [`personas/product-manager.md`](personas/product-manager.md) | PM persona — owns PRDs, priorities, success criteria |
| [`personas/architect.md`](personas/architect.md) | Architect persona — owns ADRs, system contracts, design docs |
| [`personas/tech-lead.md`](personas/tech-lead.md) | Tech Lead persona — owns backlog breakdown, sequencing, dependency graph |
