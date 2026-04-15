# PRD-010 — MCP Server

**Priority:** P2
**Source:** CLAUDE.md Phase 3 + PHASE3_DEFERRED.md
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

ScrapeFlow's primary use case is feeding structured data into ML/LLM pipelines. Currently, an LLM agent wanting to trigger a scrape must make raw HTTP API calls — which requires custom integration code in every agent that uses ScrapeFlow. Firecrawl now ships native MCP (Model Context Protocol) support, making the entire scraping platform callable from Claude, Cursor, or any MCP-compatible agent with zero integration code.

An MCP server for ScrapeFlow would make the platform directly callable from the same LLM toolchains that consume its output — closing the loop between "trigger a scrape" and "use the result" within a single agent session.

---

## Goals

1. Expose ScrapeFlow's core operations as MCP tools callable by any MCP-compatible LLM client.
2. Authenticate via API key (the user's existing ScrapeFlow API key, passed through the MCP tool call).
3. Cover the core job lifecycle: submit a scrape, check status, retrieve results, list recent jobs.
4. Deploy as a standalone process alongside the API (not inside the API).

---

## Non-goals

- MCP resource or prompt primitives (tools only in Phase 3)
- Real-time streaming results via MCP (MCP tools are request/response; streaming is a separate protocol concern)
- A hosted/shared MCP server endpoint (each user runs their own instance against their ScrapeFlow deployment)
- MCP tool discovery / dynamic tool registration (static tool set)
- Batch or crawl MCP tools (Phase 4 — cover single-URL scraping first)

---

## User stories

**As a Claude user** with Claude Desktop configured to use ScrapeFlow's MCP server, I want to say "scrape https://example.com and give me the Markdown" and have Claude trigger the scrape, poll for completion, and return the result — all within the conversation.

**As a developer** building an AI agent in Python, I want to add ScrapeFlow as an MCP tool so my agent can decide when to trigger a scrape and process the result without me writing any HTTP integration code.

**As a platform operator**, I want to run the MCP server as a Docker container pointed at my ScrapeFlow API — and hand users a single connection string to add to their MCP client config.

---

## Requirements

### MCP tools (Phase 3 scope)

#### `scrape_url`

Triggers a scrape job and waits for completion (or returns the job ID for async polling).

Input schema:
```json
{
  "url": "string (required)",
  "output_format": "html | markdown | json (default: markdown)",
  "engine": "http | playwright (default: http)",
  "wait_for_result": "boolean (default: true)"
}
```

Behavior:
- If `wait_for_result: true`: submits job, polls `GET /jobs/{id}` until terminal status, returns content inline (up to 50KB; truncate with notice if larger)
- If `wait_for_result: false`: returns `{"job_id": "...", "status": "queued"}` immediately

Returns: `{"job_id": "...", "status": "completed", "content": "..."}`

#### `get_result`

Retrieves the result for a completed job.

Input schema:
```json
{
  "job_id": "string (required)"
}
```

Returns: `{"job_id": "...", "status": "...", "content": "...", "error": "..."}`

#### `list_jobs`

Lists recent jobs for the authenticated user.

Input schema:
```json
{
  "status": "queued | running | completed | failed (optional filter)",
  "limit": "int 1–20 (default: 10)"
}
```

Returns: array of `{job_id, url, status, created_at, output_format}`

#### `get_job_status`

Returns the current status of a job without fetching the full result (useful for polling in long-running tool loops).

Input schema:
```json
{
  "job_id": "string (required)"
}
```

Returns: `{"job_id": "...", "status": "...", "created_at": "...", "completed_at": "..."}`

### Authentication

The MCP server authenticates against ScrapeFlow using a user's API key. Configuration:

```json
{
  "mcpServers": {
    "scrapeflow": {
      "command": "scrapeflow-mcp",
      "env": {
        "SCRAPEFLOW_API_URL": "https://scrapeflow.govindappa.com",
        "SCRAPEFLOW_API_KEY": "sf_..."
      }
    }
  }
}
```

The MCP server passes the API key as a `Bearer` token to all ScrapeFlow API calls. No separate MCP auth mechanism.

### Implementation

- Language: Python (consistent with the API; uses `mcp` Python SDK — the reference implementation)
- Process: standalone executable (`scrapeflow-mcp`) invoked by the MCP host via stdio transport
- Distribution: Docker image + pip-installable package
- Polling interval for `wait_for_result: true`: 2 seconds, max wait 120 seconds (configurable via env)
- No persistent state — the MCP server is stateless; all state is in the ScrapeFlow API

### Content truncation

LLM context windows are finite. When returning content inline:
- Truncate at 50KB (configurable via env `MCP_MAX_CONTENT_BYTES`)
- Append a notice: `"[Content truncated at 50KB. Use get_result(job_id) with a direct API call to retrieve full content.]"`

### Error handling

MCP tool errors are returned as structured error responses (not exceptions):
```json
{"error": "job_failed", "message": "Scrape failed: connection timeout", "job_id": "..."}
```

Common errors:
- `job_failed` — scrape worker returned an error
- `timeout` — `wait_for_result` polling exceeded max wait
- `unauthorized` — invalid API key
- `not_found` — job_id does not exist or belongs to another user

---

## Success criteria

- [ ] Claude Desktop configured with the MCP server can trigger a scrape and return results in a single conversation turn
- [ ] `scrape_url` with `wait_for_result: true` returns content inline for a URL that completes within 30 seconds
- [ ] `scrape_url` with `wait_for_result: false` returns a job_id immediately
- [ ] `list_jobs` returns the 10 most recent jobs with correct status
- [ ] Invalid API key returns `unauthorized` error in MCP tool response (not an unhandled exception)
- [ ] Content > 50KB is truncated with a notice
- [ ] MCP server runs as a Docker container pointed at the production API URL

---

## Open questions for Architect

1. The `mcp` Python SDK uses stdio transport by default. Should Phase 3 also support SSE (HTTP-based transport) for browser-based MCP clients, or is stdio sufficient for the target use case (CLI + Claude Desktop)?
2. Should the MCP server be a separate repository / package, or live in the `scrapeflow` monorepo under `mcp/`?
3. Batch scraping (PRD-006) and site crawl (PRD-007) are not in Phase 3 MCP scope. Should the tool descriptions hint at their absence ("single URL only") or be silent about it?
