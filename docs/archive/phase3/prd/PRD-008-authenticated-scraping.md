# PRD-008 — Authenticated Scraping: Cookie and Session Injection

**Priority:** P2
**Source:** PHASE3_DEFERRED.md (deferred from Phase 2)
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

Many useful scraping targets are behind authentication: internal dashboards, paywalled content, SaaS tools the user has access to. ScrapeFlow currently has no way to scrape authenticated pages. Both firecrawl and crawl4ai support session state and cookie injection as first-class features.

PHASE3_DEFERRED.md already scoped this carefully. The full credential storage design (form login, session capture, re-authentication) was deferred as too large for Phase 2. This PRD implements the **narrow** version: cookie injection via `playwright_options`, as the deferred doc's "lower scope alternative."

---

## Goals

1. Allow users to pass a set of cookies (name/value pairs, or a raw `Cookie` header string) on a per-job basis.
2. Playwright worker injects cookies into the browser context before navigation.
3. Go HTTP worker injects a `Cookie` header on the HTTP request.
4. Cookies are stored encrypted at rest and never returned in API responses.
5. No session capture, no re-authentication, no credential storage (those are Phase 4).

---

## Non-goals

- Form-based login automation (Phase 4)
- OAuth flow automation (Phase 4)
- Session capture and reuse across runs (Phase 4 — that requires session state storage design)
- Automatic re-authentication when the session expires (Phase 4)
- HTTP Basic Auth (can be handled via custom headers in PRD-009 Page Actions; not a distinct feature)

---

## User stories

**As a user** who wants to scrape an internal dashboard, I want to paste my session cookie into the job config so the Playwright worker navigates to the page as an authenticated user.

**As a user**, I want my session cookie stored securely and never exposed in API responses — the same way LLM API keys work.

**As a user** running a scheduled job with a long-lived session token, I want the cookie to persist across runs without re-entering it each time.

---

## Requirements

### New job field: `cookies`

On the `jobs` table:
- `cookies: JSONB | null` — stored encrypted at rest
- Value format: array of cookie objects
  ```json
  [
    {"name": "session_id", "value": "abc123", "domain": "example.com", "path": "/", "secure": true},
    {"name": "_csrf", "value": "xyz"}
  ]
  ```
- Alternatively, users may provide a raw `cookie_header: str` (the full `Cookie: name=value; name2=value2` string) — the worker assembles it as-is into the request header

### API changes

`POST /jobs` and `PATCH /jobs/{id}` accept `cookies` in the request body.

`GET /jobs/{id}` and `GET /jobs` responses must **not** include `cookies` or `cookie_header` — write-only, same as `proxy_url`.

Include a `has_cookies: boolean` flag in the response so users can see that cookies are configured without exposing values.

### Playwright worker behavior

When `cookies` is present in the dispatch message:
1. Create the browser context as normal
2. Call `context.add_cookies(cookies)` with the cookie array before calling `page.goto(url)`
3. Cookies apply only to this context (per-run isolation is preserved)

### Go HTTP worker behavior

When `cookies` is present in the dispatch message:
1. Assemble a `Cookie: name=value; name2=value2` header string from the cookie array
2. Add to the HTTP request headers alongside any other configured headers

### Dispatch message

Decrypted cookie values are included in the fat NATS message (same pattern as LLM API keys). The worker receives plaintext — it never touches the encryption layer.

### Cookie sensitivity and encryption

Cookies must be treated at the same sensitivity level as passwords:
- Encrypted at rest using the same mechanism as `llm_api_key` (Phase 2)
- Encrypted before insert, decrypted at dispatch time
- Never logged (mask in any debug log output that shows job config)

### Validation

At `POST /jobs` time:
- Each cookie `domain` (if provided) must match the job's target URL domain or a parent domain
- Cookie `name` and `value` must be non-empty strings
- Maximum 50 cookies per job (prevent abuse)
- Cookie values are not validated for format (raw session tokens may contain arbitrary characters)

---

## Success criteria

- [ ] A Playwright job with cookies set navigates to the target URL as an authenticated user (verified by checking page content that is only visible when logged in)
- [ ] A Go HTTP worker job with cookies set includes the `Cookie` header in the request
- [ ] `GET /jobs/{id}` shows `has_cookies: true` but does not expose cookie values
- [ ] Cookie values are encrypted in the database
- [ ] Cookie from User A's job is never visible or accessible during User B's job run
- [ ] A cookie with `domain` mismatched from the target URL is rejected at `POST /jobs` with 422

---

## Open questions for Architect

1. Should the `cookies` field be stored on the `jobs` table or in a separate `job_secrets` table alongside proxy credentials (from PRD-005)? If both PRDs ship together, a unified secrets table may be cleaner than adding two new encrypted JSONB columns to `jobs`.
2. Should `cookie_header` (raw string) be supported in addition to structured cookie objects? Raw strings are more convenient for users pasting from browser DevTools, but harder to validate and domain-check.
3. Playwright's `context.add_cookies()` requires a `domain` field on each cookie. If the user doesn't provide `domain`, the worker must infer it from the job URL. Should this inference happen in the worker or in the API at dispatch time?
