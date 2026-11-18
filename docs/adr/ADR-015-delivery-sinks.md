# ADR-015: Delivery Sinks — Destinations, Egress, and the Delivery Worker

**Status:** **Accepted 2026-11-18** (owner) — after the owner's section-by-section review (2026-11-18, 9 rounds).
Implement against it; immutable from here (a change is a superseding ADR).
**Date:** 2026-10-30
**Last updated:** 2026-11-18 (review round 9: §8–§11 — review complete; Accepted)

> **Review round 1 (owner, 2026-11-18): header + §1 The problem.** Agreed as drafted. All three facts checked:
> `validate_no_ssrf_core` resolves and returns, and the client resolves again; the infra repo has no NetworkPolicy
> in `scrapeflow`; the workflow worker holds `DATABASE_URL`, `REDIS_URL`, MinIO **root** credentials and both
> Fernet keys. Carried to §4: no read-only MinIO user exists today.
>
> **Review round 2 (owner, 2026-11-18): §2a The table.** Agreed, with three changes. (1) The API does not gate a
> destination kind by slice; no code is added only to be removed later. (2) `last_test` moves to *not yet*; the
> result is returned, not stored. (3) The precedent sentence is corrected: `LLM_KEY_ENCRYPTION_KEY` also encrypts
> job and batch webhook secrets, so today's keys are not one per purpose. Nothing is moved.
>
> **Review round 3 (owner, 2026-11-18): §2b–§2c Referring to a destination; deleting one in use.** Agreed, with
> three fixes from the code. (1) Save and trigger check *exists and belongs to the user* (404), as `_owned_keys`
> does for LLM keys. (2) The trigger checks destinations **after** `admit()` takes the lock; today's LLM-key check
> runs before it, and copying that order would leave the race open. (3) `ON DELETE CASCADE` on both
> `pipeline_version_destinations` foreign keys, because soft-deleted pipelines keep their rows. Owner asked
> whether the LLM-key check should move after the lock too: **no**. The trigger copies the key's ciphertext into
> the workflow input, so a run never finds its key gone, and the key delete takes no lock, so reordering the
> trigger alone would not serialise anything. The real LLM-key gap is that a scheduled monitor firing after the
> key is deleted fails at its trigger. That is the 3 a.m. case §2c refuses for destinations, and it is carried to
> ADR-016 (PRD-018's Architect pass).
>
> **Review round 4 (owner, 2026-11-18): §2d The on-demand test.** Agreed. **F2 accepted**: no test POST, no test
> email. The ADR now says when the test runs: `POST /destinations/test` (an unsaved form, so the delete warning
> comes before saving), save and edit (the test runs again and any failure refuses the save), and
> `POST /destinations/{id}/test`. A destination that is down cannot be saved. "No debris" for Postgres is
> qualified: an auto-number column skips one value.
>
> **Review round 5 (owner, 2026-11-18): §3 Egress.** Agreed, with changes. The httpx pin is **tested**: the
> `sni_hostname` request extension plus a `Host` header, with no custom transport. asyncpg stays *to verify at
> build*, with the NetworkPolicy named as its backstop. Owner asked how a legitimate server move is handled. It
> works with no edit, because every attempt resolves afresh and no IP is stored. Three rules were added from that
> question: each attempt (undo included) resolves afresh and recorded locations hold names; each vetted IP is
> tried in order within an attempt; a certificate mismatch is final. **F1 accepted.** **`send_signed_post`
> accepted**, carried from ADR-014's review: one send for both webhook senders, with the pin inside it and one
> test pinning identical bytes and headers. **BUG-024: fix it now, for every lane** (owner: "treat this project
> as to be used by others too; so irrespective of if i use tailscale or not; make the fixes"). Built the same day:
> the address rule is public unicast only. The fix BUG-024 proposed (`is_global and not is_multicast`) would have
> admitted the NAT64 form of `10.0.0.1`, so NAT64 addresses are judged as their IPv4 address. See open-bugs.
>
> **Review round 6 (owner, 2026-11-18): §4 The delivery worker.** Agreed; **F5 accepted**. Three additions.
> (1) A **read-only MinIO user** for the delivery worker, created through the MinIO chart's values; today every
> service uses root. (2) The NetworkPolicy also excludes **`100.64.0.0/10` and the node's own public IP(s)**: the
> node's IP is public, so the check admits it, but a pod reaching its own node arrives from inside the machine.
> Filed as **BUG-032** for the scrape lanes, which have no such fence. Release gate extended. (3) Carried from
> PRD-018's review: **email failure alerts and notices**. `webhook_deliveries` gains `kind` (`webhook` | `email`,
> in slice 1's migration); `WebhookDeliveryWorkflow` POSTs or calls `send_email` on the `delivery` queue; the row
> stays the record. Notices (PRD-018 OQ-14) are carried to ADR-016 to reuse it.
>
> **Review round 7 (owner, 2026-11-18): §5 Duplicate-safe keys.** Agreed, with three changes. (1) The Postgres
> undo's "0 rows = undone only if the destination still matches" condition is dropped. The undo always connects
> to the recorded location, so 0 rows there means the row is gone, as for the bucket. (2) Email: "many mail systems
> merge it" toned down; most inboxes show both. (3) A versioned bucket keeps older versions after rollback, and
> object lock refuses the delete; this is stated, and nothing is built. Checked: the pipeline webhook's timestamp
> is set by the workflow (`workflow.now()`), so retries send identical bytes.
>
> **Review round 8 (owner, 2026-11-18): §6 Time budgets, §7 Content size.** Agreed; **F3 accepted**. Two fixes from
> the code. (1) `load_destination` is not "inside the block overhead": the 60 s allowance covers three DB writes per
> block, and the workflow-driven retry loop adds about 13 over five tries. Each try's writes are now counted in the
> sink's need. (2) Postgres temporary errors widened: SQLSTATE classes `08` and `53`, `57P01`–`57P03`, `40001`,
> `40P01`. As drafted, a maintenance restart would have failed the run for good.
>
> **Review round 9 (owner, 2026-11-18): §8–§11, review complete.** Agreed. The release order is corrected: a
> brand-new image has no tag for an infra-first Deployment, so it is infra (Secrets, MinIO user, NetworkPolicy)
> → `main` → infra (Deployment + image policy). §11 gains BUG-032. **F4** marked accepted (round 7 agreed the
> paragraph). All of F1–F5 are answered. **Accepted 2026-11-18**, together with ADR-014.
**Deciders:** @karthik
**Resolves:** PRD-017's open questions OQ-3 to OQ-8 (the destination model, egress and the check-versus-connect
gap, duplicate-safe keys, time budgets, which worker reaches the outside world, content size). PRD-017 OQ-1
and OQ-2 (how a delivery and its undo are recorded, where rollback runs) are **ADR-014** §5 and §7. This ADR
supplies the pieces those sections call: each sink's delivery and undo activity, their timings, and the
rollback reserve. Also fixes **BUG-030** (the webhook check and connection resolve separately) as part of
slice 1.
Not a supersession.

> **Carried from ADR-014's review (owner, 2026-11-03) — raise at this ADR's review.** The two webhook senders
> (`activities/webhook_deliveries.py`, the job lane; `activities/webhook.py`, the pipeline Webhook block) share only
> `validate_no_ssrf_core`: signing, the signature header, the 10 s POST and the `< 300` rule are written twice, and
> "byte for byte" is held by docstrings. Proposal: keep both drivers (who owns the outcome differs — ADR-009 15e),
> and extract **one** `send_signed_post(url, body, secret)` that both call, with BUG-030's resolve-once,
> connect-to-that-IP fix inside it, plus one test pinning identical bytes and headers from both senders.

---

## 1. The problem

PRD-017 makes ScrapeFlow open connections to addresses its users choose — their bucket, their database, their
mail server, their webhook — with credentials they store with us, from inside a cluster that also holds every
tenant's data. Three facts about today shape the answer:

- **Every outbound check today is check-then-connect.** `validate_no_ssrf_core` looks the name up and returns;
  the client looks it up again to connect (BUG-030). A name that answers differently the second time reaches an
  internal address. Each new sink client (bucket, Postgres, SMTP) would repeat the gap three more ways.
- **No pod in the `scrapeflow` namespace has a NetworkPolicy** (infra repo, checked 2026-10-30). Any pod can
  reach Postgres, Redis, MinIO, Temporal and the node's LAN. The only barrier is the code-level check.
- **The workflow worker holds the keys to everything** — the app database, every Fernet key, MinIO. The Webhook
  block runs there today. Putting a Postgres driver and an SMTP client that dial user-chosen hosts on that pod
  maximises what one bypass is worth.

## 2. Destinations (PRD-017 OQ-3, D7, R5, R8)

### 2a. The table

```
destinations
  id                UUID PK
  user_id           UUID → users ON DELETE CASCADE
  name              VARCHAR(100)              UNIQUE (user_id, name)
  kind              VARCHAR(20)  CHECK IN ('bucket', 'postgres', 'smtp', 'webhook')
  address           JSONB        non-secret settings, by kind (below)
  encrypted_secret  TEXT         Fernet ciphertext of a JSON object, by kind (below)
  display_host      TEXT         what any API shows of the address (host only for a webhook)
  created_at, updated_at
```

| kind | `address` (returned by the API) | `encrypted_secret` (never returned) |
|---|---|---|
| bucket | endpoint (`https://…`), bucket, prefix, region | access key, secret key |
| postgres | host, port, database, schema, table, CA certificate (optional) | user, password |
| smtp | host, port, TLS mode, from, to (list), CA certificate (optional) | user, password |
| webhook | — | **URL** and signing secret (the URL is a credential — R8) |

- **All four kinds are admitted on day one.** D1 has decided them, so the slices add code, not a CHECK widening.
  The API does not refuse a kind whose slice has not shipped: that would be code added only to be removed, and
  nothing runs in production before Phase 4 completes (owner, review round 2).
- **A new Fernet key, `DESTINATION_ENCRYPTION_KEY`**, not the credentials key. The delivery worker (§4) gets this
  key and no other, so a compromised delivery pod cannot decrypt scrape cookies, proxy passwords or LLM keys. The
  workflow worker also gets it, for webhook destinations. Today's keys are not split one per purpose:
  `LLM_KEY_ENCRYPTION_KEY` also encrypts job and batch webhook signing secrets (`routers/jobs.py`,
  `activities/webhook_deliveries.py`), and `CREDENTIALS_ENCRYPTION_KEY` encrypts the pipeline Webhook block's
  inline secret as well as cookies and proxy passwords. Neither is moved; the new key is the first one that guards
  a single kind of secret.
- **Not yet: storing the last test result** (owner, review round 2). The test's result comes back in the test or
  save response (§2d) and is not kept on the row.
- **`display_host`** is written at save: the bucket endpoint's host, the database host, the mail host, or the
  webhook URL's host. Admin and user reads show it. A webhook URL is never shown whole again after save; changing
  it means pasting a new one.
- **Credentials never enter workflow history in plain text.** The workflow carries only `destination_id` and,
  for one attempt, the ciphertext (the precedent is `LLMKey.encrypted_api_key`). Sink errors are built by our code
  from the exception class and non-secret fields. A driver's own message, which can echo a connection string, is
  never passed through.

### 2b. How a pinned version refers to one

- A sink block's config holds `destination_id`. The **version pins which destination**; the destination's
  contents stay live. Each attempt loads them fresh, as PRD-017 R5 asks ("current credentials per attempt"),
  so an edit takes effect on the next attempt. An undo still targets the location recorded at delivery
  (ADR-014 §5).
- **The Webhook block takes either form**: `url` + `secret` inline (today's form, unchanged), or
  `destination_id`. Exactly one is given; the save check refuses both or neither. Both send the same payload.
- Saving a version writes **`pipeline_version_destinations (pipeline_version_id, destination_id)`** rows. The
  delete check (2c) queries that table; nobody scans definitions' JSON. Both foreign keys are **`ON DELETE
  CASCADE`**: a soft-deleted pipeline keeps its versions and their rows, and without the cascade deleting a
  destination they reference would fail on the foreign key (a 500, not the 204 that §2c allows).
- **Save and trigger check that each referenced destination exists and belongs to the user**, as LLM keys are
  checked today (`_owned_keys`). Otherwise the answer is `404`, not `403`: a pasted id of another user's
  destination must not reveal that it exists, and must never deliver with that user's credentials.

### 2c. Deleting a destination in use is refused (PRD-017 R5, the PM's preference — upheld)

`DELETE /destinations/{id}` takes BUG-023's per-user lock (`quota.lock_user`), then refuses with `409` and names:

- every **non-deleted pipeline** with any version referencing it (a run can be triggered on a chosen version);
- every **run still `running`** whose version references it, including one that is rolling back.

The trigger takes the same lock and checks each referenced destination exists, **after** taking it: the check
runs after `admit()` (which takes `lock_user`), not where today's LLM-key check runs, before it. Checked before the
lock, a delete can commit between the check and the run's insert. So a run never starts against a destination
being deleted, and an attempt never finds its destination gone. If one ever does, that attempt fails
with a non-retryable *"destination deleted"*. **Why upheld:** the alternative ("fail at the trigger saying it was
deleted") moves the surprise to a scheduled run at 3 a.m. under Monitors. A refusal names the pipelines while the
user is looking at the screen.

### 2d. The on-demand test (no debris)

The test runs on the delivery worker, never on the api pod (§4). The API starts a short `DestinationTestWorkflow`
and waits up to 30 s for its result. It runs from three places (owner, review round 4):

| Call | Tests | Effect |
|---|---|---|
| `POST /destinations/test` | an unsaved form | reports each check; nothing is stored. The SPA's *Test* button, so the delete warning is seen **before** saving (R5) |
| `POST /destinations`, `PATCH /destinations/{id}` | the form being saved | runs the same test again; **refuses** on any failed check, saves on a pass or a warning |
| `POST /destinations/{id}/test` | a saved destination | on demand, any time; nothing is stored (§2a) |

A destination that is down at the moment cannot be saved or edited; its test fails. Accepted: a failed login at
save is almost always a typo.

| kind | checks | debris |
|---|---|---|
| bucket | address check → `PUT {prefix}/.scrapeflow-test-{uuid}` → `DELETE` it | none. If the delete is refused, the test reports *"delete refused — rollback to this bucket will fail; left `.scrapeflow-test-…`"* (one empty object, named) |
| postgres | address check → TLS → in **one transaction**: the table exists; a **unique index on exactly (`run_id`, `block_id`)** exists; `INSERT` a probe row; `DELETE` it; then **`ROLLBACK`** | none, except that an auto-number column (`SERIAL`, identity) skips one value, since a rolled-back transaction does not return sequence numbers. Privileges are still checked as each statement runs |
| smtp | address check → TLS → `EHLO` → `AUTH` → `QUIT`. **Sends no mail** | none |
| webhook | address check → TLS handshake. **Sends no POST** | none |

Without the unique key, a Postgres destination is refused at save (R5). Without delete permission, a bucket or
Postgres destination is saved, but with the warning shown before save (R5).

## 3. Egress: connect to the address that was checked (PRD-017 OQ-4, D9, BUG-030)

**Decision: resolve once, check every address, then connect to that address while presenting the hostname for
TLS. One module, every outbound client. And, as a second layer, a NetworkPolicy on the pod that does it.**

```
resolve_public(host, port) → [vetted IPs]         one getaddrinfo; any private answer → refused (final)
client connects to vetted IP                       no second lookup
TLS: SNI + certificate check against `host`        so the certificate still has to match the name
```

| Client | How it is pinned to the vetted IP | Verified? |
|---|---|---|
| **httpx** (both webhook senders) | request `https://{vetted IP}/…` with `Host: {name}` and the request extension `sni_hostname={name}`; TLS sends the name and checks the certificate against it. No custom transport | **tested 2026-11-18** (api image, httpx 0.28.1): a host's IP with its own name → 200; with another name → `CERTIFICATE_VERIFY_FAILED` |
| **bucket client** (aiohttp-based, as `miniopy-async`) | an aiohttp resolver returning only the vetted IPs | to verify at build |
| **asyncpg** | connect to the IP; TLS hostname verification against the name. If the driver cannot verify a name while dialling an IP, swap in a resolver override instead | to verify at build. The hardest of the four; if pinning it proves awkward, the delivery worker's NetworkPolicy is its backstop (it leaves only MinIO and Temporal reachable, on their own ports) |
| **SMTP client** | connect to the IP; STARTTLS / implicit TLS with the name as server hostname | to verify at build |

- **Every attempt resolves afresh**, deliveries and undos alike. ScrapeFlow never stores or caches an IP, and a
  recorded delivery location (ADR-014 §5) holds names, never addresses. A destination that legitimately moves to
  another public IP keeps working with no edit. An undo after the move reaches the new server.
- **Within one attempt, each vetted IP is tried in order.** The client moves to the next IP only after a refused or
  timed-out connection, never after a TLS failure. This covers a server move where DNS lists the old and new IP
  together.
- **A certificate that does not match the name is final**, with the message *"certificate not valid for {host}"*.
  A retry does not fix it, and accepting it would defeat the name check.
- **One send for both webhook senders** (owner, review round 5). `send_signed_post(url, body, secret)` in the api
  image holds the address check, the IP pin, the HMAC, the `X-ScrapeFlow-Signature` header, the 10 s POST and the
  `< 300` rule. `activities/webhook.py` (the pipeline block) and `activities/webhook_deliveries.py` (job and batch)
  both call it and keep their own outcome handling (ADR-009 15e). One test pins identical bytes and headers from
  both. Today each file carries its own copy of the send.
- **One check, in two places on purpose.** `app/core/egress.py` (the api image: both webhook senders) and
  `delivery-worker/worker/egress.py` (the sinks). It is a deliberate duplicate, like the activity contracts.
  `contracts/test_egress.py` runs one table of cases against both copies, including the case PRD-017 checks for:
  a name that resolves public on the first lookup and private on the second, and must still be refused.
- **BUG-030 is fixed in slice 1, for both existing webhook senders** (the pipeline Webhook block and the job lane's
  `WebhookDeliveryWorkflow`). PRD-017 lists its check under slice 1. CLAUDE.md's *SSRF re-validation* row is
  corrected then.
- **The address rule is "public unicast only"**: BUG-024's fix (owner, review round 5; built 2026-11-18 in
  `core/security.is_public_address`, ahead of slice 1, for every lane). An address passes only if it is
  `is_global` and not multicast, with a NAT64 address (`64:ff9b::/96`) judged as its IPv4 address. Each egress
  copy uses this same rule, and the shared test table pins it, including BUG-024's ranges.
- **No redirects, on every sink** (R5). httpx does not follow them by default. The bucket client's region-redirect
  following is turned off.
- **TLS is required on every sink.** The bucket endpoint must be `https`. Postgres must use TLS with certificate
  verification. SMTP must use STARTTLS or implicit TLS, and never sends AUTH in plain text. A destination with a
  self-signed certificate supplies its CA certificate. A destination that cannot do TLS is refused at save and at
  test. (§9, escalation F1.)
- **The second layer: the delivery worker's NetworkPolicy** (§4). Egress is allowed to DNS (kube-dns), the
  Temporal and MinIO services on their ports, and `0.0.0.0/0` **except** the pod CIDR, the service CIDR, RFC 1918,
  `100.64.0.0/10`, link-local, loopback **and the node's own public IP(s)** (owner, review round 6). The node's IP
  is public, so the code-level check admits it, but a pod reaching its own node arrives from inside the machine,
  where the host firewall usually trusts it (BUG-032, which the scrape lanes still have). That catches any library doing a lookup the module did not wrap. The policy allows
  MinIO and Temporal by name, so for those two the code-level check remains the barrier. A user destination that
  resolves to MinIO's address is refused there. ⚠️ **Untested:** k3s enforces NetworkPolicy through its embedded
  controller unless the cluster disabled it. The release gate tests it (§8).
- The LLM worker's call to a user's `base_url` has the same check-then-connect shape. It is out of PRD-017's scope;
  it is noted here because the module above is what fixes it.

## 4. Which worker reaches the outside world (PRD-017 OQ-7)

**Decision: a new task queue, `delivery`, served by a new worker: `delivery-worker/`, its own image, lockfile and
Deployment. It has no app-database access, holds only `DESTINATION_ENCRYPTION_KEY` and read-only MinIO
credentials (a new MinIO user with read on `scrapeflow-results`, created through the MinIO chart's `users` and
`policies` values; today every service uses the root user), and carries the NetworkPolicy above. The sinks (bucket, Postgres, email) and their undo activities run
there. The Webhook block stays on the workflow worker.**

| Worker | DB | Keys | Reaches | Runs |
|---|---|---|---|---|
| workflow worker (today) | ✅ | all | everything internal | the workflow, mirrors, ledger, Webhook block, job-lane webhook deliveries |
| **delivery worker (new)** | ❌ | destinations key only | Temporal, MinIO (read), public internet | bucket / Postgres / email deliveries and undos; destination tests |

- **Why a separate pod and not the workflow worker:** a NetworkPolicy is per pod, and the workflow worker has to
  reach Postgres, Redis, MinIO and Temporal. Isolation is only possible somewhere else. A bypass of the code check
  on the delivery worker reaches the public internet and two allowed services, not the app database. This is the
  multi-tenant reason. It does not get weaker because production runs one replica (owner's standing rule).
- **Why its own directory and image, not the api image with a third entrypoint:** the api image's settings require
  the database URL and two Fernet keys at import (BUG-019's finding), and its code imports the DB layer. A worker
  that must not have the database should not be able to load it. The precedent is the LLM worker: its own
  `uv.lock`, install from the lock (BUG-013), and its own copy of the contracts, pinned by `contracts/`.
- **Why the Webhook block stays:** it already works, it is HTTP only, and slice 1 fixes BUG-030 for it. Moving it
  and the job lane's delivery activity (which reads and writes `webhook_deliveries`) to the delivery worker would
  split each into a DB step and a POST step. That is a later decision. (§9, escalation F5 — ✅ owner, round 6.)
- **Email for failure alerts and monitor notices** (owner, review round 6). ADR-014 §8 makes a failure alert a
  `webhook_deliveries` row, and PRD-019 lets *any* message-sending block carry the failed event, Email blocks
  included. So `webhook_deliveries` gains **`kind`** (`webhook` | `email`, default `webhook`). The row stays the
  record of a message that must go out. `WebhookDeliveryWorkflow` reads the row and either POSTs, as today, or calls
  a single-attempt `send_email` activity on the `delivery` queue with the destination's id, under the same retry
  schedule and the same `webhook-sweep`. The table keeps its name. Monitor notices (PRD-018 OQ-14) reuse the same
  path, which is ADR-016's to confirm.
- **Retries run in the workflow, because the delivery worker cannot write `waiting`.** The Webhook block's
  activity mirrors `waiting` itself between attempts (ADR-009 §15a). A DB-less worker cannot. So each sink attempt
  is a **single-attempt activity**, and `PipelineWorkflow` runs the loop:

  ```
  load_destination(id)        workflow queue: returns ciphertext + current address
  attempt                     delivery queue: one try, returns the delivery record or a classified error
    ├─ ok                     → mirror completed + delivery                (ADR-014 §5)
    ├─ final error            → block failed                              (SSRF refusal, auth refused, 4xx-class)
    └─ transient, tries left  → mirror waiting (+error) → workflow.sleep(backoff) → mirror running → again
  ```

  The waiting state, the "no slot while waiting" rule and "cancel ends the wait" all fall out of it. The sleep is
  a durable timer that a cancel interrupts, and `mirror running` re-checks the row (ADR-009 15d), so
  `check_cancelled` is not needed. **Retry stays in one layer** (PRD-016 R4). Every client's own retries are set
  to zero, the same discipline as the LLM SDKs' `max_retries` pin. The undo loop is the same, minus the
  `waiting` mirror (the block shows `undo_status = undoing` throughout) and minus the cancel check (ADR-014 §7).
- **New workflow code, patched** (ADR-012): patch id `sinks`, beside ADR-014's `rollback`. Moving an activity
  between queues later is replay-safe (ADR-012 §4).

## 5. Duplicate-safe keys, per sink (PRD-017 OQ-5, D11)

| Sink | Write | Retry after a lost response | Undo (recorded location, current credentials) |
|---|---|---|---|
| **bucket** | `PUT {prefix}/{pipeline_id}/{run_id}/{block_id}.{ext}` (`ext` from the input: `html`, `md`, `json`) | same key, overwritten → one object | `DELETE` that key at the recorded endpoint + bucket. A missing object is a success ("already gone" — R3) |
| **postgres** | `INSERT … ON CONFLICT (run_id, block_id) DO UPDATE SET content = EXCLUDED.content, delivered_at = EXCLUDED.delivered_at` | the same row is updated → one row | `DELETE … WHERE run_id = $1 AND block_id = $2` at the recorded host/database/table. **0 rows = undone** ("already gone" — R3), as for the bucket. A login refused there (the destination's credentials now belong to another server) is **undo failed: left in place** |
| **webhook** | body fixed by the workflow (timestamp included) → identical bytes on every attempt | receivers dedupe on **`run_id` + `block_id`** (+ `event`); `block_id` added to the payload (R4) | irreversible |
| **email** | `Message-ID: <{run_id}.{block_id}@{PUBLIC_HOST}>`, or `<{run_id}.{block_id}.failed@…>` for the failure alert | a resend repeats the Message-ID. Most inboxes still show both; a mail filter can match on it. A duplicate needs a lost reply, so it is rare | irreversible |

- The bucket key's run id + block id is what lets rollback delete exactly this delivery and nothing a user put
  under the same prefix.
- **A versioned bucket keeps older versions.** There a `DELETE` adds a delete marker, so rollback removes the
  current object and the earlier versions stay recoverable. ScrapeFlow does not purge versions: that needs more
  permission and cannot be undone. A bucket with object lock refuses the delete, which reports **undo failed: left
  in place**. Nothing is built for either.
- The Postgres row's fixed shape (R1): `run_id UUID, pipeline_id UUID, block_id TEXT, delivered_at TIMESTAMPTZ,
  content JSONB`. ScrapeFlow never issues DDL.
- **Destination edited between attempts**: attempt 1 fails against bucket A, the user re-points the destination to
  bucket B, attempt 2 succeeds against B. The recorded location is B, where the object actually is. The undo
  deletes B's key. PRD-017 R5 reads the same way.

## 6. Time budgets (PRD-017 OQ-6, R6)

Same shape as the Webhook block (ADR-009 §15b): five attempts, waits of 30 s growing ×10 and capped at 7,200 s.

| Activity | Per attempt | Attempts | Waits | Budget (≈) |
|---|---|---|---|---|
| bucket deliver | 60 s | 5 | 30 s, 300 s, 3,000 s, 7,200 s | 2.9 h + attempts ≈ **3.0 h** |
| postgres deliver | 30 s | 5 | same | **≈ 3.0 h** |
| email deliver | 60 s | 5 | same | **≈ 3.0 h** |
| bucket / postgres **undo** | 30 s | 5 | same | **≈ 3.0 h** |
| `load_destination` + the `waiting`/`running` mirrors | DB activities, 20 s each (`DB_ACTIVITY_SECONDS`) | per try | — | **counted per try** (below), not inside the block overhead |

- Each figure lives in the catalog (`Timing`, `budget` computed the way it already is), so users can add up their
  chain, as R6 asks.
- **The per-try database writes are counted** (owner, review round 8). `BLOCK_OVERHEAD_SECONDS` (60 s) covers three
  DB activities per block (`pipeline.py`). A sink's workflow-driven loop adds its own each try: `load_destination`,
  then on a temporary failure `waiting` and `running`. That is about 13 more over five tries, ≈ 260 s. So a
  deliver's need adds `(1 + 2) × DB_ACTIVITY_SECONDS` per try (no `waiting`/`running` after the last), and an undo
  adds `DB_ACTIVITY_SECONDS` per try for its load. A budget that leaves them out lets `schedule_to_close` cut a
  block off with no error (Q6).
- **The rollback reserve** = the undo budget × the number of reversible sinks in the chain. The save-time check
  becomes: forward need + reserve ≤ the run ceiling (`pipeline_max_run_seconds`, 86,400). The refusal names both:
  *"blocks need 28 h (19 h of blocks + 9 h to undo 3 deliveries); the limit is 24 h"*. PRD-017's worked example:
  `scrape → extract → bucket → db → webhook` ≈ 10 h forward + 6 h reserve = 16 h.
- **The undo is never cut by the run deadline** (ADR-014 §7). The reserve inside the ceiling is what makes that
  honest, not a timeout.
- **Final, never retried:** an address refusal, an auth refusal, a permission refusal, a missing table or key, and
  a 4xx from a bucket (other than 408/429). **Transient:** connection errors, timeouts, 5xx, 408/429, SMTP 4xx
  replies, and on Postgres: connection-class errors (SQLSTATE class `08`), insufficient resources (class `53`:
  too many connections, disk full), server shutting down or not yet accepting (`57P01`–`57P03`), serialization
  failure and deadlock (`40001`, `40P01`) (owner, review round 8; a managed database's maintenance restart must not
  fail a run for good). **Unknown → final** (PRD-016 R4). The precedent is the storage-fault
  classifier on every worker, with the same Go/Python lesson: a connection error from the destination is a
  different exception class from an error *reply*.

## 7. Content size (PRD-017 OQ-8)

- **Bucket: streamed.** `stat` the ScrapeFlow object, then stream MinIO's response body into the destination PUT
  with its known length. Nothing is held whole in memory. Pages up to 4.1 MiB have been measured, and the HTTP
  worker caps a fetch at 10 MB.
- **Postgres: in memory, capped at 10 MiB.** The content has to be one JSON value in one parameter. An extraction
  is small (the LLM answer is capped at 4,096 tokens), and a page scraped as JSON is under the fetch cap. Above
  10 MiB the block fails with a final error naming the size.
- **Email: inline up to 256 KiB of text.** Above that, the body says *"result too large to include (N KiB);
  fetch it with run {run_id}"*. No attachments in slice 3. (§9, escalation F3.)
- The delivery worker runs at most **8 activities at once**, which bounds memory to ≈ 8 × 10 MiB at the Postgres
  cap.

## 8. Rollout and release gates

**Slice 1** (bucket + rollback + several webhooks): one migration adds `destinations`,
`pipeline_version_destinations`, and `webhook_deliveries.destination_id` (ADR-014 §8b's row for a webhook
destination) and `webhook_deliveries.kind` (§4; only `webhook` until slice 3). The `delivery-worker/` image, its
Deployment and NetworkPolicy come with `DESTINATION_ENCRYPTION_KEY` in a new Secret, and the read-only MinIO user
(chart values, its password in a new Secret). The egress module fixes BUG-030 in the api image. The catalog gains `bucket`; the Webhook cap
(one per pipeline) lifts; the Webhook block gains `destination_id`, `block_id` and `deliveries` in its payload.
**Slices 2 and 3** add the `postgres` and `email` sinks, with no schema change.

Release order (owner, review round 9). The delivery worker is a new image, so it has no tag until CI builds it on
a `main` push, and a new Flux image policy must carry an existing tag:
1. **infra:** the Secrets, the MinIO user, the NetworkPolicy (it matches no pod yet) and the workflow worker's new
   key;
2. **`main`:** CI builds the delivery worker and api images; the api rolls out;
3. **infra:** the delivery worker's Deployment and image policy, carrying the tag step 2 built.

Between 2 and 3 a sink block finds no poller and fails at its 60 s `schedule_to_start`. Noted, not guarded: no
pipelines run in production before Phase 4 completes.

**Release gates** (before the slice is called done in production):
- from the delivery worker pod, a TCP connect to `scrapeflow-postgresql:5432`, Redis, the node's LAN address and
  the node's public IP **fails**, and to a public host **succeeds**. This verifies the NetworkPolicy is enforced, which is untested
  today;
- the rebinding test passes against both egress copies;
- a known test secret appears in no API response, workflow history or log line (PRD-017's check).

## 9. Escalations to the PM (owner)

| # | Question | Recommendation |
|---|---|---|
| **F1** | TLS on every sink: `https` bucket endpoints, Postgres with certificate verification, SMTP with STARTTLS or TLS. A self-signed destination brings its CA certificate. Plain connections are refused. | **Accept.** Scraped content and destination passwords otherwise cross the internet in clear. ✅ owner, round 5 |
| **F2** | The destination test sends **no** webhook POST and **no** email (it checks the address, TLS and login only). | **Accept.** A test POST can trigger the receiver's side effects. A "send me a test message" button can come later. ✅ owner, round 4 |
| **F3** | Email inline cap 256 KiB, with no attachments; Postgres content cap 10 MiB. | **Accept** for the first slices. ✅ owner, round 8 |
| **F4** | Editing a destination mid-run moves later attempts to the new address; an undo still targets where the delivery actually landed. | **Accept.** It is how R5 already reads. ✅ owner, round 7 (marked in round 9) |
| **F5** | The Webhook block and job-lane webhook deliveries stay on the workflow worker (BUG-030 fixed there); moving them onto the delivery worker is a later decision. | **Accept**, and revisit if a second HTTP-based sink appears. ✅ owner, round 6 |

## 10. Alternatives rejected

- **Sinks on the workflow worker.** It is the simplest option, and it puts user-directed Postgres and SMTP
  connections on the pod with the app database and every key, with no way to add a NetworkPolicy.
- **Sinks on the api image under a third entrypoint.** Its settings and imports need the database. Removing that
  means splitting settings, which costs more than a small worker with its own lockfile.
- **Activity-level retry for sinks (the Webhook block's pattern).** It needs the attempt to write `waiting`, so the
  worker needs the database.
- **Network-policy-only egress control.** It cannot cover the two internal services the worker must reach, and its
  enforcement on this cluster is unverified.
- **Copying destination credentials into each pinned version.** It fails R5's one-edit rotation and multiplies the
  ciphertext to re-encrypt on a key change.

## 11. Consequences

- A fifth worker Deployment, image and lockfile. Dependabot and BUG-013 discipline apply from its first commit:
  lockfile, install from the lock, manifest monitored.
- A third Fernet key to rotate.
- BUG-030 closes with slice 1. BUG-024 is fixed before it (2026-11-18). Its rule is copied into the delivery
  worker's egress module and pinned by the shared test table.
- `contracts/` grows: the sink activities' inputs and outputs, through the delivery worker's real parser; and the
  egress table.
- **BUG-032 stays open for the scrape lanes** (a user URL can reach the node's own public IP). The delivery worker
  is fenced against it from slice 1.
- PRD-017's Postgres destination needs a public hostname with TLS, as does every other destination (D9, F1). A
  homelab user's LAN database cannot be one.
