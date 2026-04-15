# ADR-004: Phase 3 Fat Message Schema v2

**Status:** Accepted
**Date:** 2026-04-15
**Deciders:** @karthik

---

## Context

ADR-002 defined the Phase 2 fat message schema — a flat JSON object containing all fields a worker needs to execute a scrape without a DB lookup. Phase 2 fields:

```json
{
  "job_id": "uuid",
  "run_id": "uuid",
  "url": "string",
  "output_format": "html|markdown|json",
  "engine": "http|playwright",
  "llm_config": { ... } | null,
  "playwright_options": { ... } | null
}
```

Phase 3 adds fields across multiple PRDs:

| PRD | New fields |
|-----|-----------|
| PRD-004 robots.txt | `respect_robots: bool` |
| PRD-005 Proxy rotation | `proxy_url: string \| null` |
| PRD-008 Authenticated scraping | `cookies: array \| null` |
| PRD-009 Page actions | `actions: array \| null` |
| PRD-007 Site crawl | `crawl_id`, `crawl_page_id`, `depth` |

Without a versioning strategy, the flat message schema will drift between what the API sends and what workers expect during rolling upgrades — especially if a Phase 3 worker is deployed before the API (or vice versa).

Two questions were raised and answered before this ADR was written:

1. Should the fat message adopt an explicit `schema_version` field?
2. Should the flat structure be grouped into sub-objects?

---

## Decisions

### 1. Add `schema_version` field

The fat message gains a top-level `schema_version: int` field.

- Phase 2 messages: no `schema_version` field (implicitly version 1)
- Phase 3 messages: `schema_version: 2`

**Worker backward-compatibility contract:**
- A worker receiving a message with no `schema_version` field treats it as version 1
- A worker receiving `schema_version: 2` processes it normally
- A worker receiving an unknown version (> its highest known version) logs a structured warning and processes best-effort — it does not nack or discard the message, since the unknown fields are additive and the core fields (`job_id`, `run_id`, `url`, `engine`) are stable

This is a one-way contract: workers must be deployed before the API sends v2 messages. The deployment order for Phase 3 rollout is: workers first, API second.

### 2. Group fields into sub-objects

The Phase 3 fat message adopts a nested structure. Fields that represent a category of concern are grouped rather than added flat.

**Full Phase 3 schema (schema_version: 2):**

```json
{
  "schema_version": 2,
  "job_id": "uuid",
  "run_id": "uuid",
  "url": "string",
  "output_format": "html|markdown|json",
  "engine": "http|playwright",
  "llm_config": { ... } | null,
  "playwright_options": { ... } | null,
  "credentials": {
    "proxy_url": "http://user:pass@host:port" | null,
    "cookies": [{"name": "...", "value": "...", ...}] | null
  },
  "options": {
    "respect_robots": true | false,
    "actions": [...] | null
  },
  "crawl_context": {
    "crawl_id": "uuid",
    "crawl_page_id": "uuid",
    "depth": 2
  } | null
}
```

**Rules:**
- `credentials` is always present (both fields nullable). Workers that don't support credentials ignore it.
- `options` is always present (`respect_robots` defaults to `false`, `actions` is nullable). Workers that don't support options ignore it.
- `crawl_context` is `null` for all non-crawl jobs. Workers that don't handle site crawl ignore it.
- `llm_config` and `playwright_options` remain at the top level for backward compatibility — they are not moved into sub-objects.

### 3. Worker field handling rule

Workers **must not fail** on unrecognised fields. They parse only the fields they need. This allows the API to be deployed with new fields before workers are updated.

---

## Consequences

**Positive:**
- Rolling upgrades are safe: old workers ignore new fields, new workers handle old messages gracefully
- The message structure reflects semantic groupings — easier for engineers to understand what each block is for
- `schema_version` provides an explicit audit trail of contract changes

**Negative:**
- Workers that use strict JSON deserialization (e.g. Go struct with `json:",omitempty"`) must use permissive parsing or pointer fields for the new sub-objects — the Go HTTP worker will need its message struct updated to include `Credentials`, `Options`, and `CrawlContext` as nullable structs
- The nesting is a mild breaking change in message structure — any external tooling that inspects NATS messages must be updated

**Implementation note for Go worker:**
Use pointer structs for the new sub-objects so they deserialise to `nil` when absent:
```go
type ScrapeMessage struct {
    SchemaVersion  int              `json:"schema_version"`
    JobID          string           `json:"job_id"`
    RunID          string           `json:"run_id"`
    URL            string           `json:"url"`
    OutputFormat   string           `json:"output_format"`
    Engine         string           `json:"engine"`
    LLMConfig      *LLMConfig       `json:"llm_config"`
    PlaywrightOpts *PlaywrightOpts  `json:"playwright_options"`
    Credentials    *Credentials     `json:"credentials"`
    Options        *Options         `json:"options"`
    CrawlContext   *CrawlContext    `json:"crawl_context"`
}
```
