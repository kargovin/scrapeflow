# PRD-003 — Security: SSRF Re-validation on Webhook Delivery

**Priority:** P1
**Source:** PHASE3_DEFERRED.md
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

Phase 2 validates `webhook_url` for SSRF (Server-Side Request Forgery) only at job creation time (`POST /jobs`). This creates a DNS rebinding window: an attacker registers a domain that resolves to a public IP during job creation, then changes the DNS record to an internal address (e.g. `169.254.169.254` cloud metadata, `10.x.x.x` internal services) before webhook delivery fires.

When the webhook delivery loop makes the HTTP request, it resolves the domain again — and now hits an internal target.

This was explicitly deferred from Phase 2 as an acceptable risk at MVP scale with known users. Phase 3 opens the platform further and this must be closed before that happens.

---

## Goals

1. Re-validate `webhook_url` against SSRF rules immediately before each delivery attempt — not just at job creation.
2. If the re-resolved IP is private/reserved, fail the delivery attempt with a clear error and log the incident.
3. No change to the webhook retry schedule or HMAC signing — only the validation gate changes.

---

## Non-goals

- Rate limiting outbound webhook requests (separate concern)
- Verifying TLS certificates on webhook endpoints (acceptable risk for user-configured endpoints)
- Detecting rebinding in real time (re-validation on each attempt is sufficient)

---

## User stories

**As a platform operator**, I want to know if a webhook_url silently resolves to an internal IP at delivery time, and I want that delivery attempt to fail loudly rather than silently contacting internal infrastructure.

**As a security reviewer**, I want SSRF protection to apply at the point where the network request actually leaves the server, not only at configuration time.

---

## Requirements

### Re-validation on every delivery attempt

Before the webhook delivery loop makes any HTTP request:
1. Resolve the hostname of `webhook_url`
2. Check the resolved IP(s) against the existing SSRF blocklist (RFC 1918, loopback, link-local, metadata ranges)
3. If any resolved IP is blocked:
   - Mark the delivery attempt as `failed` with `error = "ssrf_blocked"`
   - Log the event including `job_id`, `webhook_url`, `resolved_ip`, `timestamp`
   - Do NOT retry (rebinding is likely intentional; retry would burn delivery attempts)
   - Notify via the existing admin logging path

### Delivery attempt record

Add a new `failure_reason` field to the webhook delivery table (or equivalent) to distinguish SSRF failures from network errors and non-2xx responses. The Architect will decide the exact schema.

### Existing job creation validation

Keep the existing SSRF check at `POST /jobs`. This is a first-line filter, not a replacement for delivery-time re-validation.

---

## Success criteria

- [ ] A webhook_url pointing to a domain that rebinds to `10.0.0.1` after job creation fails delivery with `ssrf_blocked` rather than making the request
- [ ] A webhook_url pointing to `169.254.169.254` directly (AWS metadata) is blocked at delivery time
- [ ] Legitimate webhook URLs (public IPs, public hostnames) continue to deliver normally
- [ ] The failed delivery is visible in admin logs with the resolved IP recorded
- [ ] Unit tests cover loopback, RFC 1918, link-local, and metadata IP ranges

---

## Open questions for Architect

1. Should the SSRF validator be a shared utility called from both the `POST /jobs` handler and the delivery loop, or should they have separate implementations tuned for their different contexts (sync vs. async)?
2. Should SSRF-blocked deliveries still count against the retry budget, or should they be a hard permanent failure that removes the webhook_url from future jobs?
