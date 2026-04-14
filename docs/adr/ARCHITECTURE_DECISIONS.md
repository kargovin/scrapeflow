# ScrapeFlow — Architecture Decisions & Implementation Analysis

> Analysis of every non-trivial implementation choice made in Phase 1 (Steps 1–5), what alternatives existed, and why the current approach was taken.

---

## 1. Application Startup: `lifespan` Context Manager

**What was done:** FastAPI's `@asynccontextmanager lifespan` initializes all clients (Redis pool, MinIO, NATS) at startup and tears them down in reverse order on shutdown.

**Why:** `lifespan` is FastAPI's modern replacement for the deprecated `@app.on_event("startup")` / `@app.on_event("shutdown")` hooks. Reverse-order teardown (NATS → MinIO → Redis) is intentional: stop consuming work before releasing storage, stop storage before releasing the cache/rate-limit layer.

**Alternatives:**
- `@app.on_event` — deprecated, still works but less composable
- DI frameworks like `dishka` or `lagom` that manage lifecycle automatically — overkill for 3 clients at this scale

---

## 2. Infrastructure Clients: Module-Level Singletons

**What was done:** All three infrastructure clients (Redis, MinIO, NATS) use the same pattern: a module-level `_client: X | None = None` initialized at startup, exposed via a `get_X()` function that `assert`s initialization.

> **Key insight:** The `assert` on `get_X()` is intentional. It turns a missing startup call into a hard crash with a clear message ("NATS client not initialized — call connect() at startup") rather than a confusing `AttributeError: 'NoneType' object has no attribute`. It's an explicit invariant enforced at runtime.

**Alternatives:**
- `app.state` — more idiomatic FastAPI, but requires threading `request.app.state` through every dependency
- A proper DI container — avoids global state entirely, but significantly more boilerplate
- The chosen approach is pragmatic and correct for a single-process service

---

## 3. Database: SQLAlchemy Async + Alembic

**What was done:**
- `create_async_engine` with `pool_pre_ping=True` — tests each connection before checkout to survive Postgres restarts mid-run
- `async_sessionmaker` with `expire_on_commit=False` — prevents lazy-load errors after a commit
- `get_db()` yields one session per request via `async with AsyncSessionLocal()`, automatically closed on exit
- Alembic's `env.py` wraps the async engine with `conn.run_sync(do_run_migrations)` because Alembic itself is synchronous

**Why `expire_on_commit=False`:** After `db.commit()`, SQLAlchemy normally expires all attribute caches to force a reload from DB. In async code, accessing any attribute after a commit would trigger a lazy load — which is illegal in async SQLAlchemy. Disabling expiry keeps the in-memory state valid post-commit. (This is also documented in `PROGRESS.md` under Gotchas.)

**Alternatives:**
- `databases` library — lightweight async SQL, but no ORM
- Tortoise ORM — fully async, but less mature ecosystem and fewer integrations
- `asyncpg` directly — fastest, but manual SQL everywhere
- SQLAlchemy sync + threading — simpler but wastes threads in an async FastAPI context

---

## 4. Auth: Clerk JWT Verification

**What was done:**
- The Clerk SDK (`clerk_backend_api`) handles JWT verification. A module-level `_clerk` singleton is lazily initialized on first use.
- `verify_request` converts the FastAPI/Starlette `Request` into an `httpx.Request` before passing it to Clerk's `authenticate_request`.

> **Key insight:** The Starlette → httpx adapter (`jwt.py:28–33`) exists because the Clerk SDK was built for httpx-based clients, not ASGI frameworks. Rather than writing a raw JWT parser (managing JWKS fetching, key rotation, expiry), the SDK handles all of that — the one-time conversion cost is worth it.

**Alternatives:**
- Verify Clerk JWTs manually with `python-jose` or `PyJWT` using Clerk's JWKS endpoint — more control, no SDK dependency, but you must cache and rotate keys yourself
- `fastapi-clerk-auth` (third-party wrapper) — less maintained
- Auth0 / Supabase — similar tradeoffs, different vendor lock-in

---

## 5. Dual-Auth Dependency: API Key → JWT Fallback

**What was done:** `get_current_user` checks `X-API-Key` header first, then falls back to `Authorization: Bearer` JWT. API key check is fast (one DB lookup by hash). JWT fallback includes a user upsert on first login.

**Why this priority order:** API keys are explicit machine credentials issued intentionally. JWTs are user session tokens. Checking API keys first means programmatic clients are never accidentally authenticated via an ambient JWT, and the code path is shorter for the common headless/automation case.

**Alternatives:**
- Check JWT first — no meaningful difference in correctness, but flips the mental model
- Separate routes for each auth method — more explicit but duplicates route definitions

---

## 6. API Key Hashing: SHA-256 (not bcrypt)

**What was done:** API keys are hashed with `hashlib.sha256` before storage. The raw key is `"sf_" + secrets.token_urlsafe(32)` — approximately 192 bits of CSPRNG entropy.

**Why SHA-256 and not bcrypt/argon2:** bcrypt and argon2 are deliberately slow to resist brute-force *dictionary attacks on passwords*. API keys are not passwords — they're random tokens with ~192 bits of entropy. There is no dictionary to attack. SHA-256 is fast, constant-time (no branching on input), deterministic, and entirely correct here.

**Alternatives:**
- bcrypt — correct security, unjustified slowness per-request (bcrypt is called on *every* API key auth)
- HMAC-SHA256 with a server secret — stronger (leaked DB hashes are useless without the secret), but adds key management complexity
- GitHub-style: store key prefix (first 8 chars) for display + full hash for verification — good UX addition for Phase 2

---

## 7. User Sync: Lazy Upsert on First Login

**What was done:** `get_or_create_user` queries the local DB on every authenticated JWT request. On first login only, it calls the Clerk Users API to fetch the email. Subsequent requests return the cached local row.

**Why not read email from the JWT:** Clerk JWTs contain `sub` (Clerk user ID) and standard claims but not reliably email — email can change, and its presence in the token depends on Clerk's session template config. Fetching from the Clerk API on first login is explicit and correct.

**Alternatives:**
- Clerk webhooks (`user.created` event) to pre-populate users proactively — avoids the per-request DB lookup but requires a publicly reachable webhook endpoint (harder in local dev)
- Customize Clerk session template to embed email in JWT — faster per-request but couples auth config to DB schema and loses the "email can change" safety

---

## 8. Database Models: `Mapped[]` + UUID PKs + Cascades

**What was done:**
- SQLAlchemy 2.0's `Mapped[T]` annotation style — typed, IDE-friendly, no `Column()` boilerplate
- All PKs are `uuid.UUID` (not auto-increment integers)
- `cascade="all, delete-orphan"` on `User → ApiKey` and `User → Job` at ORM level
- `ondelete="CASCADE"` on FK columns at DB level

> **Key insight:** UUID PKs prevent enumeration attacks (an attacker can't discover job IDs by incrementing integers) — critical for multi-tenant isolation. The double cascade (ORM-level + DB-level) ensures deletes work correctly whether triggered through the ORM or raw SQL (e.g., a migration script or admin query).

**Alternatives:**
- Integer auto-increment PKs — simpler, smaller indexes, but enumerable
- ULIDs (sortable UUIDs) — lexicographically sortable, good for `ORDER BY id` queries — a clean upgrade for Phase 2

---

## 9. Enums: `str, enum.Enum` Mixin

**What was done:** `JobStatus` and `OutputFormat` both inherit `(str, enum.Enum)`, stored as Postgres `ENUM` column type.

**Why the `str` mixin:** Makes enum values directly JSON-serializable. Without it, `json.dumps({"status": JobStatus.pending})` raises `TypeError`. Pydantic and FastAPI's JSON encoder handle `str` subclasses natively, so responses serialize cleanly without custom encoders.

**Alternatives:**
- `IntEnum` — smaller storage, faster DB comparison, but opaque in API responses and logs (`0` vs `"pending"`)
- Plain `VARCHAR` with app-level validation — flexible for adding values, but no DB-level constraint
- `VARCHAR` + `CHECK` constraint — more portable than Postgres `ENUM` (easier to add values later), slight tradeoff in clarity

---

## 10. Message Queue: NATS JetStream (not plain NATS)

**What was done:** NATS JetStream (persistent, at-least-once delivery) is used rather than plain NATS pub/sub.

**Why JetStream over plain NATS:** Plain NATS is fire-and-forget — if the Go worker is down when a job is published, the message is lost permanently. JetStream persists messages to disk and redelivers unacknowledged messages. Scrape jobs must not be silently dropped.

**Alternatives:**
- Redis Streams — also persistent, simpler ops since Redis is already in the stack, but NATS is purpose-built for high-throughput messaging with better consumer group semantics
- RabbitMQ — mature AMQP broker, more complex to operate
- Celery + Redis — very common Python task queue, but the scrape worker is Go; Celery is Python-only
- SQS/Cloud queues — not viable for self-hosted homelab

---

## 11. Object Storage: MinIO (`miniopy-async`)

**What was done:** `miniopy-async` (async MinIO Python client) stores raw scrape results. Bucket is auto-created at startup via idempotent `bucket_exists` + `make_bucket`.

**Why MinIO:** The deployment target is a k3s homelab — MinIO provides an S3-compatible API without AWS dependency. `miniopy-async` wraps the MinIO SDK with `aiohttp` to make it non-blocking, compatible with FastAPI's async runtime.

**Alternatives:**
- `aioboto3` pointed at MinIO — works but heavier dependency with more AWS-specific baggage
- Postgres `BYTEA` / `TEXT` for raw results — simpler but Postgres is not designed for large blob storage; MinIO offloads that concern entirely
- Local filesystem — not viable for k3s (pods can reschedule; no shared storage without a PVC or NFS mount)

---

## 12. Testing Strategy: Real Infrastructure, Mock Clerk

**What was done:**
- Integration tests hit real Postgres, Redis, NATS, and MinIO running in Docker Compose
- Only Clerk is mocked — patched at the module level (`patch("app.auth.jwt._clerk", mock_clerk_instance)`)
- `ASGITransport` from `httpx` wires the test client directly to the FastAPI ASGI app — no real HTTP server needed

**Why mock Clerk only:** Clerk is an external SaaS — it cannot be run locally. Every other dependency runs in Docker. This matches the explicit project philosophy (noted in `PROGRESS.md`): mocking the DB led to a real incident where mocked tests passed but a prod migration failed.

**Why patch `_clerk` directly vs patching the method:** Both `jwt.py` and `user_sync.py` call `get_clerk()`, which returns the module-level `_clerk`. Patching `_clerk` directly means both modules get the mock without needing two separate `patch()` targets.

---

## 13. Settings: `pydantic-settings` with Repo-Root `.env`

**What was done:** `pydantic_settings.BaseSettings` resolves the `.env` path relative to `settings.py`'s location (two directories up: `api/app/settings.py` → repo root). Docker Compose overrides service-specific values (e.g., `DATABASE_URL` uses Docker service names instead of `localhost`).

**Why a single `.env` at repo root:** Works identically for local dev (`uvicorn` directly) and Docker Compose (`env_file` + `environment` overrides). `extra="ignore"` prevents errors from unrelated env vars in the shell environment.

**Alternatives:**
- Per-environment files (`.env.development`, `.env.production`) with `dotenv-cli` — cleaner separation but more files to manage
- Pure environment variables (no `.env`) — correct 12-factor style for prod, inconvenient in local dev
- `dynaconf` — more powerful multi-env config system, heavier dependency

---

## 14. CORS: Wildcard with Explicit TODO

**What was done:** `allow_origins=["*"]` in development, with a comment marking it for replacement in production. The code also notes the browser spec issue: `allow_credentials=True` + wildcard origin is rejected by browsers — must be explicit origins in production.

**Why this is fine now:** Tests use `httpx` directly (no browser, no CORS enforcement). The wildcard is a dev convenience only. The `TODO(k8s)` comment documents exactly what needs to change and why before deployment.

---

## 15. Job CRUD: DB Insert Before NATS Publish

**What was done:** `POST /jobs` inserts the job row with `status=pending` first, then publishes the job ID to NATS JetStream. The two operations are intentionally ordered and not wrapped in a distributed transaction.

**Why insert first:** If NATS is unavailable at publish time, the job still exists in the DB as `pending`. A future retry mechanism (background task, admin endpoint, or worker poll) can pick up orphaned `pending` jobs. Publishing to NATS first with no DB record means a NATS delivery succeeds but the worker finds nothing to process — the job is silently lost.

**Alternatives:**
- Publish to NATS first — simpler publish code, but loses jobs on DB failure after publish
- Transactional outbox pattern — guarantees exactly-once delivery by writing the NATS message into a DB table and using a relay process, but is significant added complexity for MVP
- Two-phase commit — not viable across a relational DB and a message broker without a coordinator

---

## 16. Job Ownership Checks: 404 Not 403

**What was done:** `GET /jobs/{id}` and `DELETE /jobs/{id}` return **404** when the job exists but belongs to a different user, rather than 403 Forbidden.

**Why 404:** Returning 403 confirms to the caller that the job ID exists — an attacker enumerating UUIDs can map which IDs are live. 404 reveals nothing: the resource simply does not exist *for this user*. UUID v4 PKs already make enumeration statistically infeasible, but the 404 response is an additional defense-in-depth layer consistent with how multi-tenant APIs (GitHub, Stripe) handle cross-tenant access.

**Alternatives:**
- 403 Forbidden — semantically more precise ("you don't have permission"), but leaks resource existence
- 401 Unauthorized — incorrect; the user is authenticated, just not authorized for this resource

---

## 17. Pagination: `limit`/`offset` from Day One

**What was done:** `GET /jobs` requires `limit` (default 50, max 200) and `offset` (default 0) query parameters from the first implementation.

**Why upfront:** API clients build around the response shape immediately. Adding pagination to an endpoint that previously returned an unbounded list is a breaking change — existing clients that do `response.json()` instead of `response.json()["items"]` break. Including it from day one also prevents the unbounded `SELECT * WHERE user_id = ?` query that will cause memory spikes at scale.

**Why offset pagination and not cursor-based:** Offset is simpler to implement and easier for clients to consume (jump to page N). The downside — inconsistent results if rows are inserted between pages — is acceptable for a job list that is mostly append-only. Cursor-based pagination is a clean Phase 2 upgrade if needed.

**Alternatives:**
- Cursor-based (keyset) pagination — stable across concurrent inserts, better for large datasets, harder to implement and consume
- No pagination — correct for a prototype, but retrofitting it later is a breaking API change

---

## 18. Job Cancellation: Status Flag, Not Message

**What was done:** `DELETE /jobs/{id}` sets `status = cancelled` in the DB. It does not send a cancellation signal to the NATS queue or the worker.

**Why status flag only (for now):** The Go worker does not exist yet. The cancellation contract between API and worker is explicitly deferred — the worker will be designed to check `status != cancelled` before writing results. This prevents a race where the worker completes after the user cancels and silently overwrites the cancelled status back to `completed`.

**The contract (documented here):** Worker must check job status before writing results. If `status == cancelled`, the worker discards the result and does not update the DB. This is a poll-before-write contract, not a push-based cancellation.

**Alternatives:**
- Publish a cancellation event to NATS — allows in-flight interruption, but requires the worker to subscribe to a separate cancellation subject and handle mid-scrape abort
- Worker-side acknowledgment — worker publishes a "cancellation acknowledged" event; API waits for it — significant complexity for MVP

---

## 19. NATS Stream Lifecycle: Init Container in Docker Compose

**What was done:** A short-lived `nats-init` service (`natsio/nats-box`) runs once at startup, creates the `SCRAPEFLOW` JetStream stream, then exits. The `api` service has `depends_on: nats-init: condition: service_completed_successfully` so it only starts after the stream exists.

**Why outside the API:** The stream is shared infrastructure — both the API (result consumer) and the Go worker (job dispatcher) depend on it. Embedding stream creation in the API means the worker can't start independently of the API. An init container (or k8s init container in production) creates the stream exactly once, idempotently, without any service owning it.

**Why `natsio/nats-box` and not the `nats:2.10-alpine` image:** The `nats:2.10-alpine` image only contains `nats-server` — the CLI tool (`nats`) is not included. `nats-box` is the official NATS tooling image that bundles the full CLI.

**The `|| nats stream info` fallback:** `nats stream add` returns exit 1 if the stream already exists. The fallback (`|| nats stream info SCRAPEFLOW`) makes the init container idempotent — if the stream already exists (e.g. on `docker compose up` after a previous run with persistent volume), the container still exits 0.

**Alternatives:**
- API asserts stream exists at startup and errors clearly — simpler but couples stream lifecycle to API lifecycle
- Manual one-time setup — fragile, breaks on `docker compose down -v`
- Terraform/Pulumi to manage NATS resources — correct for prod, overkill for local dev

---

## 20. Rate Limiting: Fixed Window Counter (MVP)

**What was done:** Per-user rate limiting uses a Redis fixed-window counter. The key `scrapeflow:rl:<user_id>:<window>` is incremented with `INCR` on each request; the window bucket is derived from `epoch // window_seconds`. TTL is set on first increment so keys auto-expire. Returns HTTP 429 when the counter exceeds the configured limit.

**Why for now:** Fixed window is simple, cheap (2–3 Redis ops), and correct enough for MVP quota enforcement. No extra data structures, no cleanup jobs.

**Known limitation:** At window boundaries, a user can fire up to `2× rate_limit_requests` in a short burst — once at the end of window N and again at the start of window N+1. For low-volume personal use this is acceptable.

**Planned upgrade (Phase 2/3):** Replace with a **sliding window log** using a Redis sorted set of request timestamps (`ZADD` + `ZREMRANGEBYSCORE` + `ZCARD`). This eliminates the boundary burst problem at the cost of more memory per user and slightly more complex logic.

**Alternatives considered:**
- Sliding window log (sorted set) — correct, no burst at boundary, higher cost; deferred to later
- Token bucket — allows short bursts intentionally, better for API clients; more complex to implement correctly in Redis
- `slowapi` / `fastapi-limiter` libraries — abstract away implementation but hide the Redis operations; we want full control for this use case

---

## 21. API Key Routes: Raw Key Returned Once via Transient Attribute

**What was done:** `POST /users/api-keys` generates a key, stores only the SHA-256 hash, then attaches the raw key as a transient Python attribute (`api_key.key = raw_key`) on the ORM object before returning it. The `ApiKeyCreatedResponse` model includes the `key` field; `ApiKeyResponse` (used by list/revoke) does not.

**Why two response models:** The separation enforces at the type level that the raw key can only appear in the creation response. After that point it is unrecoverable — the DB only has the hash. This mirrors GitHub's PAT design.

**Why transient attribute instead of a separate DTO:** The ORM object is returned directly to FastAPI's response serializer. Attaching `key` as a plain Python attribute (not a mapped column) means SQLAlchemy ignores it for persistence while Pydantic's `from_attributes=True` picks it up for serialization. Clean and no extra data class needed.

---

## 22. Clerk JWT `authorized_parties=None` in Dev

**What was done:** `AuthenticateRequestOptions(authorized_parties=None)` is passed to the Clerk SDK in development. The code comment marks it for replacement with an explicit domain list in production.

**Why not `[]`:** An empty list causes the Clerk SDK to reject all tokens — including those issued from the Clerk dashboard for testing. `None` skips the `azp` claim check entirely. This was discovered when dashboard-issued JWTs returned `TOKEN_INVALID_AUTHORIZED_PARTIES`.

**Production plan:** Set `authorized_parties=["https://scrapeflow.govindappa.com"]` loaded from a `CLERK_AUTHORIZED_PARTIES` env var.

---

## Summary Table

| Decision | Approach taken | Key reason | Main alternative not taken |
|---|---|---|---|
| Startup lifecycle | `lifespan` context manager | Modern FastAPI, clean teardown order | `@app.on_event` (deprecated) |
| Infrastructure clients | Module-level singletons + `assert` | Simple, fast, hard crash on missing init | `app.state`, DI container |
| Session `expire_on_commit` | `False` | Prevents async lazy-load crash post-commit | Leave default, call `db.refresh()` everywhere |
| Clerk JWT verification | Clerk SDK with httpx adapter | Handles JWKS, rotation, expiry automatically | Manual `PyJWT` + JWKS fetch |
| API key hashing | SHA-256 | Keys are random; bcrypt overhead is unjustified | HMAC-SHA256 (adds server secret) |
| User sync | Lazy upsert on first JWT login | Email not reliably embedded in JWT | Clerk webhooks |
| Primary keys | UUID v4 | Non-enumerable; multi-tenant isolation | Integer auto-increment |
| Enum base class | `str, enum.Enum` | Auto JSON-serializable without custom encoder | `IntEnum` |
| Message queue | NATS JetStream | At-least-once delivery; jobs must not be dropped | Redis Streams (already in stack) |
| Object storage | MinIO (`miniopy-async`) | S3-compatible, self-hosted, async-native | Postgres BYTEA, local filesystem |
| Test infrastructure | Real Docker services, mock Clerk only | Mock/prod divergence is a documented prior incident | In-memory SQLite, full mocking |
| Settings | `pydantic-settings` + repo-root `.env` | Single file works for local dev and Docker Compose | Pure env vars, per-env dotfiles |
| Job publish order | DB insert → NATS publish | Orphaned `pending` jobs are recoverable; lost NATS messages are not | Publish first, transactional outbox |
| Cross-tenant job access | Return 404 (not 403) | 403 leaks resource existence; 404 reveals nothing | 403 Forbidden |
| List endpoint pagination | `limit`/`offset` from day one | Breaking to add later; prevents unbounded queries | No pagination, cursor-based |
| Job cancellation | Status flag only (`status=cancelled`) | Worker contract deferred; worker polls status before writing | NATS cancellation signal |
| NATS stream creation | `nats-init` Docker Compose service (`nats-box`) | Stream is shared infra; neither API nor worker should own its lifecycle | API creates stream on startup, manual setup |
| Rate limiting | Fixed window counter (`INCR` + `EXPIRE`) | Simple, cheap, correct for MVP; 2–3 Redis ops | Sliding window log (planned upgrade), token bucket |
| API key creation response | Two Pydantic models (`ApiKeyCreatedResponse` includes `key`, `ApiKeyResponse` does not) | Enforces at type level that raw key is shown once only | Single model with optional `key` field |
| Clerk `authorized_parties` | `None` in dev, explicit domain list in prod | Empty list `[]` rejects all tokens including dashboard-issued ones | Hardcode domain, skip check entirely |
| LLM/webhook secret encryption | Fernet (symmetric AES-128-CBC + HMAC) | Single shared key; deterministic — decryptable at runtime without per-record metadata | Asymmetric encryption (complex key management), plaintext (unacceptable) |
| Scheduler multi-instance coordination | `FOR UPDATE SKIP LOCKED` | Advisory lock semantics; competing replicas silently skip locked rows without blocking | Redis distributed lock, single-scheduler instance requirement |
| Webhook delivery mechanism | `webhook_deliveries` table polled by loop | Delivery loop can retry with backoff, track attempts, and survive API restarts | NATS publish (fire-and-forget, no retry tracking), immediate HTTP POST in result consumer |
| LLM worker isolation | Separate Python service (`llm-worker/`) | Isolates LLM dependency surface (Anthropic/OpenAI SDKs) from API; independent scaling | LLM calls inside API request handler, inline in result consumer |
| Diff strategy | Text diff (non-LLM runs) + JSON diff (LLM runs) | Non-LLM produces markdown/HTML — text diff is meaningful; LLM produces structured JSON — field-level diff is more useful | Unified diff algorithm for both, no diff |
| Scrape worker pool | Pull consumer + semaphore | Fetch exactly `available_slots` messages — prevents AckWait timer starting on messages that can't run yet | Push consumer (starts timers regardless of capacity), thread pool |

---

## 23. Fernet Encryption for LLM API Keys and Webhook Secrets

**What was done:** LLM API keys (`user_llm_keys.encrypted_api_key`) and webhook secrets (`jobs.webhook_secret`) are encrypted at rest using Fernet (from the `cryptography` library). The encryption key (`LLM_KEY_ENCRYPTION_KEY`) is a 32-byte base64-urlsafe string stored in the `.env` file, validated at startup.

**Why Fernet:** LLM API keys must be decryptable at runtime — the LLM worker needs the plaintext key to call the provider API. Irreversible hashing (bcrypt) is inappropriate here. Fernet provides authenticated encryption (AES-128-CBC + HMAC-SHA256) with a simple API: `Fernet.encrypt()` / `Fernet.decrypt()`. It handles IV generation and MAC verification internally.

**Why symmetric (not asymmetric):** The same process (API) encrypts and later decrypts (via `get_fernet` dependency). There is no trust boundary between encryptor and decryptor — asymmetric encryption would add complexity with no security benefit.

**Key management:** A single `LLM_KEY_ENCRYPTION_KEY` is used across all records. A future improvement (Phase 3 / hardening) would rotate to per-record keys or a KMS-backed envelope encryption scheme if the key is ever compromised.

**Alternatives:**
- Asymmetric encryption — correct only if the encryptor and decryptor are separate (e.g. client encrypts, server decrypts)
- Vault / KMS — production-grade key management; over-engineered for homelab Phase 2
- Plaintext — unacceptable for third-party API keys

---

## 24. Scheduler Multi-Instance Coordination: `FOR UPDATE SKIP LOCKED`

**What was done:** The scheduler loop uses `SELECT ... FOR UPDATE SKIP LOCKED` when querying jobs with `next_run_at <= NOW()`. Each row is locked for the duration of the job dispatch transaction; competing API replicas that run the same query silently skip already-locked rows.

**Why:** Without `SKIP LOCKED`, a second replica would block waiting for the first to release its lock, then dispatch the same job a second time after the lock is released. `SKIP LOCKED` eliminates this — a replica only processes rows it actually acquired a lock on. Commit-per-job (rather than a batch commit) ensures each row lock is released immediately after the run is created and NATS message published.

**Why DB commit before NATS publish:** If the process crashes after committing but before publishing to NATS, the `job_runs` row exists with `status='pending'`. The stale-pending recovery loop (re-publishes runs with `status='pending'` older than 10 minutes) catches this. The inverse (NATS publish before commit) would silently lose the run on DB failure with no recovery path.

**Alternatives:**
- Redis distributed lock — works, but adds a Redis round-trip and a separate lock TTL concern
- Single scheduler instance (enforced via k8s leader election) — eliminates the problem but adds infrastructure complexity and a SPOF

---

## 25. Webhook Delivery: `webhook_deliveries` Table + Polling Loop

**What was done:** When a scrape result triggers a webhook, a `WebhookDelivery` row is inserted (atomically with the `job_runs` status update) with `status='pending'`. A separate `webhook_delivery_loop` background task polls every 15 seconds for pending deliveries and fires the HTTP POST with exponential backoff.

**Why a DB table instead of NATS or immediate HTTP:**
- Durability: delivery state survives API restarts
- Retry tracking: `attempts`, `last_error`, `next_attempt_at`, and `status='exhausted'` are all queryable by the admin API
- Backoff: the backoff schedule (0s, 30s, 5m, 30m, 2h) is enforced by `next_attempt_at <= NOW()` — no in-memory timers needed
- Admin retrigger: `POST /admin/webhooks/deliveries/{id}/retry` resets `attempts=0` and `status='pending'` — trivial because the delivery is a DB row

**Why not publish to NATS:** NATS JetStream redelivery is configurable (max deliver, ack wait) but not designed for multi-hour backoff schedules or admin-triggered retries. The delivery table gives full observability and control.

**HMAC signing:** Every delivery POST includes `X-ScrapeFlow-Signature: sha256=<hmac>`. The webhook secret (Fernet-encrypted at rest) is decrypted at delivery time and used as the HMAC key over the JSON payload body. Receivers can verify integrity and authenticity without a shared session.

**Alternatives:**
- Immediate HTTP POST in the result consumer — no retry, no tracking, blocks the consumer on slow endpoints
- Celery task — heavier infrastructure, requires a separate worker and broker for a background loop already running in the API

---

## 26. LLM Worker as a Separate Python Service

**What was done:** LLM structured extraction runs as an independent `llm-worker` Python service. It consumes from `scrapeflow.jobs.llm` (a separate NATS subject), decrypts the user's API key, calls the provider (Anthropic or OpenAI-compatible), writes structured JSON to MinIO, and publishes a result event.

**Why a separate service:**
- **Dependency isolation:** Anthropic and OpenAI SDKs (and their transitive dependencies) are contained to the LLM worker. They do not bloat the API image.
- **Independent scaling:** LLM calls are slow (seconds to tens of seconds). The API can handle thousands of requests/second while the LLM worker processes a handful concurrently. Keeping them separate allows the right resource allocation.
- **Failure isolation:** An LLM provider outage or SDK bug cannot bring down the API.
- **Subject-based routing:** The result consumer transitions the `job_runs` row to `status='processing'` and publishes the LLM payload to `scrapeflow.jobs.llm` — the LLM worker picks it up without the API knowing which provider or model is involved.

**Why `scrapeflow.jobs.llm` is a separate subject from `scrapeflow.jobs.run.*`:** The LLM worker needs a different message schema (includes `encrypted_api_key`, `output_schema`, `raw_minio_path`). Subject-based routing also means the Go and Playwright workers never accidentally receive LLM dispatch messages.

**Alternatives:**
- LLM calls inline in the result consumer — simpler, but blocks the result consumer on slow LLM calls
- LLM calls as a FastAPI background task — correct, but couples LLM retries to the API's NATS consumer retry budget

---

## 27. Text Diff vs JSON Diff Strategy

**What was done:** The diff algorithm used for change detection depends on the upstream worker:
- **HTTP / Playwright worker** (no LLM) → `compute_text_diff()`: fetches two `history/` MinIO objects, diffs line-by-line using `difflib.unified_diff`, returns `{detected: bool, summary: {added: int, removed: int}}`
- **LLM worker** → `compute_json_diff()`: parses both MinIO objects as JSON, compares field-by-field, returns `{detected: bool, summary: {changed_fields: [...]}}`

**Why two strategies:** The output format determines what a meaningful "diff" is. Markdown/HTML output from scrape workers is best compared as text (line diffs surface wording changes). Structured JSON from the LLM is better compared as a field-level diff (a change in `{"price": 100}` → `{"price": 105}` should report `changed_fields: ["price"]`, not a raw text diff of the JSON string).

**Discriminated dispatch:** The result consumer dispatches to `compute_text_diff` vs `compute_json_diff` based on `job_run.status` at the time the `completed` result arrives: `status='running'` → text diff (from scrape worker); `status='processing'` → JSON diff (from LLM worker).

**Alternatives:**
- Single unified diff algorithm — loses the structured signal for LLM outputs; unified text diff of a JSON string is noisy and hard to interpret
- LLM-based diff ("did the content change meaningfully?") — expensive, latent, circular (using LLM to evaluate LLM output)

---

## 28. Pull Consumer + Semaphore Worker Pool

**What was done:** Both the Playwright worker and LLM worker use a pull consumer (`js.pull_subscribe()` / nats-py equivalent) with an `asyncio.Semaphore`. The fetch batch size equals `semaphore._value` (current available slots) — the worker only fetches as many messages as it can immediately start processing.

**Why pull over push:** A push consumer delivers messages as fast as NATS can send them, regardless of worker capacity. If the worker is slow (Playwright rendering, LLM call), unprocessed messages start their AckWait timers as soon as they're delivered. When AckWait expires, NATS redelivers — the worker processes the same message twice even though it was never lost. Pull consumers start the AckWait timer only when the message is actually fetched.

**Why semaphore over threading:** The workers are async Python — `asyncio.Semaphore` caps concurrency within the event loop without spawning OS threads. Each job runs as a coroutine; the semaphore ensures at most `MAX_WORKERS` coroutines are in-flight simultaneously. This matches the Playwright browser's constraints (each job gets its own context; too many concurrent contexts OOMs).

**Alternatives:**
- Push consumer with `max_inflight` — NATS-side flow control, but AckWait timing still starts at delivery, not at processing
- Thread pool executor — correct, but mixes async and threading; adds complexity with no benefit in an async-first codebase
