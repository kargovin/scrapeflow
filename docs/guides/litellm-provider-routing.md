# LiteLLM — Multi-Provider LLM Routing

> **Purpose:** Run a single OpenAI-compatible proxy that routes LLM calls across multiple providers (Modal, Groq, Together.ai) with automatic fallback when a provider exhausts its free tier or returns errors.
> **Last updated:** 2026-04-20

---

## How It Works

LiteLLM is a self-hosted proxy (no GPU needed — pure Python) that presents a single OpenAI-compatible endpoint to ScrapeFlow. Behind it, you configure a provider priority list and per-provider budget limits. When the primary provider hits its credit cap or returns a 429, LiteLLM falls through to the next provider automatically.

```
ScrapeFlow LLM worker
      │  openai_base_url → http://litellm:4000
      ▼
  LiteLLM proxy  (Docker, your server)
      ├── 1. Modal vLLM       — primary  ($30/month free)
      ├── 2. Groq             — fallback (free tier)
      └── 3. Together.ai      — last resort ($25 signup credit)
```

ScrapeFlow never changes. One `openai_base_url`, one API key, routing is invisible.

---

## Setup

### 1. Provider API keys

Collect keys from each provider before starting:

| Provider | Where to get key |
|----------|-----------------|
| Modal vLLM | Your deployed endpoint + `VLLM_API_KEY` from `modal secret` |
| Groq | console.groq.com → API Keys |
| Together.ai | api.together.xyz → Settings → API Keys |

### 2. `litellm/config.yaml`

```yaml
model_list:
  - model_name: default        # alias ScrapeFlow uses
    litellm_params:
      model: openai/meta-llama/Llama-3.1-8B-Instruct
      api_base: https://<your-modal-endpoint>/v1
      api_key: os.environ/MODAL_API_KEY
      # Modal is a custom endpoint — LiteLLM has no built-in price for it.
      # Without these, budget_limit accumulates $0 per call and never triggers.
      # Estimate: A10G @ $0.000383/sec, ~1.25s per 500-token job ≈ $0.00048/job
      # Approximate that as $0.30 input + $0.60 output per 1M tokens.
      input_cost_per_token: 0.0000003
      output_cost_per_token: 0.0000006
      budget_limit: 28.00      # stop at $28, leave $2 buffer before Modal cuts off
      budget_duration: 1mo

  - model_name: default
    litellm_params:
      model: groq/llama-3.1-8b-instant
      api_key: os.environ/GROQ_API_KEY

  - model_name: default
    litellm_params:
      model: together_ai/meta-llama/Llama-3.1-8B-Instruct
      api_key: os.environ/TOGETHER_API_KEY

router_settings:
  routing_strategy: simple-shuffle
  fallbacks:
    - default: ["default", "default", "default"]

litellm_settings:
  drop_params: true            # ignore unsupported params per provider
  request_timeout: 60
```

> Multiple entries with the same `model_name` form the fallback chain in order.

### 3. `litellm/.env`

```env
MODAL_API_KEY=your-vllm-api-key
GROQ_API_KEY=gsk_...
TOGETHER_API_KEY=...
LITELLM_MASTER_KEY=sk-litellm-secret   # ScrapeFlow uses this to call the proxy
```

### 4. `docker-compose.yml` addition

Add to your existing dev compose or run standalone:

```yaml
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    ports:
      - "4000:4000"
    volumes:
      - ./litellm/config.yaml:/app/config.yaml
    env_file:
      - ./litellm/.env
    command: ["--config", "/app/config.yaml", "--port", "4000"]
```

### 5. ScrapeFlow job config

```json
{
  "llm_provider": "openai",
  "openai_base_url": "http://litellm:4000",
  "api_key": "sk-litellm-secret",
  "model": "default"
}
```

Or for production (external LiteLLM host):

```json
{
  "openai_base_url": "http://<your-server-ip>:4000",
  "api_key": "sk-litellm-secret",
  "model": "default"
}
```

---

## Fallback Behavior

| Trigger | LiteLLM action |
|---------|---------------|
| Provider budget exceeded (`budget_limit`) | Skip to next provider |
| 429 rate limit | Retry once, then fall through |
| 5xx / timeout | Immediate fall through |
| All providers exhausted | Returns error to ScrapeFlow |

`budget_limit: 28.00` on Modal leaves a $2 buffer — prevents hitting Modal's hard cutoff mid-request.

---

## Monitoring

LiteLLM exposes a spend dashboard at `http://localhost:4000/ui` (requires `LITELLM_MASTER_KEY` login). Shows per-provider spend, request counts, and fallback events.

To check spend via CLI:
```bash
curl http://localhost:4000/spend/logs \
  -H "Authorization: Bearer sk-litellm-secret" | jq '.spend_per_model'
```

---

## Related

- `docs/guides/modal-llm-inference.md` — setting up the Modal vLLM primary endpoint
- `docs/project/PHASE3_ADDITIONS.md` ADD-001 — user-level LLM defaults (fold into PRD-011)
