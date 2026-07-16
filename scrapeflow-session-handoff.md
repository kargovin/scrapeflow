# ScrapeFlow Session Handoff

You are a **coding mentor** helping the user build ScrapeFlow, a self-hosted multi-tenant web scraping platform (Apify clone). Read @CLAUDE.md for the full architecture.

## Your role in this session

**Do not write code unless the user explicitly says "build it", "implement it", or similar.**

Instead:
- Explain what needs to be built and why
- Walk through design decisions, trade-offs, and patterns
- Point out relevant existing code the user should look at before writing
- Review code the user writes and give feedback
- Answer questions about the spec, architecture, or implementation approach

When the user is ready to build something, they will say so. Until then, guide and explain.

---

## Project reference

| What | Where |
|------|-------|
| Architecture + key decisions | `CLAUDE.md` |
| Docs index (ADRs, reference, archive) | `docs/README.md` |
| ADRs (ADR-001 through ADR-008) | `docs/adr/` |
| Operational reference (commands, devops, usages) | `docs/project/` |
| Multi-persona process starter prompts | `docs/process/` |
| Phase 3 engineering spec (historical) | `docs/archive/phase3/phase3-engineering-spec.md` |
| Phase 3 ordered backlog (historical) | `docs/archive/phase3/PHASE3_BACKLOG.md` |
| Phase 3 deferred items → Phase 4 candidates | `docs/archive/phase3/PHASE3_DEFERRED.md` |
| Progress tracker (historical) | `docs/archive/PROGRESS.md` |
| Phase 2 spec (historical) | `docs/archive/phase2/phase2-engineering-spec-v3.md` |
| Phase 2 production readiness review | `docs/archive/phase2/production-review.md` |
| Phase 3 production readiness review | `docs/archive/phase3/production-review.md` |
| Idempotency audit (NATS redelivery) | `docs/archive/phase3/idempotency-checks.md` — 7 findings; all fixed |
| Service failure & recovery audit | `docs/archive/phase3/service-failure-recovery.md` — all findings fixed |

---

## Commands

**Tests** (must run inside Docker — `uv` manages the venv inside the container):
```bash
# from ./docker
docker compose exec api uv run pytest tests/ -v
docker compose exec api uv run pytest tests/test_jobs.py -v
```

**MCP server tests** (built and run as a standalone Docker image — not in docker-compose):
```bash
# from repo root
docker build -t scrapeflow-mcp mcp/
docker run --rm -e SCRAPEFLOW_API_KEY=test-key scrapeflow-mcp python -m pytest tests/ -v
```

**Migrations** (Alembic auto-run is enabled in `main.py` — runs on API startup):
```bash
# from ./docker
docker compose exec api uv run alembic upgrade head
docker compose exec api uv run alembic current
docker compose exec api uv run alembic revision --autogenerate -m "migration_3_N_description"
```

---

## Current state

- Branch: `main` (develop is in sync). Latest commit `ba8fb8a`.
- Phase 1 + Phase 2 + Phase 3 complete and production-verified at `scrapeflow.govindappa.com`
- **Auth on production Clerk instance** as of 2026-07-03 (was dev instance). See "Clerk production cutover" below.
- **In Phase 4 — investigation/triage + small feature work.** No formal spec yet; the backlog is being assembled from real production usage. Open findings cluster around **LLM-worker reliability** (Q5/Q6/Q7) and a **state-machine design flaw** (Q8) that already caused a live incident. Shipped Phase 4 work: **admin result viewer + user-email surfacing**, the **user-facing job dashboard**, and the **Playwright anti-bot hardening (ADR-008)** — Patchright + real headed Chrome under Xvfb, verified passing BrowserScan in prod (see Post-Phase-3 changes + ADR-008).
- **⚠ Q6 (`ack_wait` redelivery loop) is now CONFIRMED, not just latent** — it fired in prod on the playwright worker (headed Chrome is slower than the old headless Chromium, so scrapes cross the default 30s). Fixed there (`67ba983`). **The LLM worker's consumer almost certainly has the same bug — audit its `ack_wait` next (see Phase 4 triage Q6).**
- 243 API tests passing (deterministic — first-run clean); **70** playwright-worker tests passing; 14 MCP tests passing.
- Alembic auto-migration enabled in `api/app/main.py`

### Post-Phase-3 changes (since handoff, 2026-05-13 → 2026-07-03)

| Commit | Change |
|--------|--------|
| `5cb8c7f` | **Incident fix** — LLM dispatch loop. The regular + batch result handlers had un-source-guarded `if worker_status == "running"` branches; the LLM stage's `running` clobbered `run.status` from `processing` back to `running`, so the next `completed` re-matched the scrape-completed branch and re-dispatched to `scrapeflow.jobs.llm` — a tight feedback loop that burned ~200 LLM API calls in ~5 min before the worker was stopped. Fix: `if source == "scrape":` guards on the `running` transition in both `_handle_job_result` and `_handle_batch_result`. Root cause + permanent fix tracked as **Q8** (status-value overloading — needs a total state machine, not more guards). |
| `63b2dfc` | playwright-worker: split proxy URL userinfo into `server`/`username`/`password` for `browser.new_context(proxy=…)` (Playwright rejects credentials embedded in the URL). |
| `4b2d1d6`, `d9e1edb` | Added top-level `README.md`; replaced ASCII architecture diagram with Mermaid; updated `docs/project/COMMANDS.md`. |
| `70ce8bc` | **Admin result viewer + user email** (first Phase 4 feature). Backend: `user_email` added to `JobResponse` (admin jobs query joins `User`); new `GET /admin/jobs/{id}/result` (admin-scoped, no owner check) via shared `load_completed_result()` helper extracted from `get_job_result`. Frontend: read-only Monaco viewer on Job Detail (Source/Preview toggle — markdown via react-markdown, HTML via sandboxed iframe, JSON pretty-printed), User email row, and a User column + user filter on the Jobs list. Monaco is bundled locally (config in `frontend/src/lib/monaco.ts`) and lazy-loaded so the main admin bundle stays ~93 KB gzip. New deps: `@monaco-editor/react`, `monaco-editor`, `react-markdown`, `remark-gfm`. 4 new admin tests → 243. |
| `d8a6ce1` | **CI fix** for `70ce8bc`. The frontend's committed `package.json` uses **vite `^8`** (a working-tree bump that rode along in `70ce8bc`), but `@vitejs/plugin-react@4.7.0` only peers vite ≤7 — so the API Docker build's `npm install` failed with ERESOLVE. Bumped `@vitejs/plugin-react` → `^6.0.3` (peers vite `^8`), so the tree resolves with **no `--legacy-peer-deps` needed** locally or in CI. Build output/bundle sizes unchanged. |
| `d0a64b5` | **Docs** — handoff + CLAUDE.md updated for the admin result viewer; Phase 4 triage docs (`open-questions.md`, `open-bugs.md`, `usage-findings.md`) added. |
| _(user-dashboard commit)_ | **User-facing job dashboard + admin/user nav cross-link** (frontend-only; no backend/API/schema change — the owner-scoped `/jobs*` routes already existed). New user routes `/app/dashboard/jobs` + `/app/dashboard/jobs/:jobId` (nav item added; `/app/dashboard` now lands on Jobs). The admin `Jobs`, `JobDetail`, and `ResultViewer` are **shared, not duplicated** — each takes a `mode: 'admin' \| 'user'` prop that swaps the API base (`/admin/jobs` ↔ `/jobs`), the route/link base, and the result endpoint. Admin-only bits (User column, user filter which calls `/admin/users`, User detail row) render only in `mode='admin'`; the user Job Detail gets a **soft Cancel** button (`DELETE /jobs/{id}`) in place of the admin permanent-delete Danger Zone. `AdminJob` type renamed to `Job` (back-compat alias kept). Admin-detection extracted from `RequireAdmin` into a shared `lib/useIsAdmin.ts` hook (same `['admin-check']` cache key — no extra request); `Layout` gained a `variant` prop that renders the cross-link ("← My dashboard" in admin; "Admin panel →" in user, only if the user is an admin). **Layout bug fixed**: shell `min-h-screen` → `h-screen` so the sidebar footer (cross-link + Sign out) no longer falls below the fold on tall pages (Jobs/Stats) — `main` scrolls internally instead of the whole page. Bundle unchanged (~93 KB gzip main; Monaco stays a lazy chunk). No new tests (backend untouched; 243 API tests still green). |
| `92df7ea` | **Playwright anti-bot hardening (ADR-008 + `docs/guides/anti-bot-hardening.md`).** Scrapes were blocked despite residential proxies; BrowserScan diagnosed 3 fingerprint fails (`navigator.webdriver`, `HeadlessChrome` UA, CDP `Runtime.enable` leak). Fix: swap Playwright → **Patchright**; run **real Google Chrome** (`channel="chrome"`) **truly headed under Xvfb** (only mode with a clean UA — even `--headless=new` leaks `HeadlessChrome`); `--disable-blink-features=AutomationControlled` (clears `webdriver`; Patchright alone didn't) + `--no-sandbox`/`--disable-dev-shm-usage`; `new_context(no_viewport=True)`; no UA spoofing. All env-tunable in `worker/config.py`. k8s: `patchright install chrome` in image; playwright-worker Deployment resources bumped (infra repo `538fba5`). 70 playwright-worker tests. **Verified in prod: BrowserScan now `Normal`, 0 Robot / 18 checks.** |
| `4257183` | **Entrypoint fix** — first stealth deploy looked healthy (pod 1/1, 0 restarts) but ran nothing. `xvfb-run` as pid 1 masked worker crashes (container never exited → k8s never restarted), a cold-start race killed Chrome before Xvfb was ready, and `PYTHONUNBUFFERED` unset hid the logs. Replaced with `playwright-worker/entrypoint.sh` (start Xvfb → wait for its socket → `exec python` as pid 1) + `PYTHONUNBUFFERED=1` + pre-create `/tmp/.X11-unix 1777`. Now: python is pid 1, logs flow, crashes surface as CrashLoopBackOff. |
| `67ba983` | **NATS `ack_wait` + heartbeat (Q6 — now CONFIRMED & FIXED for playwright worker).** A headed-Chrome scrape (~37s) exceeded the pull consumer's default 30s `ack_wait`, so NATS redelivered mid-scrape; the late `ack()` was a no-op and, with `max_deliver=-1`, the job **looped forever** (re-scrape + re-upload every ~20s). Live incident mitigated by purging the subject + raising the live consumer to `ack_wait=120` (via `add_consumer` — this nats-py has no `update_consumer`). Permanent fix: `pull_subscribe(config=ConsumerConfig(ack_wait=120))` + `msg.in_progress()` heartbeat every 30s (covers jobs longer than `ack_wait`). **Caveat: JetStream won't apply a new `ack_wait` to an existing durable consumer — must update/recreate out-of-band.** |

### Clerk production cutover (2026-07-03)

Moved auth from the Clerk **dev** instance to a **production** instance. No app code changed — Clerk is derived entirely from keys/config (backend `jwt.py` gets the instance from the secret key; frontend `pk` is a build-time env). Work was dashboard + Cloudflare + cluster secret only.

- **DNS**: production Frontend API `clerk.scrapeflow.govindappa.com` + account portal + mail CNAMEs added **manually in Cloudflare, grey-cloud (DNS-only)**. Not via ExternalDNS — its `sources` are `ingress`/`service` only, so it neither creates these nor prunes them (they carry no `txtOwnerId` TXT registry record, so `policy: sync` ignores them).
- **OAuth**: production needs **own** Google (and GitHub, if used) OAuth credentials — Clerk's shared demo app is dev-only. Symptom when missing: Google `Error 400: invalid_request / Missing required parameter: client_id`. Google client created, creds pasted into Clerk → login works.
- **Keys** (easy to mix up):
  - backend **`sk_live`** → k8s secret `scrapeflow-app-secrets/clerk-secret-key` (patched via the README "Rotating the Clerk secret" flow + `rollout restart`)
  - frontend **`pk_live`** → GH Actions secret `VITE_CLERK_PUBLISHABLE_KEY`, baked into the api image at build (CI rebuilds api on `api/**`/`frontend/**` changes — the `a0c905b` `index.html` title tweak was the trigger for that rebuild).
  - **Incident during cutover**: `pk_live` was accidentally pasted into the `clerk-secret-key` slot → backend couldn't auth to Clerk's Backend API to load JWKS → `TokenVerificationErrorReason.JWK_FAILED_TO_LOAD` (HTTP 401 in the SPA). Fixed by patching the real `sk_live`. Verified: pod env is `sk_live`, `api.clerk.com/v1/jwks` + Frontend-API `/.well-known/jwks.json` both 200, no verification errors in logs.
- **Fresh start**: prod **Postgres app tables truncated + MinIO `scrapeflow-results` emptied** on 2026-07-03 (schema + `alembic_version 8f4b6eb47abb` preserved; bucket kept). Rationale: a prod Clerk instance issues **new `sub` (user) IDs**, so old `users` rows keyed on the dev `clerk_id` would be orphaned — clean slate instead. Fernet keys (`llm-key-encryption-key`, `credentials-encryption-key`) were **not** rotated (would orphan encrypted-at-rest data).
- **Loose end**: GitHub OAuth custom credentials only need setup if GitHub sign-in is offered (Google done).

### Phase 3 — complete (archived)

All 28 steps + the full production-review findings (all `[x]` done, with `[?] 40` and `[?] 43`
deferred to Phase 4) are recorded in `docs/archive/phase3/production-review.md` and the Phase 3
backlogs under `docs/archive/phase3/`. The still-open deferred items are echoed in the Phase 4
"Deferred from Phase 3" table below.

### Phase 4 entry point

Phase 4 scope is **still not formally specced**, but real production usage has surfaced a concrete triage list (below). When returning:

1. Read the **Phase 4 triage** table below — these are captured findings, not yet a backlog
2. Read the source docs in full before acting: `docs/project/open-questions.md` (Q5–Q8 have detailed options + recommendations), `docs/project/open-bugs.md`, `docs/project/usage-findings.md`
3. Check `docs/archive/phase3/PHASE3_DEFERRED.md` for items already scoped and deferred
4. Decide whether to run the full PM → Architect → Tech Lead → Engineer process (see `docs/process/`) or a lighter spec approach. The LLM-worker cluster (Q5/Q6/Q7) is tightly coupled and should be designed together, not piecemeal.

---

### Phase 4 direction — Temporal Workflows migration (intended)

The strategic direction for Phase 4 (exploratory — not yet a committed build). Two design docs
capture it in full: `docs/project/workflows-scoping.md` (feature + engine comparison) and
`docs/project/temporal-full-migration.md` (complete change inventory + migration sequence).

**Engine decision: Temporal.** Chosen over DBOS/Restate/Prefect/Windmill for portfolio value +
first-class Python *and* Go SDKs (fits both our service languages). Grounded in the **Q8**
incident: our orchestration is hand-rolled polling-loops + Postgres state, and the overloaded
`job_runs.status` machine already caused a live feedback-loop incident — exactly the class of
code a durable-execution engine owns natively.

**The feature — "ScrapeFlow Workflows"** — one feature in nested layers, natural build order:

- **Pipelines (A)** — user-defined multi-step chains (scrape → clean → LLM → validate → deliver),
  replacing today's single hard-coded `scrape → LLM → diff → webhook`.
- **Delivery sinks (C)** — a rich block type on A: deliver to S3 / DB / Sheet / email with saga
  rollback (today output is only MinIO + one webhook).
- **Monitors (B)** — a pipeline wrapped in a durable loop: long sleeps, human-approval waits,
  scheduling. Absorbs the **live scheduled-crawl gap** (`crawls.schedule_cron` is accepted +
  persisted but the scheduler only ever dispatches `Job`, never `Crawl` — so scheduled crawls
  silently never run today).

**How it lands (v1/v2 coexistence, no big-bang).** Temporal comes up *alongside* the NATS stack.
It's **one product, two orchestration engines, a routing switch, and a drain period** — same
Postgres/MinIO/auth underneath. New work is routed (flag / per-tenant canary) to **v2 (Temporal)**
while **v1 (NATS + `result_consumer`)** keeps serving in-flight work; cut v1 per-flow once v2 is
proven. Reversible at every step (flip the flag back). Cutover gotchas to handle *at migration
time*, not defer: (1) a job must run on **exactly one lane** — never both (double-scrape / double
LLM-bill risk); (2) when moving a recurring job to a Temporal Schedule, **disable it in v1**
(`schedule_status`) or it fires on both; (3) keep the NATS workers alive (integration option **a**)
until v1 is drained — worker cutover to Temporal activities (option **b**) is what removes v1's
executors.

**Complete end-state (deep adoption).** Retire `result_consumer.py`, `scheduler.py`,
`webhook_loop.py`, `advisory.py`, and the `coordinator/` service; **remove NATS** entirely; workers
become Temporal activity workers. New infra: **Temporal Server + its own Postgres + Web UI**, plus a
**workflow-worker pod**. Orchestration logic leaves the API pod (into the workflow worker) — the API
becomes thin and **horizontally scalable**, removing the current single-replica / `Recreate`
constraint (see "Deferred from Phase 3" → NATS pull consumers). Q8 dissolves; the ack_wait/Q6 class
of bug disappears with NATS.

**Next artifacts if pursued:** a PRD (PM template) for the Workflows feature, then an engine **ADR**
(next number after ADR-008 → ADR-009) recording the Temporal decision + the coexistence contract.

---

### Phase 4 triage — captured findings (what bugs we still have)

The dominant theme is **LLM-worker reliability**: Q5, Q6, Q7 are three faces of the same problem and their recommendations explicitly say they resolve together. Q8 is the design flaw behind the incident already fixed in `5cb8c7f`.

**Open questions needing a decision** (`docs/project/open-questions.md`):

| # | Severity | Summary | State |
|---|----------|---------|-------|
| Q5 | High | LLM worker can't survive scale-to-zero cold starts (90–120s). `LLM_REQUEST_TIMEOUT_SECONDS=60` → `httpx.ReadTimeout`; worker acks-on-failure so NATS never retries → job permanently `failed`. Recommend A+B (bump timeout + classify exceptions, nak transient). | Needs decision |
| Q6 | High (**CONFIRMED in prod**) | No `ack_wait` on consumer → default 30s. Any job >30s → NATS silently redelivers → duplicate processing / **double MinIO upload** / (for LLM) double billing. **Fired live on the playwright worker** on 2026-07-03 (headed Chrome scrapes cross 30s) — infinite loop, `max_deliver=-1`. **FIXED on the playwright worker** (`67ba983`): `ConsumerConfig(ack_wait=120)` + `msg.in_progress()` heartbeat every 30s; live consumer updated out-of-band. **TODO: the LLM worker's pull consumer has the same default — audit + apply the same fix (ack_wait floor above `LLM_REQUEST_TIMEOUT_SECONDS` + heartbeat).** Caveat learned: JetStream won't change `ack_wait` on an existing durable consumer — update/recreate it out-of-band (this nats-py has no `update_consumer`; use `add_consumer` with the modified config). | **Playwright done; LLM worker pending** |
| Q7 | Medium | No worker-level retry on transient LLM failures (instance death mid-call, 503, conn reset). Only SDK default `max_retries=2`, invisible + doesn't cover conn errors. Recommend B (SDK + worker retry with an explicit exception-classification table). | Needs decision; blocked on Q5/Q6 |
| Q8 | Medium (root cause of the incident) | `job_runs.status` values overloaded across pipeline stages — `running`/`completed` mean different things for scrape vs LLM, disambiguated only by `source`. Caused the `5cb8c7f` dispatch loop. Source-guards are a patch; recommend **Option B** (distinct per-stage status values → total state machine) as a Phase 4 refactor. Should land *after* Q5/Q6/Q7 so retry logic isn't rebuilt twice. | Needs decision |
| Q1–Q4 | — | Phase 1/2 questions — Q1 (api_keys uniqueness) already resolved in Phase 3; Q2–Q4 largely superseded. Re-confirm and close out. | Mostly stale |

**Open bugs** (`docs/project/open-bugs.md`):

| # | Severity | Summary | Fix |
|---|----------|---------|-----|
| BUG-001 | Low (noisy, harmless) | Scheduler `_recover_stale_pending` selects batch runs (`job_id IS NULL`) → `db.get(Job, None)` emits `SELECT ... WHERE jobs.id IS NULL` every 60s per stuck batch run, flooding logs. No data corruption. | Add `JobRun.job_id.is_not(None)` to the stale-pending query. One-liner. |
| BUG-002 | Mixed (1 critical, 11 high, 41 moderate, 20 low) | **73 GitHub Dependabot vulnerability alerts** on `kargovin/scrapeflow` default branch (surfaced 2026-07-03 on push). Not yet triaged — unknown which are in prod paths vs transitive/dev-only. | Review at https://github.com/kargovin/scrapeflow/security/dependabot ; triage the critical + highs first (likely a mix of Python/`api`, Go/workers, and frontend/npm). Bump or accept per-advisory. |
| BUG-003 | High (silent data corruption) | **Bot-block / interstitial pages stored as successful output.** A 200-status bot wall (seen live on Amazon — the "Continue shopping" page) passes `page.goto()` without throwing, so the playwright worker uploads the interstitial and marks the run `completed`. Only failure signal today is "did the browser throw." Different layer from ADR-008 (fingerprint clean, but commercial bot managers still 200-wall). Poisons dedup baselines; no vendor observability. | Add a block-detection stage after `worker.py:183` (status / final-URL / vendor-cookie / content signals). **Minimum fix (ship first):** publish `status="failed"` (`error="blocked"`) instead of `completed`. Retry-on-fresh-IP + unblocker-provider tiers deferred (interacts with Q8 + UF-002). Full writeup: `docs/project/open-bugs.md` BUG-003. |

**Usage findings** (`docs/project/usage-findings.md`):

| # | Summary |
|---|---------|
| UF-001 | `/health/ready` checks DB/Redis/NATS but **not MinIO** — endpoint reports `200 ok` while every job silently fails to store output if MinIO is down. Add a MinIO ping to the degraded check. |
| UF-002 | `DEFAULT_PROXY_URL` is a single platform-wide proxy — one user's behaviour can get the shared IP banned for everyone. Decision: per-user proxy model (`user_proxies` table, provider-side rotation, no platform default). Larger change: schema + secrets + dispatch + frontend UI. |

**Deferred from Phase 3** (already scoped):

| Item | Detail |
|------|--------|
| `[?] 40` | Custom robots.txt parsers (Go + Python) vs established packages — deferred to Phase 4 |
| `[?] 43` | Content hash re-reads freshly-written MinIO object — bundle with Phase 4 schema_version 3 worker contract changes |
| NATS pull consumers | API result consumer uses a push consumer (durable); limits to one replica and requires `Recreate` strategy. Phase 4: migrate to pull consumer model. (Note: overlaps with Q6 — both touch JetStream consumer config.) |
