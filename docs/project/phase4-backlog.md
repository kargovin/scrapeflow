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
> **Last restructured:** 2026-07-17

---

## Source docs (deep-dive references)

| Doc | Covers |
|-----|--------|
| `docs/project/open-questions.md` | Q1–Q8 — full context, options, recommendations |
| `docs/project/open-bugs.md` | BUG-001 → BUG-003 |
| `docs/project/usage-findings.md` | UF-001, UF-002 |
| `docs/project/workflows-scoping.md` | Temporal Workflows feature scoping + engine comparison |
| `docs/project/temporal-full-migration.md` | Complete change inventory + strangler-fig sequence |
| `docs/archive/phase3/PHASE3_DEFERRED.md` | Items deferred out of Phase 3 |

---

## 1. Pre-Phase 4 — fix before starting the migration

Selection rule: **survives Temporal** (the migration won't fix it) **and** stands alone
(no unresolved design decision blocking it). Plus one exception noted below.

| # | Item | Why now | Size |
|---|------|---------|------|
| **P1** | **Q6 — LLM worker `ack_wait`** | ⚠️ **Exception to the rule: this one *does* dissolve under Temporal, but it is actively firing in production.** No `ack_wait` → default 30s → NATS redelivers mid-call → **duplicate LLM calls billed to users**. The same bug caused an infinite re-scrape loop on the playwright worker. Fix is already written and proven (`67ba983`): `ConsumerConfig(ack_wait=…)` above `LLM_REQUEST_TIMEOUT_SECONDS` + `msg.in_progress()` heartbeat. NATS deletion is step 6 of a multi-month migration — too long to leave a money-burning bug live. **Caveat:** JetStream won't change `ack_wait` on an existing durable consumer — recreate out-of-band (`add_consumer`; this nats-py has no `update_consumer`). | S |
| **P2** | **UF-001 — MinIO missing from `/health/ready`** | Endpoint reports `200 ok` while every job silently fails to store output if MinIO is down. Health checks are API-side and untouched by the migration. Same silent-failure class that has already bitten this project three times. | S |
| **P3** | **BUG-003 — bot-block pages stored as `completed`** (minimum fix only) | Silent data corruption; poisons dedup baselines. **Temporal cannot fix this** — it can't tell a 200-status bot wall from real content. Minimum fix: publish `status="failed"` (`error="blocked"`) instead of `completed`. Uses the *existing* `failed` status, so it adds no new state values and doesn't conflict with the migration. Middle/full tiers (retry-on-fresh-IP, unblocker providers) stay deferred — they depend on UF-002. | S |
| **P4** | **BUG-002 — Dependabot alerts** (critical + high only) | 73 alerts on the default branch, untriaged. Dependency CVEs are orthogonal to orchestration — the migration neither fixes nor worsens them. Triage the 1 critical + 11 high; defer the 41 moderate / 20 low. | M |
| **P5** | **Q1–Q4 — formally close out** | All four are effectively resolved already (Q1 api_keys uniqueness shipped in Phase 3; Q2 `jobs.updated_at` resolved via DB trigger; Q3 `webhook_url` → `Text` housekeeping; Q4 schedule-disable resolved via `schedule_status`). Pure bookkeeping — clears noise before the migration starts. | XS |

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
| **Q6 — `ack_wait` / redelivery** | NATS is removed entirely; the whole bug class goes with it. *(Being fixed pre-migration anyway as **P1** — live billing bug, see §1.)* |
| **Q7 — no worker-level retry on transient LLM failures** | Activity `RetryPolicy` makes retry declarative config with backoff. The hand-rolled retry loop + exception-classification table is never written. |
| **Q8 — `job_runs.status` overloaded across stages** | Temporal owns the state machine; `result_consumer.py` is deleted. The proposed per-stage status refactor becomes moot. |
| **BUG-001 — scheduler stale-pending log spam** | `scheduler.py` is deleted — `_recover_stale_pending` ceases to exist. Harmless log noise until then; not worth a commit. |
| **NATS pull consumers** (P3 deferred) | Moot with NATS gone. The API single-replica / `Recreate` constraint it caused lifts as a migration payoff. |
| **Crawl webhooks bypass `create_webhook_delivery`** (P3 deferred) | The `coordinator/` service is deleted; webhook delivery collapses to a single `deliver_webhook` activity, so the duplicate insertion path disappears. |
| **Scheduled-crawl gap** (`crawls.schedule_cron` accepted but never dispatched) | Absorbed by Monitors (B) / Temporal Schedules — a scheduled crawl becomes a monitor whose pipeline is "crawl the site." |
| **Q5 — LLM cold-start handling** *(partial)* | ⚠️ **Only half dissolves.** The "worker acks on failure so NATS never retries" half goes away (Temporal retries). The **cold-start logic itself survives** — timeout tuning and an `ensure_ready()` warm-up probe are activity *business* logic; Temporal won't know a scale-to-zero endpoint is cold. **Carry the cold-start requirement into the activity design.** |

---

## 4. Survives Temporal — deferred beyond Phase 4

Real work, untouched by the migration, but not blocking it. Revisit after Phase 4.

| Item | Note |
|------|------|
| **UF-002 — per-user proxy pool** | Replace the shared platform-wide `DEFAULT_PROXY_URL` (one user's behaviour can ban the shared IP for everyone). New `user_proxies` table, provider-side rotation, secrets + dispatch + frontend UI. Also gates BUG-003's middle/full tiers. |
| **BUG-003 middle/full tiers** | Retry-on-fresh-IP and pluggable unblocker providers (Bright Data Web Unlocker, Oxylabs, ZenRows). Depends on UF-002's proxy model. |
| **BUG-002 moderate/low alerts** | The remaining 41 moderate + 20 low Dependabot advisories. |
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
