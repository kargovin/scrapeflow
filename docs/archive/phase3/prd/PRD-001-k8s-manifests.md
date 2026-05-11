# PRD-001 — K8s Manifests: Phase 2 Services

**Priority:** P1
**Source:** CLAUDE.md Phase 3 + PHASE3_DEFERRED.md
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

Phase 2 introduced three new long-running processes (playwright-worker, llm-worker) and one periodic maintenance script (cleanup_old_runs.py). These services run in Docker Compose locally but have no Kubernetes manifests. The FluxCD GitOps pipeline on `main` still deploys only Phase 1 services. Production is effectively running Phase 1 code at `scrapeflow.govindappa.com`.

Phase 3 cannot ship until Phase 2 is promoted to production.

---

## Goals

1. Deploy all Phase 2 services to the k3s homelab cluster under namespace `scrapeflow`.
2. Maintain the same GitOps pattern already working for Phase 1 (FluxCD, infra repo at `govindappa-k8s-config`).
3. Ensure playwright-worker has sufficient memory to run Chromium without OOM evictions.
4. Ensure the cleanup CronJob runs on a predictable schedule without manual intervention.

---

## Non-goals

- Horizontal pod autoscaling (static replica counts are sufficient for Phase 3; HPA is Phase 4)
- Ingress or public endpoints for workers (workers are internal; only the API has external ingress)
- Secrets management overhaul (existing pattern reused)

---

## User stories

**As the platform operator**, I want to `git push` Phase 2 code and have FluxCD automatically deploy playwright-worker, llm-worker, and the cleanup job to k3s — the same way it deploys the API today.

**As the platform operator**, I want the playwright-worker to restart automatically if it crashes without manual `kubectl rollout restart`.

---

## Requirements

### playwright-worker Deployment

| Field | Value |
|-------|-------|
| Namespace | `scrapeflow` |
| Image | same image/tag pattern as existing workers |
| Replicas | 1 initially |
| Memory request | 512Mi |
| Memory limit | 1.5Gi (Chromium needs headroom) |
| CPU request | 250m |
| CPU limit | 1000m |
| Restart policy | Always (via Deployment) |
| Shared memory | `/dev/shm` volume mount, 512Mi (Chromium `--shared-memory-size` equivalent in k8s) |
| Liveness probe | NATS consumer health check or simple process check |
| Env source | Same `scrapeflow-env` Secret pattern as API |

### llm-worker Deployment

| Field | Value |
|-------|-------|
| Namespace | `scrapeflow` |
| Replicas | 1 initially |
| Memory request | 128Mi |
| Memory limit | 512Mi |
| CPU request | 100m |
| CPU limit | 500m |
| Restart policy | Always |
| Note | LLM API keys come from per-job payload (fat message), not worker env |

### cleanup CronJob

| Field | Value |
|-------|-------|
| Schedule | `0 3 * * *` (3 AM daily) |
| Namespace | `scrapeflow` |
| Concurrency policy | Forbid |
| Restart policy | OnFailure |
| Success history | 3 |
| Failure history | 1 |

### FluxCD integration

- Manifests go into the infra repo at `/home/karthik/Documents/govindappa/govindappa-k8s-config`
- Same Kustomization pattern already used for Phase 1 services
- Must not require manual `kubectl apply`

---

## Success criteria

- [ ] `kubectl get pods -n scrapeflow` shows playwright-worker, llm-worker running and READY
- [ ] cleanup CronJob appears in `kubectl get cronjobs -n scrapeflow`
- [ ] A full end-to-end Playwright job submitted via API completes successfully in production
- [ ] playwright-worker pod does not OOM on a standard Playwright scrape
- [ ] FluxCD reconciliation picks up manifest changes within the standard sync interval

---

## Open questions for Architect

1. Should playwright-worker use a `StatefulSet` (for stable pod identity with NATS consumer group) or `Deployment`? Phase 2 NATS consumer groups may have ordering expectations.
2. Does `/dev/shm` need to be mounted as `emptyDir: { medium: Memory }` or is `hostPath` more appropriate for k3s single-node?
3. Should the cleanup CronJob share the API image (with a different entrypoint) or be a standalone image?
