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
> ✅ **Symptom gone by construction, 2026-09-11 — P9 (BUG-011), not a fix of this bug.** The
> recovery loop now branches on `batch_item_id` *before* the job lookup, so `db.get(Job, None)` is
> never called and `WHERE jobs.id IS NULL` never fires. Two things below are therefore stale and
> left as written: the **Fix** section proposes *excluding* batch runs from the query — P9 did the
> opposite, and recovers them; and *"batch runs are not recoverable via this path anyway"* is now
> false. Status unchanged: nothing here was worked on for its own sake.
> ⚠️ The v1-vs-v2 lane filter in this bug's `phase4-backlog.md` row (ADR-009 §7 mechanism 4) is a
> different question — which *engine owns* a run, not which *parent* it has — and is **still owed at
> migration step 2**.

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
> ➕ **Fourth fingerprint added 2026-09-19 — deployed the same day (`421cbfe`).** A live
> Myntra wall the classifier missed (a 481 B "Site Maintenance" 200, served to the cluster's
> datacenter IP); see the **BUG-003 addendum** directly below this section. The mechanism is
> unchanged — Tier 2 ran and had no phrase for it.

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

## BUG-003 addendum (2026-09-19) — a live wall the fingerprint list did not have

Filed against BUG-003 rather than separately: the mechanism caught nothing wrong, the list did.

**What happened.** Job `106f8e90…` (myntra.com product page, playwright, `html`) came back
`completed` in **0.8 s** with a **481 B** body:

```html
<title>Site Maintenance</title> … <h1>Oops! Something went wrong</h1>
<p>Please contact your administrator</p>
```

A `200`, stored as a result, `content_hash` computed on it — BUG-003's original symptom exactly.

**Why the classifier let it through, against the code.** Tier 1: none of the vendor markers — the
Myntra wall captured 2026-07-22 was *Akamai's* interstitial (`Reference #…`, fingerprinted); this is
Myntra's own template. Status rule: it was a 200. Empty-body rule: 481 B is over the 100 B floor.
**Tier 2 ran** (481 B is well under the 20 KB gate) and none of its fourteen phrases is on the page.
A false negative of the list, not of the tiering.

**Why it is a block and not an outage.** Fetched at the same time with the same UA: from a
residential Indian IP, Myntra served its full site (404 KB); from the cluster's egress IP —
**`139.99.121.44`, an OVH Singapore datacenter range** — it served this page in 0.18 s. The
operative test says *"a real person on a normal browser and a residential connection"*: the word
*residential* is there so that datacenter-IP denial counts. Geo-blocks are excluded only when the
page honestly says so; this one lies about why. The tell is **"please contact your
administrator"** — denial-template language addressed to a client the server has already decided
against. A genuine maintenance page says "we'll be back" and has no administrator for the visitor
to contact.

**Fixed** (deployed 2026-09-19, `421cbfe`) with one Tier 2 entry in `blocking.py` — `contact\s+your\s+administrator`,
vendor `unknown`, signal `tier2:contact_administrator` — size-gated like every Tier 2 phrase.
**The title is deliberately not matched**: `test_genuine_maintenance_page_is_not_a_block` pins
that a small "Site Maintenance / We'll be back soon" page stays a genuine result. The captured body
is a verbatim fixture (`MYNTRA_MAINTENANCE_WALL`); 173 worker tests pass (168 → 173), and the three
new positive cases fail against the previous detector. The worker's `error` will read
`blocked:unknown` — the honest vendor for a site's own template.

**What it does not fix.** The detector change makes the failure *honest*; it does not get Myntra's
content. From a Singapore datacenter IP Myntra will keep serving this, and no stealth setting
changes an IP decision — this is the UF-002 / middle-tier problem (an Indian residential exit via
the proxy layer). The one-request Amazon and Flipkart passes the same day were the browser
fingerprint winning; Myntra is the IP losing.

⚠️ **Prod knock-on, owner's call, not done:** every one of the day's Myntra test jobs stored
*something* as `completed` and hashed it — the wall (`106f8e90…`, `5d0710d5…`), a genuine 404
(`cda0aacf…`, the slug URL without `/buy`), and the BUG-016 error-boundary pages (`4ed66910…`,
`b4e2f2c8…`). All one-off jobs, no schedule, no webhook — and a baseline is only *read* by a later
completed run of the same job (`result_consumer.py`, `_get_previous_completed_run`), which for a
one-off job can only come from a `PATCH` adding a `schedule_cron`. **Inert; deleting them is
tidiness, not a fix.** The 2026-07-22 closeout had to null six because those jobs *were* scheduled.

**How the rest of the thread resolved (so nobody re-diagnoses it as a wall).** With a residential
exit (owner's Evomi proxy) Myntra served the real site every time. What still looked broken was
two worker bugs, filed the same day: `networkidle` dying at 30 s under a 90 s budget (**BUG-015**),
and "Oops! Something went wrong" over a healthy header because `block_images` aborts CSS chunks
(**BUG-016**, confirmed by `block_images: false`). Myntra's product data is server-embedded in
`window.__myx.pdpData` (price, sizes, descriptors, media, sellers) and was complete in every
"Oops" body — `output_format: html` keeps it, markdown strips the `<script>` and loses it. Also
site-specific: a Myntra slug URL without `/buy` is a 404; the bare `https://www.myntra.com/{id}`
form works.

⚠️ **Deployment fact this surfaced, recorded in `CLAUDE.md` → Deployment:** the cluster's egress
is a datacenter IP in Singapore, which is also why `amazon.com` redirected the same day's Amazon job
to `amazon.sg`. Any geo-sensitive target behaves accordingly.

---

## BUG-004 — Screenshots are written to MinIO and then dropped on the floor

**Severity:** Medium (unbounded storage leak + quota bypass; no data corruption)
**Discovered:** 2026-07-22 (while wiring BUG-003's block path)
**Status:** Open — **facet 2 (the quota bypass and the leak) closed in code by P8, 2026-09-15**;
**facet 1 (the product gap) is still open and still a product call.** The consumer now reads
`screenshot_paths`: each screenshot is sized with the page, counted against the storage quota with
it (over the wall → all of them deleted, run fails), recorded as a ledger row, and released by the
same delete paths as the page — no separate mechanism, exactly as ADR-009 §8d predicted. Not yet
surfaced on `GET /jobs/{id}/result`; the rows exist, so surfacing is a query once decided.
⚠️ **One leak survives, on the worker side:** the playwright worker omits `screenshot_paths` from
its `failed` messages (bot-wall and exception paths), so screenshots taken before a failure are
still orphaned. Fixing that means the worker includes the field on `failed` and the consumer's
`failed` branch releases them — a contract addition, left for the facet-1 decision.

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

---

## BUG-005 — Batch is broken on all three execution paths (`job_id` is NULL, and the contract assumes it never is)

**Severity:** High (two paths hang forever with no error; the third silently returns the wrong
content and breaks tenant isolation)
**Discovered:** 2026-08-04, reviewing inputs for ADR-009
**Status:** ✅ **FIXED — DEPLOYED 2026-09-19 (`421cbfe`), the queue's single release.** All three
parts of the fix below shipped together, across five services, as `schema_version` 3. Production
runs ADR-011's convention from that release; objects written before it keep their old-format
`result_path` strings (no backfill, by design). **The `latest/` sweep ran the same day**
(`api/scripts/sweep_latest_objects.py --apply`): 36 objects, 21,938,965 bytes, 0 failed; a re-run
found 0. The stream was verified drained before the push (0 messages, 0 outstanding acks on all
four consumers) and `last_seq` did not move during the cutover, so no v2/v3 mismatch occurred.
**Design settled 2026-09-03**: [ADR-011](../adr/ADR-011-artifact-identity-and-paths.md) is Accepted
and answers fix part (2). **P6 had no remaining design dependency** and covered **all three lanes**.
⚠️ **The cutover is a hard cut against a drained stream** — v2 and v3 have incompatible required
fields in *both* directions, so all five services deploy together. Owner's call, 2026-09-04.

### What happens

Batch scraping is broken end to end, in three different ways, all from one root cause. None of
the three produces a user-visible error.

**Path A — batch on the `playwright` engine: every item is dropped at the scrape stage.**
`POST /batch` dispatches `"job_id": None` in the fat message (`routers/batch.py:126`). The
Playwright worker's `JobMessage.job_id` is a **required `str`**
(`playwright-worker/worker/models.py:31`), so `model_validate_json` raises. `handle_message`'s
parse guard logs `malformed_message`, **acks, and returns** (`worker/worker.py:42-46`) — the ack
tells JetStream the message was handled, so it is never redelivered. Every item stays `pending`
forever. Stale-pending recovery cannot rescue them: it resolves `Job` by `run.job_id`, which is
`None`, and skips (`_recover_stale_pending`'s job lookup — *since rewritten by P9, 2026-09-11*). **This is already recorded** — see BUG-001, which
notes in passing that "batch runs stuck in `pending` are not recoverable via this path anyway."
That line is Path A, written down and read as an aside.

**Path B — batch on the `http` engine: it "succeeds" and returns the wrong pages.**
Go is more permissive than Python here. `encoding/json` unmarshals JSON `null` into a plain
`string` field as the zero value `""` **and returns no error**, so the Go worker proceeds with an
empty job id and writes (`internal/storage/minio.go:53,69`):

```
latest/.html                 ← a single global object: every batch item, every user, every batch
history//1759000000.html     ← note the empty path segment; keyed only by second
```

Within one batch the items run concurrently and many complete in the same second, so they write
**the same history key** and overwrite each other. Each item's `job_runs.result_path` then points
at that one object, holding whichever page landed last. The batch reports success and returns
duplicated, wrong content.

Across users the same collision is worse than corruption: two batch items from **different
tenants**, same second, same output format, resolve to the same object, and each user's result
endpoint serves the other's scraped content. That breaks the platform's cross-tenant isolation
invariant through the storage layer rather than the API layer, where all the existing 404 guards
live.

**Path C — batch with `llm_config` (either engine): items hang at `processing` forever.**
On scrape completion the result consumer dispatches the LLM stage with `"job_id": None`
(`result_consumer.py:206`). The LLM worker's `JobMessage.job_id` is likewise a required `str`
(`llm-worker/worker/models.py:6`) → validation error → `malformed_message` → **ack and drop**
(`llm-worker/worker/worker.py:48-53`). The run sits at `processing`; nothing in the system
recovers a run in that state (stale-pending only looks at `pending`). `batch.completed + failed`
never reaches `batch.total`, so the batch never transitions to `completed`/`partial_failure` and
the `batch.completed` webhook never fires. Even had the message parsed, the LLM worker would then
upload with `job_id=None`, reproducing Path B's `history/None/{ts}.json` collision.

### Root cause

`job_runs.job_id` is nullable **by design** — ADR-006 made a run belong to *either* a `jobs` row
*or* a `batch_items` row, precisely so batch items would not have to masquerade as job templates.
That decision was correct and is not in question.

What was not carried through is that **two contracts still assume `job_id` always exists**:

1. **The fat-message and LLM-message schemas** type it as a required string, so a legitimately
   absent job id is indistinguishable from a malformed message — and the malformed-message
   handler's job is to discard.
2. **The MinIO path convention** (ADR-002 §4) keys every artifact on `job_id`:
   `latest/{job_id}.{ext}` and `history/{job_id}/{ts}.{ext}`. With no job id there is no distinct
   path, so all artifacts pile into one.

ADR-006 explicitly states "workers are unchanged" — true of the *routing*, but not true of the
*identity* the message and storage layers depend on.

### Evidence

Both parser behaviours were reproduced directly rather than inferred:

| Check | Result |
|---|---|
| Python (Pydantic v2) `JobMessage` with `job_id: null` | `ValidationError: job_id — Input should be a valid string` |
| Go `encoding/json` into `JobID string` with `"job_id":null` | `err=<nil>`, `JobID=""` → `latest/.html`, `history//1759000000.html` |

Per the JSON spec Go implements, unmarshalling `null` into a non-pointer type is a **no-op**, not
an error — which is exactly how a hard failure on one worker became silent data corruption on
another.

### Why the tests did not catch it

`api/tests/test_batch.py:451` asserts `payload["job_id"] is None` — **the test pins the broken
value as the expected one.** The API-side tests prove the API emits `None`; each worker's tests
prove it parses well-formed messages. No test ever feeds an API-produced message into a worker's
parser, so every suite is green while the wire between them is severed.

This is the more important half of the fix. A contract check — take the payload the API actually
builds, run it through each worker's parser — is cheap and would have caught all three paths at
once. Without it, the same class of gap reopens the next time a message field changes.

### Fix — two parts, plus the test

1. **Make "a run with no job" representable in the message contract.** The Python workers must
   accept it rather than discard it as malformed; the Go worker must *stop silently accepting* a
   missing id, because "quietly defaulted to empty string" is what converted Path A's loud failure
   into Path B's silent corruption. A missing identifier should fail loudly or be explicitly
   optional — never default.
2. **Key the artifact path on something that always exists.** ✅ **Decided — see
   [ADR-011](../adr/ADR-011-artifact-identity-and-paths.md), Accepted 2026-09-03.** Artifacts key on
   **the row that produced them** (`job_runs.id` for job and batch, `crawl_pages.id` for crawl),
   carried in a lane-neutral **`artifact_id`**; objects are named by **producing stage**
   (`history/{artifact_id}/scrape.{fmt}`, `llm.json`); **`latest/` is removed**; and `job_id` leaves
   the wire entirely.
   ⚠️ **This paragraph previously proposed `run_id`, and ADR-011 §1 rejects it by name.** Every run
   has one only if every lane has runs, and the crawl lane does not: `coordinator/dispatcher.py`
   fabricates a `uuid4()` for a lane that creates no `job_runs` row, so keying on it would name
   objects after a row that does not exist — untraceable, and unlinkable from P8's ledger. The same
   `crawl_pages.id` is already in the message twice today, once honestly and once as `job_id`.
   ⚠️ ADR-011 also rejects a **flat** `history/{artifact_id}.{ext}`: `llm-worker` hardcodes
   `ext="json"`, so an `output_format=json` job's extraction would overwrite its own scraped page.
   The stage segment is what prevents it; today only the timestamp does, and only because LLM calls
   are slow.
3. **Add the cross-service contract test**, and delete the assertion that currently pins the bug.
   ⚠️ **This has a precondition ADR-011 §6 names**: there are **ten publish sites building raw
   dicts and zero producer-side schemas**, so the API must get one typed message model *first* — a
   contract test that hand-writes the payload is the same guess the worker fixtures already make,
   one layer up.

Note that fixing only (1) leaves Path B's collisions intact, and fixing only (2) leaves Paths A
and C dropping messages. They ship together or not at all.

### Triage — why this is fixed pre-migration despite §3

Both `result_consumer.py` and the workers' NATS message models are on the migration's deletion
list, so `phase4-backlog.md` §3's rule ("do not fix bugs in code the migration deletes") points at
do-not-fix. Fixed anyway, for the same reason **Q6** was: §3's principle is *don't spend effort on
code that is about to vanish*, and it yields when the bug is live in production and the migration
is not imminent. Q6 dissolved under Temporal and was still fixed pre-migration because it was
actively billing users. This one is not costing money, but it is silently breaking a shipped
feature, and every path fails without telling anyone.

Two things here **survive** the migration regardless and are not throwaway work:

- **The identity decision** (what a run that is not a job is called, and what its artifacts are
  keyed on) is exactly OQ-1's question, and pipelines are the next thing to ask it.
- **The cross-service contract test** outlives the transport it tests.

Mitigating factor on urgency: batch appears unused in production, so the corruption in Path B is
latent rather than realised.

### Interactions

- **ADR-009 / PRD-016 OQ-1 — the strongest evidence available.** Batch was the platform's first
  "a run that is not a job," and it broke in three places because the identity model assumed
  otherwise. Pipelines are the second, larger instance of the same shape: no `job_id`, multiple
  artifacts per run, a second consumer of the quota meters. This bug is what OQ-1 looks like when
  it is answered implicitly instead of decided.
- **ADR-002 §4** — the MinIO path convention was the thing that had to change, and the fix did not
  bypass the ADR: **ADR-011 supersedes it, Accepted 2026-09-03.** ADR-002 keeps its subjects and
  message schemas; only §4 moved.
- **[ADR-011](../adr/ADR-011-artifact-identity-and-paths.md)** — P6's design dependency, now
  closed. It also settles two things that are not obviously part of this bug: `latest/` is deleted
  (which removes BUG-007's fourth symptom by construction), and the crawl lane loses its fabricated
  `run_id` — in scope by explicit confirmation at promotion, even though BUG-008 means nothing on
  v1 reads a crawl result.
- **ADR-006** — not wrong, but its "workers are unchanged" claim needs a footnote: routing was
  unchanged, identity was not.
- **BUG-001** — its "harmless log noise" reading stands for the log spam itself, but the sentence
  about unrecoverable batch runs is Path A's symptom. Worth cross-linking so neither is closed on
  the strength of the other.
- **UF-003 / the transient-vs-terminal work** — orthogonal. Those classifiers fire on exceptions
  during processing; this is a parse failure *before* processing, on a path whose only handler is
  "discard." Worth noting that the malformed-message branch is the one remaining place where a
  worker still acks unconditionally on failure, which is the shape of bug UF-003 spent three
  commits removing everywhere else.
- **BUG-011 — this fix does NOT close it.** ⚠️ Path A's "stuck batch runs are unrecoverable" has two
  halves: messages being dropped (fixed here) and stale-pending recovery not knowing how to rebuild
  a batch dispatch (**not** fixed here). Closing BUG-005 leaves the recovery net holed. Filed
  separately as **P9**, sequenced immediately after this.

---

## BUG-006 — Dependabot scans 3 of 7 dependency manifests; the unscanned majority contains the only reachable instance of a live CVE

**Severity:** Medium (contained DoS vector — but the coverage gap behind it is the larger problem)
**Discovered:** 2026-08-05, from a push-time Dependabot banner
**Status:** Open — **deferred behind BUG-005 and the Temporal migration** (owner's call, recorded
in `phase4-backlog.md` §4)
⚠️ **Read the [addendum of 2026-09-04](#bug-006-addendum-2026-09-04--the-coverage-gap-produced-a-concrete-outage) at the end of this file before acting on this bug.** The gap
stopped being theoretical: it produced a real outage, and it shifts what the fix has to be —
**lockfiles, not scanning coverage.** The addendum is filed at the end rather than inline because
it is dated evidence about this bug, not a revision of it; this pointer exists so the two are never
read apart.

### What happens

Two separate problems that were found together and are best fixed together.

**1. Four of seven dependency manifests are never scanned.** There is no
`.github/dependabot.yml`, so the repository runs on default auto-setup. Every open alert is filed
against one of the three manifests that has a lockfile:

| Manifest | Open alerts |
|---|---|
| `api/uv.lock` | 29 |
| `frontend/package-lock.json` | 21 |
| `http-worker/go.mod` | 1 |

**`coordinator/`, `llm-worker/`, `playwright-worker/` and `mcp/` produce zero alerts** — not
because they are clean, but because nothing looks at them. Each has a `pyproject.toml` carrying
floor-only constraints (`aiohttp>=3.9.0`, `cryptography>=41.0.0`) and **no lockfile**. **Four of
the five Python services in the platform are unmonitored** — every one except `api/`.

⚠️ **Corrected 2026-08-28 (ADR-009 §13 review): this bug undercounted its own scope.** It was
filed as "3 of 6" and omitted **`mcp/`**, which has a `pyproject.toml`, no lockfile, and is the
LLM-callable public surface (`scrape_url`, `get_result`, `list_jobs`). The true figure is **7
manifests, 3 scanned**:

| | scanned | unscanned |
|---|---|---|
| | `api/uv.lock` · `http-worker/go.sum` · `frontend/package-lock.json` | `coordinator/` · `llm-worker/` · `playwright-worker/` · **`mcp/`** |

The undercount is the same shape as the bug itself — a manifest is invisible to the count for
exactly the reason it is invisible to the scanner.

**2. Because of (1), the one *reachable* instance of a live high-severity CVE is invisible, while
the visible alert is for an unreachable copy.**

`CVE-2026-69244` / `GHSA-cq5v-8q36-5273` — an out-of-bounds heap read in aiohttp's **C HTTP
response parser**, on the error path for a malformed **chunked** response. Vulnerable `<= 3.14.2`,
fixed in **3.14.3**.

Reachability turns entirely on *whose* HTTP responses aiohttp parses, and the codebase splits
cleanly:

| Service | What its aiohttp parses | Reachable |
|---|---|---|
| `api`, `llm-worker`, `playwright-worker` | MinIO responses only — aiohttp is `miniopy-async`'s backend. The two workers import `aiohttp` solely to name its exception types in their transient/terminal classifiers | **No** — in-cluster, trusted |
| `coordinator` | `robots.txt` and sitemap XML fetched **from the user-supplied target site** (`coordinator/coordinator/sitemap.py:11`, `:28`, `:45`) | **Yes** |

Everything else that contacts an untrusted host already uses **httpx**: webhook delivery
(`webhook_loop.py`), the Playwright worker's robots.txt fetch (`playwright-worker/worker/robots.py`),
LLM calls and the warm-up probe (`llm-worker/worker/llm.py`), and Clerk JWKS (`auth/jwt.py`). The
coordinator's sitemap discovery is **the only place aiohttp faces a server we do not control** — and
it is in the one service Dependabot cannot see.

The alert that exists (`#94`, against `api/uv.lock`, aiohttp `3.13.3`) is for a copy that only ever
talks to MinIO.

**Compounding the gap:** `coordinator/Dockerfile` builds with `pip install --no-cache-dir .`
against an unbounded floor. The aiohttp version actually running in production is whatever PyPI
resolved at image-build time — unpinned, non-reproducible, and not determinable without inspecting
the image. The same is true of both Python workers.

### Root cause

Two independent omissions that mask each other:

1. **No `dependabot.yml`.** Default setup discovers a subset of manifests; nothing enumerates the
   monorepo's **seven** dependency roots.
2. **No lockfiles outside `api/`, `http-worker/` and `frontend/`.** Without a lock, there is no
   resolved version for a scanner to compare against an advisory, and no reproducibility for the
   build either. The floor-only constraints were adequate when these services were new and are
   not now.

### Severity assessment — deliberately not inflated

The reachable defect is an out-of-bounds **read**, not a write. The realistic outcome is a crash of
the coordinator pod, or a small heap disclosure surfacing inside a parse error — not remote code
execution. Triggering it requires a crawl aimed at a server the attacker controls, which is
ScrapeFlow's advertised function, so the practical bar is *"has an account."*

Blast radius is contained: the coordinator is its own pod, k8s restarts it, and because the BFS
frontier is persisted in the `crawl_queue` Postgres table rather than held in memory, in-progress
crawls survive the restart. That is ADR-005's placement decision paying off in a scenario it was
not written for.

The **coverage gap** is the more serious half, and it is not scoped to this CVE: three services
have never been scanned, so the true count of unaddressed advisories in this repository is unknown.

### The second high alert (`#95` — not reachable, recorded for completeness)

`CVE-2026-69247` / `GHSA-g6cj-pr64-35w5` — `cryptography` PKCS#7 EnvelopedData decryption exposes a
Bleichenbacher oracle. Vulnerable `>= 44.0.0, < 50.0.0`; we run **48.0.1**; fixed in **50.0.0**.

**Not reachable.** The only symbols any service imports from `cryptography` are `Fernet` and
`InvalidToken`. Fernet is AES-CBC + HMAC-SHA256; PKCS#7 EnvelopedData is RSA key transport. There is
no `pkcs7` reference anywhere in the repository, and `clerk-backend-api` uses the library for JWKS
signature *verification*, not enveloped decryption.

Worth noting for whoever does the bump: **clerk-backend-api 6.0.1 declares `cryptography` with no
upper bound**, so 48 → 50 will not repeat BUG-002's problem, where clerk 5.x pinned
`cryptography<47` and made the CVE fix unreachable without a major version bump.

### Fix — four steps, in this order

1. **Add `.github/dependabot.yml` enumerating all six manifest directories** (`api`, `frontend`,
   `http-worker`, `coordinator`, `llm-worker`, `playwright-worker`). This is the actual fix; the
   two CVEs are the symptom that exposed it.
2. **Generate lockfiles** for `coordinator`, `llm-worker` and `playwright-worker`. Without one,
   Dependabot has no resolved version to compare and the images stay non-reproducible regardless of
   step 1.
3. **aiohttp → `>= 3.14.3`**, coordinator first — it is the only reachable instance.
4. **cryptography → `50.0.0`** whenever convenient. Not urgent; not reachable.

**Expect the alert count to rise before it falls.** Steps 1 and 2 will surface advisories against
three services that have never been scanned. That is the point, but it should not be mistaken for a
regression.

### Sequencing

**Deferred behind BUG-005 and the Temporal migration** — owner's decision, 2026-08-05. Neither CVE
is being exploited, the reachable one needs an authenticated user pointing a crawl at their own
server, and the coverage work is bounded and self-contained whenever it is picked up.

### Interactions

- **⚠️ Do not close this as dissolved by the migration.** The `coordinator/` service *is* deleted
  (backlog §3), so a reader may reasonably assume the reachable instance disappears with it. **It
  does not.** Sitemap and robots.txt discovery is *business logic* that ports into a `CrawlWorkflow`
  activity — the fetch still happens, still targets a user-supplied host, and inherits whichever
  HTTP client the activity uses. Same shape as the Q5 `ensure_ready()` carry-forward: plumbing goes,
  behaviour stays.
- **✅ The port must use `httpx`, not `aiohttp` — now recorded in ADR-009 §13** (added by the §10
  review, 2026-08-26; it landed in §13 rather than §10 because it belongs to the crawl migration
  step). Every other untrusted-target fetch in the platform already uses httpx —
  `playwright-worker/worker/robots.py:10` is the direct sibling. Converging on one client for
  untrusted responses removes this exposure as a side effect of the migration rather than as
  separate work, and leaves `aiohttp` used only where `miniopy-async` requires it — against MinIO,
  which we control. ⚠️ Note what makes this one fragile: it is the **only** item on ADR-009 §10's
  do-not-delete list that must be **modified** rather than copied across, so **a faithful port is
  the failure mode here, not the success one.**
- **BUG-002** — same ecosystem, different problem. BUG-002 was "these known alerts need version
  bumps." This is "the scanner has a hole in it," and no amount of triaging visible alerts finds
  it. The remaining-alerts row in `phase4-backlog.md` §4 was written from a count that only ever
  covered half the repository.
- **BUG-005** — unrelated in mechanism, related in shape: both are failures that every existing
  check reported as green, because the check did not cover the thing that was broken.

---

## BUG-007 — LLM jobs charge storage for the wrong object, leak the scraped page, and inflate the counter on delete

**Severity:** Medium (billing accuracy + unbounded storage leak; no data corruption, no cross-tenant exposure)
**Discovered:** 2026-08-17 (tracing ADR-009 §8's metering-parity claim against live code)
**Status:** ✅ **FIXED — DEPLOYED 2026-09-19 (`421cbfe`) and production RECONCILED the same day.** The shared per-object storage
ledger (`storage_objects`, `api/app/core/ledger.py`) is the fix vehicle this bug was sequenced
behind, and building it closes all three `history/` symptoms at once: the consumer records every
stored object as a row (`test_llm_job_charges_both_objects` pins 2000 + 50, not 2000), the delete
paths enumerate rows instead of statting `result_path` (`test_permanent_delete_releases_every_ledger_row`
pins both objects removed and the counter back to zero), and the counter is a materialised sum
that only the ledger moves. ⚠️ **The deploy alone did not repair production** — a pre-ledger object has no row — so
`api/scripts/reconcile_storage_ledger.py --apply` was run right after it (2026-09-19): 46 objects
scanned, **35 recorded** (21,457,872 bytes), **11 orphans deleted** (5,336,209 bytes), 0 dangling,
0 failed; a second run found nothing to do. Two findings from the run: **production's counter was
never inflated** — `storage_bytes_used` already equalled the attributable total to the byte, so
`counters_changed=0`; and the 11 orphans were not this bug's leaked pages but **Q6's** 2026-07-03
re-scrape residue (10 uploads under one BrowserScan job whose only run is `failed`, plus one
redelivered re-scrape discarded by the terminal-status guard) — the same class, handled the same
way. Bucket, ledger and meter now agree: 35 objects / 35 rows / 21,457,872 bytes. **The delete
paths' legacy branch (`_release_legacy_result`) has nothing left to serve in production** and can
be removed when the code is next touched. The pre-fix text below is kept as the record of what
the bug was.
⚠️ **Its fourth symptom was deleted upstream:** ADR-011 removed `latest/` in **P6**, so the two
orphaned `latest/` keys stopped existing before this bug was reached. What P8 fixed is the
`history/` half — the wrong object charged, and the scraped page never deleted.

### What happens

An LLM job stores **two** objects and charges for the wrong one. On delete it subtracts the wrong
one again, in the opposite direction, so the user's storage counter never returns to zero and the
scraped page is never removed at all.

| step | code | effect |
|---|---|---|
| scrape completes | `result_consumer.py:411` | adds **HTML** size to `storage_bytes_used`, sets `storage_accounted_at` |
| LLM completes | `result_consumer.py:485` → `:81` | tries to add **JSON** size — **skipped**, stamp already set |
| run finalised | `result_consumer.py:500` | `result_path` repointed at the **JSON** |
| what the worker wrote | `llm-worker/worker/storage.py:23` | JSON to a **new** key; the HTML object is untouched |
| job hard-deleted | `cancel_job(permanent=True)` in `routers/jobs.py`, `admin.py:336` | enumerates `JobRun.result_path` → stats the **JSON** → decrements by **that** |

So: charged for a 291 KiB–4.1 MiB page, credited back a few KiB of JSON.

### Root cause

**`_try_increment_storage`'s idempotency stamp is keyed on the run when it should be keyed on the
stored object** (`result_consumer.py:75-90`). The short-circuit itself is correct and necessary —
it makes NATS redelivery idempotent. But at run granularity it cannot tell a *redelivered result*
from a *genuinely second artifact*, and an LLM run produces exactly the latter. Same pattern on the
batch path (`:237` scrape, `:274` LLM).

This is why the bug reads as three separate problems: one wrong granularity, three symptoms.

### Symptoms

1. **The counter is permanently inflated by every deleted LLM job.** `decrement_storage_bytes`
   clamps at zero (`quota.py:205`), so it never goes negative — it also never balances. A user who
   deletes every job they own still sees non-zero usage, against a 5 GB wall.
2. **The scraped page is never deleted.** Nothing enumerates it: not job delete, not admin
   user-delete, not `cleanup_old_runs.py`. It outlives the run, the job and the user.
3. **The charge is on the object the user did not ask for.** The user requested structured
   extraction; they are billed for the raw HTML and shown the JSON.

### Why this is not covered by backlog §3

Neither `result_consumer.py`'s accounting nor the two delete paths is "dissolved by Temporal" in
the way §3 means. `core/quota.py`, `routers/jobs.py` and `routers/admin.py` all **survive** the
migration — only the *call site* inside `result_consumer.py` moves into an activity. The wrong
granularity would be ported along with it. Same carry-forward shape as the Q5 `ensure_ready()`
case: the plumbing goes, the mistake stays unless it is fixed first.

### Interactions

- **ADR-009 §8 (reviewed 2026-08-17)** decided storage is charged for **bytes actually stored**,
  which makes all three symptoms defects by definition rather than judgement calls. §8a records the
  trace; this bug is the tracker entry it points at.
- **✅ Unblocked 2026-08-25 — the dual-write question is decided: charge one copy.** Every
  `history/` object is charged, once; **`latest/` is never charged**, and is deleted with the
  artifact it mirrors. ADR-009 §8a carries the call, and **the charging rule stands unchanged.**
  ⚠️ **Corrected 2026-09-03 — the second half of that call is reversed.** This bullet used to add
  *"`latest/` is kept as-is (v2 drops it anyway, so the 2× discrepancy is v1-only with a known end
  date)."* [ADR-011](../adr/ADR-011-artifact-identity-and-paths.md) §4 (Accepted 2026-09-03)
  **removes `latest/` on v1**, inside **P6** — because P6 rewrites the path convention anyway, so
  keeping it would mean carrying a dead concept into a brand-new convention and through P8's ledger
  as an *uncharged mirror* class, then deleting it. **The 2× discrepancy therefore ends at P6, not
  at the v2 cutover.**
- **The same pass found a fourth symptom, in the deletion path.** An LLM job leaves **four**
  objects, not two: the scrape writes the job's own format and the LLM always writes `.json`, so
  `latest/{job}.{fmt}` and `latest/{job}.json` are **different keys** and neither overwrites the
  other. Hard delete derives **one** filename from `job.output_format` (`cancel_job(permanent=True)` in `routers/jobs.py`), so
  it removes whichever `latest/` copy matches the declared format and **orphans the other** — and
  *which* one survives depends on the format (an `output_format=json` job has only three objects,
  because the LLM's write lands on the scrape's key). The assumption underneath is **one artifact in
  one format per job**: the same per-run granularity error as the counting stamp, expressed in the
  deletion path. Add to the fix.
- **⚠️ Now depends on P8, and should not be fixed before it.** The fix *is* per-object accounting,
  which is P8's shared ledger (ADR-009 §8d). Patching the stamp in place — a second column, or a
  special case for the LLM stage — reproduces the per-run granularity error one artifact later and
  does nothing for pipelines, where the object count per run is unknown at schema-design time. With
  the ledger in place both halves become mechanical: **count** each object as it is stored, and
  **delete by enumerating rows** rather than deriving a filename.
- **BUG-004** — the same missing idea from the other end. Screenshots are stored, never counted and
  never deleted; here the *scraped page* is. Both are "an object exists that no accounting path
  knows about," and §8's rule makes them one problem. **Under P8 they stop being two fixes:** a
  screenshot is a stored object, so it is a ledger row, so it is charged and deletable by the same
  path with no separate mechanism.
- **P7 / crawl quota** — same family again: a whole lane storing objects nothing counts. Both are
  now **consumers of P8**, so the sequence is **P6 → P9 → P8 → P7 + BUG-007 together** rather than
  P6 → P7 with this trailing behind.

---

## BUG-008 — The crawl coordinator's result consumer has never existed; crawls cannot complete

**Severity:** High (the feature cannot work at all — pages dispatch and never complete, with no
error anywhere) — **but entirely latent: no crawl has ever been run in production**
**Discovered:** 2026-08-23 (tracing ADR-009 §9's option-(a) bridge against the live NATS stream)
**Status:** Open — **WILL NOT FIX on the v1/NATS path.** See *Disposition*.

### What happens

`coordinator/result_handler.py:203` subscribes to `scrapeflow.jobs.result` with the durable name
`coordinator-result-consumer`, so the coordinator can pick up crawl page results and advance the
crawl. **That consumer has never existed.** On the live cluster:

```
$ nats consumer ls SCRAPEFLOW
  api-result-consumer
  go-worker
  python-llm-worker
  python-playwright-worker
```

So the coordinator dispatches crawl pages and then never learns any of them finished.

### Why it fails

The `SCRAPEFLOW` stream is created `--retention work` — a work queue — with subjects
`scrapeflow.jobs.>` (`docker/docker-compose.yml:81`, and identically in the infra repo at
`clusters/k3s-server/scrapeflow/app/nats-init-job.yaml:20`). **A work-queue stream refuses a
second consumer whose filter subject overlaps an existing one**, and `api-result-consumer` already
claims `scrapeflow.jobs.result` in full, unfiltered. The coordinator's `pull_subscribe` therefore
raises.

The ack-and-skip pattern in both services — the API skipping messages where `crawl_context` is
set (`result_consumer.py:603`), the coordinator skipping those where it is null
(`result_handler.py:225`) — was written for a stream where **both consumers receive every
message**. That is interest or limits retention, not work-queue. The routing design and the stream
configuration disagree, and the stream wins.

### Why nobody noticed

Two independent silencers:

1. **The subscribe is outside the loop that would have caught it.** `result_handler_loop` does
   `pull_subscribe` at line 203, logs `result_handler_subscribed` at 204, and only *then* enters
   `while True:` with its `except Exception` handler. So the failure happens before any of the
   loop's own error handling exists.
2. **The exception is swallowed at the top level.** `main.py:82` runs
   `await asyncio.gather(dispatch_task, handler_task, return_exceptions=True)`. That captures the
   dead task's exception and never re-raises, so the process stays up serving the surviving task.

The observable result is a pod that is `1/1 Running` and looks healthy with half of itself dead.
Its own logs show the asymmetry plainly — `dispatch_loop_started` appears, and
`result_handler_subscribed` appears **zero** times in the entire retained log.

**Note the API is not affected by the same pattern.** Its `return_exceptions=True`
(`api/app/main.py:103`) is at *shutdown*, after explicitly cancelling the tasks, which is the
correct use. And its four background loops each wrap their work in `while True: try: … except
Exception: log and continue`, so a runtime error does not kill them. The coordinator is the only
service that puts failure-prone setup outside its protective loop.

### Blast radius today: zero

`crawls` and `crawl_pages` are both **empty** in production — no crawl has ever been run. Same
shape as BUG-005: a shipped feature that has never worked, and has never been exercised.

If a crawl *were* started, it would: dispatch its seed page, scrape it successfully, store the
result in MinIO, and then stop. The queue item stays `dispatched` forever, the crawl never
completes, no webhook fires, and no error is recorded. `reenqueue_stalled`
(`dispatcher.py:27`) runs **only at coordinator startup**, so each restart would delete the stalled
`CrawlPage` rows, reset the items to `pending`, and re-scrape the same pages — repeated waste on
every restart rather than a tight loop.

### Disposition: not fixed here; dissolved by the Temporal migration

**Owner's decision, 2026-08-23: this is not fixed on the NATS path.** It belongs in
`phase4-backlog.md` §3 — the set of bugs the migration deletes rather than repairs.

The reasoning is that the fix and the migration are the same work. The bug *is* the NATS
integration: a second consumer that a work-queue stream will not allow. ADR-009
[§13](../adr/ADR-009-workflow-engine-temporal.md) deletes `coordinator/` outright, and crawl result
handling becomes workflow logic — a workflow observing its child activities' return values, with no
queue, no second consumer, and no subject to contend for. **There is no version of this defect that
survives into the end state**, so repairing it on the NATS path means writing code with a known
expiry date, in the component with the shortest remaining life in the system.

**⚠️ The deferral rests on one condition, and it should be stated rather than assumed: crawls stay
unused until they migrate.** ADR-009 §13 migrates the crawl coordinator **last**, so this stays
broken for the entire migration — the longest possible deferral. That is acceptable only while the
usage is zero. If crawls are ever offered to users before the crawl migration lands, this becomes a
live High and the cheap mitigation is to reject `POST /crawls` rather than to repair the consumer.

### Contrast with BUG-006 — why *that* one is not dissolved and this one is

BUG-006 carries an explicit warning not to close it as dissolved by the migration: `coordinator/`
is deleted, but sitemap and robots.txt discovery is *business logic* that ports into a
`CrawlWorkflow` activity and takes its `aiohttp` exposure with it. **This bug is the opposite
case.** What is broken here is not logic that ports — it is the transport itself. Crawl result
handling does port, but it ports as a workflow awaiting activity results, which is a mechanism in
which "a second consumer on a work-queue subject" has no counterpart. The distinction worth
carrying: **ask whether the broken thing is the behaviour or the plumbing.** Behaviour ports and
takes its bugs along; plumbing is replaced.

### Interactions

- **ADR-009 §9 (reviewed 2026-08-23)** — this bug is the load-bearing evidence for rejecting the
  option-(a) NATS bridge. The bridge needed exactly this addition: a second consumer on
  `scrapeflow.jobs.result` so a Temporal activity could hear its result. The coordinator proves
  that is not merely awkward but impossible on the current stream, and proves the failure is
  silent when attempted.
- **BUG-005** — same family: shipped, silently broken on every path, never exercised, and found by
  reading rather than by an alert.
- **P7 / crawl quota** — ✅ built 2026-09-18 on a lane that has never successfully completed a
  page. Consequence, not a bug in P7: **every v1 crawl stays `running` forever and therefore holds
  a concurrency slot until the user cancels it** — the meter is telling the truth about a lane that
  never finishes. `scripts/audit_crawl_quota.py` lists them. The per-page storage insert is *not*
  on v1 for the same reason (its only site is the handler this bug is about); it lands in the
  `CrawlWorkflow` port.
- **BUG-010 / ADR-009 §13a (2026-08-28)** — the blast radius of *this* bug is wider than "one
  consumer is missing". Traced through for the §13 review: because nothing reads results,
  `_process_crawl_result`, `_enqueue_url`, `_fetch_minio_bytes`, **link extraction and sitemap
  discovery have all never run either**. Only `dispatcher.py` plus `check_completion` /
  `enqueue_crawl_webhook` (which the dispatcher calls directly) have executed in production, so
  **a crawl has never got past dispatching its seed page**. Two consequences: the crawl migration
  is a **rewrite, not a port**, with no v1 run to diff against; and **BUG-010** — the unvalidated
  SSRF path in sitemap discovery — is latent *because this component is dead*, so fixing or
  replacing it is what exposes it.

---

## BUG-009 — `JobNotifier` never reconnects; one dropped connection silently deafens every WebSocket in the process

**Severity:** Medium (live, silent; a stale dashboard rather than data loss — but with no signal
that anything is wrong, and it gets worse under Phase 4's longer runs)
**Discovered:** 2026-08-26 (reviewing ADR-009 §11, which preserves this mechanism and leans on it
harder)
**Status:** Open — pre-migration fix on live code. Not dissolved by Temporal: ADR-009 §11 keeps
this component and adds a second channel to it.

### What happens

`JobNotifier` is the fan-out between Postgres and the browser. It opens **one** dedicated asyncpg
connection at API startup (`main.py:54` → `job_notifier.py:36`), registers two `LISTEN` channels on
it (`job_status`, `batch_status`), and holds it for the process lifetime. That connection is
deliberately kept out of the SQLAlchemy pool, because a `LISTEN` connection must keep its
subscriptions.

**There is no termination handler and no reconnect path.** `start()` is called once at startup;
`stop()` closes the connection at shutdown. Nothing observes the connection dying in between.

So if that single connection drops for any reason — a Postgres restart or minor-version upgrade, a
failover, an idle cut by a pooler or network device, a transient network fault — then:

1. Both channel subscriptions are gone and are never re-registered.
2. `pg_notify` still fires correctly from every writer; Postgres simply has no listener to deliver
   to in that process.
3. Every open WebSocket in that API process stops receiving updates. The socket itself stays open,
   so the browser sees a healthy connection.
4. Each watcher sits until its 300-second timeout (`jobs.py:745`), receives `{"type": "timeout"}`,
   and the socket closes.
5. The frontend does nothing with that (`JobDetail.tsx:81` — `ws.onclose = () => setWsLive(false)`),
   and the react-query cache is invalidated only by a *terminal* WebSocket message, which never
   arrived. **The page shows a stale status until the user manually refreshes.**

**Nothing logs any of this.** There is no error, no warning, and no metric — the notifier simply
stops receiving. The only symptom is "the dashboard stopped updating", reported by a human.

### Why it has stayed hidden

- The API pod restarts often enough (deploys, rollouts) that a broken notifier is usually
  short-lived, and a restart silently repairs it.
- A job run takes about 40 seconds, so a watcher's exposure window is tiny — the run almost always
  finishes and closes the socket cleanly before anything drifts.
- The failure looks exactly like "the job is still running", which is the normal state.

### Why Phase 4 raises the stakes

ADR-009 §11 decided to **preserve this mechanism** rather than stream engine events to the browser,
and to add a **second notify channel** for pipelines on the same single connection. Two
consequences:

- More surface behind one connection — the pipeline channel dies with the job and batch channels.
- **Much longer-lived watchers.** §15 gives a Webhook block a delivery horizon of ≈2.6 hours, and
  Monitors (layer B) introduce durable sleeps measured in days. A watcher that used to be exposed
  for 40 seconds is exposed for hours, which is when a connection blip actually gets a chance to
  happen.

### Interaction with §11's reconnect decision — this bug is *not* fixed by it

ADR-009 §11b requires the **client** to reconnect on any non-terminal close, which does mask this
bug: a browser that reconnects every 5 minutes re-reads the row and self-heals. **That is a
mitigation, not the fix**, and the two must not be confused:

- The client reconnect repairs *the browser's view*. The server-side listener stays dead, so the
  API process degrades from push to a 5-minute poll, permanently, with no operator signal.
- §11b explicitly **rejected a server keep-alive** partly *because* it would make this bug
  invisible. Fixing the client and not the server reaches a similar place by a different road.

### Fix sketch

Not a design decision, so no ADR is owed — but three things belong in it: detect the drop
(asyncpg exposes a termination callback), re-establish the connection **and re-register every
channel** on a backoff, and **log loudly** when it happens, because today the failure is
indistinguishable from silence. Worth checking whether any update can be lost in the gap between
the drop and the re-`LISTEN` — `pg_notify` has no replay, so an update that fires while
unsubscribed is gone; that is another argument for the client-side re-read in §11b.

### Interactions

- **ADR-009 §11** — filed by that review. §11 preserves this component, adds a channel to it, and
  its 11b reconnect decision cites this bug as a reason not to use a server keep-alive.
- **BUG-001 / BUG-005 / BUG-008** — same family: shipped, silently broken or silently degradable,
  found by reading rather than by an alert. The recurring shape is a failure with no log line.

---

## BUG-010 — URLs discovered *during* a crawl are never SSRF-checked, and no worker checks either

**Severity:** High by shape, latent by accident — a crawled site can choose URLs the platform
fetches from inside the cluster, and the response body is served back to the tenant. It has never
fired only because the code path that reaches it is dead (**BUG-008**), and **the Temporal
migration is what switches it on**.
**Discovered:** 2026-08-28 (reviewing ADR-009 §13, which ports this file into a `CrawlWorkflow`)
**Status:** Open. Part 1 is decided and lands with the crawl migration (ADR-009 §13d); part 2 is a
separate cross-lane hardening item, not yet scheduled.

### What happens

The platform's rule is **check the URL once, at the front door.** `validate_no_ssrf` runs exactly
twice, both inside the crawl-creation request, on the seed URL and the webhook URL
(`api/app/routers/crawls.py:34-36`). After that:

- the coordinator validates nothing — there is no SSRF check anywhere in `coordinator/`;
- **no worker validates anything** — no SSRF check, IP-range test or `getaddrinfo` call exists in
  `http-worker/`, `playwright-worker/` or `llm-worker/`. A worker fetches whatever URL its message
  names.

Two routes discover new URLs mid-crawl, and only one of them is guarded:

| Route | Guard |
|---|---|
| Links extracted from a fetched page | `link_extractor.py:33` discards anything not on the seed's own origin — **guarded, by accident of a different requirement** |
| Sitemap entries | `sitemap.py:39` takes them **verbatim from the target site's `robots.txt`**, `:45` fetches them, and `result_handler.py:180-185` enqueues them into `crawl_queue` with **no origin filter** (the include/exclude path filters apply only to extracted links) — **unguarded** |

So the crawled site chooses. End to end:

```
user submits   https://evil.example/            → SSRF-checked, public, allowed
evil.example/robots.txt says
    Sitemap: http://169.254.169.254/latest/meta-data/...
coordinator fetches it                           → unchecked   sitemap.py:45
coordinator enqueues it                          → unchecked   result_handler.py:183
a worker scrapes it, uploads the body to MinIO   → unchecked   no worker validates
user reads it via GET /crawls/{id}/pages         → the response comes back out
```

The final step is what makes this a **read** primitive rather than a blind fetch: the response is
persisted to the tenant's bucket and served through the normal API.

### Why it has never fired

`discover_sitemap_urls` is reachable only from `_process_crawl_result`, which runs only in
`result_handler_loop` — the consumer that **has never existed in production** (BUG-008). No crawl
has ever got past dispatching its seed page, so sitemap discovery has never executed.

This is the inverse of the usual latent-bug note. The code is not latent because nobody hit the
input; it is latent because the component is dead, **and reviving it is exactly what the crawl
migration does.** Anything that fixes BUG-008 — or the `CrawlWorkflow` that replaces it — turns
this on.

### Fix — two parts

**Part 1 — SSRF-check at frontier admission (decided; ADR-009 §13d).** The check belongs where a
URL joins the queue, which is the one point all three discovery routes converge on (seed, extracted
link, sitemap entry). A rejected URL is **skipped and the crawl continues** — not a crawl failure,
because the user did not choose that URL and cannot fix it. The refusal is **terminal and never
retried**, matching ADR-009 §10's rule for webhook SSRF refusals: retrying means re-resolving a
hostname an attacker is actively rebinding.

⚠️ Still open: whether sitemap entries are also restricted to the seed's origin the way extracted
links are. The SSRF check stops the internal-address case; it does not stop a `robots.txt` pointing
the crawl at an unrelated *public* site, which is a quota and attribution question rather than a
security one. Legitimate sitemaps do occasionally cross subdomains, so this is a real trade-off.

**Part 2 — a worker-side check (recommended, not scheduled).** The correct security position is to
validate at the point of use: the worker is what opens the socket, and a check there covers every
lane, including ones not yet built. It is **not** a substitute for part 1 — a worker cannot tell
*"the user typed a bad URL"* (should 400 at creation) from *"a crawl discovered one"* (should skip
a page and continue). Three things make it larger than it looks:

1. **Two implementations in two languages** that must not drift — the risk
   `playwright-worker/worker/robots.py`'s *"Mirrors the Go worker's internal/robots package"*
   already carries.
2. **DNS rebinding.** Resolving to check and then resolving again to connect leaves a window a
   naive check narrows but does not close. Closing it means resolving once and connecting to that
   IP with the `Host` header set — a real change to how the HTTP client is used, twice. This is why
   webhook delivery re-validates on every attempt rather than trusting creation time.
3. **A new terminal failure class**, which must be wired into each worker's transient/terminal
   classifier at the same time, or Temporal will retry a policy refusal for the full retry horizon.

### Interactions

- **ADR-009 §13d** — decided part 1 and recorded part 2 as out of scope for the crawl migration.
- **BUG-008** — the reason this is latent. Fixing or replacing that component exposes this.
- **BUG-006** — same file, different problem. That one is the aiohttp dependency (`sitemap.py`
  must port to `httpx`); this one is what the file *does*. Both must be handled in the same port,
  and neither substitutes for the other.
- **BUG-005 / BUG-009** — same family: shipped, silently broken, found by reading.

---

## BUG-011 — Stale-pending recovery silently skips every batch run, under a comment saying the case is impossible

**Severity:** Low — latent by construction once P6 shipped in code (2026-09-04). Filed rather
than folded into BUG-005 because the net it holes is the platform's **only** recovery path for a
lost dispatch, and the code hid the gap rather than recording it.
**Discovered:** 2026-09-03, tracing BUG-005's fix scope against [ADR-011](../adr/ADR-011-artifact-identity-and-paths.md)
**Status:** ✅ **FIXED — DEPLOYED 2026-09-19 (`421cbfe`).** Built 2026-09-11 (`ed4d63c`). All three parts below
shipped, plus one thing the fix surfaced: the job lane had the same duplication the batch lane
would have gained — `create_job` built its message inline while the scheduler used a helper, so
"recovery re-sends the run that was lost" was only *enforced* for scheduled runs. Both lanes' builders
now live in **`api/app/core/dispatch.py`** (`build_scrape_message`, `build_batch_scrape_message`)
and all five dispatch sites call them; two tests pin dispatch-vs-recovery **byte equality** per lane.
Ships with the pre-migration queue's single release (`phase4-backlog.md` §1).
⚠️ **Knock-on, not a fix: BUG-001's symptom is gone by construction.** A batch run now branches on
`batch_item_id` before `db.get(Job, …)` is reached, so `WHERE jobs.id IS NULL` never fires. BUG-001
stays closed-as-dissolved; see its note. ⚠️ **This is job-vs-batch routing, not v1-vs-v2** —
ADR-009 §7 mechanism 4 (the lane marker owed at migration step 2) is untouched.

### What happens

`_recover_stale_pending` (`api/app/core/scheduler.py`) is the only guard against
**crash-after-commit-before-publish**: a `job_runs` row is committed as `pending`, the NATS publish
then fails or the process dies, and nothing would ever run it. Every scheduler tick it re-publishes
runs that have sat in `pending` past the threshold.

Its `SELECT` filters on `status` and `created_at` only — **no lane filter** — so batch runs are
selected along with everything else. Then:

```python
job = await db.get(Job, run.job_id)
if job is None:
    continue  # orphaned run — should not happen with CASCADE deletes
```

`run.job_id` is `None` for a batch run **by design** (ADR-006), so `db.get` returns `None` and the
run is skipped — every tick, for every batch run, with **no log line**, under a comment asserting
the case cannot occur.

Recovery therefore covers the job and scheduled lanes and silently excludes batch. Crawls are
excluded too, but honestly: they create no `job_runs` rows at all, so the query never sees them.

### Why this is not part of BUG-005

BUG-005 Path A mentions it in passing — stuck batch items "cannot be rescued via this path" — but
as a *symptom* of messages being dropped, not as a defect in its own right. **ADR-011 removes the
cause and leaves this untouched.** Once messages stop being discarded as malformed, batch items
stop getting stuck, so nothing needs recovering. The hole in the net remains exactly as it is,
because it is not a parsing problem — it is the recovery function having only one payload shape.

Latent is not the same as fixed. It goes live the first time a batch dispatch is genuinely lost,
which is the precise scenario the function exists for.

### The fix is small — everything it needs is already persisted

Checked before filing, because "recovery needs data the batch lane never stored" would have made
this a data-model change:

| field | source for a batch run |
|---|---|
| `url` | `batch_items.url` |
| `output_format`, `engine`, `respect_robots`, `llm_config` | `batches` — all four are columns |
| `credentials`, `actions`, `playwright_options` | not applicable — `POST /batch` dispatches these as `None` |

So recovery resolves the parent by **whichever FK is set** and builds the payload from
`job_runs → batch_items → batches` instead of `job_runs → jobs`. That is the same
route-by-which-FK-is-set shape the result consumer already uses.

Three things ship with it:

1. The batch payload shape in `_recover_stale_pending`.
2. **The misleading comment goes.** `job is None` genuinely does mean "orphaned" for a job-lane run
   and must keep skipping; the batch case is a different branch, not the same one.
3. A log line on the branch that still skips. A silent `continue` inside the only recovery path is
   how this stayed invisible.

✅ **As shipped (`ed4d63c`):** the loop resolves the message per lane — `run → jobs` or
`run → batch_items → batches` — then publishes through one shared tail. All four skip sites
(orphaned job, orphaned item, orphaned batch, neither FK) log at warning with the `run_id`; each is
unreachable by schema (CASCADE / the mutual-exclusion CHECK) and the comment now says which. The
`SAWarning` this bug emitted in `api/tests/test_scheduler.py` — *"fully NULL primary key identity
cannot load any object"* — is gone from the suite. Mutation-checked: with the batch branch reverted
to a bare `continue`, both batch-lane tests fail.

### Sequencing

**Immediately after P6.** Not before: ADR-011 changes the dispatch payload (`job_id` leaves the
wire, `artifact_id` arrives), so writing a second payload builder first means writing it twice.

⚠️ **P6 already edits this exact function** — `_recover_stale_pending` builds a payload containing
`job_id` and must change regardless. Doing both in one pass is materially cheaper than two visits,
even though they are tracked separately. *(That saving was spent — P6 landed 2026-09-04 without
this, and P9 was a fresh visit on 2026-09-11.)*

### Interactions

- **BUG-005** — the parent finding. Its Path A is this gap's symptom; the gap itself survives its fix.
- **BUG-001** — same function, different defect. That one is the `WHERE jobs.id IS NULL` query
  noise; this is the silent skip that follows it. Neither closes the other.
- **ADR-006** — nullable `job_id` is correct and not in question. This is the third place that
  assumed it never happens, after the message schemas and the artifact-path convention.
- **Dissolved by Temporal?** Partly — `_recover_stale_pending` exists because NATS dispatch can be
  lost, and a Temporal workflow does not lose its own steps. But it is fixed pre-migration for the
  same reason BUG-005 is: it is on a live, shipped path with the migration months out, and P6 is
  already in the file.

---

## BUG-012 — `reenqueue_stalled` deletes `crawl_pages` while `crawl_queue` still references them; the coordinator cannot start

**Severity:** High (the coordinator crash-loops on boot and never reaches its loops — a total
crawl-lane outage, not a degradation)
**Discovered:** 2026-09-04, restarting the rebuilt workers after P6. Not caused by P6 — the
restart is only what ran the startup path against a database that had stalled items in it.
**Status:** Open — **owner deferred on 2026-09-04, deliberately not fixed** (triage below).

### What happens

`reenqueue_stalled` (`coordinator/coordinator/dispatcher.py`) is **crash recovery**: it resets
queue items left `dispatched` by a previous unclean stop back to `pending`. It runs from
`coordinator/coordinator/main.py` **before either loop starts**:

```python
# Crash recovery: reset stalled dispatched items before loops start.
async with AsyncSessionLocal() as db:
    await reenqueue_stalled(db)
```

It does three things in this order:

1. `SELECT crawl_queue.crawl_page_id` for stale dispatched items — collects `page_ids`.
2. `DELETE FROM crawl_pages WHERE id IN (page_ids)`.
3. `UPDATE crawl_queue SET status='pending', crawl_page_id=NULL` for those items.

Step 2 deletes rows that step 3 has not yet stopped referencing:

```
sqlalchemy.exc.IntegrityError: ForeignKeyViolationError:
  update or delete on table "crawl_pages" violates foreign key constraint
  "crawl_queue_crawl_page_id_fkey" on table "crawl_queue"
DETAIL: Key (id)=(9126e36a-…) is still referenced from table "crawl_queue".
```

The exception propagates out of `run()`, the process exits, the container restarts, and the same
startup path runs again against the same rows. **Observed in local dev: 31 restarts, 194 stalled
items.** There is no recovery — the state that triggers it is exactly the state the function exists
to clean up, so it cannot clear itself.

⚠️ **The trigger is ordinary.** Any coordinator stopped mid-crawl leaves `dispatched` items behind;
once they age past `stale_threshold_minutes` the next start is fatal. A deploy, a node drain, or an
OOM kill is enough. The bug is dormant only while no crawl has ever been interrupted.

### Root cause

Two things line up, and either alone would be harmless:

**1. The statement order is inverted relative to the constraint.** The comment above the delete
states the intent — *"Delete CrawlPage rows that are about to lose their FK reference"* — and it is
correct about *what* should happen. The rows do need deleting, and `page_ids` is already collected
into a Python list before either write, so the delete does not depend on the reference still
existing. Only the order is wrong.

**2. The FK has no `ON DELETE` action, while its sibling on the same table does.**

```
crawl_queue_crawl_id_fkey       FOREIGN KEY (crawl_id)      REFERENCES crawls(id) ON DELETE CASCADE
crawl_queue_crawl_page_id_fkey  FOREIGN KEY (crawl_page_id) REFERENCES crawl_pages(id)
```

`crawl_id` cascades; `crawl_page_id` defaults to `NO ACTION`. Two FKs on one table with different
delete semantics, which is why deleting a `crawls` row has never hit this and deleting a
`crawl_pages` row always will.

Introduced by `2edffce` — *"fix(coordinator): delete orphaned CrawlPages in reenqueue_stalled (prod
review #51)"*. **A fix commit that introduced its own bug**, the same shape as BUG-005: the problem
it set out to solve (orphaned pages breaking the 1:1 `CrawlQueueItem` ↔ `CrawlPage` invariant) is
real, and the remedy is right; only the sequencing is wrong.

### Why the tests did not catch it — 🔴 the test pins the broken order as correct

`coordinator/tests/test_dispatcher.py::test_reenqueue_stalled_deletes_linked_pages` **passes**, and
all 49 coordinator tests pass. It passes because it asserts the defect:

```python
db = AsyncMock()
db.execute = AsyncMock(side_effect=[select_result, delete_result, update_result])
await reenqueue_stalled(db)
assert db.execute.call_count == 3
```

Its docstring is explicit: *"reenqueue_stalled must DELETE those CrawlPage rows **before** nulling
the FK"*. The `side_effect` list encodes that order positionally — SELECT, DELETE, UPDATE — so
**correcting the code will fail this test**, and the test must be updated as part of the fix.

⚠️ **This is the second confirmed instance of this exact failure mode in this codebase.** The first
is BUG-005, where `api/tests/test_batch.py` asserted `payload["job_id"] is None` — pinning the
broken value as expected. Both are mock-based tests asserting a shape against a real constraint the
mock cannot express: an `AsyncMock` enforces no foreign keys, just as an API-side assertion parses
no worker schema. **A green suite is evidence about the mock, not about the database.**

The generalisable lesson is the one ADR-011 §6 already drew for the wire: a test that stands
entirely on one side of a boundary tells you nothing about the boundary. Here the boundary is the
schema, and the missing test is one that runs `reenqueue_stalled` against a real Postgres with the
FK in place.

### The fix — a reorder, plus the test

Move the `DELETE` to **after** the `UPDATE`. `page_ids` is already materialised into a list before
either write, so nothing is lost by nulling the references first:

1. `SELECT` the `page_ids` (unchanged).
2. `UPDATE crawl_queue … SET crawl_page_id = NULL` — releases the references.
3. `DELETE FROM crawl_pages WHERE id IN (page_ids)` — now unconstrained.

Then update `test_reenqueue_stalled_deletes_linked_pages`: its `side_effect` order becomes
`[select_result, update_result, delete_result]` and its docstring must stop asserting the inverted
requirement.

**Considered and not chosen: adding `ON DELETE SET NULL` to the FK.** It would also stop the crash,
and it would make the constraint consistent with its sibling. It is not the fix because it papers
over a statement order that is wrong on its own terms, and because a migration to alter a
constraint is a heavier change than moving one statement — on a table in a service the migration
deletes. Worth revisiting only if the same FK bites somewhere else.

### Triage — why this is filed and not fixed

`coordinator/` is on the migration's deletion list, so **`phase4-backlog.md` §3 points at
do-not-fix**. This bug is a candidate for the same override BUG-005 and Q6 got — it is live, it is
severe, and the migration is months out — but that override is the owner's call, and on 2026-09-04
the owner **declined to take it now**. Filed so the decision is recorded rather than rediscovered.

Two things bear on a later decision:

- **The `CrawlWorkflow` port does not inherit it.** Temporal's own retry and timeout handling
  replaces `reenqueue_stalled` outright; there is no equivalent function to port the bug into. This
  is genuinely dissolved by the migration, unlike BUG-005's identity decision.
- **The blast radius is bounded by BUG-008.** Crawl results are acked and dropped on v1 anyway, so a
  coordinator that cannot start is losing a lane that already cannot complete a crawl. That lowers
  the practical urgency without changing the severity of the mechanism.

### Interactions

- **BUG-008** — the crawl lane is already non-functional end to end on v1 (will-not-fix). This bug
  stops the service starting at all; that one stops it finishing a crawl.
- **BUG-005** — same failure mode in the tests (a mock-based assertion pinning the defect), and the
  same *"a fix commit introduced it"* provenance.
- **P6 / ADR-011** — unrelated to the mechanism. P6 touched this file (the dispatch payload) but not
  this function; the restart is only what exposed it.
- **P7** — ✅ built 2026-09-18: quota admission, the counting views and artifact reclaim are all
  `api/`-side, so a coordinator that cannot boot does not affect them. What it changes for this bug:
  `reenqueue_stalled` deletes `crawl_pages` rows, and `storage_objects.crawl_page_id` cascades — so
  if the port ever gave this function ledger rows to delete behind, it would recreate the orphan
  class P8 closed. Moot on v1 (no crawl row is ever written, BUG-008) and dissolved with the function.

---

## BUG-013 — Five of seven dependency manifests resolve at build time; the tested image and the deployed image are different artifacts

**Severity:** Medium (no live symptom today, and the one realised instance was caught before it
reached production — but every rebuild is an unreviewed dependency change, and one has already
produced a crash-looping image)
**Discovered:** 2026-09-09, working out what a `main` fast-forward would actually build for the P6
cutover
**Status:** Open — **step 3 (frontend) done 2026-09-19, steps 1, 2, 4, 5 untouched.**
`api/Dockerfile`'s builder stage now copies `package-lock.json` beside `package.json` and runs
**`npm ci`**, so the frontend bundle is built from the committed lock — done in the Dependabot
sweep of 2026-09-19 (the 58 alerts on the three scanned manifests), because 26 of those alerts
were against `frontend/package-lock.json`, and fixing a lock the build ignores would have closed
them without changing what ships. Verified by a `--target production` build (`npm ci` installs
226 packages from the lock, bundle builds). The four unlocked Python services are still exactly
as filed. Sequencing of the rest is the owner's call. Deliberately carved out of BUG-006 rather
than folded into it; see *Relationship to BUG-006* below.

### What happens

Dev images and production images are built from the same Dockerfiles at different times. Five of the
seven dependency roots resolve their versions **at build time** instead of reading a committed lock,
so the image a test suite went green against and the image the cluster runs are two different
artifacts that happen to share a commit SHA. Nothing in the pipeline detects the difference, and
nothing records what was installed.

| Manifest | How the image installs it | Reproducible? |
|---|---|---|
| `api/` (Python) | `uv sync --frozen` against committed `uv.lock` | ✅ Yes |
| `http-worker/` | `go mod download` against committed `go.sum` | ✅ Yes |
| `frontend/` | **`npm install`**, in `api/Dockerfile`'s `frontend-builder` stage | ❌ **No — and it has a lockfile** |
| `coordinator/` | `pip install --no-cache-dir .` | ❌ No lockfile |
| `llm-worker/` | `pip install --no-cache-dir .` | ❌ No lockfile |
| `playwright-worker/` | `pip install --no-cache-dir .` | ❌ No lockfile |
| `mcp/` | `pip install --no-cache-dir .` | ❌ No lockfile |

**The four Python services are the known half** — floor-only constraints (`aiohttp>=3.9.0`,
`anthropic>=0.30.0`, `cryptography>=41.0.0`) and no lockfile, so `pip` resolves whatever PyPI offers
on the day. This is the *"Compounding the gap"* paragraph of BUG-006 stated as its own defect.

**The frontend is the half nobody had looked at, and it is worse than missing a lockfile.**
`frontend/package-lock.json` is committed, and it is one of the three manifests Dependabot *does*
scan — 21 open alerts are filed against it. But `api/Dockerfile` copies **only `package.json`**
into the builder stage before installing:

```dockerfile
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./          # ← the lockfile arrives here, after the install
RUN npm run build
```

So the lockfile is not merely un-enforced — it is **absent from the stage that resolves versions**,
and `npm install` without one resolves the whole tree fresh from the semver ranges in
`package.json`. The consequence is that **the resolution Dependabot scans has never been the
resolution that ships.** Those 21 alerts describe a tree the production bundle does not contain,
in either direction: an alert may not apply, and a real vulnerability in what actually shipped
would not appear.

### Why this is not theoretical

It has already happened once, on this exact mechanism. Rebuilding `llm-worker` for P6's deploy
produced an image that crash-looped on import with `ModuleNotFoundError: No module named 'httpx'`.
The resolver had moved past the floors to `anthropic 1.3.0` and `openai 3.8.0`, and those majors
migrated to **`httpx2`** — a differently *named* package — so the image contained `httpx2 2.12.0`
and no `httpx` at all. Full account: the [BUG-006 addendum](#bug-006-addendum-2026-09-04--the-coverage-gap-produced-a-concrete-outage).

That outage is recorded there as evidence for BUG-006's fix. This bug is the defect it is evidence
*of*: not "we could not see the version", but **"we did not choose the version, and could not have
reproduced it if we had."**

⚠️ **The unresolved half is still unresolved.** `anthropic 1.3.0` and `openai 3.8.0` are what a
fresh build installs today, against code written for `0.x` / `1.x`. The suite passes and
`worker/errors.py` builds its exception tuple at import time, so the names it references do resolve
— real signal, not an assumption. But **nobody chose those majors**, and until a lock records them,
nobody chose the next ones either.

### Why it matters more than usual right now

The P6 cutover rebuilds **all five services at once** (`paths-filter` matches every service
directory, so a `main` fast-forward triggers all five jobs). Four of the five are unlocked, and the
fifth — `api/` — carries the unlocked frontend. So the release that already has the least margin
(a hard `schema_version` cut, five deployments, no per-service rollback) is also the one where the
largest number of dependency trees re-resolve simultaneously.

A crash-loop after that push may have nothing to do with P6. That is a diagnosis cost paid at the
worst moment.

### Root cause

Two independent gaps, and neither is visible from the other:

1. **No lockfile** for `coordinator/`, `llm-worker/`, `playwright-worker/`, `mcp/`. The floor-only
   constraints were adequate when these services were new and are not now.
2. **A lockfile that the build cannot see.** `frontend/` has one; `api/Dockerfile` does not copy it
   before `npm install`. A committed lock that the build ignores is worse than no lock, because it
   reads as solved on inspection — which is why this survived a scanning-coverage bug that
   explicitly enumerated all seven manifests.

Underneath both: nothing asserts that a rebuild of a given commit produces the same dependency set,
so neither gap has a failing check.

### Fix

The acceptance criterion is one sentence: **rebuilding a given commit twice must install identical
dependency versions, and those versions must be readable from the repository.**

1. **Generate and commit lockfiles** for `coordinator/`, `llm-worker/`, `playwright-worker/`, `mcp/`
   — `uv lock`, matching `api/`, which already works this way and is the in-repo precedent.
2. **Install from the lock in each Dockerfile.** Committing a lock changes nothing on its own —
   `pip install .` ignores it. `api/Dockerfile`'s `uv sync --frozen` is the shape to copy; `--frozen`
   is the load-bearing flag, because it *fails* rather than silently re-resolving when the lock and
   the manifest disagree.
3. ~~**Fix the frontend build**: copy `frontend/package-lock.json` alongside `package.json`, and use
   **`npm ci`** rather than `npm install`.~~ ✅ **Done 2026-09-19.** `npm ci` requires a lock and
   refuses to update it, which is the enforcement `--frozen` gives on the Python side.
4. **Decide the SDK majors.** `anthropic` and `openai` need a deliberate upper bound or a pinned
   version, not just a lock recording an accident. Step 1 makes today's resolution reproducible;
   this step makes it *chosen*. Open decision, per the BUG-006 addendum.
5. **Then** `.github/dependabot.yml` enumerating all seven roots — BUG-006's step 1. It is listed
   last on purpose: scanning a manifest with no enforced lock reports versions that are not what
   ships.

**Expect the alert count to rise.** Four services that have never been scanned will start
reporting, and the frontend's numbers will change because the scanned tree will finally be the
shipped one. That is the fix working, not a regression.

**Smaller item, same file, worth doing in the same pass:** all four unlocked services list
`pytest` and `pytest-asyncio` in runtime `dependencies` rather than a dev extra, so the test
framework ships inside every production image. Harmless today; it widens the dependency surface
being locked, and a lock is the natural moment to split it.

### Relationship to BUG-006

Same fix vehicle, different defects — filed separately so neither can be closed by the other.

| | BUG-006 | BUG-013 |
|---|---|---|
| The defect is | **Visibility** — Dependabot scans 3 of 7 manifests, so the true advisory count is unknown | **Reproducibility** — the artifact tested is not the artifact deployed |
| Closed when | Every manifest is scanned and its alerts triaged | A rebuild of a commit installs identical versions, readable from the repo |
| Would a perfect fix of the other close it? | No — full scanning coverage still leaves the build re-resolving on every push | No — perfect reproducibility of an unscanned tree is still an unscanned tree |

BUG-006's fix list already contains "generate lockfiles" as step 2, and its addendum concluded that
lockfiles rather than scanning are the real fix. This bug is that conclusion promoted to its own
record, plus the frontend finding, which BUG-006 cannot hold: it counts `frontend/package-lock.json`
among the **scanned** manifests, and is right to — the gap there is not scanning at all.

### Interactions

- **BUG-006** — carved from it; see the table above. ⚠️ **Fixing this one changes BUG-006's
  premise**: once the frontend installs from its lock, BUG-006's 21 frontend alerts describe the
  shipped bundle for the first time and should be re-counted, not carried forward.
- **BUG-002 alert counts** (`phase4-backlog.md` §4) — already flagged as stale and half-repo. They
  are further off than that row says, because one of the three counted manifests was never the
  deployed resolution.
- **P6 / the cutover** — not a cause and not a blocker, but it is the release with the most
  services rebuilding at once, so it is where an unlocked resolution is most likely to surface.
  ⚠️ **After that push, check pod logs for import-time crash-loops on the four Python services
  before attributing any failure to the `schema_version` change.**
- **ADR-010** — needs a *"pinned, offline public-suffix list with a lockfile"*. That requirement
  cites BUG-006 as its reason; the mechanism it actually needs is this one. A public-suffix list
  that re-resolves at build time changes crawl admission decisions between builds.
- **Survives Temporal.** The migration deletes `coordinator/` and rewrites the workers, but every
  service it produces still installs dependencies at image build. Locking is unaffected by the
  engine change, and the Temporal SDK becomes one more unpinned dependency if this is not fixed
  first. `phase4-backlog.md` §4.

---

## BUG-014 — `DELETE /admin/users/{id}` returns 500 for any user who has ever created a batch or a crawl

**Severity:** Medium (an admin endpoint that fails on most real users — but it fails *closed*,
so nothing is orphaned or half-deleted)
**Discovered:** 2026-09-18, building P7's reclaim path and checking what `release_user_objects`'s
caller does after it
**Status:** ✅ **FIXED — DEPLOYED 2026-09-19 (`421cbfe`)**, migration `9a1ebad3fca2` applied on the release; both FKs verified `confdeltype = c` in production. Built the same day (`b57211a`): `ondelete="CASCADE"` on
both FKs (`models/crawl.py`, `models/batch.py`); migration `9a1ebad3fca2` swaps the two
constraints and keeps their names, so nothing that names them changes; one test
(`test_admin_delete_user_with_crawl_and_batch`) fails with this bug's exact
`ForeignKeyViolationError` on the old schema and passes on the new one. It sits on `develop`
ahead of the queue's release, so **it rides that release as its third Alembic revision** — the
sequencing call the filing left open. The endpoint itself is untouched: its release-before-delete
order was already right. Not a §3 do-not-fix: user deletion is not orchestration and
`routers/admin.py` survives the migration.

### What happens

`admin_delete_user` releases every ledger row the user holds (P8), then `db.delete(user)` and
commits. `users` is referenced by seven tables. Five cascade; **`crawls.user_id` and
`batches.user_id` do not** — both are `ForeignKey("users.id")` with no `ondelete`, so Postgres
runs `NO ACTION` — and the `User` model's relationships cascade only `api_keys` and `jobs`, so the
ORM does not delete them either. The `DELETE FROM users` raises
`violates foreign key constraint "crawls_user_id_fkey"` (or `batches_user_id_fkey`), the request
500s, and the user stays.

Verified 2026-09-18 with an inserted user + crawl inside a rolled-back transaction: the delete
fails on `crawls_user_id_fkey` exactly as read.

⚠️ **The failure is ordered after the release, so it is not free.** The release and the row
delete share one transaction, and the FK fires at its commit — so the DB side (ledger rows,
counter) rolls back, but **the MinIO deletes have already happened and are not transactional**.
Result: the user's objects are gone from the bucket while their ledger rows and their bytes on
the counter stay, and every retry repeats the (idempotent — S3 delete of a missing key succeeds)
removals and fails at the same FK. Nothing double-counts: the first commit that succeeds, after
the FK is fixed, releases each row exactly once. Until then the counter holds bytes for objects
that no longer exist, which `scripts/reconcile_storage_ledger.py` would also detect (a row whose
object is gone is dropped and the counter recomputed).

### Why it was missed

The two tables were added in Phase 2/3 (ADR-006, ADR-005) after the user-delete endpoint, and
each added its own `user_id` without looking at what `users`' other referrers did. `test_admin.py`
deletes users who have jobs only. The backlog's P7 row says *"even deleting a user orphans their
crawl artifacts in MinIO"* — the truth is one step earlier: **the delete never gets that far**.

### Fix

Two lines and a migration: `ondelete="CASCADE"` on both FKs, matching `jobs`, `api_keys` and
`storage_objects`. **Order matters in the same way P8's 503 does** — the release must still run
*before* the row delete, because a cascade that drops `crawl_pages` and `batch_items` → `job_runs`
would take `storage_objects` rows with it (both FKs cascade) for objects still on disk. That is
already the endpoint's order; the fix only removes the FK that stops it completing. One test:
delete a user who owns a crawl and a batch, assert 204 and that both parent rows are gone.

✅ **Built as written (2026-09-19, `b57211a`).** The test goes one step further than the plan: each
lane holds a ledger row (a `crawl_pages` row and a batch `job_runs` row, both with a
`storage_objects` row), so the release runs before the delete and the assertion covers the
children and the ledger rows going with the parents, not only the parents. Two things from the
build worth keeping:

- **Autogenerate emits `create_foreign_key(None, …)` for the replacement constraints**, and the
  matching `drop_constraint(None, …)` in the downgrade cannot run. The committed revision names
  both explicitly with Postgres' own default names (`crawls_user_id_fkey`, `batches_user_id_fkey`),
  so the downgrade round-trips — verified `c` → `a` → `c` on `confdeltype`.
- **The P7 views read `crawls.user_id` and `batches.user_id`, and this migration does not need to
  drop them.** A constraint swap leaves the column identity alone; the drop/recreate obligation in
  `CLAUDE.md`'s *Run-counting views* row is for `DROP COLUMN` / `ALTER … TYPE` only. Verified by
  the migration applying cleanly with both views in place.

### Relationship to P7

None in mechanism. P7 gives `DELETE /crawls/{id}?permanent=true` its own release path, which works
because it deletes the crawl row with a Core `DELETE` and lets the DB cascade — the same shape
the user delete needs.

---

## BUG-015 — `timeout_seconds` does not govern the wait strategy

**Severity:** Medium (a user-set budget is silently capped at 30 s on the step that most often
needs it; the failure reads as the site's fault)
**Discovered:** 2026-09-19, reading two proxied Myntra runs that failed with
`TimeoutError: Timeout 30000ms exceeded` under `playwright_options.timeout_seconds: 90`
**Status:** ✅ **FIXED — DEPLOYED 2026-09-19 (`421cbfe`).** Built as `9a72bb7`. One argument:
`page.wait_for_load_state(wait_state, timeout=timeout_ms)` — the wait shares `goto`'s budget,
the simple form the filing recommended. Two tests (`test_wait_for_load_state_receives_job_timeout`,
`…_receives_default_timeout`) pin the kwarg for an explicit `timeout_seconds: 90` and for the
worker default; both fail on the old worker with `KeyError: 'timeout'`. `playwright-worker/`
only, on `develop` ahead of the queue's release, so it rides it (P6 rebuilds the worker anyway).
Not §3: page rendering is activity logic and ports into the Temporal `PlaywrightWorkflow`
activity unchanged.

### What happens

`playwright-worker/worker/worker.py` computes `timeout_ms` from `timeout_seconds` and passes it to
`page.goto(job.url, timeout=timeout_ms)` — then calls `page.wait_for_load_state(wait_state)` on the
next line **with no timeout**, so the wait gets Playwright's default 30 s regardless of what the
job asked for. With `wait_strategy: networkidle` on a page that never goes quiet (any site with a
bot sensor or analytics beacons; every request slower through a residential proxy) the run fails
at `goto` elapsed + 30 s. Observed: two runs at ~55 s each — ~24 s of `goto` through the proxy
plus the 30 s cap — under a 90 s budget that was never reached.

### Why it was missed

`load` (the default) usually fires before or with `goto`, so the missing timeout is invisible
unless the strategy is `networkidle` or `domcontentloaded` *and* the site is slow. Both were true
for the first time on 2026-09-19.

### Fix

One argument: `page.wait_for_load_state(wait_state, timeout=timeout_ms)`. Whether the wait should
share the `goto` budget (total ≤ `timeout_seconds`) or get its own is a small design call; the
simple form — same value for both — is what the option's name promises and is enough. One test
that pins the timeout being forwarded (the worker tests mock the page; assert the kwarg).

---

## BUG-016 — `block_images` also blocks stylesheets, which breaks SPA routes

**Severity:** Medium (a `completed` run whose visible DOM is an error boundary; the data may
still be in the page, as it was on Myntra, but a user reading the rendered output sees a broken
scrape and a markdown job loses everything)
**Discovered:** 2026-09-19, three Myntra runs through a working proxy that rendered the full
header and footer around "Oops! Something went wrong. Refresh"; confirmed by re-running with
`block_images: false`, which rendered the product
**Status:** ✅ **FIXED — DEPLOYED 2026-09-19 (`421cbfe`).** Built as `14c6136`. `css` dropped from the
route glob (`**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}`); images and fonts still abort. The
option keeps its name (an API field); the worker comment and `docs/guides/playwright-primer.md`'s
field row now say what it blocks and why CSS must never return. One test
(`test_block_images_aborts_images_and_fonts_but_never_css`) feeds the registered glob through
Patchright's own `glob_to_regex_pattern` and asserts `.png`/`.jpg`/`.woff2` match and
`.css`/`.js`/a page URL do not — it pins what the browser will abort, not the pattern's spelling,
and fails on the old glob with `route-chunk.css must not be aborted`.
Not §3, same reasoning as BUG-015.

### What happens

The option's route glob is `**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}` — images, fonts
**and CSS** (the comment says so: "block images/fonts/CSS"). A React app whose route is
lazy-loaded fetches that route's CSS chunk through webpack's chunk loader; an aborted stylesheet
rejects the `import()`, the error reaches React's error boundary, and the page renders the
boundary's fallback instead of the route. The document, the state blob and everything already
styled still render, which is why it looks like the *site* failed rather than the scraper.

⚠️ **This is not a bot wall and must not be fingerprinted as one.** "Something went wrong" is a
generic error-boundary string. The 2026-09-19 Myntra bodies were 456 KB with the complete product
in `window.__myx.pdpData`; the detector was right to pass them.

### Why it was missed

Server-rendered targets — the bulk of what the platform scrapes (CLAUDE.md: "HTTP first,
Playwright opt-in") — do not lazy-load CSS, so aborting it only speeds them up. Myntra is the
first observed target that renders its product client-side from a route chunk.

### Fix

Take `css` out of the glob. Images and fonts stay: a missing font falls back silently, a missing
stylesheet chunk throws. Keep the option's name (an API field), fix its docstring to say what it
blocks. One test: with `block_images` on, a `.png` request is aborted and a `.css` request is not.

---

## BUG-017 — `proxy_url` credentials are decoded by one engine and not the other

**Severity:** Low (latent — only bites a password containing a URL-reserved character; found by
reading, not by a failure)
**Discovered:** 2026-09-19, explaining how to translate a proxy vendor's `curl -x … -U user:pass`
into `proxy_url`
**Status:** ✅ **FIXED — DEPLOYED 2026-09-19 (`421cbfe`).** Built as `f26c7ca`. `unquote()` on both
`.username` and `.password` in `playwright-worker/worker/worker.py`, aligning Python to Go and the
URL standard; `proxy_url` on `api/app/schemas/jobs.py` gained a `Field(description=…)` stating that
reserved characters in credentials must be percent-encoded and both engines decode them. One test
per engine with `us%40er:p%40ss%3Aw0rd`: the playwright test asserts the decoded pair reaches
`new_context` (fails on the old worker with the encoded strings in the actual call); the Go test
(`TestWithProxy`) stands up an `httptest` proxy and asserts the decoded pair in the
`Proxy-Authorization` header the transport actually sends — a regression pin on behaviour Go
already had, deliberately on the wire rather than on `url.User.Password()`. Both sides port into
the activities; the port now copies one behaviour instead of two.

### What happens

`playwright-worker/worker/worker.py` splits `proxy_url` with `urllib.parse.urlparse` and passes
`.username` / `.password` to Playwright verbatim. Python does **not** percent-decode userinfo:
`urlparse("http://user:p%40ss@h:1").password == "p%40ss"`. The Go http-worker
(`internal/fetcher/fetcher.go`, `url.Parse` + `http.ProxyURL`) **does** decode it. So a
`proxy_url` written the standard way — reserved characters in the password percent-encoded —
authenticates on `engine: http` and fails on `engine: playwright`; written unencoded, a `@` or
`:` in the password mis-splits on both. There is no valid spelling that works on both engines for
such a password.

### Fix

`unquote()` both fields on the Python side, matching Go and the URL standard; document on the
schema field that reserved characters in credentials must be percent-encoded. One test per
engine with a password containing `@`. Workaround until then: issue proxy credentials without
reserved characters (most vendors let you regenerate).

---

## BUG-018 — The SPA caches the Clerk session token for exactly its own lifetime, so any page left open goes "Failed to load"

**Severity:** Medium (every list page in the admin SPA and the user dashboard breaks after ~60 s
of sitting on it while anything polls; self-heals on a tab switch, which is why it reads as
random)
**Discovered:** 2026-09-19, owner's report after the queue release — "after some time the
frontend shows failed to load in the job list and user list". Pre-existing; not introduced by the
`react-router-dom` 6 → 7 or `clerk-backend-api` 6 → 7 bumps in that release.
**Status:** 🔴 **Open — owner's call 2026-09-19: tabled until after the Temporal pipeline.**
Not a Phase 4 item (backlog §4). Root cause read from the code; not yet confirmed against a
captured 401 — the confirmation step is in *What to capture*.

### What happens

Five pages — `frontend/src/pages/Jobs.tsx`, `JobDetail.tsx`, `Users.tsx`, `ApiKeys.tsx`,
`UsageStats.tsx` — obtain the bearer token the same way:

```ts
const { data: token } = useQuery({
  queryKey: ['token'],
  queryFn: () => getToken() as Promise<string>,
  staleTime: 60_000,
})
```

and pass that **string** to every `apiGet`/`apiPost`/… call. A Clerk session JWT expires **60 s**
after issue. The cached string is therefore handed to requests right up to, and past, the moment
the API starts rejecting it — nothing ties the cache's staleness to the token's `exp`, and a
stale query is only re-run on a remount, a window-focus event or a reconnect, never because a
*different* query is about to use its value.

So on any page that refetches without remounting — `Jobs` polls every 5 s while a run is
non-terminal, `UsageStats` polls every 30 s, and every page refetches on a filter, page or action
— the first request after the 60 s mark gets **401** from the API, `apiFetch` throws
`HTTP 401: …`, the `QueryClient`'s `retry: 1` retries with the same dead string, and the page
renders its `isError` branch: *"Failed to load jobs."* / *"Failed to load users."* Switching to
another tab and back fires `refetchOnWindowFocus`, which re-runs the stale `['token']` query and
the page recovers — hence intermittent.

`frontend/src/lib/useIsAdmin.ts` already does it right: it calls `getToken()` **inside** its
`queryFn`, per probe. Clerk's `getToken()` is designed for exactly that — it caches internally and
refreshes when the token is about to expire — so wrapping its *result* in a second cache defeats
the one mechanism that knew the expiry.

### Root cause

The token is treated as data (cached by value, with a wall-clock `staleTime`) when it is a
credential with its own expiry that the issuing library already manages. Two caches with
different notions of "fresh"; the outer one wins and is wrong.

### Fix

`frontend/src/api.ts`: `apiFetch` takes a token *getter* instead of a string and calls it per
request —

```ts
async function apiFetch(path: string, getToken: () => Promise<string | null>, options?: RequestInit) {
  const token = await getToken()
  if (!token) throw new Error('Not signed in')
  …
```

— then each of the five pages passes `getToken` straight from `useAuth()`, drops its `['token']`
query and its `enabled: !!token` guard. Six files, no backend change; `useIsAdmin.ts` is the
in-repo precedent. There is no frontend test suite (BUG-013 note), so the check is manual: open
Jobs while a run is in flight, keep the tab focused, wait 90 s — the list must keep refreshing.

### What to capture, when it recurs

DevTools → Network → the red request: status **401** is the confirmation, and a tab switch that
recovers the list within a couple of seconds is the second signal. A screenshot of the page adds
nothing — the request status is the evidence.

---

## BUG-006 addendum (2026-09-04) — the coverage gap produced a concrete outage

Filed against BUG-006 rather than separately: this is not a new bug, it is the first realised
instance of the one already recorded.

**What happened.** Rebuilding `llm-worker` for P6's deploy produced an image whose worker
crash-looped on import:

```
File "/app/worker/errors.py", line 23, in <module>
    import httpx
ModuleNotFoundError: No module named 'httpx'
```

**Why.** `llm-worker/worker/llm.py` (the `ensure_ready()` warm-up probe) and
`llm-worker/worker/errors.py` (which matches `httpx.TransportError` in the transient classifier)
both import `httpx` **directly**, and `llm-worker/pyproject.toml` never declared it. It arrived
transitively through the provider SDKs. Between the previous image build and this one, the
resolver moved to **`anthropic 1.3.0`** and **`openai 3.8.0`** — major-version jumps past the
`anthropic>=0.30.0` / `openai>=1.30.0` floors — and those releases migrated to **`httpx2`**, a
**differently named package**. The image ended up with `httpx2 2.12.0` installed and no `httpx` at
all.

**Fixed** in `1c456a4` by declaring `httpx>=0.27.0`. Verified: 101 llm-worker tests pass against
the rebuilt image, and the worker starts and subscribes cleanly.

**Why this is BUG-006 and not a one-off.** The parent bug's *"Compounding the gap"* paragraph
already predicted it — *"the version actually running in production is whatever PyPI resolved at
image-build time — unpinned, non-reproducible"*. What it did not anticipate is the sharper form:
an unpinned range let a transitive dependency **change its identity**, not merely its version. A
version bump degrades behaviour; a package rename removes the module. Both are invisible without a
lockfile, and only one of them fails loudly.

**Two things this sharpens for the parent bug's fix:**

1. **Lockfiles are the fix, and this is the argument for them.** BUG-006 is currently framed around
   *scanning coverage* — Dependabot seeing 3 of 7 manifests. Scanning was never the whole problem:
   a lockfile would have prevented this outage outright, and no amount of alerting would have.
2. **An audit for undeclared imports belongs in the same pass.** Checked across all three Python
   services on 2026-09-04: `llm-worker`'s `httpx` was **the only** undeclared direct import.
   `playwright-worker` declares both `httpx` and `aiohttp`; `coordinator` declares `aiohttp` and
   imports no `httpx`. Notably `llm-worker` *did* declare `aiohttp`, with a comment explaining
   exactly why (*"declared explicitly because worker/errors.py matches its connection-error
   types"*) — the same reasoning applied to `httpx` would have caught this in 2026-07. The
   omission was an oversight in a file that demonstrates the correct instinct one line above.

⚠️ **Unresolved, and larger than the fix:** the SDK majors themselves. `anthropic 1.3.0` and
`openai 3.8.0` are now what a fresh build installs, against code written for `0.x` / `1.x`. The
test suite passes, and `worker/errors.py` builds its exception tuple at **import time**, so the
`anthropic.APITimeoutError` / `openai.RateLimitError` names it references still resolve — a real
signal, not an assumption. But no one chose those versions, and whether to pin them is an open
decision, not a settled one.
