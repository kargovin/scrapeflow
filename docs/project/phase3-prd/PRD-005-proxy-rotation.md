# PRD-005 — Proxy Rotation

**Priority:** P2
**Source:** CLAUDE.md Phase 3 + PHASE3_DEFERRED.md
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

Both Go HTTP worker and Playwright worker make outbound requests from the server's public IP. High-frequency scraping of the same domain will result in IP-based blocks or CAPTCHAs. Proxy rotation is the standard mitigation, and both crawl4ai and firecrawl treat it as table-stakes infrastructure.

CLAUDE.md already committed proxy rotation as a Phase 3 deliverable. The design decision made in Phase 2 is "pluggable proxy provider config (Bright Data, Oxylabs, etc.)".

---

## Goals

1. Allow a user to configure a proxy (URL, provider type) at the job level.
2. Workers route requests through the configured proxy — no proxy = direct request (existing behavior unchanged).
3. Design is pluggable: adding a new provider should require only configuration, not code changes.
4. The platform operator can also configure a default proxy applied to all jobs that don't specify one.

---

## Non-goals

- The platform does not purchase or manage proxy subscriptions (user provides their own credentials)
- Automatic proxy health checking or failover between providers (Phase 4)
- Residential proxy pools managed by the platform (user provides endpoint)
- IP stickiness / session persistence across multiple runs of the same job (Phase 4)

---

## User stories

**As a user** scraping a site that rate-limits by IP, I want to attach a proxy URL to my job so requests route through that proxy rather than my server's IP.

**As a platform operator**, I want to configure a default proxy for the entire platform (all jobs, unless overridden) via environment variable — so I can apply a shared proxy account without every user needing to configure one.

**As a user**, I want proxy credentials to be stored encrypted at rest and never returned in API responses (same sensitivity as LLM API keys).

---

## Requirements

### Proxy configuration model

Three levels, evaluated in order (most specific wins):

1. **Per-job proxy** — set by user on `POST /jobs` or `PATCH /jobs/{id}`
2. **Platform default proxy** — set by operator via environment variable `DEFAULT_PROXY_URL`
3. **No proxy** — direct request (current behavior)

### Per-job proxy fields

On the `jobs` table:
- `proxy_url: Text | null` — stored encrypted at rest (same mechanism as LLM API keys in Phase 2)
- `proxy_provider: Enum(generic, brightdata, oxylabs) | null` — controls any provider-specific request formatting

`proxy_url` format: `http://user:password@host:port` or `socks5://user:password@host:port`

### Proxy not returned in API responses

`proxy_url` must be redacted (omitted or masked) from all `GET /jobs` and `GET /jobs/{id}` responses. It is write-only from the user's perspective.

### Worker behavior

**Go HTTP worker:**
- Inject proxy via `http.Transport.Proxy` using `proxy_url` from the dispatch message
- No proxy → existing direct transport

**Playwright worker:**
- Inject proxy via `browser.new_context(proxy={"server": proxy_url})` at context creation
- No proxy → existing context creation without proxy

### Dispatch message change

The NATS fat message gains a `proxy_url` field (encrypted value decrypted by the API before dispatch, so the worker receives a plaintext URL — the worker never touches the DB or the encryption layer).

This is consistent with the existing pattern for LLM API keys in the fat message.

### Error handling

If the proxy is unreachable or returns a connection error:
- Worker retries using NATS redelivery (existing retry mechanism)
- After max retries: `job_run.error = "proxy_connection_failed: {error}"`
- No fallback to direct request (falling back could leak the user's server IP unexpectedly)

---

## Success criteria

- [ ] A job with `proxy_url` set routes all HTTP requests through that proxy (verified by checking requests received at a controlled proxy)
- [ ] A job without `proxy_url` but with `DEFAULT_PROXY_URL` set uses the default proxy
- [ ] A job with neither uses direct requests
- [ ] `GET /jobs/{id}` does not return `proxy_url` in the response
- [ ] A failing proxy results in job failure with `proxy_connection_failed` error — not a fallback to direct
- [ ] `proxy_url` stored in DB is encrypted at rest (same cipher as LLM API keys)

---

## Open questions for Architect

1. Should `proxy_url` be stored on the `jobs` table (alongside other job config) or in a separate `job_credentials` table (alongside authenticated scraping credentials from PRD-008)? If PRD-008 ships in the same phase, a unified credentials model may make sense.
2. The dispatch message currently has a flat structure. Grouping proxy config and credential config into a nested object would be cleaner — is this the right time to introduce that nesting, or should we keep the flat message for backwards compatibility?
3. Should `proxy_provider` be an enum (limited set) or a free-form string? The provider enum only affects header/format nuances — if there are no provider-specific transformations in Phase 3, it may be unnecessary.
