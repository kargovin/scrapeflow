# Builder brief — delegated build, one builder subagent per backlog task

Written for Group M (2026-11-20), group lines updated for Group N (2026-11-22), Group O and Group P (2026-11-24); the orchestrator points each builder at this file. Update the group-specific lines (ADRs, patch ids, baselines) when a group starts.

You are building ONE task of the implementation backlog, start to finish, then reporting back to the
orchestrator. The owner has delegated the build: no explaining, no asking — build, test, commit.

## Where things are
- Repo: `/home/karthik/Documents/Claude/scrapeflow` (branch `develop`). `CLAUDE.md` is the architecture.
- **Your task spec:** `docs/project/phase4-implementation-backlog.md`, section `#### <task>` (grep `^#### N\.` etc.),
  plus its group header (`## Group …`, including any *Carried from M's build* notes) and the cross-cutting rules in `### Traps that span groups`
  (≈ line 3154 — read the whole list) and `### Releases` / `### Bugs and backlog items` just above it. The status table row for your
  task is near line 144.
- Design of record (Accepted, implement against them, never edit): `docs/adr/ADR-015-delivery-sinks.md`
  (§2d destination test, §3 egress, §4 the delivery worker, §5 duplicate-safe keys, §6 time budgets, §7 size),
  `docs/adr/ADR-014-block-outcomes-stop-rollback-alerts.md` (§7 rollback), `docs/adr/ADR-016-monitors.md`,
  `docs/adr/ADR-017-approval-expiry-notice.md`, `docs/adr/ADR-012-*` (workflow versioning). Product:
  `docs/project/phase4-prd/PRD-017-delivery-sinks.md` (slice 3 acceptance criteria — email). Patch ids already in
  `PipelineWorkflow` that a sink rides: `sinks`, `rollback` (M.8), `email-delivery` (M.14), `approval`, `approval-settled` (N); O.1's postgres sink added no patch.
- Read the existing code a task touches before changing it; follow the patterns of the L/K/J tasks
  (e.g. `git log --oneline -40` and `git show <hash> --stat` for similar earlier steps).
- Infra repo (infra tasks only): `/home/karthik/Documents/govindappa/govindappa-k8s-config`.

## Hard rules
- **Commits:** on `develop` only. One reviewable step per commit (a task may be several commits; never two
  tasks in one). Each commit: suites green, with its tests. Message = short subject + at most a few terse
  bullets, factual. **No `Co-Authored-By` trailer, no Claude attribution.** Never push. Never touch `main`.
- **Never** run anything against production (no kubectl to the cluster except `--dry-run=client` style local
  checks if the task allows; no `flux`, no pushes). Infra-repo changes: commit locally, never push.
- **Tests run in Docker, never on the host.** From `./docker`: `docker compose ps`; if the stack is down,
  `docker compose --profile gate up -d`. API: `docker compose exec api uv run pytest tests/ -q -x -p no:cacheprovider`
  (bare `python` in the container has no packages). Replay test: `tests/test_workflow_replay.py`. Contracts and
  worker tests: commands in `scrapeflow-session-handoff.md` → `## Commands`. Go: `cd http-worker && go test ./...`.
  Baselines: the handoff's current state (after O: API 1552 incl. replay 125, contracts 227, delivery worker 243 + 23 skipped).
- **The workflow worker does not hot-reload** — `docker compose restart workflow-worker` after `app/` changes it
  runs. Rebuild `llm-worker-temporal` after any `llm-worker/` change.
- **Migrations:** model first, then autogenerate with the api paused (handoff `## Commands` → Migrations).
  The generated file is root-owned: `chown` it inside the one-off container (or edit via the container) BEFORE
  editing; **never unpause the api until the edited file is upgraded** (otherwise hot-reload applies the raw file
  and drops `idx_webhook_deliveries_dedup`). Delete that dedup false positive op from the file; name every FK.
  Check the round trip (downgrade -1 / upgrade head) and `alembic check`. A migration that ALTERs/DROPs a
  column the quota views read must drop+recreate both views (see CLAUDE.md *Run-counting views*).
- **Workflow code:** every new `PipelineWorkflow` path under its `workflow.patched("<id>")` (the ids the task names); add new recorded histories (`api/tests/replay/export_history.py`, `record_harness.py`); **never
  edit or regenerate an existing history**. A new workflow class joins the worker registration and
  `workflows=[...]` in `api/tests/test_workflow_replay.py`. Activity contracts change additively only.
- **Tests share the dev database**: scope delete/cutoff tests to their own rows. **Mutation-check** the
  load-bearing tests of your task (break the code, see the test fail, restore): use `PYTHONDONTWRITEBYTECODE=1`
  and clear `__pycache__`; afterwards sweep any MinIO objects with no ledger row and orphaned test users.
- **Code style:** no rationale/decision prose in docstrings or comments — terse trap notes only; decisions go
  in docs. No code added only to be removed later. Treat ScrapeFlow as multi-tenant and used by others — no
  homelab shortcuts. Never mention third-party platform names in code/docs. Bundle frontend assets locally;
  new SPA pages use `useApi()`, never a cached token.
- **Open calls:** make them yourself, keep them reversible, and record them as "my call" in the task's backlog
  status row. If you hit a genuine **product decision, something irreversible, or a contradiction between the
  task text and an Accepted ADR** you cannot resolve, stop at a clean commit and report it instead of guessing.
- Don't write elapsed-time claims ("for a year") into code/docs; write dates.

## Bookkeeping (part of your task)
- Update your task's row in the backlog status table: `🔧 **Built <today’s date, from `date +%F`>** (`hash`, `hash`). My calls: …`
  following the style of the L rows (L.1–L.11). Same commit as your last code step or one small `docs:` commit.
- If your task fixes/changes something CLAUDE.md states as current (a Key-decisions row), correct that row.
- Do NOT edit `scrapeflow-session-handoff.md` — the orchestrator does that.

## Report back (≤ 250 words, no code dumps)
1. Commits in order: `hash subject`.
2. Tests: what you ran and the final counts (API / replay / contracts / worker), all green?
3. Mutation checks: what was broken, killed or not.
4. My calls (one line each), anything beyond the task text, anything deferred.
5. Environment state you changed (images rebuilt, containers restarted, compose up/down).
6. Anything the next tasks must know (new helpers, names, traps).

## Carry-forward notes
Before starting, read `carry-forward.md` (the orchestrator's scratchpad; it names the path) for the items addressed to your task.
Before reporting, append a `## From <task>` section to it: commits, suite count, new names/helpers, and one bullet per
later task that must know something (address it as **<task>:**). Keep it terse.
