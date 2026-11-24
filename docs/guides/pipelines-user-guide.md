# Pipelines — user guide

## Destinations

### Postgres

A **postgres** block writes its input as one row of a table **you create in your own database**.
ScrapeFlow never creates, alters or drops anything there: it only runs `INSERT … ON CONFLICT`,
`DELETE` and, when you test the destination, reads the catalog to check the table.

**What it takes.** JSON only: an `llm_extract` block's extraction, or a page scraped with
`output_format: json`. A postgres block after an HTML or Markdown page is refused when the pipeline is
saved. The content is held in memory and capped at 10 MiB; a larger input fails the block.

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
certificate from your own CA, paste the CA certificate into the destination. The login (user and
password) is stored encrypted and never shown again.

**The test.** Testing or saving the destination connects, logs in, and in one transaction that is
rolled back: checks the table exists, checks the key, inserts a probe row and deletes it. Nothing is
kept, except that a `SERIAL` or identity column, if you added one, skips a number.

**Retries and timing.** Each try has 30 s. A connection error, a server shutting down or not yet
accepting connections, too many connections, a full disk, a serialization failure or a deadlock is
tried again — five tries, waiting 30 s, 5 min, 50 min and 2 h between them (about 3 h in all); while
it waits the run holds no concurrency slot. A refused login, a missing table or permission, or a
certificate that does not match is final. The undo, on rollback, follows the same ladder and always
goes to the database and table the row was written to, with the destination's current login.
