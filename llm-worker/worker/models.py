from typing import Any, Literal

from pydantic import BaseModel, Field


class JobMessage(BaseModel):
    # Pinned wire version (ADR-011) — see the note in the playwright worker's models.
    # This lane matters most for a version mismatch: a stale worker here re-runs a
    # billable call against the user's own API key.
    schema_version: Literal[3]

    # What this stage's output is keyed on — job_runs.id, supplied by the dispatcher
    # and used verbatim (ADR-011 §2). Never legitimately absent, so it fails loudly.
    artifact_id: str = Field(min_length=1)
    # Required here, unlike on the scrape message: the LLM stage is only reachable on
    # the job and batch lanes, and both create a job_runs row.
    run_id: str
    raw_minio_path: (
        str  # bucket-qualified: "{bucket}/history/{artifact_id}/scrape.{ext}"
    )
    provider: str  # "anthropic" | "openai_compatible"
    encrypted_api_key: str  # Fernet ciphertext — decrypted by llm.py
    base_url: str | None = None  # required for openai_compatible, None for anthropic
    model: str
    output_schema: dict[str, Any]


class ResultMessage(BaseModel):
    # job_id has left the wire (ADR-011 §2) — the API reads it from the run row.
    run_id: str
    status: str  # "running" | "completed" | "failed"
    source: str = "llm"
    minio_path: str | None = None
    nats_stream_seq: int | None = None
    error: str | None = None

    def to_nats_bytes(self) -> bytes:
        # exclude_none omits unset fields — matches the Go worker's omitempty tags
        return self.model_dump_json(exclude_none=True).encode()
