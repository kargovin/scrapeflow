"""
Per-job processing logic for the LLM worker.

Separated from main.py so the job lifecycle can be unit-tested
without standing up a NATS pull consumer loop.
"""

import json
from typing import Any

import structlog
from miniopy_async import Minio

from .config import settings
from .llm import call_llm
from .models import JobMessage, ResultMessage
from .storage import upload

log = structlog.get_logger()

RESULT_SUBJECT = "scrapeflow.jobs.result"


async def publish_result(js: Any, result: ResultMessage) -> None:
    await js.publish(RESULT_SUBJECT, result.to_nats_bytes())


async def fetch_content(minio: Minio, raw_minio_path: str) -> str:
    """Read raw scrape content from MinIO; return as a UTF-8 string.

    raw_minio_path is bucket-qualified: "{bucket}/history/{job_id}/{ts}.{ext}"
    """
    bucket = settings.minio_bucket
    object_key = raw_minio_path[len(bucket) + 1 :]
    response = await minio.get_object(bucket, object_key)
    try:
        data = await response.read()
    finally:
        response.close()
        await response.release()
    return data.decode("utf-8")


async def handle_message(msg: Any, js: Any, minio: Minio) -> None:
    """Full ADR-002 job lifecycle for a single LLM job."""
    # --- Step 1: Parse the incoming job message ---
    try:
        job = JobMessage.model_validate_json(msg.data)
    except Exception as exc:
        log.error("malformed_message", error=str(exc), data=msg.data[:200])
        await msg.ack()
        return

    log.info("job_received", job_id=job.job_id, run_id=job.run_id, model=job.model)

    # --- Step 2: Publish "running" with nats_stream_seq (ADR-002 §3) ---
    nats_seq = msg.metadata.sequence.stream
    await publish_result(
        js,
        ResultMessage(
            job_id=job.job_id,
            run_id=job.run_id,
            status="running",
            nats_stream_seq=nats_seq,
        ),
    )

    try:
        # --- Step 3: Fetch raw scrape content from MinIO ---
        content = await fetch_content(minio, job.raw_minio_path)

        # --- Step 4: Call LLM (decrypt key, truncate, dispatch to provider) ---
        result_dict = await call_llm(
            encrypted_api_key=job.encrypted_api_key,
            provider=job.provider,
            base_url=job.base_url,
            model=job.model,
            content=content,
            output_schema=job.output_schema,
        )

        # --- Step 5: Upload structured JSON result to MinIO ---
        result_bytes = json.dumps(result_dict).encode()
        minio_path = await upload(minio, job.job_id, result_bytes)

        # --- Step 6: Publish "completed" ---
        await publish_result(
            js,
            ResultMessage(
                job_id=job.job_id,
                run_id=job.run_id,
                status="completed",
                minio_path=minio_path,
            ),
        )
        # --- Step 7: Ack after MinIO write succeeds (ADR-002 §6) ---
        await msg.ack()
        log.info("job_completed", job_id=job.job_id, run_id=job.run_id, path=minio_path)

    except Exception as exc:
        log.error("job_failed", job_id=job.job_id, run_id=job.run_id, error=str(exc))
        await publish_result(
            js,
            ResultMessage(
                job_id=job.job_id,
                run_id=job.run_id,
                status="failed",
                error=str(exc),
            ),
        )
        # Ack even on failure — the API already knows it failed via the result event.
        # Re-delivery won't recover a bad LLM key or a missing MinIO object.
        await msg.ack()
