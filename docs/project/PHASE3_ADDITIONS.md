# ScrapeFlow Phase 3 — Additions & Discoveries

> **Purpose:** Capture features, gaps, and small additions identified *during* Phase 3 build that were not in the original PRD backlog. Each item records what was found, why it matters, and where it should land.
> **Last updated:** 2026-04-20

---

## ADD-001 — User-level LLM defaults

**Discovered:** During review of `user_llm_keys` model while evaluating self-hosted LLM inference options.

**What was found:**

`user_llm_keys` already stores `provider`, `base_url`, and `encrypted_api_key` at the user level. `jobs.llm_config` only adds `llm_key_id` (which key to use), `model`, and `output_schema` on top of that.

The gap: users must specify `llm_key_id` on every job even when they have only one key and always use the same model. There is no "default key" concept.

**Proposed addition:**

Allow a user to designate one `user_llm_keys` row as their default. When `POST /jobs` or `PATCH /jobs/{id}` receives an `llm_config` with only `output_schema` (no `llm_key_id`), the API resolves the user's default key automatically. `model` should also be defaultable at the user level.

`output_schema` must remain per-job — it defines what to extract and is job-specific by nature.

**Why it matters:**

- Eliminates repetitive `llm_key_id` + `model` on every job for users with a single key
- Enables plug-in of a self-hosted OpenAI-compatible endpoint (e.g. Modal.com vLLM server — see `docs/guides/modal-llm-inference.md`) without reconfiguring each job
- Sets the groundwork for a "platform-managed LLM" tier in Phase 4 if billing/quotas ever cover LLM costs

**Scope assessment:** Small — not a standalone PRD.

**Recommended landing:** Fold into **PRD-011 (Admin SPA)** — the SPA is the natural UI for configuring a default key. The Architect adds a `is_default` boolean (or `default_llm_key_id` FK on `users`) to the ADR for PRD-011, and the Engineer handles it alongside the Admin SPA implementation.

---

## ADD-002 — `instructor` library for structured LLM output

**Discovered:** During Phase 3 architecture review of the LLM worker and PRD-010 (MCP server) discussions around agentic AI patterns.

**What was found:**

The [`instructor`](https://github.com/jxnl/instructor) library patches the OpenAI/Anthropic SDK to accept a `response_model=PydanticModel` argument. It handles schema-to-tool-call conversion, response parsing, and automatic retry on validation failure. It is well-suited to structured extraction pipelines.

**Why it does not apply to ScrapeFlow Phase 3:**

ScrapeFlow's `llm_config.output_schema` is user-defined at job creation time — an arbitrary JSON schema stored in Postgres. `instructor` requires a static Pydantic model known at dev time. Generating Pydantic models dynamically from arbitrary user JSON schemas (`pydantic.create_model()`) breaks down on nested objects, `$ref`, `anyOf`, and discriminated unions.

The simpler and more correct path for user-defined schemas is native structured output:
- **Anthropic:** pass the user schema directly as a `tool_use` input schema
- **OpenAI:** `response_format={"type": "json_schema", "json_schema": <user schema>}`

Validate the LLM response with `jsonschema.validate()` — no Pydantic model needed.

**When `instructor` becomes the right call:**

If a future phase introduces **system-defined extraction templates** (fixed schemas authored by the ScrapeFlow team, not user-supplied) — e.g. "extract e-commerce product fields", "extract news article metadata" — `instructor` is the correct abstraction. The schemas are known at dev time, and the retry + validation machinery pays off.

**Recommended landing:** Phase 4. Flag for PM if a "managed extraction templates" feature enters the roadmap.

---
