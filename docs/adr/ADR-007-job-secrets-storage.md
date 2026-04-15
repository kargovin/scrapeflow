# ADR-007: Job Secrets Storage

**Status:** Accepted
**Date:** 2026-04-15
**Deciders:** @karthik

---

## Context

Phase 3 introduces two features that require storing sensitive credentials on a per-job basis:

- **PRD-005 (Proxy rotation):** `proxy_url` contains embedded credentials — `http://user:password@host:port`. Must be encrypted at rest and never returned in API responses.
- **PRD-008 (Authenticated scraping):** `cookies` contains session tokens and CSRF tokens — effectively passwords for the authenticated session. Same sensitivity level.

The question was whether to add encrypted columns directly to the `jobs` table (following the `llm_config` JSONB precedent from Phase 2), or to introduce a dedicated `job_secrets` table.

**Why the `jobs` table approach was rejected:**

Phase 3 adds exactly two credential types. A column-per-credential approach works today, but the Phase 4 roadmap includes form-based login credentials, OAuth tokens, and custom `Authorization` headers — each a new sensitive field. Adding encrypted columns to `jobs` repeatedly across phases results in a wide table of nullable encrypted columns. Extracting those to a secrets table in Phase 4 would require a migration under production load.

The correct time to build the abstraction is Phase 3, while there is no production data to migrate.

**Why the `llm_config` JSONB precedent is not sufficient justification:**

`llm_config` is job *configuration* (model name, output schema, temperature) that happens to contain a key reference (`llm_key_id`). The key itself lives in `user_llm_keys`. The `jobs` table never stores the LLM API key value. Proxy and cookie values are themselves the credentials — there is no indirection layer. They require encryption, not just a reference.

---

## Decisions

### 1. New `job_secrets` table

```sql
CREATE TYPE job_secret_type AS ENUM ('proxy', 'cookies');

CREATE TABLE job_secrets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    secret_type     job_secret_type NOT NULL,
    encrypted_value TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_id, secret_type)
    -- one row per secret type per job
);

CREATE INDEX idx_job_secrets_job_id ON job_secrets (job_id);
```

**Encryption:** Same Fernet cipher used for `user_llm_keys` in Phase 2. The `LLM_KEY_ENCRYPTION_KEY` env var is reused — it is already shared between the API and LLM worker for this purpose, and its name is a misnomer that reflects its origin, not its scope.

**`secret_type` ENUM:** Adding a new credential type in Phase 4 requires an `ALTER TYPE job_secret_type ADD VALUE '...'` migration. This uses the same `COMMIT`/`BEGIN` pattern established in Phase 2 for `jobstatus` additions.

### 2. Stored values by type

| `secret_type` | `encrypted_value` contents (before encryption) |
|---|---|
| `proxy` | Plain string: `http://user:pass@host:port` or `socks5://...` |
| `cookies` | JSON array: `[{"name": "...", "value": "...", "domain": "...", ...}]` |

Both are stored as encrypted text. The API decrypts at dispatch time and includes the plaintext value in the fat NATS message. Workers never touch the encryption layer.

### 3. API write path

`POST /jobs` and `PATCH /jobs/{id}` accept `proxy_url` and `cookies` in the request body. After the `jobs` row is created/updated, the API upserts into `job_secrets`:

```python
# Upsert — insert or replace if the secret type already exists for this job
INSERT INTO job_secrets (job_id, secret_type, encrypted_value)
VALUES ($1, $2, $3)
ON CONFLICT (job_id, secret_type)
DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value,
              updated_at = NOW()
```

### 4. API read path — presence flags only

`GET /jobs/{id}` and `GET /jobs` responses **never** return secret values. Instead, the job response includes boolean presence flags:

```json
{
  "job_id": "...",
  "has_proxy": true,
  "has_cookies": false,
  ...
}
```

This pattern is consistent with PRD-008's explicit requirement and mirrors how Phase 2 handles webhook secret visibility (shown once at creation, never returned again).

### 5. Dispatch path

At dispatch time, the API resolves job secrets before publishing to NATS:

```python
secrets = db.query(JobSecret).filter_by(job_id=job.id).all()
secrets_map = {s.secret_type: fernet.decrypt(s.encrypted_value) for s in secrets}

message = {
    ...
    "credentials": {
        "proxy_url": secrets_map.get("proxy"),       # plaintext or None
        "cookies": json.loads(secrets_map["cookies"]) if "cookies" in secrets_map else None
    }
}
```

Workers receive plaintext credentials in the fat message. They never query `job_secrets` directly.

### 6. Secret deletion

Secrets are deleted via `ON DELETE CASCADE` when the `jobs` row is deleted (hard delete). There is no standalone "delete proxy credential" endpoint in Phase 3 — users rotate by re-`PATCH`ing the job with a new value, which triggers the upsert.

---

## Consequences

**Positive:**
- `jobs` table stays clean — no encrypted credential columns
- New credential types in Phase 4 require only an ENUM migration and application-layer handling; no `jobs` schema change
- `UNIQUE (job_id, secret_type)` enforces one secret per type per job at the DB level
- Cascade delete keeps `job_secrets` self-cleaning

**Negative:**
- Every job dispatch now requires an additional `SELECT` on `job_secrets` — one extra DB query per dispatch. At current scale this is negligible; at high dispatch volume, batch-loading secrets alongside job data should be considered.
- The `LLM_KEY_ENCRYPTION_KEY` env var name is misleading (it now encrypts proxy and cookie secrets too). Renaming it is a Phase 4 housekeeping item to avoid confusion for new engineers.
