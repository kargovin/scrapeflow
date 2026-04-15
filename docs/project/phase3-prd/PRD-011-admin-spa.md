# PRD-011 — Admin SPA

**Priority:** P3
**Source:** CLAUDE.md Phase 3 + PHASE3_DEFERRED.md
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

The admin API routes (Steps 23–24 in Phase 2) expose user management, job oversight, and usage stats — but only to callers who can make HTTP requests directly. There is no UI. Operating the platform requires knowing the API surface and crafting curl commands. This is not acceptable for a multi-user deployment or a portfolio project that should demonstrate end-to-end product engineering.

---

## Goals

1. A React SPA that wraps the existing `/admin/*` API endpoints.
2. Covers: user list, per-user job list, platform usage stats, quota management, force-cancel, webhook retry.
3. Accessible only to admin users (same auth as the admin API routes).
4. Ships as a static build served by Nginx or via Traefik in the k3s cluster.

---

## Non-goals

- User-facing (non-admin) dashboard — that is a separate product decision (Phase 4)
- Real-time push updates in the SPA (polling is acceptable for Phase 3)
- Mobile responsiveness (desktop browser only)
- i18n or accessibility compliance beyond semantic HTML

---

## User stories

**As an admin**, I want to view all users and their job counts, last active time, and quota status in a table — without making API calls manually.

**As an admin**, I want to click into a user and see their recent jobs, force-cancel a stuck job, or manually retry a failed webhook delivery.

**As an admin**, I want a stats dashboard showing total jobs run, error rate, and MinIO storage usage across the platform.

**As an admin**, I want to set or adjust quota limits for individual users.

---

## Requirements

### Pages / views

| Route | Content |
|-------|---------|
| `/admin` | Platform overview: total users, jobs today, error rate, storage used |
| `/admin/users` | Paginated user list: id, email, job count, quota used/limit, last active |
| `/admin/users/{id}` | User detail: recent jobs, API keys list, quota config, suspend/activate toggle |
| `/admin/jobs` | Cross-user job list with status filter; link to job detail |
| `/admin/jobs/{id}` | Job detail: runs history, force-cancel button, webhook delivery history + retry button |

### Auth

- SPA uses the existing Clerk JWT (the admin checks in the API middleware remain the same)
- No new auth mechanism; if the logged-in user is not an admin, the SPA shows a 403 page

### API surface consumed

The SPA uses only existing API endpoints:
- `GET /admin/users`, `GET /admin/users/{id}`
- `GET /admin/jobs`, `GET /admin/jobs/{id}`
- `GET /admin/stats`
- `DELETE /admin/jobs/{id}` (force cancel)
- `POST /admin/webhooks/{delivery_id}/retry`
- `PATCH /admin/users/{id}` (quota update — this endpoint may need to be added to the API; Architect confirms)

### Housekeeping items resolved by this PRD

These deferred items from PHASE3_DEFERRED.md are decided in the context of building the Admin SPA:

1. **`api_keys (user_id, name)` uniqueness** — Implement the `UniqueConstraint` migration and 409 response. The Admin SPA's user detail page lists API keys by name; duplicates would be confusing.
2. **`jobs.updated_at` maintenance** — Decision: implement Option B (wire it up — ensure all mutation paths set `updated_at` explicitly). The Admin SPA's job list sorts by `updated_at`; stale data would mislead operators. A DB trigger (Option C) would be more robust — Architect decides between B and C.

### Tech stack

- React 18 + TypeScript
- Vite for build tooling
- React Query for data fetching and cache management
- Tailwind CSS (utility-first, no component library dependency in Phase 3)
- Located at `frontend/` in the monorepo

### Deployment

- Static build served at `/admin` path
- Traefik route in k3s config (infra repo)
- Build step added to CI

---

## Success criteria

- [ ] Admin can view all users and navigate to a user's job history
- [ ] Admin can force-cancel a running job from the job detail page
- [ ] Admin can retry a failed webhook delivery
- [ ] Admin can view and edit a user's quota limits
- [ ] A non-admin user who navigates to `/admin` sees a 403 page, not an error
- [ ] Stats dashboard shows accurate aggregate numbers (verified against direct API call)
- [ ] `api_keys` uniqueness constraint prevents duplicate key names per user
- [ ] `jobs.updated_at` is accurate after a cancel or scheduler update

---

## Open questions for Architect

1. Should the SPA be served by the FastAPI app (as a static mount under `/admin`) or as a separate Nginx deployment? The latter is cleaner but adds another k3s service to manage.
2. `PATCH /admin/users/{id}` for quota updates — does this endpoint exist in Phase 2, or does it need to be added? Confirm before the Engineer starts the SPA.
3. Is Clerk's session cookie compatible with the SPA making API requests to the same domain? Or does the SPA need to read the JWT from localStorage and attach it as a Bearer header?
