# PRD-014 — WebSocket Real-time Job Tracking

**Priority:** P3
**Source:** NEW — identified from firecrawl (`WS /crawl/{id}` endpoint)
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

Clients currently poll `GET /jobs/{id}` to track job progress. For long-running Playwright jobs (10–30 seconds) or site crawls (minutes), polling is wasteful and adds latency between completion and the client knowing. The Admin SPA (PRD-011) also needs real-time updates for its job list to be useful without manual refreshes.

Firecrawl ships a `WS /crawl/{id}` endpoint for live crawl tracking. The same pattern applied to ScrapeFlow single jobs and batch jobs would eliminate polling from both user integrations and the Admin SPA.

---

## Goals

1. WebSocket endpoint for real-time status updates on a single job run.
2. WebSocket endpoint for real-time progress on a batch job (PRD-006).
3. Admin SPA (PRD-011) uses WS for its job list — no manual refresh.
4. No breaking change to the existing REST polling API (WebSocket is additive).

---

## Non-goals

- WebSocket for site crawl (PRD-007) in Phase 3 — site crawl WS is Phase 4
- Persistent WebSocket connections (client reconnects on disconnect; no server-side session management)
- Push notifications beyond job status (no arbitrary event streaming)

---

## User stories

**As a user** polling a Playwright job, I want to open a WebSocket connection and receive a message the moment the job completes — instead of polling every 2 seconds for 30 seconds.

**As the Admin SPA**, I want to subscribe to a batch job's WebSocket feed and update the progress bar live as URLs complete — without polling.

---

## Requirements

### Endpoints

`WS /jobs/{job_id}/watch`
- Authenticates via query param `?token={api_key_or_jwt}` (WebSocket handshake can't set headers in most browser environments)
- Streams status update messages until the job reaches a terminal state, then closes the connection
- Client can also close the connection at any time

`WS /batch/{batch_id}/watch` (requires PRD-006)
- Same auth pattern
- Streams per-item completion events and aggregate progress updates

### Message format

Status update message (JSON):
```json
{
  "type": "status_update",
  "job_id": "...",
  "status": "running",
  "updated_at": "2026-04-15T10:30:00Z"
}
```

Completion message:
```json
{
  "type": "completed",
  "job_id": "...",
  "status": "completed",
  "result_url": "/jobs/{id}/result",
  "completed_at": "2026-04-15T10:30:05Z"
}
```

Error message:
```json
{
  "type": "failed",
  "job_id": "...",
  "status": "failed",
  "error": "timeout",
  "completed_at": "..."
}
```

Batch progress message:
```json
{
  "type": "batch_progress",
  "batch_id": "...",
  "total": 50,
  "completed": 32,
  "failed": 3,
  "latest_item": {"url": "...", "status": "completed"}
}
```

### Implementation approach

The Architect decides the exact mechanism. PM requirement: status changes must be pushed to connected WS clients within 1 second of the status change occurring in Postgres or being published to the result consumer.

Options (not prescriptive — Architect decides):
- PostgreSQL `LISTEN/NOTIFY` → FastAPI WebSocket push
- Redis pub/sub → FastAPI WebSocket push
- Polling a job status table at the FastAPI layer (simple but adds 1s latency)

### Connection lifecycle

1. Client connects → server sends current status immediately
2. Server pushes status_update messages as the job progresses
3. When job reaches terminal state: server sends completed/failed message, then closes connection
4. If job is already in terminal state when client connects: send the terminal message immediately and close

### Auth

- API key in query param: `?token=sf_abc123` — validated same as Bearer token in REST API
- JWT in query param: `?token={jwt}` — same Clerk JWT verification
- Unauthorized: close with 4001 code and message "unauthorized"
- Job belonging to another user: close with 4004 code and message "not found"

---

## Success criteria

- [ ] A client connecting to `WS /jobs/{id}/watch` receives a `completed` message within 1s of the job completing
- [ ] A client connecting to a job already in terminal state receives the terminal message immediately
- [ ] Unauthorized token returns 4001 close code
- [ ] Job belonging to another user returns 4004 close code
- [ ] Admin SPA uses the WS endpoint to update the job list in real time (no polling)
- [ ] WS connection closes cleanly when the job reaches terminal state

---

## Open questions for Architect

1. FastAPI supports WebSockets natively via `starlette`. Is the existing single-worker Uvicorn deployment sufficient for Phase 3 WebSocket connections, or does WebSocket support require a change to the ASGI configuration?
2. PostgreSQL `LISTEN/NOTIFY` is the most architecturally consistent choice (no new infra) but requires a persistent async listener in the API process. Is this compatible with how the API currently manages database sessions?
3. How many concurrent WS connections should Phase 3 support? For a single-user homelab, 10–20 concurrent connections is a reasonable target. Does this affect the choice of notification mechanism?
