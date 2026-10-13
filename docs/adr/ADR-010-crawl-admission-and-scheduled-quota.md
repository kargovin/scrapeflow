# ADR-010: Scheduled-run Quota Admission, and Sitemap Origin Scope

**Status:** **Draft.** Both decisions below were taken by the owner on 2026-09-08; this document
is my write-up of them and has not been reviewed. Promoting it to Accepted is a separate step.
**Date:** 2026-09-08
**Deciders:** @karthik
**Resolves:** the two open rows in
[ADR-009](./ADR-009-workflow-engine-temporal.md#deliberately-not-decided-here)'s **"Deliberately
not decided here"** table — *"What a quota-blocked scheduled run does"* (ADR-009 §7) and
*"Whether sitemap entries are origin-restricted like extracted links"* (ADR-009 13d).
**Supersedes:** nothing. ADR-009 is Accepted and immutable; it **deferred** these two questions by
name rather than answering them, so this is a resolution, not a reversal.
**Amended 2026-10-13 (owner, while still Draft):** §1's `monthly_runs` row changed from *park* to
*skip this firing*, and the rider rewritten to match. The original row and the reason it was
reversed are kept in [§1 → *Amendment 2026-10-13*](#amendment-2026-10-13--monthly_runs-skips-it-does-not-park).
The same review filed **BUG-023** (quota check and run insert are not serialized per user).

---

## Why these two are in one ADR

They have nothing in common as problems. They are together because they are the two items ADR-009
named as open, and both are **needed before the batch-and-crawl cutover** — one because the crawl
step builds the frontier admission path, the other because the schedule-and-webhook cutover is the
step after it and its design constrains what the counting view is asked for.

Both are small. Neither justifies its own document; leaving them in a review log would repeat the
failure ADR-009 14d is about — a decision with no home is a decision that gets rediscovered.

---

## 1. A quota-blocked scheduled run waits only when the meter frees itself soon

**Decision: the Temporal Schedule fires unconditionally. The workflow's first step consults the
run-counting view and then behaves differently per meter.**

| meter | on breach | why |
|---|---|---|
| `concurrent_jobs` | **park** on a durable timer, re-check | clears in minutes without the user doing anything. ADR-009 §8: a parked run holds no slot, so parking cannot deadlock against itself |
| `monthly_runs` | **skip this firing** — the workflow ends at once, the skip is recorded where the user can see it, and the Schedule's next tick runs normally | clears on its own, but only at the month boundary — weeks away. Parking that long turns every tick of every scheduled job into a parked workflow, all released together on the 1st (*Amendment 2026-10-13* below) |
| `storage_bytes_used` | **fail the run now**, with an error naming the remedy | **never clears on its own.** The user must delete artifacts. Parking would hide an actionable condition behind an indefinite wait |

**Why the check cannot live in the Schedule.** Temporal offers `SKIP`, `BUFFER_ONE`, `BUFFER_ALL`,
`CANCEL_OTHER` and `TERMINATE_OTHER`. None of them is this. As the *quota gate*, `SKIP` would
discard a firing because an earlier one is still running, not because of quota — a user briefly at
their concurrency ceiling would silently lose a run, a user-visible regression PRD-016 R5 forbids.
More fundamentally, every overlap policy reacts to *a previous execution still running*, whereas
this gate reacts to *the account's meters*, which a Schedule cannot read.

**Why per-meter rather than one uniform rule.** A uniform "park with a timeout" makes the user wait
out a timeout to be told something that was knowable when the run started; a uniform "park forever"
parks a recurring job indefinitely and silently on the one meter that will never free itself. The
question to ask of a meter — including a fourth, if one is ever added — is **does the breach clear
without user action, and soon?** Yes → park. Clears on its own but only weeks away → skip this
firing. Never on its own → fail.

This composes with ADR-009 §8d's headroom buffer, which already refuses to *start* runs near the
storage ceiling. The buffer is the early guard; this is what happens when a run gets past it and
the ceiling is crossed anyway.

**What "recorded where the user can see it" must not be.** ⚠️ **Not a `job_runs` row** (nor a
`pipeline_runs` row for a pipeline schedule): `quota_run_units` counts **every** such row whatever
its status (migration `9db1dcda44f7`), so recording the skip as a run would spend a monthly unit to
say that no monthly units are left. The mechanism — a field on the schedule's owning row, an event
the SPA reads, a webhook event — is F.1's to choose; the two requirements are *visible to the user*
and *invisible to the meter*.

### Amendment 2026-10-13 — `monthly_runs` skips, it does not park

The row first read **"park on a durable timer, re-check — clears at the month boundary. Long, but
bounded and self-clearing — and it is exactly what the current scheduler does."** Reversed by the
owner before promotion, on this case:

```
A user hits their 1,000-run limit on Oct 28. Their scheduled jobs keep firing until Nov 1.

  one hourly job               ≈ 72 parked workflows
  10,000 scheduled jobs        ≥ 10,000 parked workflows (no overlap policy reaches across jobs)

Nov 1 00:00 — all wake together
```

- **The quota is not what breaks** — provided each woken run re-checks the meter under a per-user
  lock (BUG-023), only the month's limit runs and the rest park again. **What breaks is everything
  around it:** the backlog consumes the *next* months' quota ahead of the user's other work, every
  parked workflow is a timer plus history in Temporal's database that one tenant can grow without
  limit, and the herd repeats every month boundary.
- **The backlog has no value.** A scheduled scrape fetches the page as it is when the run
  *executes*. A run fired on Oct 29 and executed on Nov 1 gets Nov 1's page — 72 catch-up runs are
  72 fetches of the same content.
- **Skipping costs one bounded thing, stated plainly:** after the reset, the job runs at its **next
  tick**, not at 00:00 on the 1st. For an hourly job that is the same moment. For a weekly job it
  can be days later; for a monthly cron (`0 0 15 * *`) the skipped month's run is simply not made.
  The v1 scheduler would have run either one on the 1st. This is a deliberate departure from v1
  behaviour, accepted for the reasons above.

### ⚠️ Rider — the Schedule overlap policy is still undecided, and is now a smaller question

With `monthly_runs` skipping, only `concurrent_jobs` parks, and that wait is minutes. A Schedule
still fires on every tick and a parked workflow is still *running*, so a short-interval job (the
floor is 5 minutes, `schedule_min_interval_minutes`) parked for longer than its interval stacks one
workflow per tick. The overlap policy bounds that, per Schedule:

- **`SKIP`** — later firings are dropped while one is parked or running. ⚠️ *Corrected 2026-10-13:*
  the first draft rejected this as "the regression this decision rejected". That was wrong: while a
  run is **parked**, the parked run itself still executes when quota frees, so the user still gets
  one run. `SKIP` loses a run only when the earlier execution is genuinely *overrunning* its
  interval.
- **`BUFFER_ONE`** — at most one firing queued behind the running or parked one, the rest dropped.
  In the parking case this adds one near-duplicate run right after the parked one; in the overrun
  case it delivers the missed tick late instead of dropping it.
- **`BUFFER_ALL`** reproduces the pile-up.

So the choice turns on the **overrun** case, not on parking. Note for it: v1 does not serialise a
job's runs at all — `_dispatch_due_jobs` never looks for the job's own active run — so v1 behaves
like `ALLOW_ALL`, which neither candidate matches. **Not decided here; decide at the
schedule-and-webhook cutover (F.0), before the first Schedule is created.**
→ **Decided 2026-10-13 (owner): `SKIP`** — written up in [ADR-013](./ADR-013-schedule-policy.md) (Draft),
with the cap's open parameters below and the catch-up window.

**No overlap policy reaches across Schedules — so the number of Schedules is capped per user
(owner, 2026-10-13).** N scheduled jobs blocked on concurrency are N parked workflows waking
together, and nothing caps how many scheduled jobs a user may create. Decision: a **per-user limit
on active scheduled jobs**, enforced by the API wherever a job becomes an active schedule — create
with `schedule_cron`, a PATCH that sets `schedule_cron`, and a PATCH that resumes
`schedule_status` to `active`. It bounds parked runs as a consequence (at most one or two per
Schedule, by the overlap policy), so **parked runs get no cap of their own**. The check is a
count-then-insert like the run meters and goes through BUG-023's per-user lock. **Not decided
here:** the default limit, and whether paused schedules count — carried by
[ADR-013](./ADR-013-schedule-policy.md) §5.

---

## 2. Sitemap entries are restricted to the seed's registrable domain

**Decision: a sitemap-discovered URL is admitted only if its registrable domain (eTLD+1) matches
the seed's. Subdomains are allowed; unrelated domains are dropped.**

```
seed: https://shop.example.com/

  https://cdn.example.com/sitemap.xml    ADMIT
  https://www.example.com/p/1            ADMIT
  https://example.co.uk/...              DROP
  https://unrelated.com/...              DROP
```

**This is a quota-and-attribution rule, not a security one.** The security half is already settled:
ADR-009 13d decided that **every** URL entering the frontier is SSRF-checked at admission, and a
rejected URL is skipped while the crawl continues. That stops the internal-address case. It does
not stop a `robots.txt` pointing the crawl at an unrelated *public* site, which would spend the
user's quota and land another site's content in their bucket attributed to their crawl.

**Why not same-origin, matching extracted links.** `link_extractor.py:33` requires an exact
scheme+host+port match. Applying that to sitemaps would drop the ordinary cases — sitemaps hosted
on a CDN subdomain, and the `www` versus apex split — and the user would see pages go missing with
no way to know why. Extracted links can afford the stricter rule because a page that links
off-origin is usually genuinely leaving the site; a sitemap that names a sibling subdomain is
usually still describing the same property.

**Why not leave it unrestricted.** Then a third party's `robots.txt` decides where a tenant's quota
goes. `max_pages` bounds the damage but does not make it correct.

### The cost, stated plainly

**This needs a public-suffix list, and that is a real dependency, not a detail.** eTLD+1 cannot be
computed by taking the last two labels — `example.co.uk` would collapse to `co.uk` and admit every
`.co.uk` site, turning the restriction into a wider hole than having none. The two honest options
are a PSL library (`tldextract` or equivalent) or a hand-maintained deny-list of multi-part TLDs,
and the second is a worse PSL.

Two requirements come with it:

- **Pin the suffix list and resolve it offline.** `tldextract` fetches and caches the PSL from the
  network by default. A crawl activity that reaches out to a third-party URL on first use is a new
  startup failure mode and a new egress path; use the bundled snapshot and disable the live fetch.
- **⚠️ It must land with a lockfile.** This is the direct consequence of **BUG-006**: `coordinator/`
  has no lockfile and has never been scanned by Dependabot, which is why 13d's `aiohttp` exposure
  sat unseen. Adding a new dependency to the workflow worker while repeating that omission would
  recreate the exact condition BUG-006 exists to record. **BUG-006 is not closed by the migration**
  and this makes it sharper, not moot.

---

## Consequences

**Paid**

- A per-meter branch in the workflow's admission step, rather than one rule. Three cases, and a
  stated test for a future fourth: *does this breach clear without user action, and soon?*
- A scheduled job blocked on `monthly_runs` runs at its first tick after the reset, not on the 1st;
  a monthly cron loses that month's run (amended 2026-10-13).
- A skip record that the user can see and the meter cannot count — a new mechanism, F.1's to choose.
- Woken parked runs re-check concurrently, so the meter check and the run insert must be serialised
  per user (BUG-023) before F.1 ships.
- A PSL dependency in the crawl path, pinned and offline, plus the lockfile BUG-006 says is missing.
- An open follow-on: the Schedule overlap policy (rider above), due at the schedule-and-webhook
  cutover.

**Bought**

- The waiting room survives the migration for the one meter where a wait is short
  (`concurrent_jobs`), stops being a silent indefinite hold on the meter that never frees itself
  (storage), and stops being a weeks-long backlog that one tenant can grow without limit
  (`monthly_runs`).
- Sitemap and link discovery stop being *"two ways of discovering a URL, one guarded, one not"* —
  after this and 13d's admission check, both routes pass the same two gates.

**Not addressed here**

- **Worker-side SSRF validation.** ADR-009 13d filed it separately as a cross-lane item: two
  implementations in two languages that must not drift, a DNS-rebinding window a naive check
  narrows without closing, and a new terminal failure class for the retry classifier. Still not the
  crawl migration's to carry.
- Whether the include/exclude **path** filters — which today apply only to extracted links — should
  also apply to sitemap entries. Same shape of question, different axis; not raised by ADR-009 and
  not decided here.
