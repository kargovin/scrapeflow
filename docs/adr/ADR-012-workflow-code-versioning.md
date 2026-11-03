# ADR-012: Workflow-Code Versioning — `patched()` Plus a Replay Test That Gates the Build

**Status:** **Accepted 2026-11-03** (owner). The *decision* is the owner's, taken 2026-10-12 (*"go with patched
and the replay test"*) and built at C.13; the text, written by the Tech Lead, was reviewed and promoted
2026-11-03. Immutable from here — a change is a new, superseding ADR.
**Date:** 2026-10-12

> **Review (owner, 2026-11-03).** Four corrections: the Webhook block's window (≈2.9 h, not 2.6 h); the replay
> test covers seven workflows, not two; the drain window is per workflow (a scheduled tick can park 24 h); Scrape's
> `schedule_to_start` is no longer 60 s. One rule added (§2.3): **retiring a patch retires its histories** — found
> by checking the corpus: ~15 job/pipeline histories predate `host-slot` and would fail every build once its old
> branch is deleted, with no permitted way out.
**Deciders:** @karthik
**Resolves:** ADR-009's deferral-table row *"Mechanism for versioning our workflow code (Worker Versioning
vs `patched`)"* — deferred there on purpose, to *"the first workflow-code deploy that must survive in-flight
runs"*. That deploy is C.14's: a Webhook block parks a run for ≈2.9 h (its five attempts, `catalog.py`). Not a supersession —
ADR-009 chose nothing here.

---

## 1. The problem

Temporal continues a run by replaying the workflow body against the run's history. A deploy that changes
the body — an activity call added, removed or reordered, a branch that decides whether one happens — makes
an in-flight run's replay diverge (`NondeterminismError`), and its workflow task then retries forever: the
run stays `running`. Activity code is not replayed (recorded results are read back), so it is out of scope —
except its **input/output shapes**, which are recorded in the history too.

## 2. Decision

1. **Workflow-body changes that alter the command sequence are wrapped in `workflow.patched("<id>")`**,
   old branch kept. Once every run started before the deploy has finished, the wrapper becomes `workflow.deprecate_patch("<id>")` and the old branch goes;
   once no run carrying the marker remains, the line goes. **The drain window is per workflow: the longest
   one of its runs can live**, not the pipeline figure — `PipelineWorkflow` ≈3 h (its run budget with a Webhook
   block); `ScheduledJobWorkflow` up to 24 h parked for a slot (`WAIT_CAP`) plus its job; Monitors (PRD-018) up
   to their approval wait. A workflow's new runs take the new branch from the deploy on — a Schedule's tick is a
   new run, so a year-old Schedule's next tick is already on the new branch.
2. **A replay test gates the api image build.** `api/tests/test_workflow_replay.py` replays every history in
   `api/tests/replay/histories/` against every workflow the worker registers (seven since F.4:
   `PipelineWorkflow`, `JobWorkflow`, `BatchWorkflow`, `CrawlWorkflow`, `ScheduledJobWorkflow`,
   `WebhookDeliveryWorkflow`, `WebhookSweepWorkflow`); `.github/workflows/build-push.yml`
   runs it (no services, `--noconftest`) and `build-api` depends on it. The workflow worker ships in the api
   image, so a failure blocks exactly the image that would strand runs.
3. **The corpus only grows.** Histories are recorded from real compose runs (`tests/replay/export_history.py`)
   or the mocked-activity harness (`tests/replay/record_harness.py`); **a history is never regenerated or
   edited to make the test pass.** Add one for each new behaviour of the workflow body.
   *One exception, before release only:* a workflow that has never been deployed has started no run
   anywhere, so its histories protect nothing yet and may be replaced. Used once — `JobWorkflow`'s
   `load_job_llm` → `read_llm_settings` rename, 2026-10-12, owner's call, four histories re-recorded.
   From a workflow's first release on, the rule above is absolute.
   **Retiring a patch retires its histories.** The `deprecate_patch` deploy moves every history that carries no
   marker for that patch — it exercises only the deleted branch, which no run can take any more — into
   `histories/retired/<patch-id>/`, which the test does not replay. Moved, never edited or deleted, in a commit
   that names the patch. (`host-slot`'s retirement moves the pre-E.9 job and pipeline histories; crawl's are
   unaffected — `CrawlWorkflow` was born with the host slot and never patched.)
4. **Activity contracts change additively only.** A new field is optional with a default on both sides; a
   removal or rename is add-new → release → drain → remove-old. Neither mechanism protects against a
   non-additive change: the recorded result no longer decodes (the replay test catches that), and the
   workflow worker and the activity workers are separate Deployments that never roll at the same instant.

## 3. Alternatives

- **Worker Versioning** (ADR-009's "stated default") — each build is a version and a run stays on the one it
  started on. Rejected *for now*: the old version's workers must stay up until its runs drain, which the
  production shape cannot do (one `RollingUpdate` Deployment, `replicas: 1`, Flux image automation replacing
  the pod within a minute). It needs Temporal's Kubernetes worker controller or a two-Deployment release
  procedure, plus server-side enablement — a new infra component or a manual step every release. And
  Monitors (layer B) run for days, where patching is the usual answer anyway, so the discipline is owed
  regardless. Revisit if the controller is adopted.
- **Drain before every deploy** — refuse new runs, wait out the longest budget, deploy. Workable for 3 h,
  not for Monitors; and it turns every release into a scheduled outage of the lane.

## 4. Consequences

- **Deploys stay as they are.** The workflow worker's `RollingUpdate` starts the new pod before stopping the
  old one, so the gap with no poller is short. This matters because every catalog block — the Webhook
  included — has a 60 s `schedule_to_start` per attempt (Scrape's is longer since E.9, to cover the host slot), which ADR-009 15c meant to leave unset for Webhook:
  an attempt scheduled during a gap longer than that fails the block. Accepted for now; a deploy that keeps
  the worker down longer must first switch the lane off (`PIPELINES_ENABLED=false`).
- **A forgotten wrapper fails the build, not production** — provided the corpus covers the path. Verified at
  build: an unwrapped new activity call and a swapped call order each fail 9 of 9 histories; the same call
  wrapped in `patched()` passes; a new required field on `ScrapeOutput` fails every history with a Scrape.
  Moving an activity to another task queue is replay-safe (Temporal compares activity types, not queues).
- **Branches accumulate** until cleaned up; the `deprecate_patch` step is owed per patch.
- `config_schema_version` (the user's definition) and this ADR (our code) stay separate problems, as
  ADR-009 §6 put it: pin the recipe; patch the interpreter.
