# Pipelines — user guide

## Destinations

### Postgres

A **postgres** block writes its input as one row of a table **you create in your own database**.
ScrapeFlow never creates, alters or drops anything there: it only runs `INSERT … ON CONFLICT`,
`DELETE` and, when you test the destination, reads the catalog to check the table.

**What it takes.** JSON only: an `llm_extract` block's extraction, or a page scraped with
`output_format: json`. A postgres block after an HTML or Markdown page is refused when the pipeline is
saved. The content is held in memory and capped at 10 MiB; a larger input fails the block.
JSON that a `JSONB` column cannot hold fails the block before anything is sent: `NaN`, `Infinity`,
`-Infinity`, a NUL character (`\u0000`) or an unpaired surrogate (`\ud800`–`\udfff`) anywhere in it.

**The row.** One row per run and block, in this fixed shape:

| column | type | holds |
|---|---|---|
| `run_id` | `UUID` | the pipeline run |
| `pipeline_id` | `UUID` | the pipeline |
| `block_id` | `TEXT` | the postgres block's id in the pipeline |
| `delivered_at` | `TIMESTAMPTZ` | when the row was last written (the database's `now()`) |
| `content` | `JSONB` | the block's input, as one JSON value |

**The table you run once** (any schema and table name; name them in the destination):

```sql
CREATE TABLE scrapeflow_deliveries (
    run_id       UUID        NOT NULL,
    pipeline_id  UUID        NOT NULL,
    block_id     TEXT        NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL,
    content      JSONB       NOT NULL,
    PRIMARY KEY (run_id, block_id)
);

-- The login you save in the destination:
GRANT USAGE ON SCHEMA public TO scrapeflow_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON scrapeflow_deliveries TO scrapeflow_writer;
```

- **The key on `(run_id, block_id)` is required.** A delivery whose reply was lost is tried again;
  with the key the retry updates the same row, without it the retry would add a second row. A
  `PRIMARY KEY` or a `UNIQUE` constraint on exactly those two columns (either order) both work; a
  partial, deferrable or wider unique index does not. A destination whose table has no such key is
  refused when it is tested or saved.
- **`DELETE` is what rollback uses.** If a later block of the run fails (and no webhook or email has
  gone out yet), ScrapeFlow deletes the row it wrote, by its `run_id` and `block_id`. A login without
  `DELETE` can still be saved, but the test warns first: rollback to that table will fail and the row
  is left in place, as the run page says. A row you already deleted counts as undone.
- `SELECT` is needed because the `WHERE run_id = … AND block_id = …` of that delete reads the two
  columns; `UPDATE` because a retry updates the row.
- You may add columns with defaults, indexes or triggers; ScrapeFlow writes only the five above.

**Connection.** The database must be reachable from the internet (private, local and loopback
addresses are refused, when saved and on every attempt) and must speak **TLS with a certificate valid
for the host name you give**. A server without TLS is refused: nothing is sent in plain text. For a
certificate from your own CA, paste the CA certificate into the destination. A hosted Postgres whose certificate
is signed by the provider's own CA needs that CA pasted the same way (the test fails at *tls* without it). Behind a
connection pooler, use its session mode: a transaction-mode pooler cannot hold the prepared statements ScrapeFlow
uses. If the provider exposes a schema through a public API (often `public`), put the table in a schema of its own. The login (user and
password) is stored encrypted and never shown again.

**The test.** Testing or saving the destination connects, logs in, and in one transaction that is
rolled back: checks the table exists, checks the key, inserts a probe row and deletes it. Nothing is
kept, except that a `SERIAL` or identity column, if you added one, skips a number.
Each problem is reported under the step that met it: an address that cannot be reached under
*address*, the certificate or a server without TLS under *tls*, a refused login under *login*, a lost
connection under the step that was running (*table*, *unique key*, *write*, *delete*, *rollback*).

**Retries and timing.** Each try has 30 s. A connection error, a server shutting down or not yet
accepting connections, too many connections, a full disk, a serialization failure or a deadlock is
tried again — five tries, waiting 30 s, 5 min, 50 min and 2 h between them (about 3 h in all); while
it waits the run holds no concurrency slot. A refused login, a missing table or permission, or a
certificate that does not match is final. The undo, on rollback, follows the same ladder and always
goes to the database and table the row was written to, with the destination's current login.

### Email

An **email** block sends its input as one message to the recipients of a saved **email (SMTP)**
destination, through **your own mail server**: ScrapeFlow runs no mail relay. Name the destination in
the block: `{"destination_id": "<id>"}`.

**What it sends.** Any input: a page (HTML, Markdown or JSON) or an extraction. The subject is
`[ScrapeFlow] <pipeline name>: run <run id>`. The body names the run, the block and the pipeline, links
to the run page, and is followed by the input itself, inline, up to **256 KiB**. A larger input is not
attached; the body says `result too large to include (N KiB); fetch it with run <run id>` instead. On a
monitor's check the body also says what changed: *first check — nothing to compare*, or *changed since
check …* followed by the lines of the page that changed and each extracted value that changed
(`/price: 80 → 70`), as the run page shows them; a long diff is cut and says so.

**When it sends — `events`.** As for a webhook block:
- `["pipeline.result"]` (the default): the result, when the run reaches the block;
- `["pipeline.failed"]`: only when the run breaks — a failure message naming the block that failed, the
  error, and what rollback undid, left in place or kept. It is sent after rollback has finished, even if
  the run broke before reaching the email block. On a run that succeeds this block is skipped
  (*alert only*);
- both.

**It cannot be undone.** An email that has gone out stays sent. So every bucket and postgres block must
come **before** the first email (or webhook) block that sends the result; a pipeline that puts one after
it is refused when it is saved, naming both blocks. Once the email has gone out, a later failure fails
the run but undoes nothing before it: those deliveries read *kept*. An *alert only* email block sends
nothing on the way through, so it may sit anywhere.

**Duplicates.** Each message carries a fixed `Message-ID`, `<run id>.<block id>@<this site's host>`
(a failure message adds `.failed`). A message whose server reply was lost is sent again with the same
`Message-ID`; most inboxes still show both, and a mail filter can match on it. Every message carries the
run id and the block id in its body.

**Connection.** The mail server must be reachable from the internet (private, local and loopback
addresses are refused, when saved and on every attempt) and must offer **TLS**: STARTTLS (usually port
587) or implicit TLS (usually 465). A server that does not offer STARTTLS gets no login and no mail.
The login is sent only over TLS; for a certificate from your own CA, paste the CA certificate into the
destination. The login is stored encrypted and never shown again. Testing or saving the destination
connects, starts TLS and logs in; **it sends no mail**.

**Retries and timing.** Each try has 60 s. An unreachable server, a timeout or a `4xx` reply (the
server deferring the message) is tried again — five tries, waiting 30 s, 5 min, 50 min and 2 h between
them (about 3 h in all); while it waits the run holds no concurrency slot. A refused login, a `5xx`
reply, a certificate that does not match or a private address is final, and fails the run — which
undoes the bucket and postgres deliveries before it, since nothing had gone out. Recipients the server
refused while it accepted others are listed on the block (`recipients refused: …`); the run still
completes.
