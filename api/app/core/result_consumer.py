import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from miniopy_async import Minio
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from sqlalchemy import select

from app.constants import NATS_JOBS_LLM_SUBJECT, NATS_JOBS_RESULT_SUBJECT
from app.core.db import AsyncSessionLocal
from app.core.diff import compute_json_diff, compute_text_diff
from app.core.webhooks import create_webhook_delivery
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey

logger = structlog.get_logger()

_DURABLE_NAME = "api-result-consumer"


async def _get_previous_completed_run(db, job_id: str, current_run_id: str) -> JobRun | None:
    """Return the most recent completed run for this job, excluding the current run."""
    result = await db.execute(
        select(JobRun)
        .where(
            JobRun.job_id == job_id,
            JobRun.status == "completed",
            JobRun.id != current_run_id,
        )
        .order_by(JobRun.completed_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _handle_result(msg: Msg, js: JetStreamContext, minio: Minio) -> None:
    """Process a single job result message from the worker (ADR-002)."""
    try:
        data = json.loads(msg.data.decode())
        job_id = data["job_id"]
        run_id = data["run_id"]
        worker_status = data["status"]
        minio_path = data.get("minio_path")
        error = data.get("error")
        nats_seq = data.get("nats_stream_seq")
    except (KeyError, json.JSONDecodeError) as e:
        # Malformed message — ack to prevent infinite redelivery, log and discard
        logger.error("Malformed result message, discarding", error=str(e), data=msg.data)
        await msg.ack()
        return

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)

        if run is None:
            logger.warning("Received result for unknown run, discarding", run_id=run_id)
            await msg.ack()
            return

        # Cancellation guard — discard results for cancelled runs (ADR-002)
        if run.status == "cancelled":
            logger.info("Run was cancelled, discarding worker result", run_id=run_id)
            await msg.ack()
            return

        if worker_status == "running":
            run.status = "running"
            run.started_at = msg.metadata.timestamp  # publish time on the worker side
            run.nats_stream_seq = nats_seq  # stored for MaxDeliver advisory (Step 22)

        elif worker_status == "completed":
            if run.status == "running":
                # --- Completed message from HTTP / Playwright scrape worker ---
                job = await db.get(Job, job_id)

                if job is not None and job.llm_config:
                    llm_key = await db.get(UserLLMKey, job.llm_config["llm_key_id"])
                    if llm_key is None:
                        run.status = "failed"
                        run.error = "LLM key not found or deleted"
                        run.completed_at = datetime.now(UTC)
                        if job.webhook_url:
                            create_webhook_delivery(
                                db,
                                job,
                                run_id,
                                event="job.failed",
                                minio_path=None,
                                error=run.error,
                            )
                    else:
                        run.status = "processing"
                        llm_payload: dict[str, Any] = {
                            "job_id": job_id,
                            "run_id": run_id,
                            "raw_minio_path": minio_path,
                            "provider": llm_key.provider,
                            "encrypted_api_key": llm_key.encrypted_api_key,
                            "base_url": llm_key.base_url,
                            "model": job.llm_config["model"],
                            "output_schema": job.llm_config["output_schema"],
                        }
                        await js.publish(NATS_JOBS_LLM_SUBJECT, json.dumps(llm_payload).encode())
                        # Fall through to outer db.commit() and msg.ack()

                else:
                    # No LLM — finalize immediately with text diff
                    run.status = "completed"
                    run.result_path = minio_path
                    run.completed_at = datetime.now(UTC)

                    prev = await _get_previous_completed_run(db, job_id, run_id)
                    diff = None
                    if prev and prev.result_path:
                        diff = await compute_text_diff(minio_path, prev.result_path, minio)
                        run.diff_detected = diff.detected
                        run.diff_summary = diff.summary

                    if job is not None and job.webhook_url:
                        create_webhook_delivery(
                            db,
                            job,
                            run_id,
                            event="job.completed",
                            minio_path=minio_path,
                            diff=diff,
                        )

            elif run.status == "processing":
                # --- Completed message from LLM worker ---
                run.status = "completed"
                run.result_path = minio_path
                run.completed_at = datetime.now(UTC)

                job = await db.get(Job, job_id)
                prev = await _get_previous_completed_run(db, job_id, run_id)
                diff = None
                if prev and prev.result_path:
                    diff = await compute_json_diff(minio_path, prev.result_path, minio)
                    run.diff_detected = diff.detected
                    run.diff_summary = diff.summary

                if job is not None and job.webhook_url:
                    create_webhook_delivery(
                        db,
                        job,
                        run_id,
                        event="job.completed",
                        minio_path=minio_path,
                        diff=diff,
                    )

        else:
            # worker_status == "failed"
            run.status = "failed"
            run.error = error
            run.completed_at = datetime.now(UTC)

            job = await db.get(Job, job_id)
            if job is not None and job.webhook_url:
                create_webhook_delivery(
                    db,
                    job,
                    run_id,
                    event="job.failed",
                    minio_path=None,
                    error=error,
                )

        await db.commit()

    await msg.ack()
    logger.info("Job result processed", job_id=job_id, run_id=run_id, status=worker_status)


async def start_result_consumer(js: JetStreamContext, minio: Minio) -> asyncio.Task:
    """Subscribe to the result subject and return the background task."""

    async def _cb(msg: Msg) -> None:
        await _handle_result(msg, js, minio)

    sub = await js.subscribe(
        NATS_JOBS_RESULT_SUBJECT,
        durable=_DURABLE_NAME,
        cb=_cb,
    )

    async def _run() -> None:
        try:
            await asyncio.Future()  # run until cancelled
        except asyncio.CancelledError:
            await sub.unsubscribe()

    return asyncio.create_task(_run())
