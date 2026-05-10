# ScrapeFlow — DevOps Deployment Spec

> **For:** DevOps agent deploying ScrapeFlow to the homelab k3s cluster via FluxCD GitOps
> **Date:** 2026-04-14 (Phase 2) / 2026-05-10 (Phase 3 delta appended)
> **Status:** Phase 3 complete — see §17 for the k8s changes required before merging `develop → main`

---

## 1. Context You Need to Read First

Before writing any files, read the following in order:

| File | Why |
|------|-----|
| `govindappa-k8s-config/clusters/k3s-server/kustomization.yaml` | Root kustomization — you must add `scrapeflow/` to its `resources` list |
| `govindappa-k8s-config/clusters/k3s-server/test_app/test-app-manifest.yaml` | Reference pattern for Deployment + Service + Ingress |
| `govindappa-k8s-config/clusters/k3s-server/test_app/test-app-image-repository.yaml` | Reference pattern for Flux ImageRepository |
| `govindappa-k8s-config/clusters/k3s-server/test_app/test-app-image-update-automation.yaml` | Reference pattern for Flux ImageUpdateAutomation |
| `govindappa-k8s-config/clusters/k3s-server/mlflow/mlflow-helm.yaml` | Reference pattern for Namespace + HelmRelease |
| `govindappa-k8s-config/infrastructure/sources.yaml` | Existing HelmRepositories — add bitnami here if not present |
| `scrapeflow/docker/docker-compose.yml` | Authoritative source for all service configs, env vars, and dependencies |
| `scrapeflow/.env.example` | All environment variables the API and workers consume |

---

## 2. Target Cluster

| Property | Value |
|----------|-------|
| Cluster | k3s homelab |
| GitOps tool | FluxCD (already bootstrapped) |
| GitOps repo | `/home/karthik/Documents/govindappa/govindappa-k8s-config` |
| Ingress controller | Traefik (already installed) |
| TLS | cert-manager with `letsencrypt-prod` ClusterIssuer (already installed) |
| DNS | ExternalDNS + Cloudflare (already installed) |
| Domain | `scrapeflow.govindappa.com` |
| Namespace | `scrapeflow` (create it) |
| Container registry | DockerHub — image names follow the `k4rth/<service>` pattern |

---

## 3. Services to Deploy

### 3a. Application Services (custom images — need CI/CD)

| Service | Image | Port | Ingress | Notes |
|---------|-------|------|---------|-------|
| `api` | `k4rth/scrapeflow-api` | 8000 | Yes — `scrapeflow.govindappa.com` | FastAPI; runs Alembic migrations on startup |
| `http-worker` | `k4rth/scrapeflow-http-worker` | none | No | Go binary; stateless |
| `playwright-worker` | `k4rth/scrapeflow-playwright-worker` | none | No | Needs 1.5Gi memory limit |
| `llm-worker` | `k4rth/scrapeflow-llm-worker` | none | No | Stateless Python |

### 3b. Infrastructure Services (stable images — use Bitnami Helm charts)

| Service | Helm Chart | Version | Storage | Notes |
|---------|-----------|---------|---------|-------|
| PostgreSQL | `bitnami/postgresql` | `16.x` | 10Gi PVC | Single instance; no HA needed |
| Redis | `bitnami/redis` | `7.x` | 2Gi PVC | `architecture: standalone` |
| MinIO | `bitnami/minio` | latest stable | 20Gi PVC | Object storage for scrape results |
| NATS | `nats/nats` (NATS official) | `2.10.x` | 5Gi PVC | JetStream enabled; see §6 for stream init |

Add any missing HelmRepositories to `infrastructure/sources.yaml`:
- Bitnami: `https://charts.bitnami.com/bitnami`
- NATS: `https://nats-io.github.io/k8s/helm/charts/`

---

## 4. File Layout in the GitOps Repo

Create the following directory tree. All files go under `clusters/k3s-server/scrapeflow/`:

```
clusters/k3s-server/scrapeflow/
├── kustomization.yaml                    # lists all files in this dir
├── namespace.yaml                        # Namespace: scrapeflow
├── infrastructure/
│   ├── postgres.yaml                     # HelmRelease: postgresql
│   ├── redis.yaml                        # HelmRelease: redis
│   ├── minio.yaml                        # HelmRelease: minio
│   └── nats.yaml                         # HelmRelease: nats
├── app/
│   ├── api.yaml                          # Deployment + Service + Ingress
│   ├── http-worker.yaml                  # Deployment
│   ├── playwright-worker.yaml            # Deployment
│   ├── llm-worker.yaml                   # Deployment
│   └── nats-init-job.yaml               # Job: creates SCRAPEFLOW stream
└── image-automation/
    ├── image-repositories.yaml           # ImageRepository x4
    ├── image-policies.yaml               # ImagePolicy x4
    └── image-update-automation.yaml      # ImageUpdateAutomation
```

Then add `- scrapeflow/` to the `resources` list in `clusters/k3s-server/kustomization.yaml`.

---

## 5. Secrets

All secrets must be created manually on the cluster before FluxCD reconciles. They are NOT stored in git.

Run these commands on the cluster to create the secrets:

### scrapeflow-db-credentials
```bash
kubectl create secret generic scrapeflow-db-credentials \
  --namespace scrapeflow \
  --from-literal=postgres-password=<strong-password> \
  --from-literal=postgres-user=scrapeflow \
  --from-literal=postgres-db=scrapeflow \
  --from-literal=database-url="postgresql+asyncpg://scrapeflow:<strong-password>@scrapeflow-postgresql:5432/scrapeflow"
```

### scrapeflow-minio-credentials
```bash
kubectl create secret generic scrapeflow-minio-credentials \
  --namespace scrapeflow \
  --from-literal=root-user=scrapeflow \
  --from-literal=root-password=<strong-password>
```

### scrapeflow-app-secrets
```bash
kubectl create secret generic scrapeflow-app-secrets \
  --namespace scrapeflow \
  --from-literal=clerk-secret-key=sk_live_... \
  --from-literal=llm-key-encryption-key=<fernet-key>
  # Generate fernet key: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Reference all three secrets in the app Deployments via `envFrom` or individual `env[].valueFrom.secretKeyRef` entries. Do NOT hardcode values in the YAML files.

---

## 6. NATS Stream Init

The NATS JetStream `SCRAPEFLOW` stream must exist before the API starts. Model this as a Kubernetes `Job` (not an initContainer on the API, since the stream only needs to be created once, not on every API pod restart).

**Stream parameters (from `docker/docker-compose.yml` nats-init):**
```
stream name:   SCRAPEFLOW
subjects:      scrapeflow.jobs.>
retention:     work
storage:       file
replicas:      1
```

The Job should:
1. Use image `natsio/nats-box:latest`
2. Run the idempotent create-or-edit command:
   ```sh
   if nats stream info SCRAPEFLOW --server nats://scrapeflow-nats:4222 >/dev/null 2>&1; then
     nats stream edit SCRAPEFLOW --subjects 'scrapeflow.jobs.>' --server nats://scrapeflow-nats:4222 --force;
   else
     nats stream add SCRAPEFLOW --subjects 'scrapeflow.jobs.>' --retention work --storage file --replicas 1 --server nats://scrapeflow-nats:4222 --defaults;
   fi
   ```
3. Set `restartPolicy: OnFailure`

The API Deployment should have an `initContainer` that waits for NATS to be reachable (a simple `nc -z scrapeflow-nats 4222` loop) — this guards against race conditions on pod restart.

---

## 7. Alembic Migrations

The API runs Alembic migrations on startup. The migration code is already written and tested — it just needs to be uncommented. Before writing the API Deployment, make this edit in the ScrapeFlow repo:

**File:** `api/app/main.py`, lines 37–43

Uncomment the migration block:
```python
# Alembic migrations — run in separate thread to avoid blocking the event loop, since Alembic doesn't support async DB connections.
try:
    await asyncio.get_event_loop().run_in_executor(None, _run_migrations_online)
    logger.info("Database migrations complete")
except Exception:
    logger.exception("Database migration failed")
    raise
```

Remove the `# TODO: uncomment when pushing` comment and the outer `# ` prefixes. This means migrations run automatically on every API pod start, which is the intended behavior (Alembic is idempotent on already-applied migrations).

---

## 8. Deployment Specs

### API Deployment

```yaml
# Key config — fill in the full Deployment manifest
image: k4rth/scrapeflow-api:<tag>  # Flux will manage the tag
containerPort: 8000
resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: 500m
    memory: 512Mi
livenessProbe:
  httpGet:
    path: /health/live
    port: 8000
  initialDelaySeconds: 15
  periodSeconds: 30
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 10
replicas: 1
```

**Environment variables (set these on the API Deployment):**

| Env var | Value |
|---------|-------|
| `APP_ENV` | `production` |
| `DEBUG` | `false` |
| `DATABASE_URL` | from `scrapeflow-db-credentials` secret (key: `database-url`) |
| `REDIS_URL` | `redis://scrapeflow-redis-master:6379/0` |
| `NATS_URL` | `nats://scrapeflow-nats:4222` |
| `MINIO_ENDPOINT` | `scrapeflow-minio:9000` |
| `MINIO_ACCESS_KEY` | from `scrapeflow-minio-credentials` secret (key: `root-user`) |
| `MINIO_SECRET_KEY` | from `scrapeflow-minio-credentials` secret (key: `root-password`) |
| `MINIO_SECURE` | `false` |
| `MINIO_BUCKET` | `scrapeflow-results` |
| `CLERK_SECRET_KEY` | from `scrapeflow-app-secrets` secret (key: `clerk-secret-key`) |
| `LLM_KEY_ENCRYPTION_KEY` | from `scrapeflow-app-secrets` secret (key: `llm-key-encryption-key`) |
| `RATE_LIMIT_RPM` | `60` |
| `SCHEDULE_MIN_INTERVAL_MINUTES` | `5` |
| `SCHEDULE_RUN_RETENTION_DAYS` | `90` |
| `WEBHOOK_MAX_ATTEMPTS` | `5` |
| `ALLOWED_ORIGINS` | `https://scrapeflow.govindappa.com` |

**Dockerfile target:** The `api/Dockerfile` has a `production` target (non-root user). Build with `--target production`.

### Go HTTP Worker

```yaml
image: k4rth/scrapeflow-http-worker:<tag>
resources:
  requests:
    cpu: 50m
    memory: 64Mi
  limits:
    cpu: 500m
    memory: 256Mi
replicas: 1
```

**Environment variables:**

| Env var | Value |
|---------|-------|
| `NATS_URL` | `nats://scrapeflow-nats:4222` |
| `MINIO_ENDPOINT` | `scrapeflow-minio:9000` |
| `MINIO_ACCESS_KEY` | from `scrapeflow-minio-credentials` secret |
| `MINIO_SECRET_KEY` | from `scrapeflow-minio-credentials` secret |
| `MINIO_BUCKET` | `scrapeflow-results` |
| `MINIO_SECURE` | `false` |
| `FETCH_TIMEOUT_SECS` | `30` |
| `NATS_MAX_DELIVER` | `3` |

### Playwright Worker

```yaml
image: k4rth/scrapeflow-playwright-worker:<tag>
resources:
  requests:
    cpu: 200m
    memory: 512Mi
  limits:
    cpu: 1000m
    memory: 1536Mi   # 3 workers × ~400Mi Chromium + headroom
replicas: 1
```

**Environment variables:**

| Env var | Value |
|---------|-------|
| `NATS_URL` | `nats://scrapeflow-nats:4222` |
| `MINIO_ENDPOINT` | `scrapeflow-minio:9000` |
| `MINIO_ACCESS_KEY` | from `scrapeflow-minio-credentials` secret |
| `MINIO_SECRET_KEY` | from `scrapeflow-minio-credentials` secret |
| `MINIO_BUCKET` | `scrapeflow-results` |
| `MINIO_SECURE` | `false` |
| `PLAYWRIGHT_MAX_WORKERS` | `3` |
| `PLAYWRIGHT_DEFAULT_TIMEOUT_SECONDS` | `60` |

### LLM Worker

```yaml
image: k4rth/scrapeflow-llm-worker:<tag>
resources:
  requests:
    cpu: 50m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 512Mi
replicas: 1
```

**Environment variables:**

| Env var | Value |
|---------|-------|
| `NATS_URL` | `nats://scrapeflow-nats:4222` |
| `MINIO_ENDPOINT` | `scrapeflow-minio:9000` |
| `MINIO_ACCESS_KEY` | from `scrapeflow-minio-credentials` secret |
| `MINIO_SECRET_KEY` | from `scrapeflow-minio-credentials` secret |
| `MINIO_BUCKET` | `scrapeflow-results` |
| `MINIO_SECURE` | `false` |
| `LLM_KEY_ENCRYPTION_KEY` | from `scrapeflow-app-secrets` secret |
| `LLM_MAX_WORKERS` | `3` |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `60` |
| `LLM_MAX_CONTENT_CHARS` | `50000` |

---

## 9. Ingress (API only)

```yaml
host: scrapeflow.govindappa.com
annotations:
  cert-manager.io/cluster-issuer: letsencrypt-prod
  external-dns.alpha.kubernetes.io/hostname: scrapeflow.govindappa.com
ingressClassName: traefik
tls:
  secretName: scrapeflow-tls
```

---

## 10. Flux Image Automation

Follow the same pattern as `test_app/`. Create three resources per application image:

1. **ImageRepository** — polls DockerHub for new tags
2. **ImagePolicy** — selects which tag to use (pattern: `main-*` semver or regex matching the CI tag format `main-<run_id>-<sha>`)
3. **ImageUpdateAutomation** — commits updated image tags back to the gitops repo on branch `main`

Services needing image automation: `api`, `http-worker`, `playwright-worker`, `llm-worker`.

Place the `# {"$imagepolicy": "flux-system:<policy-name>"}` marker comment on the `image:` line of each Deployment, exactly as done in `test-app-manifest.yaml:25`.

The `ImageUpdateAutomation` `update.path` should point to `./clusters/k3s-server/scrapeflow/app`.

---

## 11. Infrastructure HelmRelease Notes

### PostgreSQL (bitnami/postgresql)
```yaml
values:
  auth:
    existingSecret: scrapeflow-db-credentials
    secretKeys:
      adminPasswordKey: postgres-password
      userPasswordKey: postgres-password
      replicationPasswordKey: postgres-password
    username: scrapeflow
    database: scrapeflow
  primary:
    persistence:
      size: 10Gi
```

### Redis (bitnami/redis)
```yaml
values:
  architecture: standalone
  auth:
    enabled: false   # internal cluster use only; not exposed outside namespace
  master:
    persistence:
      size: 2Gi
```

### MinIO (bitnami/minio)
```yaml
values:
  auth:
    existingSecret: scrapeflow-minio-credentials
    rootUserSecretKey: root-user
    rootPasswordSecretKey: root-password
  defaultBuckets: "scrapeflow-results"
  persistence:
    size: 20Gi
```

### NATS (nats/nats official chart)
```yaml
values:
  config:
    jetstream:
      enabled: true
      fileStore:
        pvc:
          size: 5Gi
```

---

## 12. Service Name Reference

When writing env vars and service DNS names, use these in-cluster DNS hostnames (k8s convention: `<release-name>-<chart-name>.<namespace>.svc.cluster.local`):

| Service | In-cluster hostname |
|---------|-------------------|
| PostgreSQL | `scrapeflow-postgresql.scrapeflow.svc.cluster.local` (short: `scrapeflow-postgresql`) |
| Redis | `scrapeflow-redis-master.scrapeflow.svc.cluster.local` |
| MinIO | `scrapeflow-minio.scrapeflow.svc.cluster.local` |
| NATS | `scrapeflow-nats.scrapeflow.svc.cluster.local` |

Verify the exact service names after the HelmReleases reconcile — bitnami chart service names follow the pattern above but may vary by chart version.

---

## 13. CI/CD — GitHub Actions

The workflow file is already committed at `.github/workflows/build-push.yml` in the ScrapeFlow repo.

### How it works

1. Triggers on every push to `main`
2. A `changes` job runs `dorny/paths-filter` to detect which service directories changed
3. Four build jobs (`build-api`, `build-http-worker`, `build-playwright-worker`, `build-llm-worker`) each `need: changes` and only run if their respective directory was modified — so pushing a fix to `llm-worker/` does not rebuild the other three images
4. Each job builds and pushes to DockerHub with the tag format: `main-<unix_ts>-<sha>` — identical to the existing `gitops-test-app` pattern, so Flux ImagePolicy regexes match consistently

### Required GitHub repository secrets

Add these two secrets to the ScrapeFlow GitHub repo (`Settings → Secrets → Actions`):

| Secret | Value |
|--------|-------|
| `DOCKER_USERNAME` | Your DockerHub username (e.g. `k4rth`) |
| `DOCKER_PASSWD` | DockerHub access token (not your account password — generate one at hub.docker.com → Account Settings → Security) |

### Tag format and Flux ImagePolicy

The pushed tag format is: `main-<unix_ts>-<sha>`

Example: `main-1745612400-a1b2c3d4e5f6...`

When writing `ImagePolicy` resources in the gitops repo, use this regex filter:

```yaml
filterTags:
  pattern: '^main-\d+-[a-f0-9]+'
  extract: '$ts'
policy:
  numerical:
    order: asc
```

This selects the tag with the highest timestamp — i.e., the most recently built image on `main`.

---

## 15. Startup Dependency Order

The services must come up in this order. Encode this in the Deployments using `initContainers` that probe readiness:

```
postgres  →  api (runs migrations)
redis     →  api
nats      →  nats-init-job (creates SCRAPEFLOW stream)  →  api, http-worker, playwright-worker, llm-worker
minio     →  api, http-worker, playwright-worker, llm-worker
```

Use a simple init container pattern (e.g. `busybox` with `nc -z <host> <port>` loop) to block application containers until their dependencies are reachable. This is critical for the API — if Postgres is not up when the API starts, the migration will fail and the pod will crash-loop.

---

## 16. What NOT to Do

- Do not put any secret values in git — only `secretKeyRef` / `existingSecret` references
- Do not use `hostPath` volumes for application data (MinIO, Postgres, Redis, NATS all need PVCs)
- Do not expose PostgreSQL, Redis, NATS, or MinIO via Ingress — internal cluster access only
- Do not set `replicas > 1` on workers until the resource budget is understood — the playwright worker is the most memory-intensive service
- Do not skip the `# {"$imagepolicy": ...}` marker comments — without them Flux cannot update image tags

---

## 17. Phase 3 — k8s Delta (apply before merging develop → main)

Phase 2 k8s manifests are live at `govindappa-k8s-config/clusters/k3s-server/scrapeflow/`. Phase 3 adds one new service (coordinator) and three new required env vars across the existing deployments. All gaps below will cause hard startup failures if not applied before the Phase 3 image is deployed.

### 17a. Secret update — `scrapeflow-app-secrets`

A new `credentials-encryption-key` literal must be added to the existing secret. **This must be done before updating any Deployment** — all three workers validate the key at startup and crash-loop if it is missing.

The value must be the same Fernet key used by the API, all three workers, and (if set) the platform-level `DEFAULT_PROXY_URL` fallback. Generate once and share:

```bash
# Generate the key (Python)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Recreate the secret with all three keys
kubectl delete secret scrapeflow-app-secrets --namespace scrapeflow
kubectl create secret generic scrapeflow-app-secrets \
  --namespace scrapeflow \
  --from-literal=clerk-secret-key=sk_live_... \
  --from-literal=llm-key-encryption-key=<existing-fernet-key> \
  --from-literal=credentials-encryption-key=<new-fernet-key>
```

> **Do not reuse `llm-key-encryption-key` as `credentials-encryption-key`.** They are separate keys used for different data (DB-stored LLM keys vs. NATS-message proxy credentials). Mixing them means a key rotation on one invalidates the other.

### 17b. `app/api.yaml` — two new env vars

Add to the `env:` block of the `api` container:

```yaml
- name: CREDENTIALS_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: scrapeflow-app-secrets
      key: credentials-encryption-key
- name: CLERK_AUTHORIZED_PARTIES
  value: "https://scrapeflow.govindappa.com"
```

**Why `CLERK_AUTHORIZED_PARTIES`:** Production review item #35 — without it, the API accepts JWTs from any Clerk app that shares the same Clerk instance. Setting it to the production domain restricts JWT acceptance to tokens issued for this app.

**Note:** `SCHEDULE_MIN_INTERVAL_MINUTES` is already present in `api.yaml` (line 95) — item #21 from the Phase 3 review is already done.

**Note:** `RATE_LIMIT_RPM: "60"` is a stale key left over from Phase 2. The Phase 3 sliding-window rate limiter reads `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW_SECONDS` (both defaulting to 60). The manifest entry has no effect but also causes no harm. Replace with the correct keys on the next manifest edit:

```yaml
# Replace:
- name: RATE_LIMIT_RPM
  value: "60"
# With:
- name: RATE_LIMIT_REQUESTS
  value: "60"
- name: RATE_LIMIT_WINDOW_SECONDS
  value: "60"
```

### 17c. `app/http-worker.yaml` — one new env var

The Go http-worker hard-fails startup if `CREDENTIALS_ENCRYPTION_KEY` is not set (see `http-worker/internal/config/config.go`).

Add to the `env:` block:

```yaml
- name: CREDENTIALS_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: scrapeflow-app-secrets
      key: credentials-encryption-key
```

### 17d. `app/playwright-worker.yaml` — one new env var

Same requirement as the Go worker (see `playwright-worker/worker/config.py`).

Add to the `env:` block:

```yaml
- name: CREDENTIALS_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: scrapeflow-app-secrets
      key: credentials-encryption-key
```

### 17e. New file: `app/coordinator.yaml`

Phase 3 Step 23 added the crawl coordinator service. Without it, `POST /crawls` creates rows in the DB but nothing ever dispatches them — crawls silently stall.

The coordinator is a stateless long-running Python process. It needs Postgres, NATS, and MinIO but **no ingress and no service** (it makes outbound connections only).

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: scrapeflow-coordinator
  namespace: scrapeflow
spec:
  replicas: 1
  selector:
    matchLabels:
      app: scrapeflow-coordinator
  template:
    metadata:
      labels:
        app: scrapeflow-coordinator
    spec:
      initContainers:
        - name: wait-for-postgres
          image: busybox
          command: ['sh', '-c', 'until nc -z scrapeflow-postgresql 5432; do echo waiting for postgres; sleep 2; done']
        - name: wait-for-nats
          image: busybox
          command: ['sh', '-c', 'until nc -z scrapeflow-nats 4222; do echo waiting for nats; sleep 2; done']
        - name: wait-for-minio
          image: busybox
          command: ['sh', '-c', 'until nc -z scrapeflow-minio 9000; do echo waiting for minio; sleep 2; done']
      containers:
        - name: coordinator
          image: k4rth/scrapeflow-coordinator:main-0000000000-placeholder # {"$imagepolicy": "flux-system:scrapeflow-coordinator-policy"}
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 200m
              memory: 256Mi
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: scrapeflow-db-credentials
                  key: database-url
            - name: NATS_URL
              value: "nats://scrapeflow-nats:4222"
            - name: MINIO_ENDPOINT
              value: "scrapeflow-minio:9000"
            - name: MINIO_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: scrapeflow-minio-credentials
                  key: rootUser
            - name: MINIO_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: scrapeflow-minio-credentials
                  key: rootPassword
            - name: MINIO_BUCKET
              value: "scrapeflow-results"
            - name: MINIO_SECURE
              value: "false"
            - name: COORDINATOR_DISPATCH_BATCH_SIZE
              value: "10"
            - name: COORDINATOR_DISPATCH_POLL_INTERVAL
              value: "2"
            - name: COORDINATOR_STALE_THRESHOLD_MINUTES
              value: "10"
```

> **Do not set `replicas > 1`** for the coordinator. The BFS dispatch loop uses `FOR UPDATE SKIP LOCKED` on `crawl_queue` rows, so multiple replicas are safe at the queue level, but running multi-replica against a single-node cluster adds coordination overhead with no throughput benefit at this scale.

### 17f. Image automation — add coordinator

**`image-automation/image-repositories.yaml`** — append:

```yaml
---
apiVersion: image.toolkit.fluxcd.io/v1
kind: ImageRepository
metadata:
  name: scrapeflow-coordinator-repo
  namespace: flux-system
spec:
  image: k4rth/scrapeflow-coordinator
  interval: 1m
```

**`image-automation/image-policies.yaml`** — append:

```yaml
---
apiVersion: image.toolkit.fluxcd.io/v1
kind: ImagePolicy
metadata:
  name: scrapeflow-coordinator-policy
  namespace: flux-system
spec:
  imageRepositoryRef:
    name: scrapeflow-coordinator-repo
  filterTags:
    pattern: '^main-(?P<ts>[0-9]+)-(?P<sha>[a-f0-9]+)'
    extract: '$ts'
  policy:
    numerical:
      order: asc
```

### 17g. `kustomization.yaml` — add coordinator

Add `- app/coordinator.yaml` to the `resources` list.

### 17h. CI/CD — already done

The GitHub Actions workflow at `.github/workflows/build-push.yml` already has the `build-coordinator` job and the `coordinator:` path filter. No changes needed there.

### 17i. Summary checklist

| # | File | Action | Blocking? |
|---|------|--------|-----------|
| 1 | cluster (imperative) | Recreate `scrapeflow-app-secrets` with `credentials-encryption-key` literal | Yes — must be first |
| 2 | `app/api.yaml` | Add `CREDENTIALS_ENCRYPTION_KEY` + `CLERK_AUTHORIZED_PARTIES` env vars; fix `RATE_LIMIT_RPM` → `RATE_LIMIT_REQUESTS`/`RATE_LIMIT_WINDOW_SECONDS` | Yes (CREDENTIALS); Security (CLERK); Low (rate limit) |
| 3 | `app/http-worker.yaml` | Add `CREDENTIALS_ENCRYPTION_KEY` env var | Yes |
| 4 | `app/playwright-worker.yaml` | Add `CREDENTIALS_ENCRYPTION_KEY` env var | Yes |
| 5 | `app/coordinator.yaml` | Create new file (Deployment) | Yes — crawls stall without it |
| 6 | `image-automation/image-repositories.yaml` | Append coordinator ImageRepository | Yes — Flux needs this to poll |
| 7 | `image-automation/image-policies.yaml` | Append coordinator ImagePolicy | Yes — needed for tag selection |
| 8 | `kustomization.yaml` | Add `- app/coordinator.yaml` | Yes |
