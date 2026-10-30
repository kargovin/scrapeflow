# ADR-015: Delivery Sinks — Destinations, Egress, and the Delivery Worker

**Status:** **Draft** — Architect pass, 2026-10-30. Not reviewed; awaits the owner's section-by-section review.
Hold acceptance until PRD-018 is drafted, as with ADR-014.
**Date:** 2026-10-30
**Deciders:** @karthik
**Resolves:** PRD-017's open questions OQ-3 to OQ-8 (the destination model, egress and the check-versus-connect
gap, duplicate-safe keys, time budgets, which worker reaches the outside world, content size). PRD-017 OQ-1
and OQ-2 (how a delivery and its undo are recorded, where rollback runs) are **ADR-014** §5 and §7. This ADR
supplies the pieces those sections call: each sink's delivery and undo activity, their timings, and the
rollback reserve. Also fixes **BUG-030** (the webhook check and connection resolve separately) as part of
slice 1.
Not a supersession.

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
  last_test         JSONB NULL   {at, reachable, write, delete, unique_key, error}
  created_at, updated_at
```

| kind | `address` (returned by the API) | `encrypted_secret` (never returned) |
|---|---|---|
| bucket | endpoint (`https://…`), bucket, prefix, region | access key, secret key |
| postgres | host, port, database, schema, table, CA certificate (optional) | user, password |
| smtp | host, port, TLS mode, from, to (list), CA certificate (optional) | user, password |
| webhook | — | **URL** and signing secret (the URL is a credential — R8) |

- **All four kinds are admitted on day one.** D1 has decided them, so the slices add code, not a CHECK widening.
- **A new Fernet key, `DESTINATION_ENCRYPTION_KEY`**, not the credentials key. The delivery worker (§4) gets this
  key and no other, so a compromised delivery pod cannot decrypt scrape cookies, proxy passwords or LLM keys. The
  workflow worker also gets it, for webhook destinations. This follows `user_llm_keys`, which has its own key.
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
  delete check (2c) queries that table; nobody scans definitions' JSON.

### 2c. Deleting a destination in use is refused (PRD-017 R5, the PM's preference — upheld)

`DELETE /destinations/{id}` takes BUG-023's per-user lock (`quota.lock_user`), then refuses with `409` and names:

- every **non-deleted pipeline** with any version referencing it (a run can be triggered on a chosen version);
- every **run still `running`** whose version references it, including one that is rolling back.

The trigger takes the same lock and checks each referenced destination exists. So a run never starts against a
destination being deleted, and an attempt never finds its destination gone. If one ever does, that attempt fails
with a non-retryable *"destination deleted"*. **Why upheld:** the alternative ("fail at the trigger saying it was
deleted") moves the surprise to a scheduled run at 3 a.m. under Monitors. A refusal names the pipelines while the
user is looking at the screen.

### 2d. The on-demand test (no debris)

The test runs on the delivery worker, never on the api pod (§4). The API starts a short `DestinationTestWorkflow`
and waits up to 30 s for its result.

| kind | checks | debris |
|---|---|---|
| bucket | address check → `PUT {prefix}/.scrapeflow-test-{uuid}` → `DELETE` it | none. If the delete is refused, the test reports *"delete refused — rollback to this bucket will fail; left `.scrapeflow-test-…`"* (one empty object, named) |
| postgres | address check → TLS → in **one transaction**: the table exists; a **unique index on exactly (`run_id`, `block_id`)** exists; `INSERT` a probe row; `DELETE` it; then **`ROLLBACK`** | none — the transaction is rolled back, and privileges are still checked as each statement runs |
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
| **httpx** (both webhook senders) | a custom `httpcore` network backend whose `connect_tcp` dials the vetted IP for the one allowed host and refuses any other; TLS uses the URL's host | to verify at build |
| **bucket client** (aiohttp-based, as `miniopy-async`) | an aiohttp resolver returning only the vetted IPs | to verify at build |
| **asyncpg** | connect to the IP; TLS hostname verification against the name. If the driver cannot verify a name while dialling an IP, swap in a resolver override instead | to verify at build |
| **SMTP client** | connect to the IP; STARTTLS / implicit TLS with the name as server hostname | to verify at build |

- **One check, in two places on purpose.** `app/core/egress.py` (the api image: both webhook senders) and
  `delivery-worker/worker/egress.py` (the sinks). It is a deliberate duplicate, like the activity contracts.
  `contracts/test_egress.py` runs one table of cases against both copies, including the case PRD-017 checks for:
  a name that resolves public on the first lookup and private on the second, and must still be refused.
- **BUG-030 is fixed in slice 1, for both existing webhook senders** (the pipeline Webhook block and the job lane's
  `WebhookDeliveryWorkflow`). PRD-017 lists its check under slice 1. CLAUDE.md's *SSRF re-validation* row is
  corrected then.
- **BUG-024** (the check admits `100.64.0.0/10` and multicast) stays the owner's call. The address predicate lives
  in one function per copy, and the shared test table is where its fix is pinned.
- **No redirects, on every sink** (R5). httpx does not follow them by default. The bucket client's region-redirect
  following is turned off.
- **TLS is required on every sink.** The bucket endpoint must be `https`. Postgres must use TLS with certificate
  verification. SMTP must use STARTTLS or implicit TLS, and never sends AUTH in plain text. A destination with a
  self-signed certificate supplies its CA certificate. A destination that cannot do TLS is refused at save and at
  test. (§9, escalation F1.)
- **The second layer: the delivery worker's NetworkPolicy** (§4). Egress is allowed to DNS (kube-dns), the
  Temporal and MinIO services on their ports, and `0.0.0.0/0` **except** the pod CIDR, the service CIDR, RFC 1918,
  link-local and loopback. That catches any library doing a lookup the module did not wrap. The policy allows
  MinIO and Temporal by name, so for those two the code-level check remains the barrier. A user destination that
  resolves to MinIO's address is refused there. ⚠️ **Untested:** k3s enforces NetworkPolicy through its embedded
  controller unless the cluster disabled it. The release gate tests it (§8).
- The LLM worker's call to a user's `base_url` has the same check-then-connect shape. It is out of PRD-017's scope;
  it is noted here because the module above is what fixes it.

## 4. Which worker reaches the outside world (PRD-017 OQ-7)

**Decision: a new task queue, `delivery`, served by a new worker: `delivery-worker/`, its own image, lockfile and
Deployment. It has no app-database access, holds only `DESTINATION_ENCRYPTION_KEY` and read-only MinIO
credentials, and carries the NetworkPolicy above. The sinks (bucket, Postgres, email) and their undo activities run
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
  split each into a DB step and a POST step. That is a later decision. (§9, escalation F5.)
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
| **postgres** | `INSERT … ON CONFLICT (run_id, block_id) DO UPDATE SET content = EXCLUDED.content, delivered_at = EXCLUDED.delivered_at` | the same row is updated → one row | `DELETE … WHERE run_id = $1 AND block_id = $2` at the recorded host/database/table. **0 rows = undone** only if the destination's address and table still equal the recorded ones; otherwise **undo failed: left in place** (R3) |
| **webhook** | body fixed by the workflow (timestamp included) → identical bytes on every attempt | receivers dedupe on **`run_id` + `block_id`** (+ `event`); `block_id` added to the payload (R4) | irreversible |
| **email** | `Message-ID: <{run_id}.{block_id}@{PUBLIC_HOST}>`, or `<{run_id}.{block_id}.failed@…>` for the failure alert | a resend repeats the Message-ID; many mail systems merge it | irreversible |

- The bucket key's run id + block id is what lets rollback delete exactly this delivery and nothing a user put
  under the same prefix.
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
| `load_destination` (workflow queue) | DB activity | — | — | inside the block overhead |

- Each figure lives in the catalog (`Timing`, `budget` computed the way it already is), so users can add up their
  chain, as R6 asks.
- **The rollback reserve** = the undo budget × the number of reversible sinks in the chain. The save-time check
  becomes: forward need + reserve ≤ the run ceiling (`pipeline_max_run_seconds`, 86,400). The refusal names both:
  *"blocks need 28 h (19 h of blocks + 9 h to undo 3 deliveries); the limit is 24 h"*. PRD-017's worked example:
  `scrape → extract → bucket → db → webhook` ≈ 10 h forward + 6 h reserve = 16 h.
- **The undo is never cut by the run deadline** (ADR-014 §7). The reserve inside the ceiling is what makes that
  honest, not a timeout.
- **Final, never retried:** an address refusal, an auth refusal, a permission refusal, a missing table or key, and
  a 4xx from a bucket (other than 408/429). **Transient:** connection errors, timeouts, 5xx, 408/429, Postgres
  connection-class errors, SMTP 4xx replies. **Unknown → final** (PRD-016 R4). The precedent is the storage-fault
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
destination). The `delivery-worker/` image, its Deployment and NetworkPolicy come with `DESTINATION_ENCRYPTION_KEY`
in a new Secret. The egress module fixes BUG-030 in the api image. The catalog gains `bucket`; the Webhook cap
(one per pipeline) lifts; the Webhook block gains `destination_id`, `block_id` and `deliveries` in its payload.
**Slices 2 and 3** add the `postgres` and `email` sinks, with no schema change.

Release order (ADR-012 §2.4, the H.1 pattern): **infra first** (Secret, the delivery worker's Deployment +
NetworkPolicy, the workflow worker's new key); then the delivery worker image; then the api image.

**Release gates** (before the slice is called done in production):
- from the delivery worker pod, a TCP connect to `scrapeflow-postgresql:5432`, Redis and the node's LAN address
  **fails**, and to a public host **succeeds**. This verifies the NetworkPolicy is enforced, which is untested
  today;
- the rebinding test passes against both egress copies;
- a known test secret appears in no API response, workflow history or log line (PRD-017's check).

## 9. Escalations to the PM (owner)

| # | Question | Recommendation |
|---|---|---|
| **F1** | TLS on every sink: `https` bucket endpoints, Postgres with certificate verification, SMTP with STARTTLS or TLS. A self-signed destination brings its CA certificate. Plain connections are refused. | **Accept.** Scraped content and destination passwords otherwise cross the internet in clear. |
| **F2** | The destination test sends **no** webhook POST and **no** email (it checks the address, TLS and login only). | **Accept.** A test POST can trigger the receiver's side effects. A "send me a test message" button can come later. |
| **F3** | Email inline cap 256 KiB, with no attachments; Postgres content cap 10 MiB. | **Accept** for the first slices. |
| **F4** | Editing a destination mid-run moves later attempts to the new address; an undo still targets where the delivery actually landed. | **Accept.** It is how R5 already reads. |
| **F5** | The Webhook block and job-lane webhook deliveries stay on the workflow worker (BUG-030 fixed there); moving them onto the delivery worker is a later decision. | **Accept**, and revisit if a second HTTP-based sink appears. |

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
- BUG-030 closes with slice 1. BUG-024's fix lands in one predicate per egress copy, pinned by one shared test
  table.
- `contracts/` grows: the sink activities' inputs and outputs, through the delivery worker's real parser; and the
  egress table.
- PRD-017's Postgres destination needs a public hostname with TLS, as does every other destination (D9, F1). A
  homelab user's LAN database cannot be one.
