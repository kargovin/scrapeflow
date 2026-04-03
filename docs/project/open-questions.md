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
