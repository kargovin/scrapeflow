# Competitor Research Notes — crawl4ai & firecrawl

> **Purpose:** Raw research findings used to inform Phase 3 PRD backlog. Not a spec — a reference dump.
> **Date:** 2026-04-15
> **Used in:** `docs/project/phase3-prd/BACKLOG.md` and individual PRDs

---

## crawl4ai

**Repo:** https://github.com/unclecode/crawl4ai
**Philosophy:** Developer tool — self-hosted, modular, highly configurable. Zero managed infra.

---

### Extraction strategies

| Strategy | Mechanism | Notes |
|----------|-----------|-------|
| `JsonCssExtractionStrategy` | CSS selector schema → BeautifulSoup | Define `baseSelector` + `fields[]` with selector/type per field. Fast, free, breaks on redesign |
| `JsonLxmlExtractionStrategy` | Same idea but lxml | Faster, nth-child support, selector caching |
| `LLMExtractionStrategy` | Page chunks → LLM + JSON schema | ThreadPoolExecutor parallel, caching, token usage tracking |
| `CosineStrategy` | Embeddings + hierarchical clustering | Semantic filtering, not field extraction |
| `NoExtractionStrategy` | Passthrough | Raw HTML out |

**Key insight:** CSS selector extraction is the primary non-LLM path. Example schema:
```python
schema = {
    "name": "products",
    "baseSelector": "div.product-card",
    "fields": [
        {"name": "title", "selector": "h2.title", "type": "text"},
        {"name": "price", "selector": "span.price", "type": "text"},
        {"name": "url", "selector": "a", "type": "attribute", "attribute": "href"}
    ]
}
```
This is deterministic, zero-token cost structured extraction. ScrapeFlow has no equivalent — LLM is the only extraction path today.

---

### Content processing pipeline

**Markdown generation:**
- CustomHTML2Text with configurable options
- BM25-based noise filtering
- Heuristic content filtering for AI-friendly output
- Four output variants: `raw_markdown`, `cited_markdown` (numbered refs), `fit_markdown` (filtered)

**Content filters (three strategies):**
1. `BM25ContentFilter` — tokenizes corpus, BM25 scoring with tag-weight adjustments, stemming support
2. `PruningContentFilter` — tree-pruning on text density, link density, tag weight
3. `LLMContentFilter` — semantic extraction via LLM with chunking + overlap

**Chunking strategies (8 options):**
1. `IdentityChunking` — no chunking
2. `RegexChunking` — pattern-based (default: double newlines)
3. `NlpSentenceChunking` — NLTK sentence segmentation
4. `TopicSegmentationChunking` — NLTK TextTilingTokenizer + keyword extraction
5. `FixedLengthWordChunking` — fixed word count (default 100)
6. `SlidingWindowChunking` — overlapping chunks, configurable step
7. `OverlappingWindowChunking` — independent window/overlap config
8. Token-aware chunking (inside extraction strategies)

---

### Advanced crawling

**URL discovery:**
- `AsyncUrlSeeder` — discovers from sitemaps + Common Crawl archives, optional head metadata + live validation

**Deep crawl strategies (three):**
- `BFSDeepCrawlStrategy` — breadth-first
- `DFSDeepCrawlStrategy` — depth-first
- `BestFirstCrawlingStrategy` — priority-queue based on content relevance

**Adaptive crawling (sophisticated):**
- Statistical strategy: term frequency, coverage (Jaccard), saturation detection
- Embedding strategy: semantic gap analysis, link ranking by uncovered regions
- State snapshots for resumption after crash
- Convergence detection: stops when improvement < threshold

**Multi-URL:** `arun_many()` — concurrent processing, URL-specific config per request, batch + streaming modes

---

### Anti-bot / stealth

**Detection (3 tiers):**
- Tier 1: Structural signatures — Akamai, Cloudflare, PerimeterX, DataDome, Imperva, Sucuri, Kasada
- Tier 2: Generic terms (only on pages <10KB)
- Tier 3: Structural integrity — missing body, minimal text, script-heavy shells

**Evasion:**
- `enable_stealth=true` — StealthAdapter post-launch, no GPU overhead
- User agent generation: `ValidUAGenerator` (fake_useragent), `OnlineUAGenerator` (useragents.me), `UserAgentGenerator` (granular control)
- Client hints generation (`Sec-CH-UA` headers)
- Browser types: undetected Chrome, Chromium, Firefox, WebKit

---

### Proxy handling

**Config:**
```python
ProxyConfig(server="http://host:port", username="user", password="pass")
```
String shorthand: `"ip:port:username:password"`

**3-tier proxy escalation:**
- Tier 1: direct (no proxy)
- Tier 2: basic proxy
- Tier 3: premium/residential proxy
- Auto-escalates based on block detection result

**Sticky sessions:** `proxy_session_id` — same IP for session duration. Critical for login-bound sessions.

**Injection:** Playwright — `browser.new_context(proxy=...)` at context creation

---

### Caching

**Cache modes (5):** ENABLED, DISABLED, READ_ONLY, WRITE_ONLY, BYPASS

**Deduplication:**
- `generate_content_hash()` — xxhash (fast, non-cryptographic)
- `compute_head_fingerprint()` — extracts title, meta descriptions, og tags, article timestamps
- Jaccard similarity for consistency checking in deep crawl

**Storage architecture (hybrid):**
- SQLite (aiosqlite, WAL mode) for metadata: links, media, response headers, cache validation (etag, last_modified, head_fingerprint)
- Filesystem for content (large text stored separately, only hash in DB)

---

### Authentication

- Persistent browser profiles with session state preservation
- Cookie pre-setting and storage state management (`browser.new_context(storage_state=...)`)
- Header customization per request
- Proxy username/password in ProxyConfig

---

### Browser pool management

- Context caching by config signature (SHA256 hash)
- Reference counting for safe concurrent access
- LRU eviction at 20 contexts (configurable)
- Three lifecycle modes: Managed browser + CDP, Persistent context, External CDP
- Browser recycling after `max_pages_before_recycle`
- Per-crawl isolation: `create_isolated_context=True`

---

### Deployment

- FastAPI server (`aio_server.py`): JWT auth middleware, REST endpoints, real-time monitoring dashboard, interactive API playground
- Docker: port 11235 (Gunicorn), 4GB memory limit, `/dev/shm` mount for Chromium, multi-arch (AMD64/ARM64)
- Runs as `appuser` with auto-restart

---

### Concurrency models

1. `MemoryAdaptiveDispatcher` — priority queue, dynamic concurrency based on memory pressure, starvation prevention, result streaming
2. `SemaphoreDispatcher` — fixed concurrency via `asyncio.Semaphore`

---

### Output formats

Markdown (raw/cited/fit), JSON (structured schema), HTML (cleaned/raw), screenshots, PDFs, tables (structured), media items with relevance scores, links (internal/external with domain context)

---

## firecrawl

**Repo:** https://github.com/firecrawl/firecrawl
**Philosophy:** SaaS-first — zero-config, enterprise-grade, managed infra. Claim: 96% web coverage, 3.4s P95 latency.

---

### Extraction strategies

- **LLM-based extraction** — primary path; `/extract` endpoint takes JSON Schema + prompt; LLM figures out where data is
- **Schema-based** — JSON Schema format for structured output
- **Agent mode** — natural language description, two model tiers: `spark-1-mini` (60% cheaper) and `spark-1-pro` (complex scenarios)
- **No CSS selector path** in the public API — they bet LLM extraction generalizes better across redesigns

---

### Content processing

**Scrape `formats` array:** `markdown`, `html`, `rawHtml`, `links`, `screenshot`, `json`, `branding`, `changeTracking`

- `onlyMainContent: true` (default) — automatic noise filtering to primary content
- `jsonOptions.prompt` and `jsonOptions.systemPrompt` for custom extraction instructions
- Change tracking capability (native)
- Branding extraction (logo, colors)
- Mobile/desktop viewport options
- Location/language targeting

---

### Advanced crawling

**`POST /crawl`** — full website crawl from base URL:
- `maxDepth` (default 10), `maxDiscoveryDepth`
- `limit` (default 10000 URLs)
- `includePaths`, `excludePaths`, `ignoreSitemap`, `allowBackwardLinks`, `allowExternalLinks`

**`POST /map`** — URL discovery across a website:
- Sitemap parsing option
- Optional search filtering
- Subdomain inclusion toggle
- Max 30000 URLs per map

**`POST /batch/scrape`** — async multi-URL:
- Thousands of URLs
- Webhook configuration
- `ignoreInvalidURLs` error handling

**`POST /deep-research`** — iterative autonomous research:
- `maxDepth` 1–12 (default 7)
- `timeLimit` 30–600 seconds
- Returns markdown/JSON with sources

**`POST /llmstxt`** — generate LLM-friendly text from a site

**Page actions (`actions` array):**
```json
[
  {"type": "wait", "milliseconds": 2000},
  {"type": "click", "selector": "#accept-cookies"},
  {"type": "write", "text": "search query", "selector": "input"},
  {"type": "press", "key": "Enter"},
  {"type": "scroll", "direction": "down"},
  {"type": "executeJavascript", "script": "..."},
  {"type": "screenshot"}
]
```

---

### Anti-bot / stealth

- Rotating proxy support — automatic, no config required
- `proxy` options: `basic`, `enhanced`/`stealth`, `auto`
- Rate limit handling — automatic
- robots.txt compliance by default
- JS-blocked content handling built in
- Zero configuration for basic operation — this is the key differentiator

---

### Proxy handling

User doesn't configure proxies. firecrawl operates a managed proxy pool:
```json
{"url": "...", "proxy": "basic"}   // standard rotating
{"url": "...", "proxy": "stealth"} // residential/premium
{"url": "...", "proxy": "auto"}    // platform decides
```
Rotation, credentials, failure handling — all invisible.

---

### Authentication

- Session management, cookie handling, header customization via `headers` parameter
- Bearer token for all API endpoints
- Team/account-level access control
- No explicit storage-state or form-login support in the public API

---

### WebSocket real-time tracking

`WS /crawl/{id}` — live crawl status updates. Eliminates polling for long-running crawls. This is the pattern worth adopting for ScrapeFlow.

---

### MCP support

Native MCP (Model Context Protocol) compatibility — ScrapeFlow callable from Claude Desktop, Cursor, etc. with zero integration code. firecrawl ships this; ScrapeFlow does not yet (PRD-010).

---

### Infrastructure (SaaS internals — not self-hostable as-is)

Core services: PostgreSQL, Redis (multiple: cache, job queue, rate-limit variants), Supabase, Google Cloud Storage, ClickHouse (analytics)

External integrations: Playwright microservice, HTML-to-markdown Go service, Smart scrape API, PDF processing (MinerU, Fire PDF), SearXNG search

Worker architecture: Multiple typed workers with port configuration, startup timeout/lock management

**Billing/payments:**
- Stripe (`STRIPE_SECRET_KEY`)
- X402 protocol (payment-per-request)
- Credit + token usage tracking
- Per-request cost calculation

**Monitoring:** Sentry error tracking, trace/error sampling, system monitor service

---

### API surface (comprehensive)

```
POST   /scrape                        Single URL
POST   /batch/scrape                  Multiple URLs
GET    /batch/scrape/{id}             Status
DELETE /batch/scrape/{id}             Cancel
GET    /batch/scrape/{id}/errors      Error retrieval

POST   /crawl                         Full website crawl
GET    /crawl/{id}                    Status
DELETE /crawl/{id}                    Cancel
WS     /crawl/{id}                    WebSocket real-time updates

POST   /extract                       LLM extraction with schema
GET    /extract/{id}                  Status
POST   /map                           Sitemap/URL discovery
POST   /search                        Web search + scraping
POST   /deep-research                 Iterative autonomous research
POST   /llmstxt                       LLM-friendly text generation

GET    /team/credit-usage
GET    /team/token-usage
GET    /team/queue-status
```

---

## Comparative summary

| Dimension | crawl4ai | firecrawl |
|-----------|----------|-----------|
| Philosophy | Developer tool, BYOP, self-hosted | SaaS platform, managed infra, zero-config |
| Extraction | CSS selectors OR LLM | LLM-first (no CSS selector path) |
| Proxy | You configure (3-tier escalation) | Platform manages (basic/stealth/auto) |
| Crawl strategies | BFS, DFS, BestFirst, Adaptive | BFS only (single strategy, well-tuned) |
| Deep crawl | Convergence detection, state snapshots | maxDepth + timeLimit |
| Anti-bot | Detection + stealth mode | Transparent (proxy rotation + JS handling) |
| Real-time | None | WebSocket on crawl jobs |
| MCP | None | Native support |
| Caching | xxhash dedup + hybrid SQLite/filesystem | maxAge HTTP semantics |
| Browser pool | Context signature caching, LRU eviction | Playwright microservice (external) |
| Billing | N/A (open source) | Stripe + X402 + credit tracking |
| Self-hostable | Yes, designed for it | No (Supabase, GCS, ClickHouse dependencies) |

---

## What ScrapeFlow is missing (actionable gaps)

| Gap | Relevant PRD | Priority |
|-----|-------------|---------|
| CSS selector extraction (no LLM needed for structured pages) | No PRD yet — Phase 4 candidate | — |
| Multi-URL batch as primary primitive | PRD-006 | P2 |
| Site crawl from seed URL | PRD-007 | P2 |
| Pre-crawl page actions (click, wait, scroll, JS) | PRD-009 | P2 |
| WebSocket real-time job tracking | PRD-014 | P3 |
| MCP server | PRD-010 | P2 |
| Content dedup via content hash | PRD-015 | P3 |
| Proxy session stickiness (for login-bound sessions) | Not in PRD-005 — raise with Architect | — |
