from pydantic import BaseModel


class PlaywrightOptions(BaseModel):
    wait_strategy: str = "load"
    timeout_seconds: int = 60
    block_images: bool = False


class JobMessage(BaseModel):
    job_id: str
    run_id: str
    url: str
    output_format: str
    playwright_options: PlaywrightOptions | None = None


class ResultMessage(BaseModel):
    job_id: str
    run_id: str
    status: str
    minio_path: str | None = None
    nats_stream_seq: int | None = None
    error: str | None = None

    def to_nats_bytes(self) -> bytes:
        # exclude_none omits fields with no value — matches the Go worker's omitempty tags
        return self.model_dump_json(exclude_none=True).encode()
