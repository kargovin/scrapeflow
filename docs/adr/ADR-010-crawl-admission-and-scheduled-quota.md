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

---

## Why these two are in one ADR

They have nothing in common as problems. They are together because they are the two items ADR-009
named as open, and both are **needed before the batch-and-crawl cutover** — one because the crawl
step builds the frontier admission path, the other because the schedule-and-webhook cutover is the
step after it and its design constrains what the counting view is asked for.

Both are small. Neither justifies its own document; leaving them in a review log would repeat the
failure ADR-009 14d is about — a decision with no home is a decision that gets rediscovered.

---

## 1. A quota-blocked scheduled run parks only when waiting can succeed

**Decision: the Temporal Schedule fires unconditionally. The workflow's first step consults the
run-counting view and then behaves differently per meter.**

| meter | on breach | why |
|---|---|---|
| `concurrent_jobs` | **park** on a durable timer, re-check | clears in minutes without the user doing anything. ADR-009 §8: a parked run holds no slot, so parking cannot deadlock against itself |
| `monthly_runs` | **park** on a durable timer, re-check | clears at the month boundary. Long, but bounded and self-clearing — and it is exactly what the current scheduler does |
| `storage_bytes_used` | **fail the run now**, with an error naming the remedy | **never clears on its own.** The user must delete artifacts. Parking would hide an actionable condition behind an indefinite wait |

**Why the check cannot live in the Schedule.** Temporal offers `SKIP`, `BUFFER_ONE`, `BUFFER_ALL`,
`CANCEL_OTHER` and `TERMINATE_OTHER`. None of them is this. `SKIP` discards the firing permanently,
so a user briefly at their concurrency ceiling silently loses a run — a user-visible regression
PRD-016 R5 forbids. More fundamentally, every overlap policy reacts to *a previous execution still
running*, whereas this gate reacts to *the account's meters*, which a Schedule cannot read.

**Why per-meter rather than one uniform rule.** A uniform "park with a timeout" makes the user wait
out a timeout to be told something that was knowable when the run started; a uniform "park forever"
parks a recurring job indefinitely and silently on the one meter that will never free itself. The
distinction that matters is not the meter's name but **whether the breach clears without user
action** — and if a fourth meter is ever added, that is the question to ask of it.

This composes with ADR-009 §8d's headroom buffer, which already refuses to *start* runs near the
storage ceiling. The buffer is the early guard; this is what happens when a run gets past it and
the ceiling is crossed anyway.

### ⚠️ Rider — this decision is incomplete without a Schedule overlap policy, and that is not decided here

Today's waiting room **cannot stack**: there is one `Job` row, it stays `due`, and exactly one run
is created when quota frees. A Temporal Schedule does not work that way. It fires on every cron
tick regardless, so a workflow parked for an hour under an hourly schedule accumulates **one parked
workflow per tick** — each holding history, and all of them released at once when the meter clears.
That is a thundering herd against the very meter they were waiting for, and on `monthly_runs` each
one then consumes a unit.

A parked workflow *is* still running, so an overlap policy does reach this case even though it
cannot read the meters:

- **`BUFFER_ONE`** is the natural pairing — at most one firing queued behind the parked one, the
  rest dropped. Closest to today's single-slot behaviour.
- `SKIP` would drop every firing while parked, which is the regression this decision rejected.
- `BUFFER_ALL` reproduces the pile-up.

**`BUFFER_ONE` is the recommendation, and it is explicitly not decided here** — it was surfaced
while writing this up, after the decision above was taken, and it deserves to be chosen rather than
inherited. **Decide it at the schedule-and-webhook cutover, before the first Schedule is created.**

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
  stated test for a future fourth: *does this breach clear without user action?*
- A PSL dependency in the crawl path, pinned and offline, plus the lockfile BUG-006 says is missing.
- An open follow-on: the Schedule overlap policy (rider above), due at the schedule-and-webhook
  cutover.

**Bought**

- The waiting room survives the migration for the meters where it was ever meaningful, and stops
  being a silent indefinite hold on the one meter where it never was.
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
