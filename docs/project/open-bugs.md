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

---

## BUG-005 — Batch is broken on all three execution paths (`job_id` is NULL, and the contract assumes it never is)

**Severity:** High (two paths hang forever with no error; the third silently returns the wrong
content and breaks tenant isolation)
**Discovered:** 2026-08-04, reviewing inputs for ADR-009
**Status:** Open — **fix before the migration** (triage reasoning below)

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
`None`, and skips (`scheduler.py:154-156`). **This is already recorded** — see BUG-001, which
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
2. **The MinIO path convention** (ADR-002 §8) keys every artifact on `job_id`:
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
2. **Key the artifact path on something that always exists.** `run_id` is the natural candidate:
   every run has one, it is unique, and it is already the handle the result consumer and result
   endpoint work from. This changes the ADR-002 §8 path convention, so it belongs in an ADR
   amendment rather than a quiet edit — and it needs a decision on what happens to `latest/`,
   whose whole purpose ("the newest result for this thing") is job-shaped and has no meaning for a
   one-shot batch item.
3. **Add the cross-service contract test**, and delete the assertion that currently pins the bug.

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
- **ADR-002 §8** — the MinIO path convention is the thing that has to change; do not let the fix
  bypass the ADR.
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

---

## BUG-006 — Dependabot scans 3 of 6 dependency manifests; the unscanned half contains the only reachable instance of a live CVE

**Severity:** Medium (contained DoS vector — but the coverage gap behind it is the larger problem)
**Discovered:** 2026-08-05, from a push-time Dependabot banner
**Status:** Open — **deferred behind BUG-005 and the Temporal migration** (owner's call, recorded
in `phase4-backlog.md` §4)

### What happens

Two separate problems that were found together and are best fixed together.

**1. Three of six dependency manifests are never scanned.** There is no
`.github/dependabot.yml`, so the repository runs on default auto-setup. Every open alert is filed
against one of three manifests:

| Manifest | Open alerts |
|---|---|
| `api/uv.lock` | 29 |
| `frontend/package-lock.json` | 21 |
| `http-worker/go.mod` | 1 |

**`coordinator/`, `llm-worker/` and `playwright-worker/` produce zero alerts** — not because they
are clean, but because nothing looks at them. Each has a `pyproject.toml` carrying floor-only
constraints (`aiohttp>=3.9.0`, `cryptography>=41.0.0`) and **no lockfile**. Three of the five
Python services in the platform are unmonitored.

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
   monorepo's six dependency roots.
2. **No lockfiles outside `api/`.** Without a lock, there is no resolved version for a scanner to
   compare against an advisory, and no reproducibility for the build either. The floor-only
   constraints were adequate when these services were new and are not now.

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
- **Recommendation for that port: use `httpx`, not `aiohttp`.** Every other untrusted-target fetch
  in the platform already does. Converging on one client for untrusted responses removes this
  exposure as a side effect of the migration rather than as separate work, and leaves `aiohttp`
  used only where `miniopy-async` requires it — against MinIO, which we control. Worth adding to
  ADR-009 §10's port list when that ADR is reviewed.
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
**Status:** Open — **unblocked 2026-08-25** (the `latest/` decision landed), now **sequenced behind
P8**, the shared per-object storage ledger that is the actual fix vehicle

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
| job hard-deleted | `routers/jobs.py:391`, `admin.py:336` | enumerates `JobRun.result_path` → stats the **JSON** → decrements by **that** |

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
  artifact it mirrors. `latest/` is kept as-is (v2 drops it anyway, so the 2× discrepancy is v1-only
  with a known end date). ADR-009 §8a carries the call.
- **The same pass found a fourth symptom, in the deletion path.** An LLM job leaves **four**
  objects, not two: the scrape writes the job's own format and the LLM always writes `.json`, so
  `latest/{job}.{fmt}` and `latest/{job}.json` are **different keys** and neither overwrites the
  other. Hard delete derives **one** filename from `job.output_format` (`routers/jobs.py:395`), so
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
  now **consumers of P8**, so the sequence is **P6 → P8 → P7 + BUG-007 together** rather than P6 →
  P7 with this trailing behind.

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
- **P7 / crawl quota** — P7 adds quota counting to a lane that has never successfully completed a
  page. Worth knowing when sequencing: P7's accounting cannot be tested end to end until either
  this is fixed or the crawl migration lands.
