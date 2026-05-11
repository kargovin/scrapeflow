# ScrapeFlow Phase 3 — PM Prioritized PRD Backlog

> **Owner:** Product Manager
> **Produced:** 2026-04-15
> **Handoff target:** Software Architect
> **Source inputs:** CLAUDE.md Phase 3 scope, PHASE3_DEFERRED.md, crawl4ai feature research, firecrawl feature research

---

## How to read this document

Each row links to a PRD. The Architect reads the PRD before producing an ADR or design doc.
Priority tiers:

| Tier | Meaning |
|------|---------|
| **P1 — Must Ship** | Unblocks production, fixes known security gaps, or was committed in Phase 2 |
| **P2 — Core Phase 3** | Primary differentiating features; platform is incomplete without them |
| **P3 — Enhancements** | High value but the platform is usable without them |

Source column: `CLAUDE.md` = already committed in CLAUDE.md Phase 3 section; `DEFERRED` = carried from PHASE3_DEFERRED.md; `NEW` = identified from crawl4ai / firecrawl research.

---

## Prioritized Backlog

| # | PRD | Priority | Source | Rationale summary |
|---|-----|----------|--------|-------------------|
| 1 | [K8s Manifests — Phase 2 Services](PRD-001-k8s-manifests.md) | P1 | CLAUDE.md + DEFERRED | FluxCD production deployment is blocked; Phase 2 services (playwright-worker, llm-worker, cleanup cron) have no k3s manifests |
| 2 | [Rate Limiting — Sliding Window](PRD-002-sliding-window-rate-limit.md) | P1 | DEFERRED | Fixed window has a known 2x burst exploit; must fix before billing enforcement |
| 3 | [Security — SSRF Re-validation on Webhook Delivery](PRD-003-ssrf-revalidation.md) | P1 | DEFERRED | DNS rebinding attack bypasses Phase 2 SSRF check; known vulnerability, deferred explicitly |
| 4 | [robots.txt Compliance](PRD-004-robots-txt.md) | P1 | CLAUDE.md + DEFERRED | Legal/ethical exposure grows as platform is opened beyond single-user; per-job toggle |
| 5 | [Proxy Rotation](PRD-005-proxy-rotation.md) | P2 | CLAUDE.md + DEFERRED | Core anti-blocking capability; both crawl4ai and firecrawl treat it as table-stakes |
| 6 | [Batch Scraping](PRD-006-batch-scraping.md) | P2 | NEW (both) | Neither Phase 1 nor Phase 2 supports multi-URL parallel jobs; firecrawl and crawl4ai both treat this as a primary API primitive |
| 7 | [Site Crawl — Multi-page Crawl from Seed URL](PRD-007-site-crawl.md) | P2 | NEW (both) | Entire-site extraction is the most-requested capability firecrawl/crawl4ai both built first; meaningfully differentiates ScrapeFlow from a single-URL scraper |
| 8 | [Authenticated Scraping — Cookie and Session Injection](PRD-008-authenticated-scraping.md) | P2 | DEFERRED | Required for any private-page use case; narrow cookie-injection scope avoids full credential storage complexity |
| 9 | [Pre-crawl Page Actions](PRD-009-page-actions.md) | P2 | NEW (firecrawl) | Click, wait, scroll, JS execution before extraction; unlocks login flows, cookie banners, dynamic UI patterns that Playwright can't handle without choreography |
| 10 | [MCP Server](PRD-010-mcp-server.md) | P2 | CLAUDE.md + DEFERRED | Firecrawl now ships native MCP; LLM-callable scraping is a first-class use case for the ML pipeline persona we're targeting |
| 11 | [Admin SPA](PRD-011-admin-spa.md) | P3 | CLAUDE.md + DEFERRED | Admin API routes are built; this is the UI consumer. Required for quota management and platform operability at scale. **Priority may warrant PM reassessment now that Phase 3 is not homelab-scoped.** |
| 12 | [Billing and Per-user Quotas](PRD-012-billing-quotas.md) | P3 | CLAUDE.md + DEFERRED | Quota enforcement is essential for any multi-user deployment. **Priority may warrant PM reassessment now that Phase 3 is not homelab-scoped.** |
| 13 | [Per-event Webhook Subscriptions](PRD-013-webhook-event-filter.md) | P3 | DEFERRED | Currently all events fire all webhooks; per-event filtering was deferred until a frontend existed to configure it |
| 14 | [WebSocket Real-time Job Tracking](PRD-014-websocket-tracking.md) | P3 | NEW (firecrawl) | Firecrawl ships WS endpoints for live crawl status; eliminates polling from integrations and Admin SPA |
| 15 | [Content Deduplication](PRD-015-content-dedup.md) | P3 | NEW (crawl4ai) | crawl4ai uses xxhash content fingerprinting to skip re-processing unchanged pages; valuable for ML pipeline consumers who run change-detection jobs |

---

## What was considered and excluded

The following features from crawl4ai / firecrawl were reviewed and excluded from Phase 3 scope:

| Feature | Source | Reason excluded |
|---------|--------|----------------|
| Adaptive/BFS/DFS deep crawl strategies | crawl4ai | Site Crawl PRD-007 covers basic depth-limited crawl first; strategy selection is Phase 4 |
| Embedded LLM server (crawl4ai's own server mode) | crawl4ai | ScrapeFlow already has FastAPI; not an independent deployment |
| ClickHouse analytics backend | firecrawl | Overengineered for homelab scale; Postgres is sufficient through Phase 3 |
| X402 payment protocol | firecrawl | SaaS billing feature; not relevant to self-hosted deployment |
| Deep Research agent (multi-step autonomous) | firecrawl | Interesting but requires LLM cost the platform doesn't control; Phase 4 at earliest |
| Sitemap-based URL seeding | crawl4ai | Subsumed into Site Crawl PRD-007 as a discovery mechanism, not a standalone feature |
| C4A script DSL | crawl4ai | Custom DSL is a large surface area; Pre-crawl Page Actions (PRD-009) covers the practical need |
| PDF/DOCX document parsing | firecrawl | Valid feature but no current user demand; deferred to Phase 4 |

---

## Housekeeping items (no PRD needed)

These are small schema or code fixes from PHASE3_DEFERRED.md. Architect can fold them into the nearest related ADR or instruct the Engineer directly.

| Item | Where to fold it |
|------|-----------------|
| `api_keys (user_id, name)` uniqueness constraint | ADR for Admin SPA (PRD-011) or standalone migration |
| `jobs.updated_at` maintenance decision (Option A/B/C) | ADR for Admin SPA (PRD-011) — it affects dashboard query correctness |
| `jobs.webhook_url` column type `VARCHAR → TEXT` | Opportunistic migration; fold into any ADR that touches `jobs` |
| User-facing hard delete (`DELETE /jobs/{id}?permanent=true`) | Architect decides during Admin SPA ADR — also affects Admin SPA UI design |

---

## Cross-cutting architectural questions for the Architect

These are not feature-specific — they affect multiple PRDs and must be answered **before** the Architect begins ADRs for P2 features. The PM is raising them explicitly so the Architect addresses them upfront rather than discovering them mid-design.

---

### Q-ARCH-1: Orchestration model validation and fat message versioning

**Background:**

ScrapeFlow uses a **central orchestration** pattern: the API acts as orchestrator, workers are thin (NATS + MinIO only, no DB access), and the API result consumer updates Postgres. Workers receive a "fat message" containing everything they need to execute without a DB lookup.

This pattern is sound for Phase 1 and 2. Phase 3 adds fields to the fat message across multiple PRDs:

| PRD | New fat message fields |
|-----|----------------------|
| PRD-005 Proxy rotation | `proxy_url` |
| PRD-008 Authenticated scraping | `cookies` |
| PRD-009 Page actions | `actions[]` |
| PRD-007 Site crawl | `crawl_id`, `crawl_depth` |
| PRD-004 robots.txt | `respect_robots` |

Without a versioning strategy, the fat message schema will drift between what the API sends and what workers expect — especially if a Phase 3 worker is deployed before the API (or vice versa) during a rolling upgrade.

**Question for the Architect:**

1. Should the fat message adopt an explicit `schema_version` field in Phase 3, and if so, what is the workers' backward-compatibility contract when they receive an unknown version?
2. Is the current flat message structure (all fields at top level) sustainable as Phase 3 adds 8–10 new fields, or should fields be grouped into sub-objects (e.g. `credentials: {proxy_url, cookies}`, `options: {respect_robots, actions}`)? Restructuring now is cheaper than mid-phase.
3. The orchestration pattern is appropriate at ScrapeFlow's current scale. However, Site Crawl (PRD-007) introduces a multi-step coordination loop (BFS) that strains the thin-worker model. Before the Architect designs PRD-007, confirm whether the orchestration pattern is being extended (BFS coordinator in the API) or whether PRD-007 warrants a different execution model. See Q-ARCH-2 below.

---

### Q-ARCH-2: Site Crawl BFS coordinator placement

**Background:**

The current orchestration model handles one job → one NATS message → one worker execution cleanly. Site Crawl (PRD-007) is different: it requires a BFS loop where each completed page potentially enqueues N new pages, up to `max_depth` levels deep. This is inherently multi-step coordination — not a single dispatch.

In a pure "thin worker" model, someone has to own the BFS queue and decide what to dispatch next. That "someone" is not a worker (workers don't know about job topology) — it must live in the API or a coordinator process.

**Three options the Architect should evaluate:**

| Option | Description | Trade-offs |
|--------|-------------|-----------|
| **A. API background task** | The API's background task loop maintains a per-crawl Redis queue; on each result message for a crawl page, it enqueues the page's discovered links and dispatches next-level messages | Stays within existing architecture; adds stateful logic to the API result consumer; fails ungracefully if the API restarts mid-crawl |
| **B. Dedicated crawl coordinator process** | A new process (or Go service) owns the BFS queue per crawl, subscribes to crawl result messages, dispatches next pages | Cleaner separation; adds a new infra component; more operational complexity |
| **C. Workers self-enqueue** | On completing a page, the worker publishes the discovered links back to NATS; workers pull and process them | Breaks the "workers don't know about job topology" principle; creates circular NATS traffic; harder to enforce `max_depth` and `max_pages` limits |

**Question for the Architect:**

Which option should PRD-007 use, and does the choice require any changes to the existing NATS stream configuration (new subjects, consumer groups, or retention policies)?

The PM has no preference on implementation — this is an architectural decision. But it must be resolved before the Tech Lead can sequence the Site Crawl implementation steps.

---

## Sequencing recommendation

The Architect should be handed PRDs in this order to minimize blocking:

```
Pre-work (before any ADRs): Answer Q-ARCH-1 and Q-ARCH-2
P1 batch (parallel): PRD-001, PRD-002, PRD-003, PRD-004
P2 batch 1 (after P1 ADRs done): PRD-005, PRD-006, PRD-007
P2 batch 2 (after workers are stable): PRD-008, PRD-009, PRD-010
P3 (after Admin API is confirmed stable): PRD-011, PRD-012, PRD-013, PRD-014, PRD-015
```
