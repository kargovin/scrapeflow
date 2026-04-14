from typing import Any

from pydantic import BaseModel


class JobMessage(BaseModel):
    job_id: str
    run_id: str
    raw_minio_path: str  # bucket-qualified: "{bucket}/history/{job_id}/{ts}.{ext}"
    provider: str  # "anthropic" | "openai_compatible"
    encrypted_api_key: str  # Fernet ciphertext — decrypted by llm.py
    base_url: str | None = None  # required for openai_compatible, None for anthropic
    model: str
    output_schema: dict[str, Any]


class ResultMessage(BaseModel):
    job_id: str
    run_id: str
    status: str  # "running" | "completed" | "failed"
    minio_path: str | None = None
    nats_stream_seq: int | None = None
    error: str | None = None

    def to_nats_bytes(self) -> bytes:
        # exclude_none omits unset fields — matches the Go worker's omitempty tags
        return self.model_dump_json(exclude_none=True).encode()
