# PRD-013 — Per-event Webhook Subscriptions

**Priority:** P3
**Source:** PHASE3_DEFERRED.md
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

Phase 2 fires a webhook on every event (`job.completed`, `job.failed`) if `webhook_url` is set. Users who only care about failures (e.g. alerting pipelines) receive unwanted completed events, and users who only want completions receive failure noise. Per-event filtering was deferred from Phase 2 because "low demand until there's a frontend to configure it." The Admin SPA (PRD-011) is that frontend.

---

## Goals

1. Allow users to specify which event types trigger their webhook, per job.
2. Default behavior (all events) is unchanged for jobs that don't specify a filter.
3. No change to the webhook delivery mechanism, retry logic, or HMAC signing.

---

## Non-goals

- Global/account-level webhook subscriptions (per-job only in Phase 3)
- Custom event schemas per event type (the payload format is unchanged)
- Webhook topic routing to different URLs per event (one URL per job)

---

## User stories

**As a user** running a monitoring pipeline, I want to receive a webhook only on `job.failed` events so my alerting system isn't flooded with successful run notifications.

**As a user** building a downstream data pipeline, I want to receive webhooks only on `job.completed` events so I can trigger ingestion without filtering on my end.

---

## Requirements

### New job field: `webhook_events`

On the `jobs` table:
- `webhook_events: ARRAY(Enum) | null`
- Values: `job.completed`, `job.failed`, `change.detected` (the change detection event from Phase 2)
- `null` or empty array = subscribe to all events (existing behavior, backwards compatible)

### API changes

`POST /jobs` and `PATCH /jobs/{id}` accept `webhook_events` in the request body:
```json
{
  "webhook_url": "https://...",
  "webhook_events": ["job.failed"]
}
```

Validation: each element must be a known event type; unknown values return 422.

### Delivery logic change

In the webhook delivery loop, before enqueuing a delivery:
1. Check if `job.webhook_events` is set
2. If set: only enqueue delivery if the current event type is in `webhook_events`
3. If null/empty: enqueue delivery for all events (existing behavior)

This is a single `if` check in the result consumer / webhook enqueue path.

### Included in fat message

`webhook_events` included in the NATS dispatch message (the worker doesn't use it, but having it in the message keeps the message self-contained for any future worker-side filtering).

---

## Success criteria

- [ ] A job with `webhook_events: ["job.failed"]` fires a webhook on failure but not on success
- [ ] A job with `webhook_events: ["job.completed"]` fires a webhook on success but not on failure
- [ ] A job with `webhook_events: null` fires webhooks on all events (existing behavior preserved)
- [ ] An unknown event type in `webhook_events` returns 422 at job creation
- [ ] Change detection events (`change.detected`) are correctly filtered when specified

---

## Open questions for Architect

1. Should `webhook_events` be an array of strings (validated at the application layer) or a Postgres `ARRAY` of a defined enum type? The enum type is stricter but requires a migration if event types are added later.
2. Is `change.detected` the correct event name used in Phase 2 for change detection webhooks? Confirm against the Phase 2 result consumer implementation before writing the migration.
