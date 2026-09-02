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
> **Last restructured:** 2026-07-17 · **Last updated:** 2026-09-07
>
> **Change log.** One line per change to *this document*. ⚠️ Each item row below carries its own
> filed / confirmed / corrected dates — those are authoritative, not this table. For what an
> ADR-009 section review decided, read **ADR-009's `## Review status`** block; re-telling it here
> is what made this header 9,882 characters.

| Date | Change to this backlog |
|---|---|
| 2026-09-08 | **ADR-009 promoted to `Accepted`** (owner decision), and **`temporal-full-migration.md` redrawn** against it; ADR-009's pre-redraw notes corrected in place. No backlog items filed or moved; the artifact-chain line and the ADR-009 row restated. **`workflows-scoping.md` redrawn** the same day (six 🔴 markers cleared), closing ADR-009 §14's two-document disposition. **PRD-019 numbered** (owner's call) and given its §2 row with four obligations, closing 14d. **PRD-016's ADR-009 carry-back pass** — four owed items carried in, no decision changed. **Cutover gotchas expanded 3 → 5**: the four lane-blind admin meters (15e/16c) with their symptoms, the drain gate's two firing points, and gotcha 3 corrected — it still described the **rejected** NATS bridge |
| 2026-09-07 | Header change log condensed — the ADR-009 review narratives removed (verified duplicates of ADR-009 `## Review status`; 46-token sweep, 0 misses) |
| 2026-09-01 → 09-05 | ADR-009 §14–§17 + both closing blocks reviewed. **The section review is COMPLETE; the document is still `Draft`.** No backlog items filed or moved |
| 2026-08-28 | **BUG-010 filed → §4** (mid-crawl URLs never SSRF-checked). **BUG-006 corrected: 3 of 7 manifests, not 3 of 6** — `mcp/` was missing from its own list |
| 2026-08-26 | **BUG-009 filed → §4** (`JobNotifier` never reconnects) |
| 2026-08-25 | **P8 filed → §1** — the shared per-object storage ledger. Pre-migration, **not** Temporal-era: BUG-007 cannot be fixed without per-object accounting and P7 needs the same table. Sequenced **after P6, before P7 + BUG-007** |
| 2026-08-23 | **BUG-008 → §3 do-not-fix** (owner's decision): the coordinator's result consumer has never existed |
| 2026-08-17 | **BUG-007 filed** (LLM storage accounting inflated and leaking), from the ADR-009 §8 metering review. No row of its own — it rides §1's sequence; writeup in `open-bugs.md` |
| 2026-08-08 | **P7 filed → §1** — crawls consume **zero** of all three quota meters. PM decided (PRD-016 OQ-4 round 3) crawls join ADR-009 §3's run-counting view: **one unit per page** for monthly runs and storage, **one unit per crawl** for concurrency. Decision blocks ADR-009 §3/§8; implementation is §1 work, after P6 |
| 2026-08-05 | **BUG-006 filed → §4** — Dependabot scans a minority of dependency manifests; the §4 alert-count row was written from a number covering half the repo. Deferred behind BUG-005 + Temporal |
| 2026-08-04 | **§1 has one open item again — P6 / BUG-005**, batch broken on all three execution paths, found reviewing ADR-009's inputs. P1–P5 stay closed. Same day: PRD-016 PM review round 2; §2's PRD-016/017/018 rows updated |
| 2026-07-28 | **§1 was EMPTY — P1 through P5 all closed.** P4/BUG-002 deployed (8 crit / 13 high → 0 / 0); P5 closed Q1–Q4 with Q8 marked do-not-fix, leaving no open question. 47 medium/low alerts deferred to §4 |

---

## Source docs (deep-dive references)

| Doc | Covers |
|-----|--------|
| `docs/project/open-questions.md` | Q1–Q8 — full context, options, recommendations |
| `docs/project/open-bugs.md` | BUG-001 → BUG-010 |
| `docs/project/usage-findings.md` | UF-001, UF-002, UF-003 |
| `docs/project/workflows-scoping.md` | Temporal Workflows feature scoping + engine comparison |
| `docs/project/temporal-full-migration.md` | Complete change inventory + strangler-fig sequence |
| `docs/archive/phase3/PHASE3_DEFERRED.md` | Items deferred out of Phase 3 |

---

## 1. Pre-Phase 4 — fix before starting the migration

Selection rule: **survives Temporal** (the migration won't fix it) **and** stands alone
(no unresolved design decision blocking it). Plus one exception noted below.

> ✅ **P1, P1b, P2, P3, P3b, P4 and P5 are all closed**, and everything except P5 is verified
> in production. The rows are kept because several carry **domain knowledge that must be
> ported into Temporal activities, not deleted with the NATS plumbing** — specifically Q5's
> `ensure_ready()` + 180s timeout, and the transient/terminal MinIO classifier from P3b. See §3.
>
> ⚠️ **Reopened 2026-08-04 with one new item — P6 (BUG-005).** The queue was empty between
> 2026-07-28 and 2026-08-04. BUG-005 was found while reviewing ADR-009's inputs, not during
> triage, which is why it is late: **batch is broken on all three execution paths**, silently,
> and the root cause (`job_id` is NULL for batch runs while the message and storage contracts
> assume it is not) is the same identity question PRD-016's **OQ-1** has to answer for
> pipelines. ADR-009 does not block on it, but should cite it.
>
> ➕ **Second open item added 2026-08-08 — P7 (crawl quota).** Also found reviewing ADR-009, and the
> same family as BUG-005: **crawls consume zero of all three quota meters**, because every meter is
> keyed on `job_runs` and a crawl never creates one. Decided by the PM in **PRD-016 OQ-4 (review
> round 3)** — crawls join the run-counting view, **per page** for monthly runs and storage, **per
> crawl** for concurrency. The *decision* is a hard input to ADR-009 §3/§8; the *implementation*
> ships pre-migration, after P6.
>
> ➕ **Third open item added 2026-08-25 — P8 (storage ledger).** Not found in triage: it fell out of
> settling ADR-009 §8d, and it **re-sequences the two items above it**. Storage accounting moves to
> a single shared **per-object** table, because the current per-run stamp physically cannot record
> a run with two artifacts — which *is* BUG-007 — and a pipeline run has one object per
> content-producing block, a count unknown when the schema is written. It reads as Temporal-era
> design because that is where it was derived, but **it is pre-migration**: BUG-007 cannot be fixed
> without it, and P7's per-page counting plus reclaim is the same table under another name. Build it
> once, before both, and v2 inherits it; build it after, and P7 invents a crawl-shaped marker that
> the ledger then replaces — **the per-lane mistake this whole family of bugs is made of, committed
> on purpose.** Sequence: **P6 → P8 → P7 + BUG-007 together.**

| # | Item | Why now | Size |
|---|------|---------|------|
| **P1** ✅ **DONE** | **Q6 — LLM worker `ack_wait`** — **closed in production 2026-07-21.** Code `6fb5b9c`; live consumer recreated and verified at `Ack Wait: 2m0s` (was `30.00s`). Recreate procedure is recorded in the Q6 status block in `open-questions.md` — reuse it verbatim for P1b. | ⚠️ **Exception to the rule: this one *does* dissolve under Temporal, but it was actively firing in production.** No `ack_wait` → default 30s → NATS redelivers mid-call → **duplicate LLM calls billed to users**. Same bug caused an infinite re-scrape loop on the playwright worker. **Still worth doing:** the invoice-vs-run-count audit, to learn whether this double-billed users or stayed latent. | S |
| **P1b** ✅ **DONE** | **Q5 — cold starts + transient-failure retry** — **closed in production 2026-07-22.** A live (`df44f95`, timeout 60→180); B + C shipped (`e1fde0d`, pushed + ff-merged as `fbcf254`, image `main-1784711895-fbcf254b…`). Consumer recreated; verified on the live consumer via `--json`: `ack_wait 2m0s`, **`max_deliver: 3`** (was `-1`). | Closed. Two carry-forwards in the Q5 status block in `open-questions.md`: (1) `nats consumer info` **omits `Max Deliver` when it is `-1`** — use `--json`, the table output can't distinguish "capped" from "uncapped"; (2) the surviving half of Q5 — `ensure_ready()` + the 180s timeout — is **business logic that must be ported into the Temporal LLM activity**, not deleted with the NATS plumbing (see §3). | S |
| **P2** ✅ **DONE** | **BUG-003 — bot-block pages stored as `completed`** (minimum fix) — **closed in production 2026-07-22.** Merged as `8168760` (develop + main); image `main-1784742943-8168760c…` deployed. `playwright-worker/worker/blocking.py`, tiered classifier, 9 Tier-1 vendors; 61 new tests, 131 passing. **Prod data cleaned:** the 6 poisoned `content_hash` baselines nulled + verified. **Verified against real prod bytes, not just pod health** — the deployed classifier was run inside the pod against MinIO artifacts: Amazon → `blocked:amazon`, Myntra → `blocked:akamai`; CNN (4.1 MB), Times of India (319 KB) and **browserscan.net/bot-detection (450 KB)** all correctly passed. No consumer recreate needed (no `ConsumerConfig` change). | **Ordering conflict resolved** — this ran *ahead* of UF-001 because it was the only compounding item: every wall stored as success also became a dedup baseline. Full prod audit, signal corrections and the scope/vendor decisions are in `open-bugs.md`. Middle/full tiers (getting *past* walls) stay deferred to §4, gated on UF-002. | S |
| **P3** ✅ **DONE** | **UF-001 — MinIO missing from `/health/ready`** — fixed 2026-07-23, **not** the way the finding proposed. Adding MinIO to `/health/ready` would have been wrong: that endpoint is the k8s **readinessProbe** on a single-replica API, so a MinIO outage would have 503'd the whole API (`/jobs`, auth, admin panel) — a partial outage escalated to a total one. Shipped a **split** instead: `/health/ready` keeps serving deps only (DB/Redis/NATS, unchanged, still the probe); new **`GET /health/deps`** reports those plus MinIO (`bucket_exists` + 3s timeout), 503s when degraded, and nothing routes on it. Deployment note: **no infra change** — `api.yaml` still probes `/health/ready`. | Endpoint reported `200 ok` while every job silently failed to store output if MinIO was down. Health checks are API-side and untouched by the migration. Details in `usage-findings.md`; curl recipes in `COMMANDS.md`. 6 new tests → **249**. | S |
| **P3b** ✅ **DONE** | **UF-003 — MinIO write-path failures handled inconsistently** — **closed 2026-07-24 (unpushed).** | Surfaced closing UF-001. Three behaviours, none right: (3a) **playwright + Go workers ack on a MinIO write fault** → the Q5 ack-on-failure bug; a transient blip permanently fails a job after the expensive render/LLM call is done. **Playwright ✅ `2432be7`** (new `playwright-worker/worker/errors.py` + nak/backoff; 24 tests → 155). **LLM aiohttp gap ✅ `6ad95e3`** — the port surfaced that the LLM worker retried MinIO 5xx *codes* but not MinIO *unreachable* (`aiohttp.ClientConnectionError` is not an `S3Error`); added the two aiohttp types to its `_TRANSIENT_TYPES` (3 tests → 90). **Go worker (3a) ✅ `fbce01f`** — new `http-worker/internal/worker/errors.go` (`classify`/`classifyMinIO`/`retryDelay`) + nak/backoff in `handleMessage`, capped at the consumer's existing `NATS_MAX_DELIVER` (3, already set — unlike the Python workers). **Go-specific divergence:** a `net.Error` in Go is ambiguous (both `net/http` fetcher and `minio-go` use the net stack — a dead *site* and a dead *MinIO* both raise `*net.OpError`/`*url.Error`), so transient-eligibility is scoped to the upload step via a typed `*uploadError` wrapper; only then is `net.Error`→transient / `minio.ErrorResponse.Code`→5xx applied. New `errors_test.go` (16 subtests). (3b) `result_consumer` swallowed MinIO errors with **no log line** — **✅ `7c339a2`** added `minio_stat_failed` (`storage.py`, the money-adjacent one — silent stat→0 under-counts quota) and `content_hash_failed` (`result_consumer.py`); one `logger.warning` each, control flow unchanged. **3a survives Temporal as domain knowledge** — the transient/terminal S3 split is the same classification §3 says must port into the activity `RetryPolicy`. Full detail in `usage-findings.md` → UF-003. | S (3a) / XS (3b) |
| **P4** ✅ **DONE** | **BUG-002 — Dependabot alerts** (critical + high only) | **Closed + deployed 2026-07-28: 8 crit / 13 high → 0 crit / 0 high.** 22 crit/high collapsed to ~9 bumps across 3 ecosystems, one commit each: **`b9c8a1a`** Go `x/crypto` 0.23→0.52 (clears **11** — all 8 crit + 3 high, all SSH-only/not-reachable; forced go 1.25 + Dockerfile bump; fixed a latent bad import in an `//go:build integration` test); **`e8726bf`** API python-multipart/cryptography/starlette/pyjwt/Mako (8 high) — **clerk-backend-api 5→6 was mandatory** (clerk 5.x pins `cryptography<47`; crypto fix needs 48), clerk 6 surface verified vs our code (no change), 249 tests green, login smoke-tested; **`b110591`** frontend js-cookie (npm `overrides`, @clerk/shared exact-pins it) + postcss/vite (dev/Windows-only). The stale committed `uv.lock` got reconciled in passing. Dependency CVEs are orthogonal to orchestration — the migration neither fixes nor worsens them. **47 medium/low remain, deferred** (see §4 BUG-002 moderate/low). | M |
| **P6** 🔴 **OPEN** | **BUG-005 — batch broken on all three execution paths.** (A) `playwright` batch: workers reject `job_id: null` as malformed and **ack+drop** → items stuck `pending` forever, unrecoverable (stale-pending resolves `Job` by a NULL id and skips — the aside in BUG-001 *is* this symptom). (B) `http` batch: Go unmarshals `null` into `string` as `""` with **no error**, so every item writes `latest/.html` and `history//{ts}.{ext}` — items overwrite each other within a batch, and across users the same object is served to both tenants, breaking isolation at the storage layer where no 404 guard reaches. (C) batch + `llm_config`: same drop at the LLM stage → stuck `processing` forever, batch counters never reach `total`, `batch.completed` never fires. Both parser behaviours reproduced directly. `api/tests/test_batch.py:451` **asserts the broken value is correct**, which is why every suite is green. | ⚠️ **Second exception to the rule** — the containing code *is* deleted by the migration (§3 would say do-not-fix), but this is the **Q6 precedent**: a live, silent, shipped-feature failure with the migration months out. Two parts survive regardless: the **identity decision** (what a non-job run is keyed on) is literally OQ-1, and the **cross-service contract test** outlives the transport. Fix is not one line — the message contract and the ADR-002 §4 artifact-path convention must change together, or half the paths stay broken. Latent in prod (batch appears unused). Full writeup: `open-bugs.md` → BUG-005. | M |
| **P7** 🔴 **OPEN** *(decision ✅ owner-confirmed 2026-08-08; implementation not started)* | **Crawls consume zero quota — the crawl lane is invisible to all three meters.** `JobRun` rows are created in exactly three places (`routers/jobs.py:207`, `routers/batch.py:105`, `core/scheduler.py:80`) and **never for a crawl**; crawl work lives in `crawl_pages`. `routers/crawls.py` has **no quota check of any kind**, and `increment_storage_bytes` has one call site (`result_consumer.py:85`) gated on a `job_runs` column, which the coordinator's result handler never reaches. **A 10,000-page crawl therefore costs zero monthly runs, zero concurrent slots and zero counted bytes, from one API call** — 2.8 GB–40 GB of stored artifacts (BUG-003's measured 291 KiB–4.1 MiB page range) against an enforced 5 GB default. Nobody decided crawls are free; the query never looked. **Decided by the PM in `phase4-prd/PRD-016-workflows-pipelines.md` → OQ-4 (review round 3, 2026-08-08):** crawls join ADR-009 §3's run-counting view; the unit is **one fetch of one target URL producing one stored result**, so **per page** for `monthly_runs_limit` and `storage_bytes_used`, and **per crawl** for `concurrent_jobs_limit` (cost and contention are different quantities — per-page concurrency would put every default crawl permanently over the default ceiling of 5). Admission pre-checks `max_pages` as batch pre-checks `len(urls)`; the meter charges actuals. **Hard precondition:** crawl artifacts must become **reclaimable** first — nothing frees them today (`DELETE /crawls/{id}` deletes no objects; `routers/jobs.py:391`, `admin.py:213`, `admin.py:336` all enumerate `job_runs.result_path` only, so **even deleting a user orphans their crawl artifacts in MinIO**). Storage accounting **starts at cutover; history is not reconciled**. Rollout: monthly/concurrency need no grandfathering (both recount live, so nobody can land retroactively over-limit); run the read-only "what would 90 days of crawls have cost" audit first; if anyone is affected, bump that user's `user_quotas` row rather than the global default; then accept-and-announce. | ⚠️ **Blocks ADR-009 §3/§8 as a decision, not as code** — the view is being written now, and retrofitting a lane into "the single definition of a run" later is the audit-every-call-site failure the view exists to prevent. **Not a §3 do-not-fix:** `core/quota.py` and `routers/crawls.py` **survive** the migration, and only the storage-accounting call site sits in `coordinator/` (deleted). `CrawlWorkflow` inherits the rule unchanged. Sequence **after P6/BUG-005**, which re-keys the v1 artifact path and touches the same accounting surface. | M |
| **P8** 🔴 **OPEN** *(decision ✅ owner-confirmed 2026-08-25; implementation not started)* | **Storage accounting moves to a shared per-object ledger.** Today the "already counted" record is `job_runs.storage_accounted_at` — **one stamp per run** — and `_try_increment_storage` (`result_consumer.py:81`) short-circuits on it. That short-circuit is not wrong; it makes NATS redelivery idempotent, which is necessary. **Its granularity is wrong: it is keyed on the run when it should be keyed on the stored object**, so a redelivery of one result and a genuinely second artifact are indistinguishable. An LLM job has two chargeable objects, so the second is silently skipped — **that is BUG-007** — and the delete path has the mirror-image bug, deriving **one** filename from `job.output_format` (`routers/jobs.py:395`) when an LLM job leaves **four** objects (the scrape writes the job's format, the LLM always writes `.json`, so the two `latest/` keys differ by extension). Adding columns cannot fix this: a pipeline run produces one object per content-producing block and the count is unknown when the schema is designed. **Shape (ADR-009 §8d):** one shared table, not one per lane; **the meter reads `user_id` and `bytes` only**, so it is lane-blind and a future lane cannot be forgotten out of it; the producer link is nullable FKs used solely by delete and collection. Per-lane tables were rejected because their failure is *silent* — a missing `UNION` arm returns a well-formed, too-small number, **which is exactly P7** — while a shared table's failure is a loud rejected insert. ⚠️ **Design the `CHECK` to be widened:** `webhook_deliveries` is already a shared table whose closed `num_nonnulls(job_id, batch_id, crawl_id) = 1` structurally rejects the pipeline lane. | **Sequenced after P6, ahead of P7 and BUG-007 — both are consumers of it.** Not a §3 do-not-fix and **not Temporal-era work**, despite being derived during the ADR review: `core/quota.py` survives the migration, BUG-007 is on live v1 code the migration never touches, and P7's per-page counting plus reclaim is this table under another name. Building it after P7 means P7 invents a crawl-shaped marker that the ledger then replaces. **Three things fall out for free:** delete stops guessing filenames (kills the orphan class, not just the LLM case), **BUG-004's screenshots need no separate mechanism** (a stored object is a ledger row, so it is charged and deletable by the same path), and §8b's per-lane accounted-at markers are **withdrawn** — the ledger row is the marker, so "counting starts at cutover, no backfill" holds by construction rather than by a date comparison. ADR-009 §8a/§8d · `open-bugs.md` → BUG-007 | M |
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

**Artifact chain:** PRD-016 (✅ written, PM-reviewed ×2) → **ADR-009** (✅ **Accepted 2026-09-08**; drafted
2026-08-04, section review complete 2026-09-05) → **PRD-019** (conditional execution, layer A) →
PRD-017 (C) / PRD-018 (B). ⚠️ **Index order is not build order here.** ADR-009 §14 places PRD-019
**before** PRD-018 despite its higher number — the number is an identifier, not a schedule
(owner's call, 2026-09-08). PRD-017 is unblocked and may proceed in parallel with PRD-019.

| Artifact | State |
|---|---|
| **[PRD-016 — Workflows: Pipelines](./phase4-prd/PRD-016-workflows-pipelines.md)** | ✅ **written 2026-07-28 · PM-reviewed ×3 to 2026-08-08 · ADR-009 carry-back pass 2026-09-08 — Architect pass complete, next consumer is the Tech Lead.** The carry-back landed four items ADR-009's review owed this PRD (§4's unfixed Problem item, §10's two payload-shape divergences, §8's reversed storage rule under two OQ-4 passages, §14a's written-vs-built distinction); **no decision in the PRD changed**. Covers layer **A only**; C and B get their own PRDs so the Architect isn't designing against a moving target. Acceptance gate is **R6**: reproduce today's `scrape → LLM → webhook` recipe as a pipeline with equivalent output *before* any new block type is designed. **11 open questions** for the Architect — **OQ-2** (editing a pipeline with runs in flight) and **OQ-3** (structurally enforcing one-lane-only) are the two most likely to produce a subtle correctness bug. **OQ-6** is the do-not-delete list: LLM cold-start handling + the transient/terminal storage classifier are *block requirements*, not NATS artifacts. **Round 2 settled three Architect escalations:** (1) **no change-detection / cost gate in layer A** — both halves go to **Monitors (B)**, because "the previous run of this same thing" is undefinable once R1 run inputs exist; the repeat-run cost delta (one LLM call **+ one stored artifact**) is an accepted, measured, named exclusion and **B cannot ship without the gate**; (2) **at most one Webhook block per pipeline**, rejected at save time — multi-destination delivery is layer **C**'s defining capability and arrives there with saga rollback; (3) **cancellation never aborts a block mid-execution** (the Scrape exception is dropped), with new user-facing requirements that the wait be acknowledged, visible, time-bounded, and that completed blocks' outputs stay retrievable. **OQ-10 is half-answered; OQ-4's PM constraint is split into hard metering parity vs a waived, expiring feature-parity gap.** |
| PRD-017 — Delivery sinks (C) | not started — write after A ships. **Now carries an inherited commitment:** PRD-016 caps layer A at **one Webhook block** and points users here for "one result, several destinations" — so multi-sink fan-out **with rollback** is C's to deliver, not an optional extra. |
| PRD-018 — Monitors (B) | not started — absorbs the dormant scheduled-crawl gap. **Now carries a launch requirement:** PRD-016 assigns **both halves of change detection** (the cost gate *and* the reporting diff) to B, because a monitor is what supplies "the previous run of this same thing." B **does not ship without the cost gate** — layer A deliberately ships without it, and B is the layer that makes its absence hurt (scheduled reruns). B also depends on a layer-A primitive it cannot build for itself: a block that ends a run early with a `completed` outcome (PRD-016 OQ-1) — **that primitive is PRD-019's to deliver**, which is why PRD-019 is written and built before PRD-018 despite sorting after it. |
| **PRD-019 — Conditional execution (layer A)** | **Numbered 2026-09-08 (owner's call); not started.** ⚠️ **Sorts last, required first** — ADR-009 §14 places it **before PRD-018**, and PRD-018 cannot ship without it, so read this row rather than the number. Layer **A**, additive to the block model, deliberately *not* absorbed into Monitors. **Written** before PRD-018 and may be written during A's build; **built** after layer A ships and before B is built. **It owes four things** ([14d](../adr/ADR-009-workflow-engine-temporal.md#14d-the-deliverable-this-section-creates-has-no-name-and-that-is-the-failure-this-section-is-about)): (1) the **Validate-block precedent brief** — Validate is the existing block that already evaluates a condition, so it is the precedent a condition vocabulary must be designed against; (2) the **replay-determinism constraint** on what a condition may *say*, which is the hard question and runs into PRD-016's expression-evaluator non-goal; (3) the **halt-early block** — a block that ends a run with a `completed` outcome, which **B cannot build for itself** (PRD-016 OQ-1); (4) **run-level failure notification** (ADR-009 15f), R6's fourth exclusion — a job saved with `webhook_events: ["job.failed"]` has **no expressible layer-A pipeline equivalent at all**, because the only notification mechanism sits at the end of the success path. |
| **[ADR-009 — Workflow Engine: Temporal + v1/v2 coexistence](../adr/ADR-009-workflow-engine-temporal.md)** | ✅ **ACCEPTED 2026-09-08 — the decision of record: implement against it and cite it as settled.** The section-by-section review completed 2026-09-05 (§1–§17 and both closing blocks) and the owner has taken the promotion decision. ⚠️ An accepted ADR is immutable — a change of decision is a new, superseding ADR. **The ADR's own `## Review status` block (top of file) is authoritative** for what each section now says — read its **Reversed or withdrawn** and **Amended as a knock-on** tables before relying on any section, since they are the fastest way to catch a note that has gone stale. It answers all 11 of PRD-016's open questions. Work it pushed out of itself and into this backlog: **P7**, **P8**, **BUG-007**–**BUG-010**, and the pre-migration sequence **P6 → P8 → P7 + BUG-007**, which is the entry condition for any build work. |

**Cutover gotchas to handle at migration time (not deferred):**
1. A job must run on **exactly one lane** — never both (double-scrape / double-LLM-bill risk).
   Enforced by ADR-009 §7's four mechanisms; the lane marker on `job_runs` is built **at the job
   cutover**, not earlier, where it would be inert and untestable.
2. Moving a recurring job to a Temporal Schedule requires **disabling it in v1**
   (`schedule_status`) or it fires on both. ⚠️ That flag is **user-writable**: one
   `PATCH /jobs/{id}` with `{"schedule_status": "active"}` re-arms both lanes. Gotcha 1 gets
   structural treatment; this one rests on a switch the user owns.
3. **Keep NATS workers alive until v1 is drained** — as a **second deployment of the same image**,
   one bound to NATS and one to a Temporal task queue. *(Corrected 2026-09-08: this read "integration
   option **a**… worker cutover to option **b** is what removes v1's executors." ADR-009 §9
   **rejected option (a)**; there is no bridge. The workers port to Temporal in the **first**
   increment, and removing v1's executors is deleting the NATS-bound deployment — after the flow
   migration, not during it.)*
4. **Run the drain gate at every flow cutover, not only at deletion.** Both firings read the same
   three numbers: the flow is drained, and its NATS consumers report **zero unprocessed messages
   and zero outstanding acks**. ⚠️ Verify with **`nats consumer info --json`** — the table output
   omits `Max Deliver` when it is `-1`. An already-published, unacked message is still deliverable
   to a v1 worker no matter what the routing switch says, and **none of gotcha 1's four mechanisms
   reaches it** (the workers hold no DB access and cannot read a lane marker). ADR-009 16b.
5. **Four admin meters read a table or flag one lane stops writing, and will report numbers that
   are well-formed and wrong.** ⚠️ **The fix is naming, not features:** scope them to the job lane
   in their own identifiers, or return them absent for the pipeline lane. Declining to build
   pipeline delivery stats is a legitimate choice; leaving a meter that lies is not.

   | meter | reads | breaks at | symptom |
   |---|---|---|---|
   | `webhook_deliveries_pending` | `webhook_deliveries`, no lane filter (`admin.py:517`) | pipeline lane ships | ⚠️ rendered on the Usage page (`UsageStats.tsx:66`) |
   | `webhook_deliveries_exhausted` | same (`admin.py:518`) | pipeline lane ships | undercounts silently |
   | `webhook_delivery_success_rate_7d` | same (`admin.py:675`) | pipeline lane ships | reports **100%** while every pipeline delivery fails |
   | `active_recurring_jobs` | `schedule_status = 'active'` (`admin.py:519`) | schedule and webhook cutover | ⚠️ rendered (`UsageStats.tsx:67`); counts **down toward zero** as schedules move to Temporal and fire normally on v2 |

   The first three are ADR-009 **15e**, the fourth **16c**. ⚠️ **A dormant meter is not a correct
   one** — there are no scheduled jobs in production today, so `active_recurring_jobs` reads 0
   before and after and the defect is invisible for the same reason 13d's was. Monitors (layer B)
   is *entirely* about recurrence, so the population this breaks arrives with it.
   ⚠️ **This is the project's recurring defect, for the fourth and fifth time**: a meter keyed on a
   table a new lane does not write to — BUG-005 (batch invisible, `job_id` NULL), P7 (crawls
   invisible, every meter reads `job_runs`), and it is why ADR-009 §3 moved run counting onto a
   **view** instead of naming a table.

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
| **BUG-001 — scheduler stale-pending log spam** | `scheduler.py` is deleted — `_recover_stale_pending` ceases to exist. Harmless log noise until then; not worth a commit. ⚠️ **True of the end state, FALSE of the transition — correction owed since 2026-08-10, applied 2026-08-25.** The *log spam* dissolves; the **loop itself becomes a coexistence hazard** the moment any job runs on v2. `scheduler.py:131` re-publishes **every** `job_runs` row stale at `pending` past 10 minutes straight to NATS **with no lane filter**, and §3 of ADR-009 keeps `job_runs` as a read-model mirror for migrated jobs — so a v2-owned run whose workflow hasn't started yet (worker pod down, task-queue backlog) is dispatched to a **v1 worker**, and ADR-009 §7's mechanism 2 never intervenes because v1 started no *workflow*, it published a *message*. Silent, and it fires precisely when v2 looks stalled. **Do-not-fix applies to the log spam only.** The lane filter is real work, owed at **migration step 2**, and is ADR-009 §7's **mechanism 4** (a lane marker on `job_runs`, written in the insert transaction). Do not read this row as licence to leave the loop lane-blind through coexistence. |
| **NATS pull consumers** (P3 deferred) | Moot with NATS gone. The API single-replica / `Recreate` constraint it caused lifts as a migration payoff. |
| **Crawl webhooks bypass `create_webhook_delivery`** (P3 deferred) | The `coordinator/` service is deleted; webhook delivery collapses to a single `deliver_webhook` activity, so the duplicate insertion path disappears. |
| **Scheduled-crawl gap** (`crawls.schedule_cron` accepted but never dispatched) | Absorbed by Monitors (B) / Temporal Schedules — a scheduled crawl becomes a monitor whose pipeline is "crawl the site." |
| **BUG-008 — the coordinator's result consumer has never existed** | ✅ **Owner's decision, 2026-08-23: not fixed on the NATS path.** The defect *is* the NATS integration — `coordinator/result_handler.py:203` adds a second consumer on `scrapeflow.jobs.result`, which the **work-queue** `SCRAPEFLOW` stream refuses because `api-result-consumer` already claims that subject. So crawls dispatch pages and never complete, silently (the subscribe sits outside the loop that would catch it, and `main.py:82`'s `gather(..., return_exceptions=True)` swallows the death). `coordinator/` is deleted by the migration and crawl result handling becomes a workflow awaiting its activities — no queue, no second consumer, no subject to contend for, so **no version of this defect survives**. ⚠️ **Condition:** crawls migrate **last**, so this stays broken for the whole migration. Safe only while usage is zero — it is (`crawls` and `crawl_pages` are empty). If crawls are ever offered before that step, reject `POST /crawls` rather than repair the consumer. Full writeup: `open-bugs.md` → BUG-008. **Contrast BUG-006**, which must *not* be closed as dissolved: there the broken thing is behaviour that ports, here it is plumbing that is replaced. |
| **Q5 — LLM cold-start handling** *(partial)* | ⚠️ **Only half dissolves — and the surviving half is now real code, not a requirement.** The "worker acks on failure so NATS never retries" half goes away (Temporal retries). What survives: **`llm.ensure_ready()` and `llm_request_timeout_seconds=180`** — activity *business* logic, because Temporal has no idea a scale-to-zero endpoint is cold and would just retry a timing-out activity. **Port `ensure_ready()` into the LLM activity**; do not let it be deleted along with the NATS plumbing around it. |

---

## 4. Survives Temporal — deferred beyond Phase 4

Real work, untouched by the migration, but not blocking it. Revisit after Phase 4.

| Item | Note |
|------|------|
| **UF-002 — per-user proxy pool** | Replace the shared platform-wide `DEFAULT_PROXY_URL` (one user's behaviour can ban the shared IP for everyone). New `user_proxies` table, provider-side rotation, secrets + dispatch + frontend UI. Also gates BUG-003's middle/full tiers. |
| **BUG-003 middle/full tiers** | Retry-on-fresh-IP and pluggable unblocker providers (Bright Data Web Unlocker, Oxylabs, ZenRows). Depends on UF-002's proxy model. |
| **BUG-004 — screenshots written to MinIO then dropped** | The worker uploads screenshot PNGs and publishes `screenshot_paths`; the API result consumer never reads the field. They are never persisted, surfaced, quota-counted, or deleted — an unbounded leak plus a storage-quota bypass. Latent today (`screenshots/` is empty in prod — nobody has used the action). Facet 1 is a **product call**: wire them through, or drop the action type. Shipping an action whose output is unreachable is the current state. Survives Temporal. |
| **BUG-006 — Dependabot scans 3 of 7 manifests** | 🔴 **filed 2026-08-05; scope corrected 2026-08-28.** No `.github/dependabot.yml`, so default auto-setup covers only `api/uv.lock`, `frontend/package-lock.json` and `http-worker/go.sum`. **`coordinator/`, `llm-worker/`, `playwright-worker/` and `mcp/` are unmonitored** — floor-only `pyproject.toml`, no lockfile — so **four of five** Python services have never been scanned and the true advisory count is unknown. ⚠️ **The bug undercounted itself:** it was filed as "3 of 6" and omitted **`mcp/`**, the LLM-callable public surface — a manifest invisible to the count for the same reason it is invisible to the scanner (found in the ADR-009 §13 review). Surfaced by a live high: **aiohttp `CVE-2026-69244`** (OOB heap read in the C response parser, malformed chunked response; fixed 3.14.3). Reachability splits — for `api`/`llm-worker`/`playwright-worker` aiohttp only parses **MinIO** responses (trusted, not reachable), but **`coordinator/coordinator/sitemap.py:11` fetches robots.txt + sitemap XML from the *user-supplied target site* over aiohttp**, which is the one place it faces a server we don't control. **The visible alert is the unreachable copy; the reachable one is in a service Dependabot can't see.** ⚠️ **Do not close as dissolved by the migration** — `coordinator/` is deleted, but sitemap discovery *ports into a `CrawlWorkflow` activity* and takes the exposure with it unless the port switches to **httpx** (which every other untrusted-target fetch already uses — `playwright-worker/worker/robots.py:10` is the direct sibling). ✅ **This is now a recorded port requirement, not just a recommendation: ADR-009 §13** (added by the §10 review, 2026-08-26). It is the **only** do-not-delete item that must be *modified* rather than copied, which makes **a faithful port the failure mode**. Contained: OOB *read* → pod crash, and `crawl_queue` persistence means crawls survive the restart. **Deferred behind BUG-005 + Temporal** (owner, 2026-08-05). Writeup: `open-bugs.md` → BUG-006. |
| **BUG-002 moderate/low alerts** | ⚠️ **Count is stale and was only ever half the repo.** Recorded as 47 (32 medium + 15 low) after the 2026-07-28 crit/high sweep; **as of 2026-08-05 it is 51 open — 2 high, 34 medium, 15 low**, and every one of those is against the three *scanned* manifests (see BUG-006). Dominated by two noisy transitive deps — **aiohttp ×21** (→3.14.1) and **dompurify ×17** (→3.4.12); plus go `x/net` ×1 (→0.55.0), npm react-router ×3 + react-router-dom ×1, pip Pygments/idna/pydantic-settings/pytest ×1 each. The two highs are aiohttp `CVE-2026-69244` and cryptography `CVE-2026-69247` (PKCS#7 Bleichenbacher oracle, **not reachable** — we import only `Fernet`/`InvalidToken`, and clerk 6.0.1 sets no upper bound, so 48→50 won't repeat BUG-002's clerk pin problem). **Fix BUG-006 first** — triaging this list cannot find what it does not cover. |
| **BUG-009 — `JobNotifier` never reconnects** | 🔴 **filed 2026-08-26** (ADR-009 §11 review). One asyncpg `LISTEN` connection is opened at API startup (`main.py:54`) and held for the process lifetime, with **no termination handler and no reconnect path**. If it drops — a Postgres restart, an upgrade, a failover, an idle cut — both channel subscriptions are gone for good and **every WebSocket in that process goes deaf**, silently: the socket stays open, `pg_notify` keeps firing with nobody listening, and nothing logs it. Watchers sit until the 300s timeout and the page shows a stale status until manually refreshed. Masked today because the API restarts often and a job run lasts ~40s. ⚠️ **Not dissolved by Temporal — §11 keeps this component and adds a second channel to it**, and §15/Monitors stretch watcher lifetimes from 40s to hours and days, which is when a blip gets a chance to happen. ⚠️ **§11b's client-side reconnect masks this but is not the fix** — it repairs the browser's view while the server-side listener stays dead, degrading the process from push to a 5-minute poll with no operator signal; §11b rejected a server keep-alive partly because it would hide this bug, and fixing only the client arrives at the same place. Fix needs all three: detect the drop, re-register **every** channel on a backoff, and **log loudly**. Full writeup: `open-bugs.md` → BUG-009. |
| **BUG-010 — mid-crawl URLs are never SSRF-checked** | 🔴 **filed 2026-08-28** (ADR-009 §13 review). `validate_no_ssrf` runs exactly **twice**, both in the crawl-creation request, on the seed and webhook URLs (`routers/crawls.py:34-36`). After that the coordinator validates nothing and **no worker validates anything** — there is no SSRF check, IP-range test or `getaddrinfo` call in `http-worker/`, `playwright-worker/` or `llm-worker/`. Extracted links survive by accident (`link_extractor.py:33` restricts to the seed origin); **sitemap entries do not** — `sitemap.py:39` takes them verbatim from the target site's `robots.txt`, `:45` fetches them, and `result_handler.py:183` enqueues them with no origin filter, after which a worker scrapes the target and the body is served back through `GET /crawls/{id}/pages`. **A read primitive, not a blind fetch** — the crawled site chooses what the platform fetches from inside the cluster. ⚠️ **Latent only because the code path is dead:** sitemap discovery is reachable solely from `_process_crawl_result`, which has never run (**BUG-008**) — so **the migration is what switches this on**, which is the opposite of the usual latent-bug note. ✅ **Part 1 decided (ADR-009 §13d): SSRF-check at frontier admission**, where seed/link/sitemap all converge; a rejected URL is **skipped and the crawl continues**, and the refusal is terminal, never retried (§10's webhook-SSRF rule). Ships with the crawl migration. **Part 2 — a worker-side check — is recommended but unscheduled**: it is the correct point-of-use position and covers every lane, but it is two implementations in two languages that must not drift, it needs resolve-once-connect-to-that-IP to actually close DNS rebinding, and it adds a terminal failure class that must be wired into each worker's classifier at the same time. Not the crawl migration's to carry. Full writeup: `open-bugs.md` → BUG-010. |
| **`execute_js` sandboxing / removal** | Only Playwright action executing arbitrary caller code. CSP tightening was the short-term fix. Options: drop / isolated-context sandbox / allowlist-only actions. PM → Architect decision. |
| **`[?] 40` — robots.txt parsers** | Custom Go + Python parsers vs established packages. |
| **`[?] 43` — content-hash re-read** | Hash re-reads the freshly-written MinIO object. Bundle with `schema_version 3` worker-contract changes. |
| **Batch/crawl `webhook_events` filter** | Jobs have it (null = all); batches/crawls don't. Product config — still needs to exist post-migration. Decide: add the field, or document the intentional asymmetry. |
| **Authenticated scraping (full)** | Narrow cookie-injection shipped. Full storage-state login flows need encrypted creds, session refresh, per-domain state, tenant isolation — PRD + threat-model ADR. |

---

## Sequencing

1. **Pre-Phase 4 (§1)** — P1 → P5 are done. **Two left: P6 (BUG-005), then P7 (crawl quota).**
   Neither blocks ADR-009 as *code*: the ADR should cite P6 as OQ-1's precedent, then P6 can be
   fixed in parallel with, or after, the ADR — before any batch traffic arrives, not before the
   design lands. **P7's *decision* is a hard input to ADR-009 §3/§8** (already made — PRD-016 OQ-4,
   round 3); its implementation follows P6, which touches the same accounting surface.
2. **Phase 4 (§2)** — write the PRD, then **ADR-009**, then execute the strangler-fig sequence
   from `temporal-full-migration.md` §9.
3. **Post-Phase 4 (§4)** — revisit, led by UF-002 (which unblocks BUG-003's remaining tiers).

**Do not fix §3.** If one of those resurfaces in triage, check this table before writing code.
</content>
</invoke>
