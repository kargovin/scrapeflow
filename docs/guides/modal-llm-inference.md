# Modal.com LLM Inference — Setup Guide

> **Purpose:** Run a self-hosted, OpenAI-compatible LLM inference server on Modal.com's free tier ($30/month). Scale-to-zero means you only pay for active inference seconds — ideal for ScrapeFlow's bursty LLM extraction workload.
> **Last updated:** 2026-04-20

---

## Free Tier — What $30/Month Buys

Modal's Starter plan provides **$30/month in compute credits** (does not roll over). With scale-to-zero, you pay only during active inference — not idle time.

### GPU pricing (US/EU region, includes 1.25× regional multiplier)

| GPU | VRAM | $/hr | $30 buys (compute hrs) |
|-----|------|------|------------------------|
| T4 | 16GB | ~$0.74 | ~40 hrs |
| L4 | 24GB | ~$1.00 | ~30 hrs |
| **A10G** | **24GB** | **~$1.38** | **~22 hrs** |
| L40S | 48GB | ~$2.44 | ~12 hrs |
| A100 40GB | 40GB | ~$2.63 | ~11 hrs |
| A100 80GB | 80GB | ~$3.13 | ~9.5 hrs |
| H100 | 80GB | ~$4.94 | ~6 hrs |

**For ScrapeFlow LLM extraction jobs (scale-to-zero, A10G + 7B model):**
- GPU time per 500-token job ≈ 1–1.5 seconds
- Cost per job ≈ **~$0.0005**
- $30 → **~60,000 extraction jobs/month**

For bursty/development use, the free tier is effectively unlimited.

---

## Model Selection

| Model | FP16 VRAM | INT4 VRAM | Fits on A10G? | Notes |
|-------|-----------|-----------|---------------|-------|
| Mistral 7B | 14GB | ~3.5GB | Yes | Cheapest capable option |
| **Llama 3.1 8B Instruct** | **16GB** | **~4GB** | **Yes** | **Best quality at 7B tier** |
| Qwen 2.5 7B | 16GB | ~4GB | Yes | Strong on structured JSON extraction |
| Qwen 2.5 14B | 30GB | ~8GB | Yes (INT4 only) | Better reasoning; still cheap |
| Llama 3.1 70B | 148GB | ~35GB | No | Needs A100-80GB; 4× cost |

**Recommended:** `Llama-3.1-8B-Instruct` on `A10G` (FP16, 16GB + KV cache headroom fits in 24GB). Throughput: 400–700 tok/s — sufficient for structured extraction.

---

## Setup

### 1. Install Modal and authenticate

```bash
pip install modal
modal setup   # opens browser for auth
```

### 2. Create a Volume to cache model weights

Avoids re-downloading the model (~16GB) on every cold start.

```bash
modal volume create model-cache
```

### 3. Store secrets

```bash
modal secret create llm-secrets \
  VLLM_API_KEY=<generate-a-random-token> \
  HF_TOKEN=<your-huggingface-token>      # required for gated models (Llama)
```

### 4. Create `modal_llm.py`

```python
import modal
import subprocess

GPU = "A10G"
MODEL = "meta-llama/Llama-3.1-8B-Instruct"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("vllm", "huggingface_hub[cli]")
)

app = modal.App("scrapeflow-llm")
model_volume = modal.Volume.from_name("model-cache", create_if_missing=True)

@app.function(
    image=image,
    gpu=GPU,
    volumes={"/models": model_volume},
    secrets=[modal.Secret.from_name("llm-secrets")],
    timeout=600,
    min_containers=0,       # scale-to-zero when idle — critical for free tier
    scaledown_window=300,   # stay warm for 5 min after last request
)
@modal.web_server(8000)
def serve():
    import os
    subprocess.Popen([
        "python", "-m", "vllm.entrypoints.openai.api_server",
        "--model", MODEL,
        "--download-dir", "/models",
        "--port", "8000",
        "--api-key", os.environ["VLLM_API_KEY"],
        "--gpu-memory-utilization", "0.90",
        "--max-model-len", "8192",
    ])
```

### 5. Deploy

```bash
modal serve modal_llm.py     # dev mode — live reload, shuts down when terminal closes
modal deploy modal_llm.py    # persistent deployment
```

Endpoint URL: `https://<workspace>--scrapeflow-llm-serve.modal.run`

### 6. Verify

```bash
# List available models
curl https://<your-endpoint>/v1/models \
  -H "Authorization: Bearer <VLLM_API_KEY>"

# Check Modal logs
modal app logs scrapeflow-llm
```

---

## Calling the Endpoint

The endpoint is fully OpenAI-compatible. Use the OpenAI Python SDK unchanged:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<your-endpoint>/v1",
    api_key="<VLLM_API_KEY>",
)

response = client.chat.completions.create(
    model="meta-llama/Llama-3.1-8B-Instruct",
    messages=[{"role": "user", "content": "Extract title and price from: ..."}],
)
```

---

## Cold Start Behavior

| Scenario | Latency |
|----------|---------|
| Warm container (within scaledown_window) | ~1–2 sec |
| Container boot + model load from Volume (cached) | ~10–30 sec |
| First-ever run — model download from HuggingFace | ~2–5 min |

`scaledown_window=300` keeps the container alive for 5 minutes after the last request, costing ~$0.11 per idle window — an acceptable trade for eliminating cold starts during active sessions.

**Do not set `min_containers=1` on the free tier.** Always-on at A10G costs ~$33/day, which exhausts $30 in under a day.

---

## ScrapeFlow Integration

ScrapeFlow's LLM worker already speaks the OpenAI wire protocol. No worker code changes are needed — configure the job to point at the Modal endpoint:

```json
{
  "llm_provider": "openai",
  "openai_base_url": "https://<your-endpoint>/v1",
  "api_key": "<VLLM_API_KEY>",
  "model": "meta-llama/Llama-3.1-8B-Instruct"
}
```

If surfacing this as a first-class option in the Admin SPA, add it as a provider choice in PRD-011.

---

## Cost Scenarios

| Use case | GPU | Cost | $30 lasts |
|----------|-----|------|-----------|
| Occasional extraction jobs (dev) | A10G | ~$0.0005/job | ~60,000 jobs |
| 100 extraction jobs/day batch | A10G | ~$0.05/day | ~600 days |
| Interactive chatbot, moderate load | A10G | ~$2–4/day | 7–15 days |
| Always-on (`min_containers=1`) | A10G | ~$33/day | <1 day |

Monitor spend at `modal.com/dashboard` → Usage tab.
