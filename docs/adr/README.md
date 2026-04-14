# ScrapeFlow — ADR Index

Architecture Decision Records for the ScrapeFlow platform.

An ADR captures a significant architectural decision: what was decided, why, what alternatives were considered, and what the consequences are. Once accepted, an ADR is immutable — if a decision changes, a new ADR is written that supersedes the old one. The old ADR is updated with a supersession notice but never edited to match the new decision.

---

## Status Definitions

| Status | Meaning |
|--------|---------|
| **Proposed** | Under discussion — not yet implemented |
| **Accepted** | Decision is final and has been (or is being) implemented |
| **Partially Superseded** | Some sections replaced by a later ADR; see supersession notice for which sections remain authoritative |
| **Superseded** | Fully replaced by a later ADR; kept for historical context only |
| **Deprecated** | Decision was reversed; the described approach is no longer in use |

---

## ADR Registry

| ADR | Title | Status | Date | Supersedes | Superseded by |
|-----|-------|--------|------|------------|---------------|
| [ADR-001](ADR-001-worker-job-contract.md) | Worker Job Contract | **Partially Superseded** | 2026-03-25 | — | ADR-002 (§2 Subjects, §3 Schemas, §8 MinIO paths) |
| [ADR-002](ADR-002-phase2-worker-contract.md) | Phase 2 Worker Contract | **Accepted** | 2026-04-02 | ADR-001 (§2, §3, §8) | — |
| [ADR-003](ADR-003-job-run-split.md) | Job/Run Data Model Split | **Accepted** | 2026-04-09 | — | — |

---

## What belongs in an ADR vs `ARCHITECTURE_DECISIONS.md`

**Use an ADR when the decision:**
- Defines a contract between two or more services (especially cross-language)
- Is a schema decision that is hard or impossible to reverse
- Will be referenced by engineers implementing separate components who may never read the full codebase

**Use `ARCHITECTURE_DECISIONS.md` when the decision:**
- Is an implementation choice within a single service
- Is reversible with a normal code change (no migration, no protocol bump)
- Is primarily interesting for context, not as a binding implementation contract

---

## How to write a new ADR

1. Copy the structure of an existing ADR (Status, Date, Deciders, Context, Decisions, Consequences)
2. Number sequentially: `ADR-NNN-short-title.md`
3. Add a row to this index
4. If it supersedes an existing ADR: update the superseded ADR's status header and add inline `> ⚠ Superseded by ADR-NNN` notices at the specific sections that changed
5. Reference the ADR from `CLAUDE.md` if it defines a contract engineers will need during implementation
