# Open Bugs

> **Consolidated Phase 4 view: [`phase4-backlog.md`](./phase4-backlog.md).** This doc holds the full writeups for each bug.

---

## BUG-001 — Stale-pending recovery queries `WHERE jobs.id IS NULL` on every scheduler tick

**Severity:** Low (harmless, but noisy)
**Discovered:** 2026-05-12
**Status:** ⛔ **CLOSED as do-not-fix (2026-07-28)** — dissolved by the Temporal migration
(`phase4-backlog.md` §3). `_recover_stale_pending` exists only because the hand-rolled
scheduler has to detect its own stalled dispatches; Temporal's activity timeouts and retry
policy make the whole recovery loop unnecessary, and `scheduler.py` is deleted. Fixing the
query would be work on code scheduled for removal. It is log noise, not a correctness
problem, so it costs nothing to leave until then.

### What happens

The scheduler's `_recover_stale_pending` loop (`api/app/core/scheduler.py`) selects all `job_runs` with `status = 'pending'` — including batch runs where `job_id IS NULL` (introduced by ADR-006). For each batch run it calls `await db.get(Job, run.job_id)` with `run.job_id = None`, which SQLAlchemy translates to:

```sql
SELECT ... FROM jobs WHERE jobs.id IS NULL
```

This query returns nothing, the `if job is None: continue` guard fires, and the run is skipped. No data is corrupted. However the query fires on every scheduler cycle (every 60s) for every stuck batch run in the DB, flooding the logs.

### Root cause

`job_runs.job_id` was made nullable in Phase 3 (ADR-006) to support batch items. The stale-pending recovery was not updated to exclude batch runs, which cannot be re-dispatched via the scheduler (no job template to build the NATS payload from).

### Fix

Add `JobRun.job_id.is_not(None)` to the stale-pending query in `_recover_stale_pending`:

```python
stmt = (
    select(JobRun)
    .where(
        JobRun.job_id.is_not(None),   # ← add this
        JobRun.status == "pending",
        JobRun.created_at < stale_cutoff,
    )
    .with_for_update(skip_locked=True)
)
```

Batch runs stuck in `pending` are not recoverable via this path anyway — their recovery would need to go through a separate batch-aware loop (Phase 4 candidate).

---

## BUG-003 — Bot-block / interstitial pages stored as successful scrape output

**Severity:** High (silent data corruption — jobs report `completed` with useless content)
**Discovered:** 2026-07-04
**Status:** ✅ **Minimum tier CLOSED in production 2026-07-22** (`8168760`, image
`main-1784742943-8168760c…`). Middle/full tiers — getting *past* walls — remain deferred to
post-Phase-4, gated on UF-002. Deployed classifier verified inside the pod against real MinIO
artifacts: Amazon → `blocked:amazon` (`tier1:amazon_opfcaptcha`), Myntra → `blocked:akamai`
(`tier1:akamai_reference_id`); CNN 4.1 MB, Times of India 319 KB and **browserscan.net/bot-detection
450 KB** all correctly passed — that last one is the real false-positive test, being a page *about*
bot detection full of the matching vocabulary. The Tier 2 size gate held.

### What happens

When a site serves a bot wall or challenge page instead of the real content, the Playwright worker stores that interstitial and marks the run `completed`. Observed live on Amazon: the scraper returns the "Continue shopping" bot-wall page as the job result.

The worker's only failure signal today is "did the browser throw an exception?" (`worker/worker.py`, the `try/except` around `page.goto` → `page.content()` → upload). A bot wall returns **HTTP 200 with a fully valid HTML page**, so `page.goto()` succeeds, nothing throws, and the interstitial flows straight through `page.content()` (line 182) → `format_output` → MinIO upload → `publish_result(status="completed")` (line 189). There is no check between grabbing the HTML and declaring success on *what* the HTML is.

Downstream consequences:
- User sees a `completed` job whose stored output is a CAPTCHA/"Continue shopping" page, not data.
- Content-dedup (PRD-015) may store the block page's hash as a **baseline**, poisoning future diff detection.
- No observability into *who* blocked (which anti-bot vendor), so proxy/stealth tuning is blind.

This is a different layer from ADR-008: that hardening fixed the *browser fingerprint* (BrowserScan `Normal`), which is enough for lenient sites (news). Ecommerce (Amazon etc.) run commercial bot managers (Cloudflare, Akamai, DataDome, PerimeterX, Kasada) that block despite a clean fingerprint — and return a 200 wall the worker can't currently distinguish from success.

### Detection point

Right after `final_url = page.url` (`worker/worker.py:183`), before `format_output`/`upload`. That's where the final URL, HTTP status, response headers/cookies, and rendered HTML are all in hand.

### Signals (cheapest/most-reliable first)

1. **HTTP status** — `page.goto()` returns a `Response` object the worker currently discards (`worker.py:171`). Capture it; 403/429/503 is a block most of the time. ⚠️ **Caught none of the three live walls** — see the prod audit below.
2. **Final URL** — ~~bot walls redirect (e.g. Amazon `/errors/validateCaptcha`); `final_url` off the requested host/path family is a strong tell.~~ **CORRECTED 2026-07-22 — this is wrong for the canonical case.** Amazon serves the wall **at the requested product URL**; `/errors/validateCaptcha` is only the `action` of a form *on* the page, never navigated to. `final_url` looks completely clean. Demote to a weak signal.
3. **Vendor cookies/headers** — `datadome`/`x-datadome`, `__cf_bm` + challenge body, `_px*`, `ak_bmsc`. These also *classify the vendor* (observability).
4. **Content heuristics** — title/body markers ("Continue shopping", "Robot Check", "Just a moment...", "Verifying you are human"), suspiciously tiny body. Fuzziest; catches 200-status walls that pass 1–3. **In practice the strongest available signal — see below.**

### Prod audit — 2026-07-22 (real evidence, not theory)

Swept every `completed` run in prod. **6 of 15 (40%) are bot walls stored as success**, across
three vendors. **All six are `engine=playwright`** — the Go http-worker is not implicated in any
live case (its `fetcher.go:72` non-2xx check already covers hard walls), which settles scope:
**fix Playwright first**.

| URL | Stored size | Marker | Vendor |
|---|---|---|---|
| `amazon.com/…/B01NBKTPTS` (job `1db4f858…`) | 5.4 KiB | "Click the button below to continue shopping"; `ue_sn = "opfcaptcha.amazon.com"`; `csm-captcha-instrumentation.min.js` | Amazon in-house |
| `myntra.com/…/36854940/buy` | **411 B** | `<h1>Access Denied</h1>`; `errors.edgesuite.net` | **Akamai** |
| `walmart.com/ip/…/25920745` (×3 runs) | **464 B** | "Robot or human? Activate and hold the button" | **PerimeterX / HUMAN** |

Genuine pages in the same bucket run **291 KiB – 4.1 MiB**. Fixture saved for tests: the Amazon
wall body (`d8dd7cf3…` → `history/1db4f858-…/1783081449.html`).

**What the evidence changes:**

- **Body size is the crispest separator** — three orders of magnitude (411 B vs 291 KiB floor).
  Underweighted in the original signal list. Not safe alone (a legitimately tiny page can exist),
  but it carries most of the weight as the second leg of a two-signal rule.
- **Vendor fingerprints are strong enough to fire alone** (`opfcaptcha`, `errors.edgesuite.net`
  + "Access Denied", "Robot or human?") — and they double as the vendor classification this bug
  asks for as observability.
- **Detection must run on raw HTML *before* `format_output`.** The three Walmart jobs are
  `output_format=markdown`, so their stored artifacts have already lost every HTML-level signal
  (scripts, meta, cookies). This is an independent reason the detection point can't move later.
- **Classifier posture is the inverse of `llm-worker/worker/errors.py`.** That one fails *closed*
  (unknown → terminal) because a wrong "transient" guess re-bills the user's key. Here a false
  "blocked" fails a working job, so be **conservative about claiming blocked**: one strong signal,
  or two weak ones together — never a lone fuzzy string match.

**✅ Baseline cleanup DONE 2026-07-22.** `UPDATE job_runs SET content_hash = NULL` on the 6 wall
runs (`70cbc47a`, `a4f48eee`, `b50a3879`, `457721d6`, `8c38208e`, `d8dd7cf3`) — 6 updated, 0
remaining, verified. Hash coverage 15 → 9 of 22 runs; `diff_detected = false` still 0. Those jobs
now re-baseline on their next run instead of matching a wall forever. Original values backed up
before the write. Rows were left `completed` deliberately — rewriting historical statuses would
misrepresent what the system actually did at the time; only the *dedup baseline* was poison.

**Dedup poisoning — latent, not yet realised (state at time of audit).** `select count(*) from job_runs where
diff_detected = false` → **0**; no suppression has fired. The three Walmart runs share hash
`283ca8c3055bc219` but belong to three *separate jobs*, and dedup is scoped per `job_id`. The six
wall hashes are sitting as baselines: the next time any of those jobs re-runs into the same wall,
dedup fires, `diff_detected=False`, and real change detection dies silently for that job. **Cleanup
is 6 rows today** — do it alongside the fix, before it accretes.

### Fix — tiered (scope DECIDED 2026-07-22)

**Scope for this phase = minimum tier only: _detect_ walls and record them as failures. Getting
_past_ walls is explicitly deferred to a later phase.** Accepted consequence: the Amazon/Myntra/
Walmart jobs above will report `failed` and stay failed. Success rate on ecommerce targets will
visibly drop — that is the fix working, converting a silent wrong answer into a loud correct one.

- **Minimum (THIS PHASE):** stop marking blocks `completed`. Publish `status="failed"` with a structured reason (`error="blocked"`, ideally vendor-classified). Fixes the silent-garbage bug on its own. Reuses the *existing* `failed` status — adds no new state values, so it can't collide with the Q8 state-machine cleanup or the Temporal migration. Includes the 6-row baseline cleanup above.
- **Middle (LATER PHASE):** on block, retry with a fresh IP before failing (escalation ladder). Needs the proxy layer to hand out a *different* IP on retry — currently `DEFAULT_PROXY_URL` is a single shared proxy (see UF-002), which limits this.
- **Full (LATER PHASE):** escalate blocked jobs to a pluggable "unblocker provider" (Bright Data Web Unlocker, Oxylabs, ScrapingBee, ZenRows), a peer to the existing proxy-provider abstraction. Larger architecture piece.

### Decisions taken 2026-07-22

**What counts as a block.** A block is *the server deliberately serving something other than the
requested content because it identified us as a bot*. Note what is absent: any mention of HTTP
status — status is evidence, not definition (the canonical Amazon case is a `200`).

The operative test: **would a real person on a normal browser and a residential connection have
gotten the real content?** If yes → block → `failed`. If no → that page *is* the site's honest
response → stays `completed`. So **paywalls, login walls, geo-blocks, age gates and genuine 404s
are NOT blocks** — a human hits the same page, and reporting them as failures would be wrong.

**Where the vendor is recorded — Option A: in the `error` string, plus a structured log line.**
`error = "blocked:<vendor>"`. No schema change.

- *Rejected B (new `block_vendor` column on `job_runs`)* — NULL on ~99% of rows, and it would
  carry **zero behaviour**: nothing branches on the vendor yet, so it is data written and never
  read. Also contradicts the Q8 guidance below, and the Temporal migration would have to carry it.
- *Rejected C (reuse `warnings` JSONB)* — `warnings` means "succeeded but imperfect". A block is
  the *failure cause*. Overloading it makes the field mean different things depending on `status`,
  which is exactly the status-overloading class of bug Q8 exists to fix.
- Two consumers, two mechanisms: **the user** needs a human answer in the UI (the `error` string);
  **we** need aggregate counts to tune stealth and later choose an unblocker vendor — that is a
  logs question, so the worker emits a structured `block_detected` event (`vendor`, `url`,
  `job_id`), mirroring the existing `content_deduplicated` line in `result_consumer.py`.
- **The `error` format is now a contract.** Stable `blocked:<vendor>` prefix; vendor is a closed
  set in the worker (`akamai` / `perimeterx` / `datadome` / `cloudflare` / `amazon` / `unknown`),
  not free text. `blocked:unknown` is deliberate — size + generic markers matched but no vendor
  fingerprint did; those are the rows to read by hand to add the next fingerprint.
- **B becomes right later**, when the middle/full tier builds vendor-conditional routing (Akamai →
  Bright Data, PerimeterX → ZenRows). Then the vendor is control flow, not a tag, and earns a
  column. General principle: *don't add schema for a decision not yet made.*

### Interactions

- **Q8 (status overloading):** a new `blocked`/`failed` outcome should land with the state-machine cleanup, not bolt more overloaded status values on. Ship the *minimum* fix (which is just "don't say completed") now; hold the retry/escalation tiers until Q8 settles.
- **Content-dedup (PRD-015):** ensure a block page's hash is never stored as a baseline.
- **UF-002 (per-user proxy):** the middle/full tiers depend on proxy rotation the current single-proxy model can't provide.

### Cross-worker error-string divergence (noted 2026-07-28 — NOT a bug, a later-phase cleanup)

The two workers report the *same* wall with *different* error strings, because the server serves
each of them a **different response** (they present differently on the wire):

- **Go http-worker** (plain HTTP client → obvious bot) gets a **hard `403`**. Caught by the generic
  non-2xx guard at `fetcher/fetcher.go:72`, at fetch time, on status alone — the body is never
  inspected. Error string: `non-2xx response from <url>: 403`. **No vendor**, does not join the
  `blocked:<vendor>` contract.
- **Playwright worker** (headed real Chrome, ADR-008 stealth → passes as human) gets a **`200` + JS
  challenge**. A 200 sails past any status check, so the only tell is the body — `detect_block()`'s
  Tier-1 regex → `blocked:akamai`.

This asymmetry is **inherent, not a defect**: it is the same reason BUG-003 existed only on the
Playwright worker (a soft-block 200 is exactly what a status check cannot catch, per the CLAUDE.md
decision record). Both workers correctly *fail* the job — the divergence is cosmetic/observability
only, and unifying it would mean teaching the Go worker to body-sniff non-2xx responses.

**Deliberately deferred, not fixed now.** The clean home for a single "blocked" outcome with a
shared vendor taxonomy is the **Temporal activity layer** (fetch + block become activities with one
failure model), so doing it today is effort the migration partly redoes. It also naturally belongs
with the **middle/full block-handling tiers** (getting *past* walls), which are already deferred
post-Phase-4, gated on UF-002 — if we build vendor-conditional routing then, the Go worker will
need a vendor too, and that is the moment to unify the string. Until then: known, intentional.

---

## BUG-004 — Screenshots are written to MinIO and then dropped on the floor

**Severity:** Medium (unbounded storage leak + quota bypass; no data corruption)
**Discovered:** 2026-07-22 (while wiring BUG-003's block path)
**Status:** Open

### What happens

The `screenshot` page action is a supported action type (`api/app/schemas/jobs.py:22`). When a job
uses it, the Playwright worker captures the PNG, uploads it to
`screenshots/{job_id}/{ts}_{index}.png` (`worker/storage.py:46`), collects the path into
`screenshot_paths`, and publishes it on the `ResultMessage` (`worker/models.py:55`).

**The API never reads that field.** `grep -rn "screenshot_paths" api/` returns nothing. The result
consumer deserialises the message, ignores `screenshot_paths`, and moves on. So every screenshot
ever taken is:

- **never persisted** — no DB column, no row, nothing pointing at the object;
- **never surfaced** — the user cannot retrieve a screenshot they asked for and paid compute for;
- **never counted** — storage accounting derives `result_size` from `minio_path` only, so
  screenshot bytes bypass `user_quotas.storage_bytes_used` entirely;
- **never deleted** — not on job delete, not on dedup, not on failure. The delete paths in
  `result_consumer.py` and `admin.py` only ever target `result_path`.

This is a **pre-existing bug on the success path**, not something BUG-003 introduced. It was found
while deciding whether the new block path should clean up after itself, and turned out to be the
larger of the two problems.

**Not yet realised in prod:** `mc ls -r p/scrapeflow-results/screenshots/` is empty — no user has
exercised the action. The leak is latent, which is why this is Medium and not High.

### Two facets

1. **The product gap (primary).** A user can request a screenshot and has no way to get it back.
   Either wire `screenshot_paths` through the consumer (a `job_run_screenshots` table, or a JSONB
   column) and expose it on `GET /jobs/{id}/result`, **or** drop the action type. Shipping an
   action whose output is unreachable is the worst of the three options and is the status quo.
2. **The quota bypass (secondary).** Even once surfaced, screenshot bytes must feed the same
   storage accounting as `result_path`, or a job with N screenshot actions is free storage. Note
   the per-job action cap (see `api/tests/test_jobs.py:1458`) bounds this per run but not overall.

### Interaction with BUG-003

On the new block path the worker publishes `failed` and deliberately does **not** upload the wall
HTML. But if the job had `actions` that already ran and took screenshots *before* detection, those
PNGs are orphaned — the same way they are on every other path.

Detection deliberately runs **after** actions, not before: moving it earlier risks false-positiving
on pages whose real content only appears once actions have run. That trade is right for BUG-003 and
should not be revisited to fix this; fix the leak at its actual source (the consumer dropping
`screenshot_paths`), which fixes every path at once.

### Fix sketch

Decide facet 1 first — it is a product call, not a bug fix. If screenshots stay:

- persist `screenshot_paths` in the consumer alongside `warnings` (which already follows exactly
  this worker → `ResultMessage` → JSONB path — see the "Action warnings persistence" row in
  `CLAUDE.md`);
- include screenshot bytes in storage accounting;
- add them to the delete paths (job delete, admin delete);
- surface them on the result endpoint.

If screenshots go, remove the action type, the worker branch, and `upload_screenshot`.

**Survives Temporal.** Nothing here is orchestration — it is a missing persistence path plus a
product decision. The migration neither fixes nor worsens it.
