# ScrapeFlow — Documentation

The `docs/` folder is organized into three sections: **decisions** (ADRs — the canonical source of truth), **reference** (day-to-day operational docs), and **archive** (completed phase artifacts kept for historical context).

---

## Decisions — Architecture Decision Records

ADRs are the primary record of *why* the system is built the way it is. They are immutable once accepted — new decisions supersede old ones rather than editing in place.

| ADR | Title | Status |
|-----|-------|--------|
| [`adr/ADR-001`](adr/ADR-001-worker-job-contract.md) | Worker Job Contract | Partially Superseded by ADR-002 |
| [`adr/ADR-002`](adr/ADR-002-phase2-worker-contract.md) | Phase 2 Worker Contract | **Accepted** — authoritative NATS subjects, schemas, MinIO paths |
| [`adr/ADR-003`](adr/ADR-003-job-run-split.md) | Job/Run Data Model Split | **Accepted** |
| [`adr/ADR-004`](adr/ADR-004-phase3-fat-message-schema.md) | Phase 3 Fat Message Schema v2 | **Accepted** |
| [`adr/ADR-005`](adr/ADR-005-site-crawl-bfs-coordinator.md) | Site Crawl BFS Coordinator | **Accepted** |
| [`adr/ADR-006`](adr/ADR-006-batch-scraping-data-model.md) | Batch Scraping Data Model | **Accepted** |
| [`adr/ADR-007`](adr/ADR-007-job-secrets-storage.md) | Job Secrets Storage | **Accepted** |
| [`adr/ADR-008`](adr/ADR-008-playwright-antibot-hardening.md) | Playwright Worker Anti-Bot Hardening | **Accepted** — Patchright + headed Chrome under Xvfb |

See [`adr/README.md`](adr/README.md) for full ADR index, status definitions, and how to write a new ADR.

For single-service implementation decisions that don't rise to ADR level, see [`adr/ARCHITECTURE_DECISIONS.md`](adr/ARCHITECTURE_DECISIONS.md).

---

## Reference — Operational Docs

Day-to-day docs that are still actively consulted.

| File | Contents |
|------|----------|
| [`project/COMMANDS.md`](project/COMMANDS.md) | All runnable commands — Docker Compose, tests, Alembic, NATS, Redis, API curl |
| [`project/DEVOPS_SPEC.md`](project/DEVOPS_SPEC.md) | k3s deployment spec — namespaces, Flux, ingress, sealed secrets |
| [`project/open-questions.md`](project/open-questions.md) | Q1–Q8 design questions — **all closed** (Q1–Q7 resolved, Q8 do-not-fix); kept for the reasoning trail |
| [`project/usages.md`](project/usages.md) | Quick reference — how Redis and MinIO are used in the project |

### Phase 4 — Temporal durable-workflows migration

| File | Contents |
|------|----------|
| **[`project/phase4-backlog.md`](project/phase4-backlog.md)** | **Single source of truth for Phase 4 — start here.** §3 lists bugs the migration *deletes*; check it before fixing any orchestration bug |
| [`project/phase4-prd/`](project/phase4-prd/) | Phase 4 PRDs (PRD-016 →) — one file per feature layer |
| [`project/workflows-scoping.md`](project/workflows-scoping.md) | The Workflows feature scoping + engine comparison |
| [`project/temporal-full-migration.md`](project/temporal-full-migration.md) | Complete change inventory + strangler-fig migration sequence |
| [`project/open-bugs.md`](project/open-bugs.md) | BUG-001 → BUG-004 |
| [`project/usage-findings.md`](project/usage-findings.md) | UF-001 → UF-003 — findings from running the platform |

---

## Guides — Deep-Dives & Implementation Notes

Long-form guides on specific subsystems — the *how* and *why* behind a piece of the build.

| File | Contents |
|------|----------|
| [`guides/anti-bot-hardening.md`](guides/anti-bot-hardening.md) | Playwright worker stealth — the BrowserScan diagnosis, the config matrix, and the runbook (companion to ADR-008) |
| [`guides/playwright-primer.md`](guides/playwright-primer.md) | Playwright fundamentals and how the Playwright worker uses them |
| [`guides/litellm-provider-routing.md`](guides/litellm-provider-routing.md) | LiteLLM provider routing notes |
| [`guides/modal-llm-inference.md`](guides/modal-llm-inference.md) | Modal LLM inference notes |
| [`guides/competitor-research.md`](guides/competitor-research.md) | Competitor/landscape research |

---

## Process — How We Build ScrapeFlow

Starter prompts for the multi-persona workflow (PM → Architect → Tech Lead → Engineer). Each file onboards a new session into a persona role. Update these when starting a new phase.

| File | Role |
|------|------|
| [`process/product-manager.md`](process/product-manager.md) | PM — defines scope, writes PRDs, hands off to Architect |
| [`process/architect.md`](process/architect.md) | Architect — writes ADRs, system contracts, hands off to Tech Lead |
| [`process/tech-lead.md`](process/tech-lead.md) | Tech Lead — breaks spec into ordered backlog, hands off to Engineers |

---

## Archive — Completed Phase History

Completed phase artifacts. Kept as a permanent record of how the project evolved — never deleted.

### Phase 1

| File | Contents |
|------|----------|
| [`archive/phase1/phase1-architect-review.md`](archive/phase1/phase1-architect-review.md) | Architect review — 23 issues, all resolved before Phase 2 |
| [`archive/phase1/phase1-cleanup-backlog.md`](archive/phase1/phase1-cleanup-backlog.md) | Pre-Phase 2 cleanup backlog — 18 steps, all complete |

### Phase 2

| File | Contents |
|------|----------|
| [`archive/phase2/phase2-engineering-spec-v3.md`](archive/phase2/phase2-engineering-spec-v3.md) | Authoritative Phase 2 spec (v3 supersedes v1/v2) |
| [`archive/phase2/phase2-engineering-spec-v2.md`](archive/phase2/phase2-engineering-spec-v2.md) | Historical v2 spec |
| [`archive/phase2/phase2-engineering-spec-v1.md`](archive/phase2/phase2-engineering-spec-v1.md) | Historical v1 spec |
| [`archive/phase2/phase2-concepts.md`](archive/phase2/phase2-concepts.md) | Conceptual background written before the spec |
| [`archive/phase2/phase2-spec-review-v1.md`](archive/phase2/phase2-spec-review-v1.md) | Architect review of v1 spec |
| [`archive/phase2/phase2-spec-review-v2.md`](archive/phase2/phase2-spec-review-v2.md) | Architect review of v2 spec |
| [`archive/phase2/production-review.md`](archive/phase2/production-review.md) | Phase 2 production readiness review — all findings resolved |
| [`archive/phase2/PHASE2_BACKLOG.md`](archive/phase2/PHASE2_BACKLOG.md) | Phase 2 ordered implementation backlog (26 steps) |

### Phase 3

| File | Contents |
|------|----------|
| [`archive/phase3/phase3-engineering-spec.md`](archive/phase3/phase3-engineering-spec.md) | Phase 3 engineering spec — all 28 steps, service contracts, migration plan |
| [`archive/phase3/production-review.md`](archive/phase3/production-review.md) | Phase 3 production readiness review — all findings resolved or deferred |
| [`archive/phase3/idempotency-checks.md`](archive/phase3/idempotency-checks.md) | NATS redelivery idempotency audit — 7 findings, all fixed |
| [`archive/phase3/service-failure-recovery.md`](archive/phase3/service-failure-recovery.md) | Service failure & recovery audit — all findings fixed |
| [`archive/phase3/prod-todo.md`](archive/phase3/prod-todo.md) | Early post-implementation findings log (pre-review); superseded by production-review |
| [`archive/phase3/PHASE3_BACKLOG.md`](archive/phase3/PHASE3_BACKLOG.md) | Phase 3 ordered implementation backlog (28 steps) |
| [`archive/phase3/PHASE3_ADDITIONS.md`](archive/phase3/PHASE3_ADDITIONS.md) | Phase 3 scope additions tracked mid-sprint |
| [`archive/phase3/PHASE3_DEFERRED.md`](archive/phase3/PHASE3_DEFERRED.md) | Items deferred out of Phase 3 scope |
| [`archive/phase3/prd/`](archive/phase3/prd/) | Phase 3 PRDs (PRD-001 through PRD-015) — one file per feature |

### Build log

| File | Contents |
|------|----------|
| [`archive/PROGRESS.md`](archive/PROGRESS.md) | Step-by-step build log across all phases, including Gotchas section |
