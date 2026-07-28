# ScrapeFlow — Phase 4 Backlog (consolidated)

> **Single source of truth for Phase 4 scope.** Phase 4 findings had accreted across
> seven files; this doc consolidates every open item into one tracking list. It is a
> **backlog/index**, not a spec — each item points back to its detailed source doc for
> the full context, options, and recommendation.
>
> **Phase 4 is the Temporal durable-workflows migration.** That decision reshapes this
> backlog: a large share of the bugs collected during Phase 3 → 4 triage are
> *orchestration* bugs that the migration deletes outright. Those are recorded in
> §3 as **do-not-fix**, with the reason, so they don't get re-raised.
>
> **Last restructured:** 2026-07-17 · **Last updated:** 2026-07-28 (**§1 Pre-Phase 4 queue is now EMPTY — P1 through P5 all closed.** P4/BUG-002 deployed (8 crit / 13 high → **0 crit / 0 high**); P5 Q1–Q4 closed out, with Q8 marked do-not-fix in the same pass, so no open question remains. **Next: Phase 4 proper — the Workflows PRD, then engine ADR-009.** 47 medium/low Dependabot alerts remain, deferred to §4, alongside BUG-004.)

---

## Source docs (deep-dive references)

| Doc | Covers |
|-----|--------|
| `docs/project/open-questions.md` | Q1–Q8 — full context, options, recommendations |
| `docs/project/open-bugs.md` | BUG-001 → BUG-004 |
| `docs/project/usage-findings.md` | UF-001, UF-002, UF-003 |
| `docs/project/workflows-scoping.md` | Temporal Workflows feature scoping + engine comparison |
| `docs/project/temporal-full-migration.md` | Complete change inventory + strangler-fig sequence |
| `docs/archive/phase3/PHASE3_DEFERRED.md` | Items deferred out of Phase 3 |

---

## 1. Pre-Phase 4 — fix before starting the migration

Selection rule: **survives Temporal** (the migration won't fix it) **and** stands alone
(no unresolved design decision blocking it). Plus one exception noted below.

> ✅ **This queue is complete as of 2026-07-28 — P1, P1b, P2, P3, P3b, P4 and P5 are all
> closed, and everything except P5 is verified in production.** Nothing here blocks the
> migration any more; the next work is §2 (PRD → ADR-009). The rows are kept because
> several carry **domain knowledge that must be ported into Temporal activities, not
> deleted with the NATS plumbing** — specifically Q5's `ensure_ready()` + 180s timeout,
> and the transient/terminal MinIO classifier from P3b. See §3.

| # | Item | Why now | Size |
|---|------|---------|------|
| **P1** ✅ **DONE** | **Q6 — LLM worker `ack_wait`** — **closed in production 2026-07-21.** Code `6fb5b9c`; live consumer recreated and verified at `Ack Wait: 2m0s` (was `30.00s`). Recreate procedure is recorded in the Q6 status block in `open-questions.md` — reuse it verbatim for P1b. | ⚠️ **Exception to the rule: this one *does* dissolve under Temporal, but it was actively firing in production.** No `ack_wait` → default 30s → NATS redelivers mid-call → **duplicate LLM calls billed to users**. Same bug caused an infinite re-scrape loop on the playwright worker. **Still worth doing:** the invoice-vs-run-count audit, to learn whether this double-billed users or stayed latent. | S |
| **P1b** ✅ **DONE** | **Q5 — cold starts + transient-failure retry** — **closed in production 2026-07-22.** A live (`df44f95`, timeout 60→180); B + C shipped (`e1fde0d`, pushed + ff-merged as `fbcf254`, image `main-1784711895-fbcf254b…`). Consumer recreated; verified on the live consumer via `--json`: `ack_wait 2m0s`, **`max_deliver: 3`** (was `-1`). | Closed. Two carry-forwards in the Q5 status block in `open-questions.md`: (1) `nats consumer info` **omits `Max Deliver` when it is `-1`** — use `--json`, the table output can't distinguish "capped" from "uncapped"; (2) the surviving half of Q5 — `ensure_ready()` + the 180s timeout — is **business logic that must be ported into the Temporal LLM activity**, not deleted with the NATS plumbing (see §3). | S |
| **P2** ✅ **DONE** | **BUG-003 — bot-block pages stored as `completed`** (minimum fix) — **closed in production 2026-07-22.** Merged as `8168760` (develop + main); image `main-1784742943-8168760c…` deployed. `playwright-worker/worker/blocking.py`, tiered classifier, 9 Tier-1 vendors; 61 new tests, 131 passing. **Prod data cleaned:** the 6 poisoned `content_hash` baselines nulled + verified. **Verified against real prod bytes, not just pod health** — the deployed classifier was run inside the pod against MinIO artifacts: Amazon → `blocked:amazon`, Myntra → `blocked:akamai`; CNN (4.1 MB), Times of India (319 KB) and **browserscan.net/bot-detection (450 KB)** all correctly passed. No consumer recreate needed (no `ConsumerConfig` change). | **Ordering conflict resolved** — this ran *ahead* of UF-001 because it was the only compounding item: every wall stored as success also became a dedup baseline. Full prod audit, signal corrections and the scope/vendor decisions are in `open-bugs.md`. Middle/full tiers (getting *past* walls) stay deferred to §4, gated on UF-002. | S |
| **P3** ✅ **DONE** | **UF-001 — MinIO missing from `/health/ready`** — fixed 2026-07-23, **not** the way the finding proposed. Adding MinIO to `/health/ready` would have been wrong: that endpoint is the k8s **readinessProbe** on a single-replica API, so a MinIO outage would have 503'd the whole API (`/jobs`, auth, admin panel) — a partial outage escalated to a total one. Shipped a **split** instead: `/health/ready` keeps serving deps only (DB/Redis/NATS, unchanged, still the probe); new **`GET /health/deps`** reports those plus MinIO (`bucket_exists` + 3s timeout), 503s when degraded, and nothing routes on it. Deployment note: **no infra change** — `api.yaml` still probes `/health/ready`. | Endpoint reported `200 ok` while every job silently failed to store output if MinIO was down. Health checks are API-side and untouched by the migration. Details in `usage-findings.md`; curl recipes in `COMMANDS.md`. 6 new tests → **249**. | S |
| **P3b** ✅ **DONE** | **UF-003 — MinIO write-path failures handled inconsistently** — **closed 2026-07-24 (unpushed).** | Surfaced closing UF-001. Three behaviours, none right: (3a) **playwright + Go workers ack on a MinIO write fault** → the Q5 ack-on-failure bug; a transient blip permanently fails a job after the expensive render/LLM call is done. **Playwright ✅ `2432be7`** (new `playwright-worker/worker/errors.py` + nak/backoff; 24 tests → 155). **LLM aiohttp gap ✅ `6ad95e3`** — the port surfaced that the LLM worker retried MinIO 5xx *codes* but not MinIO *unreachable* (`aiohttp.ClientConnectionError` is not an `S3Error`); added the two aiohttp types to its `_TRANSIENT_TYPES` (3 tests → 90). **Go worker (3a) ✅ `fbce01f`** — new `http-worker/internal/worker/errors.go` (`classify`/`classifyMinIO`/`retryDelay`) + nak/backoff in `handleMessage`, capped at the consumer's existing `NATS_MAX_DELIVER` (3, already set — unlike the Python workers). **Go-specific divergence:** a `net.Error` in Go is ambiguous (both `net/http` fetcher and `minio-go` use the net stack — a dead *site* and a dead *MinIO* both raise `*net.OpError`/`*url.Error`), so transient-eligibility is scoped to the upload step via a typed `*uploadError` wrapper; only then is `net.Error`→transient / `minio.ErrorResponse.Code`→5xx applied. New `errors_test.go` (16 subtests). (3b) `result_consumer` swallowed MinIO errors with **no log line** — **✅ `7c339a2`** added `minio_stat_failed` (`storage.py`, the money-adjacent one — silent stat→0 under-counts quota) and `content_hash_failed` (`result_consumer.py`); one `logger.warning` each, control flow unchanged. **3a survives Temporal as domain knowledge** — the transient/terminal S3 split is the same classification §3 says must port into the activity `RetryPolicy`. Full detail in `usage-findings.md` → UF-003. | S (3a) / XS (3b) |
| **P4** ✅ **DONE** | **BUG-002 — Dependabot alerts** (critical + high only) | **Closed + deployed 2026-07-28: 8 crit / 13 high → 0 crit / 0 high.** 22 crit/high collapsed to ~9 bumps across 3 ecosystems, one commit each: **`b9c8a1a`** Go `x/crypto` 0.23→0.52 (clears **11** — all 8 crit + 3 high, all SSH-only/not-reachable; forced go 1.25 + Dockerfile bump; fixed a latent bad import in an `//go:build integration` test); **`e8726bf`** API python-multipart/cryptography/starlette/pyjwt/Mako (8 high) — **clerk-backend-api 5→6 was mandatory** (clerk 5.x pins `cryptography<47`; crypto fix needs 48), clerk 6 surface verified vs our code (no change), 249 tests green, login smoke-tested; **`b110591`** frontend js-cookie (npm `overrides`, @clerk/shared exact-pins it) + postcss/vite (dev/Windows-only). The stale committed `uv.lock` got reconciled in passing. Dependency CVEs are orthogonal to orchestration — the migration neither fixes nor worsens them. **47 medium/low remain, deferred** (see §4 BUG-002 moderate/low). | M |
| **P5** ✅ **DONE** | **Q1–Q4 — formally close out** — **closed 2026-07-28.** All four verified against live code, not just marked done, and each now carries a STATUS block in `open-questions.md`: **Q1** Option A — `uq_api_keys_user_name` + `IntegrityError`→409 (`api_key.py:13`, `users.py:54`, migration `f050c65b689a`). **Q2** Option **C, not the recommended B** — Postgres `BEFORE UPDATE` trigger `trg_jobs_updated_at` (migration `ebbcc72c1472`), because the scheduler and cancel route write via `db.execute(update(...))`, which silently skips SQLAlchemy `onupdate`. **Q3** as written — `webhook_url` → `Text` (migration `53a03ff4c7a1`). **Q4** Option **B, not the recommended A** — disable is its own operation (`PATCH /jobs/{id}` `{"schedule_status":"paused"}`), enforced in `scheduler.py:57` + a partial index; `DELETE` keeps soft-cancel, with Option C available as `?permanent=true`. The flag is deliberately **tri-state** (`NULL` = not scheduled at all), which a bare `is_active` boolean could not express. **Q8 also closed** in the same pass as **do-not-fix** (§3) so no question is left without a status. | XS |

---

## 2. Phase 4 — Temporal durable-workflows migration

**Decision made: Temporal**, chosen over DBOS/Restate for portfolio value + first-class
Python **and** Go SDKs (fits both service languages). Grounded in the **Q8** incident —
hand-rolled orchestration already caused a live feedback loop.

**The feature — "ScrapeFlow Workflows"** — one feature in nested layers, natural build order:

- **Pipelines (A)** — user-defined multi-step chains (scrape → clean → LLM → validate →
  deliver), replacing today's single hard-coded pipeline.
- **Delivery sinks (C)** — a rich block type on A: S3 / DB / Sheet / email with saga rollback.
- **Monitors (B)** — a pipeline wrapped in a durable loop: long sleeps, human-approval waits,
  scheduling. Absorbs the live scheduled-crawl gap.

**Rollout:** strangler-fig, never big-bang. Temporal comes up *alongside* NATS; new work routes
to v2 (Temporal) while v1 keeps serving in-flight work; cut v1 per-flow once proven; reversible
at each step. Full sequence in `temporal-full-migration.md` §9.

**End state:** retires `result_consumer.py`, `scheduler.py`, `webhook_loop.py`, `advisory.py`,
and the `coordinator/` service; removes NATS; workers become Temporal activity workers; the API
becomes thin and horizontally scalable (drops the single-replica / `Recreate` constraint).

**Next artifacts:** PRD (PM template) → engine **ADR-009** recording the Temporal decision and
the v1/v2 coexistence contract.

**Cutover gotchas to handle at migration time (not deferred):**
1. A job must run on **exactly one lane** — never both (double-scrape / double-LLM-bill risk).
2. Moving a recurring job to a Temporal Schedule requires **disabling it in v1**
   (`schedule_status`) or it fires on both.
3. Keep NATS workers alive (integration option **a**) until v1 is drained — worker cutover to
   activities (option **b**) is what removes v1's executors.

---

## 3. Dissolved by Temporal — do NOT fix

These were live findings during Phase 3 → 4 triage. The migration **deletes the code that
contains them**, so fixing them now is wasted work. Recorded here with the reason so they
don't get re-raised.

| Item | Dissolves because |
|------|-------------------|
| **Q6 — `ack_wait` / redelivery** | NATS is removed entirely; the whole bug class goes with it. *(Fixed pre-migration anyway as **P1** — was a live billing bug. ✅ closed in prod 2026-07-21.)* |
| **Q7 — no worker-level retry on transient LLM failures** | Activity `RetryPolicy` makes retry declarative config with backoff. ⚠️ **Written after all** — the Q5 option-B fix (`e1fde0d`) added exactly the hand-rolled retry + exception-classification table this row predicted we'd never need, because Q5 forced the issue years ahead of Temporal. **`llm-worker/worker/errors.py` and the nak branch in `handle_message` are deleted by the migration** — but port the *classification itself* into the activity's `RetryPolicy` `non_retryable_error_types`. The transient/terminal split is domain knowledge (paid-for, learned from a live incident), not NATS plumbing. |
| **Q8 — `job_runs.status` overloaded across stages** | Temporal owns the state machine; `result_consumer.py` is deleted. The proposed per-stage status refactor becomes moot. |
| **BUG-001 — scheduler stale-pending log spam** | `scheduler.py` is deleted — `_recover_stale_pending` ceases to exist. Harmless log noise until then; not worth a commit. |
| **NATS pull consumers** (P3 deferred) | Moot with NATS gone. The API single-replica / `Recreate` constraint it caused lifts as a migration payoff. |
| **Crawl webhooks bypass `create_webhook_delivery`** (P3 deferred) | The `coordinator/` service is deleted; webhook delivery collapses to a single `deliver_webhook` activity, so the duplicate insertion path disappears. |
| **Scheduled-crawl gap** (`crawls.schedule_cron` accepted but never dispatched) | Absorbed by Monitors (B) / Temporal Schedules — a scheduled crawl becomes a monitor whose pipeline is "crawl the site." |
| **Q5 — LLM cold-start handling** *(partial)* | ⚠️ **Only half dissolves — and the surviving half is now real code, not a requirement.** The "worker acks on failure so NATS never retries" half goes away (Temporal retries). What survives: **`llm.ensure_ready()` and `llm_request_timeout_seconds=180`** — activity *business* logic, because Temporal has no idea a scale-to-zero endpoint is cold and would just retry a timing-out activity. **Port `ensure_ready()` into the LLM activity**; do not let it be deleted along with the NATS plumbing around it. |

---

## 4. Survives Temporal — deferred beyond Phase 4

Real work, untouched by the migration, but not blocking it. Revisit after Phase 4.

| Item | Note |
|------|------|
| **UF-002 — per-user proxy pool** | Replace the shared platform-wide `DEFAULT_PROXY_URL` (one user's behaviour can ban the shared IP for everyone). New `user_proxies` table, provider-side rotation, secrets + dispatch + frontend UI. Also gates BUG-003's middle/full tiers. |
| **BUG-003 middle/full tiers** | Retry-on-fresh-IP and pluggable unblocker providers (Bright Data Web Unlocker, Oxylabs, ZenRows). Depends on UF-002's proxy model. |
| **BUG-004 — screenshots written to MinIO then dropped** | The worker uploads screenshot PNGs and publishes `screenshot_paths`; the API result consumer never reads the field. They are never persisted, surfaced, quota-counted, or deleted — an unbounded leak plus a storage-quota bypass. Latent today (`screenshots/` is empty in prod — nobody has used the action). Facet 1 is a **product call**: wire them through, or drop the action type. Shipping an action whose output is unreachable is the current state. Survives Temporal. |
| **BUG-002 moderate/low alerts** | The remaining **47** Dependabot advisories (32 medium + 15 low) after the crit/high sweep (2026-07-28). Dominated by two noisy transitive deps — **aiohttp ×21** (→3.14.1) and **dompurify ×17** (→3.4.12); plus go `x/net` ×1 (→0.55.0), npm react-router ×3 + react-router-dom ×1, pip Pygments/idna/pydantic-settings/pytest ×1 each. |
| **`execute_js` sandboxing / removal** | Only Playwright action executing arbitrary caller code. CSP tightening was the short-term fix. Options: drop / isolated-context sandbox / allowlist-only actions. PM → Architect decision. |
| **`[?] 40` — robots.txt parsers** | Custom Go + Python parsers vs established packages. |
| **`[?] 43` — content-hash re-read** | Hash re-reads the freshly-written MinIO object. Bundle with `schema_version 3` worker-contract changes. |
| **Batch/crawl `webhook_events` filter** | Jobs have it (null = all); batches/crawls don't. Product config — still needs to exist post-migration. Decide: add the field, or document the intentional asymmetry. |
| **Authenticated scraping (full)** | Narrow cookie-injection shipped. Full storage-state login flows need encrypted creds, session refresh, per-domain state, tenant isolation — PRD + threat-model ADR. |

---

## Sequencing

1. **Pre-Phase 4 (§1)** — P1 → P5. Roughly one focused session if BUG-002 triage is clean.
   Do **P1 (LLM `ack_wait`) first** — it's the only one costing money while it sits.
2. **Phase 4 (§2)** — write the PRD, then **ADR-009**, then execute the strangler-fig sequence
   from `temporal-full-migration.md` §9.
3. **Post-Phase 4 (§4)** — revisit, led by UF-002 (which unblocks BUG-003's remaining tiers).

**Do not fix §3.** If one of those resurfaces in triage, check this table before writing code.
</content>
</invoke>
