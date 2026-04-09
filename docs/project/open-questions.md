# ScrapeFlow — Open Questions

> Items raised during implementation that need a decision before code is written.
> Each entry includes the context, the options, and a recommendation so the discussion starts with something concrete.

---

## Q1 — Should `(user_id, name)` be unique on `api_keys`?

**Raised during:** Phase 1 — `POST /api-keys` implementation
**File:** `api/app/routers/users.py`, `api/app/models/api_key.py`

### Context

`POST /api-keys` creates a new named API key every time it is called. A user can currently create two keys both named `"my key"` — there is no uniqueness enforcement on the name within a user's keyspace.

The model already supports multiple named keys per user (GitHub-style): `name`, `revoked`, and `last_used_at` fields all point to this intent. The question is whether the `name` field should also be unique within a user's keys.

### Options

| Option | Behaviour | Trade-off |
|--------|-----------|-----------|
| **A — unique `(user_id, name)`** | DB constraint prevents duplicate names per user; `POST` returns `409` if name already exists | Better UX, simpler key management, requires catching `IntegrityError` in route handler |
| **B — allow duplicate names** | Users can have multiple keys with the same name | Confusing — two keys called "CI" with no way to distinguish them |
| **C — unique, but soft** | Enforce uniqueness only on non-revoked keys | Allows reuse of names after revocation; more complex constraint logic |

### Recommendation

**Option A.** The `name` field only carries value if it uniquely identifies a key. A DB-level `UniqueConstraint("user_id", "name")` is the right enforcement point — it's race-safe and gives a clear error. Route handler catches `IntegrityError` and returns `409 Conflict`.

If there is a use case for reusing names after revocation, revisit with Option C, but that should be a deliberate call.

### What needs to happen

- Add `UniqueConstraint("user_id", "name", name="uq_api_keys_user_name")` to `ApiKey.__table_args__`
- New Alembic migration
- `POST /api-keys` catches `IntegrityError` → `409 Conflict`
- Test: duplicate name returns 409, different name succeeds

---

## Q2 — `jobs.updated_at` exists but is never updated

**Raised during:** Phase 2 Step 5 — reviewing `job.py` model additions
**File:** `api/app/models/job.py`

### Context

`updated_at` was added to the `jobs` model in Phase 1 with `onupdate=lambda: datetime.now(UTC)`. SQLAlchemy's `onupdate` fires when a column value is changed via the ORM. `result_consumer.py` does mutate job fields (`job.status`, `job.result_path`, `job.error`) so `onupdate` fires there. The open question is whether all other mutation paths (cancel route, Phase 2 status transitions) also touch a field, or if some updates bypass ORM assignment and go through `db.execute(update(...))` — in which case `onupdate` would silently not fire.

### Options

| Option | Behaviour |
|--------|-----------|
| **A — remove it** | Drop the column; no misleading stale data |
| **B — keep, wire it up** | Ensure every route that mutates a job (cancel, result consumer updates) sets at least one field so `onupdate` fires, or explicitly assign `job.updated_at` |
| **C — DB trigger** | Let Postgres maintain it — more reliable than ORM-level `onupdate` |

### Recommendation

**Option B** if the column is useful for the admin panel or change detection. **Option A** if it's never queried — dead columns are a maintenance burden. Decide before Step 12 (the irreversible migration) so it can be cleaned up in the same window if needed.

---

## Q3 — `jobs.webhook_url` column type should be `Text`

**Raised during:** Phase 2 Step 10 — testing webhook URL creation
**File:** `api/app/models/job.py`

### Context

`jobs.webhook_url` is declared as `Mapped[str | None] = mapped_column(nullable=True)` with no explicit SA column type. SQLAlchemy infers `String` (unbounded `VARCHAR`) from the Python type annotation. The `url` column on the same model uses `Text` explicitly. URLs can be arbitrarily long and `Text` is the correct Postgres type for unbounded string storage — consistent with `url`, `webhook_url`, `error`, and `result_path` on the same table.

### What needs to happen

- Change `jobs.webhook_url` column type to `Text` in `api/app/models/job.py`
- New Alembic migration: `ALTER TABLE jobs ALTER COLUMN webhook_url TYPE TEXT`
- Low risk — `VARCHAR` and `TEXT` are functionally equivalent in Postgres; this is a type annotation cleanup

---
