# ScrapeFlow Session Handoff

You are a **coding mentor** helping the user build ScrapeFlow, a self-hosted multi-tenant web
scraping platform. Read @CLAUDE.md for the full architecture.

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
| ADR index + per-record status | `docs/adr/README.md` |
| **Phase 4 scope — single source of truth** | `docs/project/phase4-backlog.md` |
| **Phase 4 engine decision + coexistence contract** | `docs/adr/ADR-009-workflow-engine-temporal.md` |
| Crawl admission + scheduled-quota decisions (Draft) | `docs/adr/ADR-010-crawl-admission-and-scheduled-quota.md` |
| **Artifact identity — the live path convention (Accepted)** | `docs/adr/ADR-011-artifact-identity-and-paths.md` |
| Open bugs (BUG-004 → BUG-013) | `docs/project/open-bugs.md` |
| Open questions (Q1–Q8) | `docs/project/open-questions.md` |
| Usage findings (UF-00x) + test counts | `docs/project/usage-findings.md` |
| PRDs | `docs/project/phase4-prd/` (PRD-016 only, so far) |
| Feature scoping + engine comparison (redrawn 2026-09-08) | `docs/project/workflows-scoping.md` |
| Change inventory + migration sequence (redrawn 2026-09-08) | `docs/project/temporal-full-migration.md` |
| **The wire contract the API publishes through (P6)** | `api/app/messages.py` — and `coordinator/coordinator/messages.py`, a **deliberate duplicate** for the crawl lane (ADR-011 §6 rejected a shared package) |
| **Cross-service contract test + Go fixtures** | `contracts/` — the only test that feeds an API-produced message into each worker's real parser. Command in *Commands* below |
| `latest/` production sweep (owner-authorised, unrun) | `api/scripts/sweep_latest_objects.py` — dry-run by default |
| Multi-persona process starter prompts | `docs/process/` |
| Anti-bot hardening record (ADR-008 companion) | `docs/guides/anti-bot-hardening.md` |
| Phase 1–3 history (specs, backlogs, reviews, audits) | `docs/archive/` |

---

## Commands

**API tests** (must run inside Docker — `uv` manages the venv inside the container):
```bash
# from ./docker
docker compose exec api uv run pytest tests/ -v
docker compose exec api uv run pytest tests/test_jobs.py -v
```

**Worker tests** — not wired into compose; mount the source over the built image:
```bash
# from repo root
docker run --rm -v "$PWD/llm-worker:/app" -w /app docker-llm-worker python -m pytest -q

docker run --rm -v "$PWD/playwright-worker/worker:/app/worker:ro" \
  -v "$PWD/playwright-worker/tests:/app/tests:ro" -w /app \
  --entrypoint python scrapeflow-playwright:ackfix -m pytest tests/ -q -p no:cacheprovider
# ⚠️ the docker-playwright-worker image on disk is stale (predates credentials —
#    no cryptography, so test_main.py fails to import). Use a newer tag.

cd http-worker && go test ./...          # non-integration
go vet -tags integration ./...           # compile the integration-tagged tests

# ⚠️ the coordinator needs env vars even for pure-unit tests — coordinator/config.py
#    instantiates Settings() at import time, so collection fails without them.
docker run --rm -e DATABASE_URL="postgresql+asyncpg://u:p@localhost/db" \
  -e NATS_URL="nats://localhost:4222" \
  -v "$PWD/coordinator:/app" -w /app docker-coordinator python -m pytest -q
```

**Cross-service contract test** (ADR-011 §6) — the only test that feeds an API-produced
message into each worker's *real* parser. Needs the repo root mounted, because the four
message modules live in four build contexts:
```bash
# from repo root
docker run --rm -v "$PWD:/repo" -w /repo docker-api \
  /app/.venv/bin/python -m pytest contracts -q

# after an intentional contract change, to refresh the Go fixtures
docker run --rm -v "$PWD:/repo" -w /repo -e UPDATE_CONTRACT_FIXTURES=1 docker-api \
  /app/.venv/bin/python -m pytest contracts -q
```
⚠️ The Go arm reads `contracts/fixtures/*.json` and is **not optional**: the silent half of
BUG-005 (a JSON `null` unmarshalled to `""` with no error) exists *only* on the Python→Go
wire, so a Python-only contract test would not reach the failure that actually shipped.

**MCP tests** (standalone image, not in compose):
```bash
docker build -t scrapeflow-mcp mcp/
docker run --rm -e SCRAPEFLOW_API_KEY=test-key scrapeflow-mcp python -m pytest tests/ -v
```

**Migrations** (Alembic auto-runs on API startup, from `api/app/main.py`):
```bash
# from ./docker
docker compose exec api uv run alembic upgrade head
docker compose exec api uv run alembic current
docker compose exec api uv run alembic revision --autogenerate -m "migration_3_N_description"
```

---

## Current state — as of 2026-09-04

Phases 1–3 complete and production-verified at `scrapeflow.govindappa.com`. **Phase 4 is in
progress, and Phase 4 *is* the Temporal durable-workflows migration.** The design phase closed on
2026-09-03, and **the first build item of the pre-migration queue — P6 / BUG-005 — was built on
2026-09-04.** It is committed and **not deployed**.

🔷 **P6 is built (2026-09-04), `schema_version` 2 → 3, five services, 598 tests green.** Detail is
in `phase4-backlog.md` §1's P6 row, which is the source of truth; the two things that belong here
because they are *not* in any ADR:

- ⚠️ **The cutover is a hard cut against a drained stream — owner's call, 2026-09-04.** ADR-011
  says nothing about deployment ordering, and there is no safe order: old workers + new API drop
  every message as malformed, new workers + old API reject every message as missing `artifact_id`.
  **All five services deploy together.** The stream was confirmed empty. `schema_version` was bumped
  in the same change so a mis-ordered deploy reports *which* mismatch it hit rather than a generic
  missing field.
- ⚠️ **One step is still outstanding and is the owner's to authorise: the production `latest/`
  sweep.** `api/scripts/sweep_latest_objects.py` — **dry-run by default**, `--apply` to delete;
  verified in dry-run against the *local dev* bucket (1,916 objects). It performs **no accounting
  adjustment on purpose** — `latest/` is uncounted on both sides, so a decrement would corrupt the
  counter. Nothing in production has been touched.

**ADR-009 is `Accepted` (2026-09-08).** Its section-by-section review completed 2026-09-05 — §1–§17
and both closing blocks, reviewed from 2026-08-08 — and the owner took the promotion decision on
2026-09-08. It is the decision of record: implement against it and cite it as settled. Its
`## Review status` block (top of file) stays authoritative for what each section says and what
changed; its **Reversed or withdrawn** and **Amended as a knock-on** tables are the fastest way to
catch a stale note. ⚠️ **An accepted ADR is immutable** (`docs/adr/README.md`) — a change of
decision from here is a new, superseding ADR, not an edit to this one.

**ADR-010 is `Draft` (2026-09-08)** (`docs/adr/ADR-010-crawl-admission-and-scheduled-quota.md`). It
resolves the two items ADR-009 deferred by name — per-meter quota parking for scheduled runs, and
sitemap entries scoped to the seed's registrable domain. **Both decisions are owner-taken and
dated; the write-up is unreviewed.** Written as a separate ADR rather than an edit because ADR-009
is Accepted and immutable.

**ADR-011 is `Accepted` (2026-09-03)** (`docs/adr/ADR-011-artifact-identity-and-paths.md`) —
reviewed and promoted by the owner, drafted and accepted the same day. It was **P6's design
dependency and the last one it had**: BUG-005's fix could not be built until the artifact-path
convention was decided, because half the paths stay broken if the message contract and the path
convention do not change together. It **supersedes ADR-002 §4** — the first supersession in the
index actually to land, since every other one waits on a named step of ADR-009 §16 — and answers
**PRD-016 OQ-1** in passing. ⚠️ **Immutable from here**: a change of decision is a new ADR.

Its one open item was **confirmed at promotion**: the crawl lane stays in scope, so **P6 changes
all three lanes**, even though BUG-008 means nothing on v1 reads a crawl result. The alternative
leaves `str(page.id)` in a field named `job_id` for the `CrawlWorkflow` port to read as precedent.

⚠️ **The design phase is closed. Everything still open is code, one PRD, and one Draft ADR
(ADR-010) to promote.** Every documentation debt ADR-009's review created has been discharged: both
stale companions redrawn, PRD-016's four carry-backs landed, the conditional PRD numbered, the
lane-blind meters recorded, D5 closed.

**Nothing is blocking. P6 is built; the queue continues at P9.**

⚠️ **ADR-010 is still `Draft`, and a Draft is not a decision** (`docs/adr/README.md`) — *"do not
implement against it, and do not cite it as settled in another document."* It blocks nothing in the
queue below, but it **is needed before the batch-and-crawl cutover**, so it wants promoting before
build work reaches that far.

### ⚠️ One decision was displaced from outside an immutable ADR — read this before trusting §8d

ADR-011 §4 removes `latest/`, which **reverses ADR-009 §8d's *"`latest/` is kept as-is"*** call of
2026-08-25 (*"removing it early costs work for a convenience that expires on its own"* — false once
P6 rewrites the path convention anyway). **§8d's charging rule is upheld and unchanged**: every
`history/` object is charged once, `latest/` is never charged.

This is the one stale-note class the standing method does **not** catch. ADR-009's **Reversed or
withdrawn** and **Amended as a knock-on** tables only record what *its own review* changed — they
cannot record a later ADR displacing a clause, and ADR-009 is immutable so nothing can be added.
The displacement is declared in **ADR-011's header** instead. Two knock-ons:

- The 2× storage discrepancy §8d accepted as *"v1-only with a known end date"* ends at **P6**, not
  at the v2 cutover. Anything reading that clause for a date is wrong by months.
- **BUG-007's fourth symptom is deleted upstream by P6** — the two orphaned `latest/` keys stop
  existing — so what reaches P8 is only the `history/` half.

### Outstanding, in rough order

1. **Write PRD-019 — conditional execution (layer A).** ✅ Numbered 2026-09-08 (owner's call) and given
   its `phase4-backlog.md` §2 row; **the document itself is unwritten.** It owes **four** things,
   all on that row: the Validate-precedent brief and the replay constraint (14c), the halt-early
   block B cannot build for itself (§4), and run-level failure notification (15f). ⚠️ **It sorts
   last and is required first** — before PRD-018, which cannot ship without its primitive. The
   sort-order cost was accepted rather than engineered around; index order is not build order in
   this chain.
2. **Promote ADR-010** when convenient — it blocks nothing in the queue but is required before the
   batch-and-crawl cutover, and it opens the Schedule overlap policy (`phase4-backlog.md` §2
   gotcha 6), which is genuinely undecided.
3. **The pre-migration queue is the entry condition for any build work** (16e):
   ~~P6~~ ✅ → **P9 → P8 → P7 + BUG-007**, then engine up. `phase4-backlog.md` §1 is its source of
   truth.
   ⚠️ **P9 (BUG-011) is next, and P6 did not close it** — the reverse of the usual worry. P6
   removed the *cause* of stuck batch items but left the net holed: `_recover_stale_pending` still
   selects batch runs with no lane filter and drops them at `db.get(Job, run.job_id)`, silently,
   every tick. **The one thing P6 was expected to save has already been spent** — the argument for
   sequencing P9 immediately after P6 was that P6 edits the same function, and it now has (the
   payload it builds is a `ScrapeMessage` via `_build_scrape_message`). That saving is gone, but the
   *fix* is unchanged and still small: route by whichever FK is set, like the result consumer.
   Everything it needs is persisted. Note the live SAWarning it still emits in
   `api/tests/test_scheduler.py` — *"fully NULL primary key identity cannot load any object"* — which is
   BUG-011 visible in the test output today.

   ⚠️ **The `latest/` production sweep is the one part of P6 that did not ship**, because it is a
   production data deletion and therefore the owner's to authorise. See *Current state* above.

### Git / deploy state

- **Deployed code is `b110591`** (2026-07-28).
- **`5c7fbdf`** (the crawl page status-filter fix, 2026-08-28) is committed on `develop`,
  **not deployed, not on `main`**. ⚠️ *"Everything on top of it is docs"* was true until
  2026-09-04 and is now false — see the P6 bullet below.
- ✅ **The owner's call of 2026-08-28 stands: `main` is deliberately NOT fast-forwarded** —
  pushing it starts a push to the prod server, so a fast-forward is a **release**, not a tidy-up.
  Do not do it at session end; wait to be asked.
- ⚠️ **`5c7fbdf` is no longer the last application-code commit.** P6 (2026-09-04) is the first
  code change since 2026-08-28, and it touches **five services**: `api/`, `coordinator/`,
  `playwright-worker/`, `llm-worker/`, `http-worker/`, plus a new top-level `contracts/`.
- ✅ **`develop` was pushed to `origin/develop` on 2026-09-09** (`fa3c18d..a57e395`, ten commits,
  including P6). **`develop` and `origin/develop` are level; `main` is 57 behind and 0 ahead.**
  ⚠️ Re-check against the remote before quoting these — that is the standing rule below, and this
  line has already been stale once in this file.
- ⚠️ **Pushing `develop` builds and deploys nothing.** `.github/workflows/build-push.yml` triggers
  on `push: branches: ["main"]` only. **That is what makes a `main` fast-forward a release** — and
  because P6 touched `api/`, `http-worker/`, `playwright-worker/`, `llm-worker/` **and**
  `coordinator/`, its `paths-filter` matches **all five** service jobs. The five-service
  simultaneous build the cutover needs is therefore what a `main` fast-forward already produces;
  what it does **not** do is drain the stream first, which is still a manual step.
- ✅ **The ADR-011 promotion is committed** as `a0714f6` (ten files, all docs, no application code),
  with this file's own follow-up on top. **Unpushed**; `main` untouched.
- ⚠️ **Deploying P6 is a five-service simultaneous release against a drained stream** — not a
  rolling deploy, and not reversible one service at a time. See *Current state*.
- ⚠️ **The document timeline runs ahead of git, and has for several sessions.** Work dated
  2026-09-04 → 2026-09-08 across the ADRs, backlog and this file was committed on **2026-09-02**.
  Everything created on 2026-09-03 (ADR-011, BUG-011) is dated from the clock, so ADR-011 (09-03)
  sorts *after* ADR-010 (09-08) in the registry while carrying an earlier date. That is the
  pre-existing drift showing, not a mistake in either record — **do not "correct" one into the
  other.** Decide which timeline is real before dating the next artifact. ⚠️ The session log now has **three `2026-09-03` rows** for this reason — the top two are the clock (two separate sessions on the same day), the bottom one is the doc timeline.
- ⚠️ **Re-check ahead/behind against the remote before quoting numbers** — two consecutive handoffs
  once carried counts stale by two months. Fetch first, quote second.
- Untracked `tmp/architecture.md` predates all current work (May) and is deliberately left alone.

**Tags** (annotated; the older `v1.0.0` / `v2.0.0` are lightweight):

| Tag | Commit | Marks |
|---|---|---|
| `v3.0.0` | `d9e1edb` (2026-05-13) | End of Phase 3 |
| `prephase4` | `1965953` (2026-07-28) | Pre-Phase 4 queue closed, immediately before PRD-016. Its message records what the system *is* at that point — NATS + the five hand-rolled loops, the thing the migration replaces |

⚠️ **`prephase4` and `v3.0.0` are annotated tags, so `git rev-parse --short <tag>` returns the
*tag object's* SHA, not the commit's** (`473fb68` and `a6a39d4` respectively). The table above
holds **commits**. Use `git rev-list -n1 <tag>` to get one. `v1.0.0` / `v2.0.0` are lightweight,
where both commands agree.

---

## Where the detail lives

This handoff deliberately holds **no** review findings. Every one is written out authoritatively
somewhere else, and a second copy here is how they go stale:

| Looking for | Read |
|---|---|
| What a given ADR-009 section decided, and what its review changed | ADR-009 `## Review status` → the section itself |
| Whether a note you are holding is now wrong | ADR-009's **Reversed or withdrawn** + **Amended as a knock-on** tables. ⚠️ **They cover only what ADR-009's own review changed** — a *later* ADR displacing one of its clauses cannot appear there, and one has: ADR-011 §4 vs §8d's `latest/` sequencing. Check the superseding ADR's header too |
| The live artifact-path convention | **ADR-011** — not ADR-002 §4. ✅ **Live code implements it as of 2026-09-04** (P6, `81afbb9`), across all three lanes. ⚠️ **Production does not** — it still runs the old convention until the next release, and historical objects keep their old-format `result_path` strings forever (no backfill, by design) |
| Phase 4 scope, sequencing, what is do-not-fix | `phase4-backlog.md` (§1 queue · §2 migration · §3 **do NOT fix** · §4 survives) |
| A bug's root cause and fix plan | `open-bugs.md` |
| Why a production trap exists | `CLAUDE.md` → Key decisions (41 rows; the rationale column *is* the trap) |
| The two deferrals ADR-009 named and did not answer | `ADR-010` (Draft) — and the Schedule overlap policy it opened, in `phase4-backlog.md` §2 gotcha 6 |
| What shipped when | `git log` |

---

## Session log

Docs-only from 2026-08-04 **to 2026-09-03**; **2026-09-04 breaks that run** — P6 is the first
application-code change since `5c7fbdf` (2026-08-28), and it spans five services. Verdicts live in
ADR-009's review log; this table is only *what a session produced*.

| Date | Session produced | Commits |
|---|---|---|
| 2026-09-04 | **🔷 P6 / BUG-005 BUILT — the first code of Phase 4's pre-migration queue.** `schema_version` 2 → 3 across five services; 598 tests green (API 251 · **cross-service contract 29** · Go · playwright 168 · llm 101 · coordinator 49). Two typed producer models (`api/app/messages.py`) with all seven dict-construction sites routed through them; `artifact_id` on the wire, `job_id` off it; `run_id` optional and absent on the crawl lane; stage-named objects; `latest/` deleted from three workers and the delete path; the result-consumer parse-order move. **The ADR-011 §6 contract test exists** (`contracts/`) and was mutation-checked in both directions — it catches a reverted Go guard and a stale Go fixture. **Three findings not in the ADR**, all from the code rather than the docs: 🔴 the Go worker's `history/` write **depended on `latest/`** via `CopyObject`, so the removal is a rewrite there, not a deleted line; dropping the timestamp from screenshot keys makes redelivery **idempotent**, shrinking **BUG-004** by construction the way `latest/`'s removal shrank BUG-007; and 🔴 **ADR-011 decides nothing about deployment ordering, and no safe order exists** — owner's call taken to hard-cut against a drained stream, which is why the version was bumped. ⚠️ **The production `latest/` sweep did not ship** — written as `api/scripts/sweep_latest_objects.py`, dry-run by default, owner-authorised. **Then the deploy rehearsal found two pre-existing bugs, neither from P6.** 🔴 **BUG-012 filed** — `reenqueue_stalled` deletes `crawl_pages` while `crawl_queue` still references them, so the **coordinator crash-loops on startup** and cannot self-clear (31 restarts, 194 stalled items observed). Owner **declined the §3 override**; filed to §3, dissolves cleanly. **Its unit test pins the broken order as correct** — the second instance of that shape after BUG-005's. 🔴 **BUG-006 addendum** — `llm-worker` imported `httpx` without declaring it; the provider SDKs jumped majors and moved to **`httpx2`**, so a rebuild produced an image with no `httpx` and the worker crash-looped. Fixed (`1c456a4`); **lockfiles, not scanning, are BUG-006's real fix**, and the SDK majors are now unpinned and undecided | `81afbb9`, `d4330f4`, `1c456a4`, `427f7ce`, `5d91134` |
| 2026-09-03 ⬅ *clock, second session* | **🔷 ADR-011 reviewed and promoted to `Accepted` (owner decision) — P6's last design dependency is closed and nothing blocks the pre-migration queue.** Its one open item confirmed by name: **the crawl lane stays in scope**, so P6 changes all three lanes. Promotion sweep across ten files (ADR-002 → `Partially Superseded`; both ADR indexes; `CLAUDE.md`; backlog; `open-bugs.md`; `temporal-full-migration.md`; `docs/process/`). **Three stale premises corrected, none found by reading the passage that was marked** — the `run_id` recommendation ADR-011 rejects by name, `latest/` listed under *what does NOT change*, and 🔴 **an undeclared reversal of a clause in the Accepted ADR-009 §8d**. The third is a new class and has its own section above; the method lesson is in *Trimming the docs*. Also recorded: the `latest/` sweep is a **production data deletion**, owner-authorised; ADR-003's `result_path` shape has drifted but is **not** superseded | `a0714f6` |
| 2026-09-03 ⬅ *clock, first session* | **🔷 Owner decisions taken on artifact identity, written up as ADR-011 (Draft)** — artifacts key on the producing row via a lane-neutral `artifact_id`; `job_id` leaves the wire; objects named by **stage**; **`latest/` removed**; the crawl lane's fabricated `run_id` removed. **This closes P6's last design dependency.** Two findings that changed the design mid-session, neither from reading the docs: a flat `history/{artifact_id}.{ext}` would have **silently reintroduced the collision** (the LLM worker hardcodes `ext="json"`, so an `output_format=json` job's extraction overwrites its own scraped page — only the timestamp prevents it today, and only because LLM calls are slow); and **`run_id` cannot be the universal key**, because the coordinator fabricates one with `uuid4()` for a lane that creates no `job_runs` rows, so `crawl_pages.id` is in the message twice — once honestly, once as `job_id`. **BUG-011 filed as P9**: stale-pending recovery silently skips every batch run, under a comment asserting the case is impossible — **BUG-005's fix does not close it**, since that removes the cause of stuck items rather than the hole in the net. A contracts package was weighed and **not taken**, recorded in ADR-011 §6 with the trigger to revisit (pipelines). Also verified: change detection is **path-agnostic** — "which run came before" is a SQL query and the differs take opaque paths, so the convention change needs **no backfill** | `fa3c18d`, `b00e0f2` |
| 2026-09-08 | **🔷 Two owner decisions taken: ADR-009 promoted to `Accepted`, and the conditional-execution PRD numbered `PRD-019`** (sort-order cost accepted; index order is not build order). **Both stale companions redrawn** — `temporal-full-migration.md` (five 🔴 divergences resolved, plus three the redraw found: Web UI exposure, tenancy, the SPA contract) and `workflows-scoping.md` (six 🔴 cleared, §9's open questions turned into a table of answers). ADR-009's three pre-redraw notes **corrected in place**, which is what surfaced that the second document was owed a redraw at all. **PRD-016 carry-back pass** — four items ADR-009 owed it, no decision changed. Draft caveat cleared from the ADR header and eight downstream documents. **The four lane-blind admin meters recorded** as cutover gotchas — which also caught gotcha 3 still describing the rejected NATS bridge. **ADR-009's D5 closed**: both deferrals decided by the owner and written up as **ADR-010** (Draft), which opens one new item — a Schedule overlap policy | `1d94d5d`, `041921d`, `33d58f2`, `c76edf8`, `c8f381f` |
| 2026-09-07 | This handoff condensed 250,550 → ~15,000 chars (−94%); `phase4-backlog.md`'s header change log 9,882 → ~2,500. Corrected the `prephase4` hash back to `1965953` | `2404193` |
| 2026-09-06 | `CLAUDE.md` cleanup — the duplicated ADR-009 summary stripped, 93,141 → 27,527 chars (−70%); backlog's ADR-009 row 16,500 → 914 | `530fca3`, `e201980` |
| 2026-09-05 | Both closing blocks reviewed (14 corrections, no decision changed) — **the section review closes**; then ADR-009 condensed, review log 476 → 83 lines as `## Review status` | `3e7f32e`, `21fadd2`, `3b91bab` |
| 2026-09-04 | §17 reviewed; `ADR-002 §8` → **§4** corrected across five live docs | `87d2396` |
| 2026-09-03 *(doc timeline — see the drift note above; this row is an earlier session)* | §16 reviewed — the sequence becomes **named, not numbered** | `400cda7` |
| 2026-09-01 → 09-02 | §14 and §15 reviewed | `9b2df55` |
| 2026-08-28 | §13 reviewed; BUG-010 filed; one live fix shipped | `e4a19fc`, `5c7fbdf`, `205acd4` |
| 2026-08-26 | §10 and §11 reviewed; BUG-009 filed; consistency sweep for the §8/§9 reversals | `4d27475`, `7b9f9d5`, `1770b70`, `a40b89b` |
| 2026-08-25 | §8's two blockers closed (4 owner calls); **P8 filed**; BUG-008 filed | `2caaddc`, `9f37992`, `7e6d9ec` |
| 2026-08-23 | §9 **reversed** — the NATS bridge rejected, workers port first | `72432b2` |
| 2026-08-17 | §8 **reversed** — the meter measures bytes on disk; BUG-007 filed | `2849da3` |
| 2026-08-10 | §4–§7 reviewed; §7 gains a fourth mechanism | `8a31ec5` |
| 2026-08-08 | §2, §3, §12 reviewed (§12 **reversed**); P7 filed | — |
| 2026-08-04/05 | ADR-009 drafted; BUG-005 and BUG-006 filed | — |

---

## Historical notes not recorded elsewhere

**Clerk production cutover (2026-07-03).** The load-bearing facts are in `CLAUDE.md` → Deployment
(key split, manual grey-cloud DNS, own OAuth credentials, the `JWK_FAILED_TO_LOAD` trap, the
rotation runbook). Two things only recorded here:

- **Fresh start**: prod Postgres app tables were truncated and MinIO `scrapeflow-results` emptied
  on 2026-07-03 (schema + `alembic_version` preserved, bucket kept) — a prod Clerk instance issues
  **new `sub` IDs**, so `users` rows keyed on the dev `clerk_id` would have been orphaned. The
  Fernet keys (`llm-key-encryption-key`, `credentials-encryption-key`) were **not** rotated, which
  is why encrypted-at-rest data survived.
- **Loose end**: GitHub OAuth custom credentials are still unconfigured. Only needed if GitHub
  sign-in is offered; Google is done.

**Q5 / Q6 / Q7 are closed in code and production**; Q8 is closed as do-not-fix (the code holding
it is deleted by the migration). Status blocks and the reusable NATS consumer-recreate procedure
are in `open-questions.md`; the recreate rules are also a Key decisions row in `CLAUDE.md`.

---

## ⚠️ Trimming the docs — the method, and what it keeps catching

Used three times now (ADR-009's review log, `CLAUDE.md`, this file). **Run it in this order; the
verification is the part worth trusting, not the reading.**

1. **Forward sweep** — extract distinctive tokens from the text you mean to cut (backticked
   identifiers, `file:line` refs, figures, IDs) and confirm each has a home in the authoritative
   doc. ⚠️ **Match across newlines** — a line-wrapped `ADR-002\n§8` survived one line-based pass.
2. **Only then cut.**
3. **Reverse sweep** — check that nothing which left is now homeless, and re-verify every path
   and link in the result.

**What it has caught, none of it by reading:**

- ⚠️ **The copy being cut may be the one holding the correction.** The Backlog said Dependabot
  scanned "3 of 6" manifests; the ADR-009 bullet had corrected it to **7**. Deleting the corrected
  copy would have left the wrong number standing alone. Fixed to **3 of 7** (2026-09-06).
- ⚠️ **Line-number pointers rot faster than anyone re-checks them.** `result_consumer.py:125` had
  already drifted to ~`:126`. Keep the fact, drop the line number.
- ⚠️ **Two near-identical hashes are in play; do not "correct" one into the other.** `5c7fbdf`
  (2026-08-28) is the crawl page status-filter fix, cited in ADR-009. `5cb8c7f` (2026-07-01) is
  the **Q8 LLM-dispatch-loop source guard**, cited in `open-questions.md`. Both are correct where
  they stand. Verified 2026-09-07.
- 🔴 **A sweep can manufacture a defect, and this one did.** The 2026-09-06 pass reported
  `CLAUDE.md`'s `prephase4` hash `1965953` as wrong "because the tag resolves to `473fb68`", and
  dropped it. **`1965953` was correct.** `prephase4` is an *annotated* tag, so
  `git rev-parse --short` returned the **tag object's** SHA; the tag points at commit `1965953`.
  `git log -1 473fb68` then showed the right commit — git silently dereferences the tag object —
  which made the bogus finding look confirmed. ⚠️ **Verify a hash with the command that answers
  the question you are actually asking** (`git rev-list -n1 <tag>` for "which commit"), and treat
  a sweep finding as a hypothesis until a second, differently-shaped check agrees. Caught
  2026-09-07 while re-verifying this file; nothing downstream was affected, because the same pass
  had dropped the hash from `CLAUDE.md` rather than writing the wrong one in.

**What must NOT be trimmed further:**

- ⚠️ **`CLAUDE.md`'s 41 Key-decisions rows, in full.** The rationale column is not explanatory
  padding — it is the trap that stops the bug returning, and cutting it is the one edit that would
  make that file worse. Specifically: `xvfb-run`-as-pid-1, the **`nats consumer info --json`**
  requirement, the `llm_max_retries=0` pin and why the Q6 pin and the timeout bump are *safe
  together and unsafe apart*, the aiohttp-vs-`S3Error.code` split, the Go `*uploadError` scoping,
  and bot-wall posture being the **inverse** of the LLM classifier.
- ⚠️ **ADR-009's eight `✅ Settled on review` blockquotes** (103 lines total). They sit directly
  beneath the detail they summarise, which is the right home for a summary.
- ⚠️ **Repeated facts across ADR-009 sections** (BUG-005 ×15, the ≈2.6 h horizon ×15, the
  light-worker rule ×6) were checked and left. Each is a **cross-reference doing local work**;
  removing them would make sections stop being self-contained, which is worse than the repetition.

**Done, in order:** ADR-009's review log (2026-09-05) → `CLAUDE.md` (09-06) → this handoff and
`phase4-backlog.md`'s header change log (09-07). Nothing obvious is left; the remaining large
documents are load-bearing content rather than duplicated summary.

### The same discipline, applied to reconciling rather than trimming (2026-09-08)

Redrawing two documents against the Accepted ADR turned up four things, **none of them by reading
the passages that were marked**:

- 🔴 **A 🔴 marker is not a fix, and it outlives the thing it was meant to prompt.** The rejected
  NATS bridge survived in **five** places — `temporal-full-migration.md`, `workflows-scoping.md`
  (twice), ADR-009's own §14 table, and `phase4-backlog.md`'s cutover gotcha 3. Some were marked;
  the marked ones still read as live recommendations underneath the marker, and the gotcha was not
  marked at all. **Search for the rejected thing by name, not for its markers.**
- ⚠️ **One instruction covering two documents gets acted on for one.** §14's disposition said
  *"🔴 markers now, one redraw after the review closes"* about `temporal-full-migration.md` **and**
  `workflows-scoping.md`. Only the first was on any list. Nothing in either marker would ever have
  surfaced the other.
- ⚠️ **A reversed premise under an unchanged conclusion is invisible.** Two PRD-016 passages argued
  from ADR-009 §8's *"only the final artifact is charged"* — reversed 2026-08-17 — and reached the
  right answer anyway. They read as sound because they *are* sound; nothing flags them until
  someone reuses the stale rule on a lane where it gives a different answer. **Grep for the
  withdrawn wording, not for wrong conclusions.**
- ⚠️ **Line pointers rotted again**, second confirmed instance: `routers/crawls.py:70` had drifted
  to `:75`. Five of six spot-checked refs were exact, which is the trap — a mostly-accurate set
  invites trusting the rest. Dropped the numbers, kept the fact, per the standing rule.

### The same discipline, applied to promoting an ADR (2026-09-03)

Promoting ADR-011 and sweeping its knock-ons through ten files turned up three stale premises.
**None was found by reading the passage that was marked** — the standing lesson, third confirmation
— but one of them is a class the method did not previously cover:

- 🔴 **An immutable record can be displaced from OUTSIDE, and it cannot say so itself.** ADR-011 §4
  reverses ADR-009 §8d's *"`latest/` is kept as-is"*. ADR-009's **Reversed or withdrawn** table —
  the thing this handoff tells you to check first — records only what *its own review* changed, and
  the ADR is immutable, so nothing can be added to it. **A `git grep` for the withdrawn wording
  finds §8d reading as live and correct.** The only place the displacement can live is the
  *superseding* ADR's header. ⚠️ **So when promoting an ADR, grep for what its decisions
  contradict, not only for what it supersedes by name** — ADR-011's `Supersedes:` line said
  "ADR-002 §4" and was complete on its own terms while silently reversing a second record.
- ⚠️ **A reversal usually splits a call in half, and the halves travel together.** §8d settled
  *charge one copy* **and** *keep `latest/` for now* in one owner call. Only the second is reversed.
  Recording "§8d is reversed" would have been as wrong as recording nothing — the tracker entry in
  `open-bugs.md` had them in a single bullet, which is how they would have gone together.
- ⚠️ **A superseded document's stale copies are not all in the ADRs.** `docs/process/tech-lead.md`
  still described ADR-002 as the current contract for MinIO paths — a persona starter prompt, not
  a design doc, and on nobody's list. **Sweep `docs/process/` too.**
