# docs/project — Index

Project-level documentation for day-to-day engineering work.

---

## Active files

| File | Purpose | When to use |
|------|---------|-------------|
| [`COMMANDS.md`](COMMANDS.md) | All runnable commands — Docker Compose, tests, Alembic, NATS, Redis, API curl examples | First stop when you need to run something |
| [`PROGRESS.md`](PROGRESS.md) | Build log across all phases — step-by-step tracking of what's done and what's next; includes Gotchas section at the bottom | Check before starting a step; update when a step completes |
| [`PHASE2_BACKLOG.md`](PHASE2_BACKLOG.md) | Phase 2 task breakdown — 26 ordered steps, each with files to touch, spec reference, and verify command | Pick up the next incomplete step when starting Phase 2 work |

---

## Archive

Completed documents moved here once all work inside them is done. Kept for historical context and audit trail.

| File | What it was |
|------|------------|
| [`archive/phase1-architect-review.md`](archive/phase1-architect-review.md) | Architect's review of the Phase 1 codebase — 23 issues identified, all resolved before Phase 2 |
| [`archive/phase1-cleanup-backlog.md`](archive/phase1-cleanup-backlog.md) | Pre-Phase 2 cleanup backlog — 18 steps derived from the architect review, all complete |

---

## Conventions

- When a Phase 2 step completes: update `PROGRESS.md` (mark ✅), update `PHASE2_BACKLOG.md` if there are notes worth keeping (blockers hit, deviations from spec)
- New commands discovered during implementation go in `COMMANDS.md`
- Gotchas that trip up implementation go in the Gotchas section of `PROGRESS.md`
- When Phase 2 is complete, `PHASE2_BACKLOG.md` moves to `archive/`
