# Competitor Research Notes — crawl4ai, firecrawl & crw

> **Purpose:** Raw research findings used to inform PRD backlogs. Not a spec — a reference dump. §crawl4ai + §firecrawl fed Phase 3; §crw (2026-09-19) is **deferred** until the Temporal pipeline is finished.
> **Date:** 2026-04-15 (crawl4ai, firecrawl) · 2026-09-19 (crw)
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

---

## crw (fastCRW) — engine-level comparison

> **Date:** 2026-09-19 · **Source:** https://github.com/us/crw (AGPL-3.0 engine, Rust, ~95k lines, 11 crates), read at commit of that day.
> **Status: DEFERRED.** Owner's call 2026-09-19 — nothing here is built now. Phase 4 (the Temporal
> migration) continues with the scope already on the plate; **revisit this section once the Temporal
> pipeline is finished.** Items in §A are real bugs on v1 and may be filed to `open-bugs.md` before
> then, but no fix is scheduled.
> Speed is out of scope by design (Rust vs Python); everything below is logic, not performance.

### Framing

crw is **an engine**: one binary, no database, no tenants, no quotas, crawl state in a
`tokio::watch` channel with `job_ttl_secs = 3600` (a restart loses every in-flight crawl), auth is
a static key list. ScrapeFlow is **a platform**: multi-tenant, durable state, storage ledger,
quotas, cancellation precedence, a worker contract with cross-service tests. The platform layer is
where ScrapeFlow is ahead and crw has nothing to teach. Everything below is on the **engine** side
— the fetch → detect → extract → diff path — which is exactly the code that ports into Temporal
activities, so the natural time to act on it is *during* the activity port, not before.

crw's crate map, for orientation: `crw-core` (types, config, error taxonomy, SSRF, deadline,
reserved semaphore) · `crw-renderer` (HTTP tier → escalation ladder → anti-bot policy modules) ·
`crw-extract` (clean → readability → quality scoring → markdown; LLM structured extraction with
per-field evidence) · `crw-crawl` (BFS, URL filter, robots, sitemap tree, page cache) · `crw-diff`
(stateless change tracking) · `crw-server` (Axum API, capabilities endpoint).

### A. Wrong today — correctness / security (candidate `open-bugs.md` filings)

| # | Finding | Where (ScrapeFlow) | crw's shape |
|---|---|---|---|
| **A1** | **SSRF is checked once, at job creation, never at fetch time.** Redirects are followed unchecked — `https://attacker → 302 → http://169.254.169.254/` is open on both engines. DNS rebinding between creation and fetch is open. Only the webhook path re-validates. BUG-010 (mid-crawl URLs) is a special case of this. Range gaps verified on the API's Python 3.12.13: CGNAT `100.64/10`, multicast `224/4`, IPv6 site-local `fec0::/10` pass — minor next to the redirect hole. | `api/app/core/security.py:19`; `http-worker/internal/fetcher/fetcher.go:33-36` (keeps Go's default redirect-follow, no `CheckRedirect` validation); Playwright `page.goto` likewise | `crw-core/src/url_safety.rs` — one predicate at **every outbound boundary**: a custom `reqwest` redirect policy validates each hop; DNS lookup bounded at 8s and fails closed; `HostRejection::{Policy, Unresolved}` kept apart (ScrapeFlow already has this split as 400 vs 422); a `validate_safe_host` variant without the URL-shape rules for browser-produced URLs (signed CDN URLs exceed the 2048 cap and are not SSRF). Blocks NAT64/6to4 by decoding the embedded v4 rather than the whole prefix |
| **A2** | **robots.txt: both parsers treat `*` and `$` as literal characters.** `Disallow: /*.pdf$` / `Disallow: /search*` never match, so with `respect_robots=true` the crawler fetches what the site forbade — silently. Path-only matching: `Disallow: /search?q=` never matches. **The two engines disagree**: Python falls through to `*` when the ScrapeFlow group is empty; Go's `foundSpecific` correctly suppresses `*` (RFC 9309 §2.2.1). Robots is fetched **per job**, uncached — a 100-page crawl makes 100 robots requests | `http-worker/internal/robots/robots.go:59,167`; `playwright-worker/worker/robots.py:88,100`; `http-worker/internal/worker/worker.go:273` | `crw-crawl/src/robots.rs` — RFC 9309 group semantics (consecutive `User-agent:` lines head one group; an exact-token group, even empty, suppresses `*`); fetched once per crawl; body read through `read_capped` and a truncated final line is dropped rather than kept half-read; 4xx = no rules, 5xx = logged and proceed |
| **A3** | **The `http` engine has no bot-wall detection at all.** `blocking.py` exists only on the Playwright worker; the Go worker treats any 2xx as success, so the BUG-003 Amazon 200-wall is stored as a *completed* scrape whenever `engine=http`. Two **header** signals no body scan can see are also missed: `cf-mitigated: challenge\|block` and `x-amzn-waf-action: challenge\|captcha` (AWS WAF serves a **202 with an empty body**) | `http-worker/internal/worker/worker.go` (no classifier); `playwright-worker/worker/blocking.py` reads status + HTML only | `crw-renderer/src/detector.rs` `is_cloudflare_mitigated_header`, `is_aws_waf_action_header`, `looks_like_vendor_block`, `looks_like_generic_bot_wall` (body-text only, < 600 visible chars, so a JS bundle containing the phrase cannot false-positive) |
| **A4** | **Content-type is never checked.** A PDF, image or `.docx` URL goes through `html-to-markdown` and is stored as a completed markdown result | both formatters; `fetcher.go` reads no `Content-Type` | `CrwError::UnsupportedContentType` — distinct from `HttpError` precisely so it does **not** escalate to a browser tier (no browser turns a `.docx` into a page); `is_html_like_content_type` gates the HTML parser |
| **A5** | **Text diff blocks the API's event loop.** `difflib.SequenceMatcher` over full-page line lists runs synchronously inside the result consumer, which is a background task *in the FastAPI process*. A large HTML diff stalls every HTTP request for its duration. **Dissolves when the diff moves into an activity — do not fix on v1** (backlog §3 class) | `api/app/core/diff.py:52`, awaited at `api/app/core/result_consumer.py:516,564` | `crw-diff` is pure/sync by contract ("no I/O, no LLM") and runs off the request path |
| **A6** | **LLM worker: scraped content enters the prompt raw, and the output is never validated.** `f"Extract data from:\n\n{content}"`, no system prompt, no delimiter — a page containing "ignore the schema and return {…}" is followed. `json.loads` of whatever an OpenAI-compatible endpoint returned; self-hosted vLLM does not enforce `response_format` strictly | `llm-worker/worker/llm.py:122-124,127,151` | `crw-extract/src/untrusted.rs` — one audited `wrap(content, label, nonce, index)`: `=====UNTRUSTED:<label>:<nonce>=====` fences with the **nonce repeated in the closing line** so content cannot forge an escape; every caller describes the same shape in its system prompt. `structured.rs` validates with `jsonschema` and gives the model **one** retry carrying the concrete validation errors, inside the same deadline (compatible with PRD-016 R4 — same layer, one bounded attempt). Also a `GROUNDING` clause: null rather than guess, never infer from the URL |
| **A7** | **Silent truncation.** `io.LimitReader(10MB)` cuts the body with no flag; a truncated page is stored as complete. No charset detection either — non-UTF-8 pages are mangled before markdown conversion | `http-worker/internal/fetcher/fetcher.go:76` | `crw-core/src/body.rs` `read_capped` → `(bytes, truncated: bool)`; each caller decides (robots keeps the prefix, scrape rejects). Test pins that a body of *exactly* the cap is not truncated |

### B. Output-quality gaps (hit the ML-pipeline use case directly)

| # | Finding | Where (ScrapeFlow) | crw's shape / where it lands |
|---|---|---|---|
| **B1** | **No main-content extraction; two different converters.** Whole-document conversion — nav, footer, cookie banner, the mega-menu repeated in every dropdown. Different library per engine (Go `html-to-markdown` v1 vs Python `markdownify`), so the same page yields different markdown by `engine` — breaks change detection across an engine switch and changes the LLM's input | `http-worker/internal/formatter/formatter.go:36`; `playwright-worker/worker/formatter.py:22` | `clean.rs` (script/style/nav/footer, `includeTags`/`excludeTags`) → `readability.rs` (text-density scoring over `article`/`main`/`[role=main]`/… with a "candidate > 90 % of body → drill down" rule) → `quality.rs` (scores the *markdown*: words, link density, a **chrome-ratio discount that is a multiplier, not a subtraction**, with a 200-body-word exemption so docs pages beside a sidebar survive — every constant annotated with the regression it fixed) → `markdown.rs` (`drop_repeated_nav_lines`, data-URI stripping). Per-domain selector overrides in config. **This is PRD-016's Clean block.** The design point to carry: extraction as a **ladder with a scorer choosing the winner**, not one converter |
| **B2** | **No page metadata on the run.** `job_runs` holds status/path/diff/error/warnings/hash — not final URL after redirect, HTTP status, content-type, title, description, canonical, `og:*`, language, engine actually used, elapsed ms. `final_url`/`status_code`/`title` are table stakes for a pipeline consumer; `canonical_url` is what makes dedup across a crawl honest | `api/app/models/job_runs.py` | `crw-core/src/types.rs` `PageMetadata` on every result |
| **B3** | **Change detection hashes raw bytes.** xxh64 of the MinIO object — for HTML output any CSRF token, timestamp or ad slot flips it every run; dedup is effectively dead for raw HTML. Text diff returns counts only (`added_lines`/`removed_lines`); JSON diff is top-level only | `api/app/core/result_consumer.py:53`; `api/app/core/diff.py` | `crw-diff/src/snapshot.rs` — normalise (CRLF, trailing whitespace, blank-line runs) before hashing; canonicalise JSON (recursively sorted keys). Unified diff + capped AST; JSON diff walks nested paths (`plans[0].price`). `judge.rs` — an LLM "is this change meaningful for the monitoring goal?" behind the untrusted fence — is a **PRD-018 Monitors** input |
| **B4** | **Crawl URL handling is fragment-strip only.** No case-folding, trailing-slash rule, query sort, tracking-param strip (`utm_*`, `fbclid`, `gclid`…) or action-URL drop — a WooCommerce crawl spends `max_pages` on `?add-to-cart=` links. Exact-origin scoping: a seed at `example.com` that 301s to `www.example.com` discovers **zero** links (ADR-010's eTLD+1 rule covers sitemap only; BFS has the same problem) | `coordinator/coordinator/link_extractor.py:37` | `crw-crawl/src/url_filter.rs` + `url_filter_data.rs` — Tier A drop (cart/wishlist/nonce params), Tier B strip (ClearURLs tracking list), host overrides; `-`/`_` folded to one canonical key |
| **B5** | **Sitemap: no index support, no cap.** `findall(".//loc")` on a `<sitemapindex>` returns *child sitemap URLs*, which get enqueued as pages. No gzip, no size cap (`ET.fromstring` on whatever arrives), no `/sitemap.xml` fallback when robots has no `Sitemap:` line. (CLAUDE.md already says the `CrawlWorkflow` port must *modify* `sitemap.py` — aiohttp→httpx — this is the rest of the list) | `coordinator/coordinator/sitemap.py:51` | `crw-crawl/src/sitemap.rs` — `page_urls` vs `child_sitemaps` kept apart; recursion with a concurrency cap of 8; cross-origin sitemap redirects rejected; 50 MB body cap and **bounded gzip decompression** (bomb protection); canonical `/sitemap.xml` always tried |
| **B6** | **Structured extraction has no provenance.** | `llm-worker` returns the model's object as-is | crw `basis` (its ADR-0001): per field `supported \| unverified \| unsupported \| notFound`, with a verbatim excerpt **verified server-side by containment** against a hash of the exact bytes sent to the model; the model's claimed URL/title never reaches the wire. Bigger feature — a PRD-016 follow-on, not Phase 4 |

### C. Mechanisms to borrow into the Temporal design

Shapes, not bugs. Each maps onto a decision made during the migration.

| crw mechanism | What it is | Where it lands in Phase 4 |
|---|---|---|
| **`Deadline`** (`crw-core/src/deadline.rs`) | One absolute instant built at request entry; every layer that sleeps/retries/waits clamps its own timeout to `remaining()`. Report `requested_ms`, never the overrun — they shipped "timed out after 1ms" on a 30 s budget | PRD-016 R4 "time budgets must compose" — this is the mechanism. The *LLM cold starts* row says the activity timeout must exceed warm-up + request; a `Deadline` is how the activity *knows* that at each step |
| **`ScrapeClass` + `ReservedSemaphore`** (`scrape_class.rs`, `reserved_sem.rs`) | Interactive vs batch traffic class, set by the **job entry point** (never the wire, so a client cannot jump the lane). Two-lane semaphore — Postgres `superuser_reserved_connections` shape: batch acquires a gate of `total − reserved` first, so it can never hold the reserved slice. Applied at every chokepoint: per-host, render pool, extract, LLM (`llm_gate.rs`) | Today a 100-URL batch and a single scrape share one NATS subject FIFO. Temporal task queues make the split cheap — two queues per engine, or a reserved worker-slot count. **Decide before the batch cutover** |
| **`host_limiter`** (`crw-renderer/src/host_limiter.rs`) | Per-eTLD+1 RPS + in-flight cap, process-wide, shared by scrape and crawl; first-write-wins, GC'd on idle | ScrapeFlow has **no per-target-host politeness** — only per-user API rate limits. A crawl hammers its target as fast as the worker drains. `CrawlWorkflow` needs this and Temporal's per-workflow rate limiting will not give it — the limit must be per *host*, across crawls and users (Redis, or a Postgres row keyed on eTLD+1) |
| **`egress.rs` latch + `preference.rs`** | TTL cache: a host that hard-blocked direct egress is *preferred* onto proxy for 10 min; the TTL expiry *is* the half-open probe. Writes are direct-only (a block seen through a proxy says nothing about direct). Never suppresses direct — a `DIRECT_FALLBACK_RESERVE` of the deadline is always kept. Preference learning promotes a host to a heavier renderer after 3 failures in 15 min, counting only failure kinds that are the renderer's fault | BUG-003's "Middle" tier. The insight: **per-host memory across requests** — without it every URL on a blocking host re-climbs the whole ladder (10–20 s per URL on a 429ing site) |
| **`BreakerOutcome`** (`breaker.rs`) | The circuit breaker takes a classified outcome, not `bool`. `DeadlineClamped` (parent budget ran out) and `SiteBlocked` (the origin refused *us*) **never** advance the failure window — a blocked host was tripping the breaker for every renderer and stranding the one tier that could serve it | Your transient/terminal classifiers are the same idea one level down. When Temporal retry policies replace `nak`-with-backoff, the classification must survive as *which errors are retryable*, and "blocked" must not count against worker health |
| **Escalation ladder + auto-detect** (`detector.rs`, `lib.rs`) | `needs_js_rendering` (SPA-shell heuristics) gated by `warrants_browser_retry` — only escalate if the page ships executable JS or a meta-refresh; a thin static page gains nothing from a browser. Post-render: `looks_like_failed_render` (Next.js error boundary, empty `__next` root) and `looks_like_loading_placeholder` (spinner) — the renderer returned *early*. A hard-pinned renderer never silently falls back (runtime invariant) | Today the user picks `engine`; an SPA on `engine=http` is a "completed" shell. **Second-order effect: the ladder changes the economics of detection.** crw's antibot posture is "false positives are cheap, the fallback rescues them"; ScrapeFlow's is conservative because a false "blocked" fails a working job. Once a next tier exists, Tier 2's 20 KB gate can loosen |
| **`/v1/capabilities`** (`crw-server/src/routes/capabilities.rs`) | Every boolean derived from the build + effective config; "can this instance do X with no extra credentials"; BYOK reported separately | The admin SPA probes `/admin/users?limit=1` to detect admin. A capabilities endpoint is the general form; frontend and MCP stop hardcoding what the backend supports |
| **Error taxonomy** (`crw-core/src/error.rs`, `crw-server/src/error.rs`) | Typed enum → `error_code` string → HTTP status, one test per arm so a new variant cannot fall to 500. `reqwest_message()` strips the URL from provider errors so internal endpoints/credentials never reach the API caller | `job_runs.error` is free text with one contract (`blocked:<vendor>`). BUG-003 wants structured reasons; Q8 wants state-machine cleanup. A closed set of error codes is the cheap version |
| **Source-of-truth matrix** (`AGENTS.md`, crw ADR-0002) | Every derived surface (OpenAPI, config docs, CLI reference, capability docs) names its one generator; CI drift-checks | `contracts/` is this for the wire. The same rule for `api/app/messages.py` ↔ `coordinator/coordinator/messages.py` (the deliberate duplicate) would be a drift *check* rather than a comment |
| `clearance.rs` | Caches `cf_clearance` per `(host, proxy)` and replays it via CDP `Network.setCookie` — N challenge solves become 1 | Playwright activity, later |
| `page_cache.rs` | Caches the **fetch** (never the extraction) with per-reader `maxAge`; a multi-schema extraction pays the network once | PRD-016 pipelines where scrape → clean → LLM are separate blocks |
| `impersonated.rs`, `locale.rs` | Chrome JA3/HTTP2 fingerprint with no browser (a hop between plain HTTP and the browser ladder, fired only on wall-shaped responses); `Accept-Language` + timezone aligned to the proxy exit country (an IP/locale mismatch is a stronger bot signal than a flagged IP) | Cheap; ScrapeFlow has neither. ADR-008's headed real Chrome is the stronger posture where it applies |

### D. Where ScrapeFlow is ahead

- **Durability.** crw loses every in-flight crawl on restart. `crawl_queue` in Postgres and the Temporal migration are a different league.
- **Tenancy, quotas, the storage ledger, cancellation precedence, the two-writers rule.** crw has no per-user anything.
- **Worker contract testing.** `contracts/` feeding real producer payloads into real parsers (Go included) is stronger than crw's conformance tests, which check HTTP shapes only.
- **Transient/terminal storage-fault classification on all three workers** and the LLM cold-start handling — crw has no object store, so no equivalent.
- **Headed real Chrome under Xvfb with Patchright** (ADR-008) vs crw's LightPanda-first ladder.

### When this is picked up

1. **File A1 + A3 + A4 as bugs** (SSRF at fetch time incl. redirects; no wall detection on `http`; no content-type check). All three are "port into the activity, *with* the fix" — the same class as the SSRF/bot-wall items in CLAUDE.md's *do-not-delete* list.
2. **File A2 as a bug** (robots wildcards + query matching + engine disagreement). v1 correctness, independent of the migration, small.
3. **Add C's per-host limiter and interactive/batch lanes to the `CrawlWorkflow` / batch-cutover design notes** (ADR-010's neighbourhood). Both are hard to retrofit after the task queues are shaped.
4. B1–B6 are PRD-016 / PRD-018 inputs. Hand the Clean-block PM the `quality.rs` constants *with their comments*, not the code.
