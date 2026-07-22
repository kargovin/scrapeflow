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

- ## ✅ START HERE (2026-07-22) — nothing is in flight; pick up the pre-Phase-4 queue
  **The LLM-worker cluster (Q5/Q6/Q7) is fully closed, code *and* production.** There
  is no unshipped commit and no pending operational step. `develop` and `main` are both
  at `fbcf254`, and the running image is
  `k4rth/scrapeflow-llm-worker:main-1784711895-fbcf254b…`.

  **Next work: the pre-Phase-4 queue in `docs/project/phase4-backlog.md` §1**, which now
  starts at **P2 (UF-001, MinIO missing from `/health/ready`)** and **P3 (BUG-003, bot
  walls stored as `completed`)**.

  ⚠ **Ordering conflict to resolve first** (it predates this session, and both docs are
  still internally consistent — they just disagree with each other): the backlog orders
  these **UF-001 → BUG-003**, while the queue table further down *this* file orders them
  **BUG-003 → UF-001**. The argument for BUG-003 first is that it is the only
  *compounding* item left — every bot wall stored as a success also becomes a dedup
  `content_hash` baseline, so the damage accretes while it waits. Pick one, then make
  the other doc match.

  **Before fixing BUG-003, check whether prod baselines are already poisoned.** The
  minimum fix (publish `failed`/`blocked` instead of `completed`) stops the bleeding but
  does **not** heal it — existing poisoned `content_hash` rows will keep suppressing real
  change detection for those jobs indefinitely.

  **Verification habit worth keeping** (it has now caught the same class of bug twice):
  never verify NATS consumer state from the worker's `subscribed` log line — it prints
  **config**, not the live consumer. It logged `ack_wait=120` for weeks while JetStream
  ran `30`, and `max_deliver=3` while JetStream ran `-1`. Use `nats consumer info
  **--json**` — the table output omits the `Max Deliver` row entirely when the value is
  `-1`, so uncapped and correctly-capped look identical in the pretty output.
- Phase 1 + Phase 2 + Phase 3 complete and production-verified at `scrapeflow.govindappa.com`
- **Auth on production Clerk instance** as of 2026-07-03 (was dev instance). See "Clerk production cutover" below.
- **In Phase 4. Scope is now decided: Phase 4 *is* the Temporal durable-workflows migration.** All Phase 4 items live in one place — **`docs/project/phase4-backlog.md`** — split into Pre-Phase 4 / the migration / **dissolved by Temporal (do NOT fix)** / survives-Temporal. Read that first; it supersedes the triage tables that used to be inlined in this handoff. Shipped Phase 4 work so far: **admin result viewer + user-email surfacing**, the **user-facing job dashboard**, and the **Playwright anti-bot hardening (ADR-008)**.
- **✅ Q6 is CLOSED — code and production.** Playwright (`67ba983`) and LLM worker (`6fb5b9c`); the live `python-llm-worker` consumer was recreated on 2026-07-21 and verified at `Ack Wait: 2m0s` (was `30.00s`). The reusable recreate procedure is in the Q6 status block in `open-questions.md`.
- **✅ Q5 is CLOSED — code and production** (options A + B + C). A live via `df44f95`; B + C shipped as `e1fde0d` → pushed/ff-merged as `fbcf254` on 2026-07-22, image deployed, and the `python-llm-worker` consumer recreated. Verified on the live consumer: `ack_wait 2m0s`, `max_deliver: 3` (was `-1`). **Q7 is closed with it** — the Q5 option-B nak retry *is* the worker-level retry Q7 asked for.
- 243 API tests passing (deterministic — first-run clean); **70** playwright-worker tests passing; **87** llm-worker tests passing (was 29); 14 MCP tests passing.
  - llm-worker tests aren't wired into a compose service (the image is production-only and doesn't COPY `tests/`). Run them by mounting the source over the built image:
    `docker run --rm -v "$PWD/llm-worker:/app" -w /app docker-llm-worker python -m pytest -q`
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
| `ba8fb8a`, `9cddce0` | **Docs** — recorded the anti-bot/entrypoint/Q6 fixes; scoped the Temporal migration (`workflows-scoping.md`, `temporal-full-migration.md`); flagged the LLM-worker Q6 audit + Dependabot. |
| `35fb89f` | **Phase 4 backlog consolidated** into `docs/project/phase4-backlog.md` — Phase 4 items had accreted across seven docs. Structured by the Temporal decision, including a **§3 "dissolved by Temporal — do NOT fix"** table recording *which deleted code* removes each bug so they don't get re-raised. Pointers added from each source doc. |
| `df44f95` (infra) | **Q5 option A — LLM request timeout 60 → 180s** (env-only change in `govindappa-k8s-config`, `llm-worker.yaml`). Modal's scale-to-zero endpoint cold-starts in **90–110s**, which exceeded the 60s httpx read timeout. **This was urgent, not cosmetic:** the Q6 fix's `max_retries=0` pin removed an *accidental* cold-start mitigation — the SDK default of `max_retries=2` meant attempt 2 or 3 landed after Modal had booted, so cold starts silently succeeded. Pinned to 0, every cold start became a hard failure. The two commits are safe together and **unsafe apart**. Note `ack_wait` stays 120s: the 30s heartbeat resets the ack timer during the call, so a 180s request is never redelivered. |
| _(operational)_ | **Q6 consumer recreate — done.** Live `python-llm-worker` durable went `Ack Wait: 30.00s` → **`2m0s`**. Procedure (reusable, recorded in `open-questions.md`): deploy image → confirm pod → `nats consumer rm` → `kubectl rollout restart` → verify with `nats consumer info`. **Delete before restart** (the worker only subscribes at startup, so deleting under a running pod leaves it holding a dead subscription); **never scale to 0** (Flux reconciles `replicas: 1` back up). Safe because the stream is `--retention work` — acked messages are deleted, so a fresh consumer cannot replay completed jobs. |
| `e1fde0d` (shipped as `fbcf254`) | **Q5 options B + C.** **B:** new `llm-worker/worker/errors.py` classifies exceptions transient vs terminal; `handle_message` now `nak`s transient failures with exponential backoff (5/10/20s, capped) instead of acking everything. Critically it publishes **no** `failed` on a retry — the API's terminal-status guard (`result_consumer.py:125`) would lock the run failed and then discard the retry's `completed`. The worker caps attempts itself via `metadata.num_delivered` and publishes a real terminal `failed` on the last one, so a run can't dangle in `processing`. Classification **fails closed** (unknown → terminal): a wrong "transient" guess retries against the user's own API key. **C:** `llm.ensure_ready()` polls the OpenAI-compatible `/models` endpoint (spec'd; `/health` isn't) with a short per-probe timeout + long overall budget, so the real call runs against a warm endpoint. Only for `openai_compatible` **with** a `base_url`; 60s process-local warm cache; any HTTP response (incl. 401/404) counts as awake. Also resolves **Q7**. 29 → **87** tests. **Error strings changed format** — they now carry the exception type, because several httpx/provider errors stringify to `""` and showed as blank in the UI. |
| `6fb5b9c` | **LLM worker Q6 fix.** Same bare `pull_subscribe` as playwright, but worse: `llm_request_timeout_seconds` (60) is **2× the 30s default `ack_wait`**, so redelivery fired on ordinary slow calls, and each redelivery re-bills the **user's own** provider key, unbounded (`max_deliver` unset → `-1`). **The playwright numbers did not transfer:** both SDKs default to `max_retries=2`, so one `call_llm()` could make 3 attempts each with a fresh httpx read timeout ≈ **210s** — well past a 120s `ack_wait`. Fix: `ConsumerConfig(ack_wait=120)` + `in_progress()` heartbeat every 30s + **`max_retries` pinned** (`llm_max_retries`, default `0`) on both SDK clients. Here **`ack_wait` is the orphan-recovery window, not a job-duration budget** — the heartbeat is what covers long calls. 29 llm-worker tests. **✅ Prod consumer recreated 2026-07-21 (`ack_wait`) and again 2026-07-22 (`max_deliver`).** |

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

**Start at `docs/project/phase4-backlog.md`** — the single source of truth for Phase 4 scope. When returning:

1. Read **`docs/project/phase4-backlog.md`** first. It is an index; each item points to its source doc for full context/options/recommendation.
2. **Check §3 ("Dissolved by Temporal — do NOT fix") before writing any bug fix.** The migration deletes the code containing those bugs, so fixing them is wasted work. This is the most common way to waste a session here.
3. Only then open the source docs for depth: `docs/project/open-questions.md` (Q5–Q8 options + recommendations), `open-bugs.md`, `usage-findings.md`, `PHASE3_DEFERRED.md`.
4. Decide whether to run the full PM → Architect → Tech Lead → Engineer process (see `docs/process/`) or a lighter spec approach.

**Pre-Phase 4 queue** (§1 of the backlog), in cost-of-delay order — the first two get *worse* the longer they sit, the rest are flat-cost:

| | Item | State |
|---|---|---|
| 1 | Q6 — LLM worker `ack_wait` | ✅ **done, verified in prod** (`6fb5b9c` + consumer recreate) |
| 1b | Q5 — cold starts + transient retry | ✅ **done, verified in prod** (`fbcf254` + consumer recreate; `max_deliver: 3`) |
| 2 | BUG-003 — bot walls stored as `completed` (minimum fix) | **open — next feature work.** Compounding: each bot wall stored as success also becomes a dedup baseline, so check whether prod `content_hash` baselines are already poisoned. Stopping the bleeding does not heal it. ⚠ The backlog ranks UF-001 ahead of this — reconcile the two (see START HERE). |
| 3 | UF-001 — MinIO missing from `/health/ready` | open — ranked **P2, ahead of BUG-003**, in `phase4-backlog.md` §1 |
| 4 | Q1–Q4 close-out (bookkeeping) | open |
| 5 | BUG-002 — Dependabot critical + highs only | open — **now 8 critical / 13 high** (was 1 crit / 11 high); the count is drifting up |

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

### Phase 4 triage — moved

The triage tables that used to live here (Q1–Q8, BUG-001→003, UF-001/002, deferred-from-Phase-3)
were **duplicated** into `docs/project/phase4-backlog.md` and have been removed from this handoff
to stop the two copies drifting apart.

**They are not lost** — every item is in the backlog, now sorted by whether the Temporal migration
dissolves it. Two things the flat triage list could not express, which are worth knowing before you
open it:

- **Roughly half the triage list is now "do not fix."** Q6, Q7, Q8, BUG-001, the NATS pull-consumer
  item, the crawl-webhook bypass and the scheduled-crawl gap are all deleted outright by the
  migration. The old list gave no way to see that, so each of them read as work.
- **Q5 only *half* dissolves — and as of 2026-07-21 the surviving half is real code.** The
  ack-on-failure behaviour goes away with NATS, but the cold-start handling
  (`llm_request_timeout_seconds=180` + `llm.ensure_ready()`) is *business* logic — Temporal has
  no idea a scale-to-zero endpoint is cold and would just retry a timing-out activity.
  **Port `ensure_ready()` into the LLM activity**; don't let it be deleted with the NATS
  plumbing around it.

The **LLM-worker cluster (Q5/Q6/Q7)** framing holds, and it played out exactly that way — all three
resolved together in one session:

- **Q6** ✅ fixed on both workers **and** closed in production.
- **Q5** ✅ A, B and C all live in production as of 2026-07-22 (`fbcf254` + consumer recreate).
- **Q7** ✅ resolved *by* the Q5 option-B fix — the NATS-level nak retry replaces the SDK retry the
  Q6 pin removed. The status note at the top of Q7 in `open-questions.md` is now settled, and its
  advice **reverses**: do **not** restore `llm_max_retries=2`. With NATS-level retry in place an
  SDK retry multiplies *underneath* it (3 × 3 = 9 billable calls). Retry stays in one visible layer.

The through-line worth carrying into Phase 4: **every bug in this cluster was a retry hidden in a
layer nobody was looking at** — the SDK's `max_retries`, JetStream's redelivery, Modal's cold boot.
Temporal's value here is making retry declarative and visible in one place; the cost is that
anything left retrying underneath it multiplies silently.
