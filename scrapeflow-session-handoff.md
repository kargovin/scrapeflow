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
| Open bugs (BUG-004 → BUG-010) | `docs/project/open-bugs.md` |
| Open questions (Q1–Q8) | `docs/project/open-questions.md` |
| Usage findings (UF-00x) + test counts | `docs/project/usage-findings.md` |
| PRDs | `docs/project/phase4-prd/` (PRD-016 only, so far) |
| Feature scoping + engine comparison (redrawn 2026-09-08) | `docs/project/workflows-scoping.md` |
| Change inventory + migration sequence (redrawn 2026-09-08) | `docs/project/temporal-full-migration.md` |
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
```

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

## Current state — as of 2026-09-08

Phases 1–3 complete and production-verified at `scrapeflow.govindappa.com`. **Phase 4 is in
progress, and Phase 4 *is* the Temporal durable-workflows migration.** No build work has started —
but the design phase that was blocking it has now closed (below).

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

⚠️ **The design phase is effectively closed. Everything still open is code, plus one PRD.** Every
documentation debt ADR-009's review created has been discharged: both stale companions redrawn,
PRD-016's four carry-backs landed, the conditional PRD numbered, the lane-blind meters recorded,
D5 closed. What is left is the pre-migration queue and PRD-019.

**What is blocking: nothing.**

### Outstanding, in rough order

1. **Write PRD-019 — conditional execution (layer A).** ✅ Numbered 2026-09-08 (owner's call) and given
   its `phase4-backlog.md` §2 row; **the document itself is unwritten.** It owes **four** things,
   all on that row: the Validate-precedent brief and the replay constraint (14c), the halt-early
   block B cannot build for itself (§4), and run-level failure notification (15f). ⚠️ **It sorts
   last and is required first** — before PRD-018, which cannot ship without its primitive. The
   sort-order cost was accepted rather than engineered around; index order is not build order in
   this chain.
2. **The pre-migration queue is the entry condition for any build work** (16e):
   **P6 → P8 → P7 + BUG-007**, then engine up. `phase4-backlog.md` §1 is its source of truth.

### Git / deploy state

- **Deployed code is `b110591`** (2026-07-28).
- **`5c7fbdf`** (the crawl page status-filter fix, 2026-08-28) is committed on `develop`,
  **not deployed, not on `main`**. Everything on top of it is docs.
- ✅ **The owner's call of 2026-08-28 stands: `main` is deliberately NOT fast-forwarded** —
  pushing it starts a push to the prod server, so a fast-forward is a **release**, not a tidy-up.
  Do not do it at session end; wait to be asked.
- **As of 2026-09-08, freshly fetched:** `develop` is **20 ahead** of `origin/develop` and 0 behind;
  `main` is **45 behind** `develop` and 0 ahead. Nothing pushed this session.
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
| Whether a note you are holding is now wrong | ADR-009's **Reversed or withdrawn** + **Amended as a knock-on** tables |
| Phase 4 scope, sequencing, what is do-not-fix | `phase4-backlog.md` (§1 queue · §2 migration · §3 **do NOT fix** · §4 survives) |
| A bug's root cause and fix plan | `open-bugs.md` |
| Why a production trap exists | `CLAUDE.md` → Key decisions (41 rows; the rationale column *is* the trap) |
| The two deferrals ADR-009 named and did not answer | `ADR-010` (Draft) — and the Schedule overlap policy it opened, in `phase4-backlog.md` §2 gotcha 6 |
| What shipped when | `git log` |

---

## Session log

Docs-only from 2026-08-04 onward — **no application code has changed since `5c7fbdf`**. Verdicts
live in ADR-009's review log; this table is only *what a session produced*.

| Date | Session produced | Commits |
|---|---|---|
| 2026-09-08 | **🔷 Two owner decisions taken: ADR-009 promoted to `Accepted`, and the conditional-execution PRD numbered `PRD-019`** (sort-order cost accepted; index order is not build order). **Both stale companions redrawn** — `temporal-full-migration.md` (five 🔴 divergences resolved, plus three the redraw found: Web UI exposure, tenancy, the SPA contract) and `workflows-scoping.md` (six 🔴 cleared, §9's open questions turned into a table of answers). ADR-009's three pre-redraw notes **corrected in place**, which is what surfaced that the second document was owed a redraw at all. **PRD-016 carry-back pass** — four items ADR-009 owed it, no decision changed. Draft caveat cleared from the ADR header and eight downstream documents. **The four lane-blind admin meters recorded** as cutover gotchas — which also caught gotcha 3 still describing the rejected NATS bridge. **ADR-009's D5 closed**: both deferrals decided by the owner and written up as **ADR-010** (Draft), which opens one new item — a Schedule overlap policy | `1d94d5d`, `041921d`, `33d58f2`, `c76edf8`, `c8f381f` |
| 2026-09-07 | This handoff condensed 250,550 → ~15,000 chars (−94%); `phase4-backlog.md`'s header change log 9,882 → ~2,500. Corrected the `prephase4` hash back to `1965953` | `2404193` |
| 2026-09-06 | `CLAUDE.md` cleanup — the duplicated ADR-009 summary stripped, 93,141 → 27,527 chars (−70%); backlog's ADR-009 row 16,500 → 914 | `530fca3`, `e201980` |
| 2026-09-05 | Both closing blocks reviewed (14 corrections, no decision changed) — **the section review closes**; then ADR-009 condensed, review log 476 → 83 lines as `## Review status` | `3e7f32e`, `21fadd2`, `3b91bab` |
| 2026-09-04 | §17 reviewed; `ADR-002 §8` → **§4** corrected across five live docs | `87d2396` |
| 2026-09-03 | §16 reviewed — the sequence becomes **named, not numbered** | `400cda7` |
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
