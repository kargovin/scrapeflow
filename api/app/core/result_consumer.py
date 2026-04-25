import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
import xxhash
from miniopy_async import Minio
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from sqlalchemy import select, text

from app.constants import NATS_JOBS_LLM_SUBJECT, NATS_JOBS_RESULT_SUBJECT
from app.core.db import AsyncSessionLocal
from app.core.diff import compute_json_diff, compute_text_diff
from app.core.quota import check_storage_quota, increment_storage_bytes
from app.core.webhooks import create_batch_webhook_delivery, create_webhook_delivery
from app.models.batch import Batch, BatchItem
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


async def _compute_content_hash(minio_path: str, minio: Minio) -> str | None:
    """Fetch object bytes and return a 16-char xxh64 hex digest. Returns None on any error."""
    try:
        bucket, _, key = minio_path.partition("/")
        response = await minio.get_object(bucket, key)
        data = await response.read()
        response.close()
        return xxhash.xxh64(data).hexdigest()[:16]
    except Exception:
        return None


async def _get_user_id_for_run(run: JobRun, db) -> uuid.UUID | None:
    """Resolve the owning user's ID from either the job or batch path."""
    if run.job_id is not None:
        job = await db.get(Job, run.job_id)
        return job.user_id if job else None
    if run.batch_item_id is not None:
        item = await db.get(BatchItem, run.batch_item_id)
        if item:
            batch = await db.get(Batch, item.batch_id)
            return batch.user_id if batch else None
    return None


async def _stat_minio_size(minio: Minio, minio_path: str) -> int:
    """Return the byte size of an object. Returns 0 on any error (best-effort)."""
    try:
        bucket, _, key = minio_path.partition("/")
        stat = await minio.stat_object(bucket, key)
        size = stat.size
        return int(size) if isinstance(size, int) else 0
    except Exception:
        return 0


async def _handle_storage_quota_exceeded(db, run: JobRun, minio_path: str, minio: Minio) -> None:
    """Delete the oversized object, mark run failed, and emit the appropriate notify."""
    bucket, _, key = minio_path.partition("/")
    try:
        await minio.remove_object(bucket, key)
    except Exception:
        logger.warning("result_consumer: failed to remove oversized object", path=minio_path)

    run.status = "failed"
    run.error = "storage_quota_exceeded"
    run.completed_at = datetime.now(UTC)

    if run.batch_item_id is not None:
        item = await db.get(BatchItem, run.batch_item_id)
        if item:
            batch = await db.get(Batch, item.batch_id)
            now = datetime.now(UTC)
            item.status = "failed"
            item.error = "storage_quota_exceeded"
            item.completed_at = now
            if batch:
                sq_row = (
                    await db.execute(
                        text(
                            "UPDATE batches SET failed = failed + 1"
                            " WHERE id = :id RETURNING completed, failed"
                        ),
                        {"id": batch.id},
                    )
                ).one()
                sq_completed, sq_failed = sq_row.completed, sq_row.failed
                if sq_completed + sq_failed == batch.total:
                    batch.status = "partial_failure"
                    batch.completed_at = now
                await db.execute(
                    text("SELECT pg_notify('batch_status', :p)"),
                    {
                        "p": json.dumps(
                            {
                                "batch_id": str(batch.id),
                                "completed": sq_completed,
                                "failed": sq_failed,
                                "total": batch.total,
                                "status": batch.status,
                                "item_url": item.url,
                                "item_status": item.status,
                            }
                        )
                    },
                )
    elif run.job_id is not None:
        await db.execute(
            text("SELECT pg_notify('job_status', :p)"),
            {"p": f"{run.job_id}:{run.id}:failed"},
        )


async def _handle_batch_result(
    db, run: JobRun, worker_status: str, minio_path, error, nats_seq, started_at
) -> None:
    """Update batch item + batch counters, notify, and fire batch webhook on completion.

    Caller owns the commit. pg_notify is emitted for all status transitions including
    'running' so WebSocket subscribers see the full lifecycle.
    """
    item = await db.get(BatchItem, run.batch_item_id)
    if item is None:
        return

    batch = await db.get(Batch, item.batch_id)
    if batch is None:
        return

    now = datetime.now(UTC)

    if worker_status == "running":
        run.status = "running"
        run.started_at = started_at
        run.nats_stream_seq = nats_seq
        if batch.status == "queued":
            batch.status = "running"
        await db.execute(
            text("SELECT pg_notify('batch_status', :p)"),
            {
                "p": json.dumps(
                    {
                        "batch_id": str(batch.id),
                        "completed": batch.completed,
                        "failed": batch.failed,
                        "total": batch.total,
                        "status": batch.status,
                        "item_url": item.url,
                        "item_status": "running",
                    }
                )
            },
        )
        return

    if worker_status == "completed":
        run.status = "completed"
        run.result_path = minio_path
        run.completed_at = now
        item.status = "completed"
        item.result_path = minio_path
        item.completed_at = now
        counter_row = (
            await db.execute(
                text(
                    "UPDATE batches SET completed = completed + 1"
                    " WHERE id = :id RETURNING completed, failed"
                ),
                {"id": batch.id},
            )
        ).one()
    else:
        # failed
        run.status = "failed"
        run.error = error
        run.completed_at = now
        item.status = "failed"
        item.error = error
        item.completed_at = now
        counter_row = (
            await db.execute(
                text(
                    "UPDATE batches SET failed = failed + 1"
                    " WHERE id = :id RETURNING completed, failed"
                ),
                {"id": batch.id},
            )
        ).one()

    new_completed, new_failed = counter_row.completed, counter_row.failed

    if new_completed + new_failed == batch.total:
        batch.status = "completed" if new_failed == 0 else "partial_failure"
        batch.completed_at = now
        logger.info(
            "batch_completed",
            batch_id=str(batch.id),
            status=batch.status,
            completed=new_completed,
            failed=new_failed,
        )
        if batch.webhook_url:
            create_batch_webhook_delivery(db, batch, run.id, event="batch.completed")

    await db.execute(
        text("SELECT pg_notify('batch_status', :p)"),
        {
            "p": json.dumps(
                {
                    "batch_id": str(batch.id),
                    "completed": new_completed,
                    "failed": new_failed,
                    "total": batch.total,
                    "status": batch.status,
                    "item_url": item.url,
                    "item_status": item.status,
                }
            )
        },
    )


async def _handle_scrape_completed(
    db,
    run: JobRun,
    js: JetStreamContext,
    minio: Minio,
    job_id: str,
    run_id: str,
    minio_path: str | None,
    warnings: list | None,
    storage_user_id: uuid.UUID | None,
    result_size: int,
) -> None:
    """Handle 'completed' from HTTP/Playwright worker when run was 'running'."""
    if minio_path:
        content_hash = await _compute_content_hash(minio_path, minio)
        if content_hash:
            run.content_hash = content_hash
            prev = await _get_previous_completed_run(db, job_id, run_id)
            if prev and prev.content_hash == content_hash:
                run.status = "completed"
                run.completed_at = datetime.now(UTC)
                run.diff_detected = False
                run.warnings = warnings
                bucket, _, key = minio_path.partition("/")
                try:
                    await minio.remove_object(bucket, key)
                except Exception:
                    logger.warning(
                        "content_dedup: failed to remove history object", path=minio_path
                    )
                logger.info(
                    "content_deduplicated",
                    job_id=job_id,
                    run_id=run_id,
                    content_hash=content_hash,
                )
                return  # skip LLM/diff/webhook; pg_notify fires in _handle_job_result

    job = await db.get(Job, job_id)

    if job is not None and job.llm_config:
        llm_key = await db.get(UserLLMKey, job.llm_config["llm_key_id"])
        if llm_key is None:
            run.status = "failed"
            run.error = "LLM key not found or deleted"
            run.completed_at = datetime.now(UTC)
            if job.webhook_url and (not job.webhook_events or "job.failed" in job.webhook_events):
                create_webhook_delivery(
                    db, job, run_id, event="job.failed", minio_path=None, error=run.error
                )
        else:
            run.status = "processing"
            if storage_user_id and result_size > 0:
                await increment_storage_bytes(storage_user_id, db, result_size)
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
    else:
        # No LLM — finalize immediately with text diff.
        run.status = "completed"
        run.result_path = minio_path
        run.completed_at = datetime.now(UTC)
        run.warnings = warnings
        if storage_user_id and result_size > 0:
            await increment_storage_bytes(storage_user_id, db, result_size)
        prev = await _get_previous_completed_run(db, job_id, run_id)
        diff = None
        if prev and prev.result_path:
            diff = await compute_text_diff(minio_path, prev.result_path, minio)
            run.diff_detected = diff.detected
            run.diff_summary = diff.summary
        if (
            job is not None
            and job.webhook_url
            and (not job.webhook_events or "job.completed" in job.webhook_events)
        ):
            create_webhook_delivery(
                db, job, run_id, event="job.completed", minio_path=minio_path, diff=diff
            )


async def _handle_llm_completed(
    db,
    run: JobRun,
    minio: Minio,
    job_id: str,
    run_id: str,
    minio_path: str | None,
    storage_user_id: uuid.UUID | None,
    result_size: int,
) -> None:
    """Handle 'completed' from LLM worker when run was 'processing'."""
    run.status = "completed"
    run.result_path = minio_path
    run.completed_at = datetime.now(UTC)
    if storage_user_id and result_size > 0:
        await increment_storage_bytes(storage_user_id, db, result_size)
    job = await db.get(Job, job_id)
    prev = await _get_previous_completed_run(db, job_id, run_id)
    diff = None
    if prev and prev.result_path:
        diff = await compute_json_diff(minio_path, prev.result_path, minio)
        run.diff_detected = diff.detected
        run.diff_summary = diff.summary
    if (
        job is not None
        and job.webhook_url
        and (not job.webhook_events or "job.completed" in job.webhook_events)
    ):
        create_webhook_delivery(
            db, job, run_id, event="job.completed", minio_path=minio_path, diff=diff
        )


async def _handle_job_result(
    db,
    run: JobRun,
    js: JetStreamContext,
    minio: Minio,
    job_id: str,
    run_id: str,
    worker_status: str,
    minio_path: str | None,
    error: str | None,
    nats_seq: int | None,
    warnings: list | None,
    storage_user_id: uuid.UUID | None,
    result_size: int,
    started_at,
) -> None:
    """Handle a result message for a regular (non-batch) job run. Caller owns commit.

    Emits pg_notify('job_status') for every status transition so WebSocket
    subscribers always see the final run.status regardless of the path taken.
    """
    if worker_status == "running":
        run.status = "running"
        run.started_at = started_at
        run.nats_stream_seq = nats_seq

    elif worker_status == "completed" and run.status == "running":
        await _handle_scrape_completed(
            db, run, js, minio, job_id, run_id, minio_path, warnings, storage_user_id, result_size
        )

    elif worker_status == "completed" and run.status == "processing":
        await _handle_llm_completed(
            db, run, minio, job_id, run_id, minio_path, storage_user_id, result_size
        )

    else:
        # worker_status == "failed" (or unexpected status — treat as failed)
        run.status = "failed"
        run.error = error
        run.completed_at = datetime.now(UTC)
        job = await db.get(Job, job_id)
        if (
            job is not None
            and job.webhook_url
            and (not job.webhook_events or "job.failed" in job.webhook_events)
        ):
            create_webhook_delivery(
                db, job, run_id, event="job.failed", minio_path=None, error=error
            )

    await db.execute(
        text("SELECT pg_notify('job_status', :p)"),
        {"p": f"{run.job_id}:{run_id}:{run.status}"},
    )


async def _handle_result(msg: Msg, js: JetStreamContext, minio: Minio) -> None:
    """Process a single job result message from the worker (ADR-002)."""
    try:
        data = json.loads(msg.data.decode())
        job_id = data.get("job_id")  # None for batch runs (ADR-006 §4)
        run_id = data["run_id"]
        worker_status = data["status"]
        minio_path = data.get("minio_path")
        error = data.get("error")
        nats_seq = data.get("nats_stream_seq")
        warnings = data.get("warnings")
    except (KeyError, json.JSONDecodeError) as e:
        logger.error("Malformed result message, discarding", error=str(e), data=msg.data)
        await msg.ack()
        return

    # Crawl result messages carry crawl_context — coordinator handles those via its
    # own durable consumer. Ack here to prevent double-processing.
    if data.get("crawl_context") is not None:
        await msg.ack()
        return

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        if run is None:
            logger.warning("Received result for unknown run, discarding", run_id=run_id)
            await msg.ack()
            return

        if run.status == "cancelled":
            logger.info("Run was cancelled, discarding worker result", run_id=run_id)
            if run.job_id is not None:
                await db.execute(
                    text("SELECT pg_notify('job_status', :p)"),
                    {"p": f"{run.job_id}:{run_id}:cancelled"},
                )
            await db.commit()
            await msg.ack()
            return

        # Storage quota enforcement: stat the object post-write, delete + fail if over limit.
        storage_user_id: uuid.UUID | None = None
        result_size: int = 0
        if worker_status == "completed" and minio_path:
            storage_user_id = await _get_user_id_for_run(run, db)
            if storage_user_id:
                result_size = await _stat_minio_size(minio, minio_path)
                if result_size > 0 and not await check_storage_quota(
                    storage_user_id, db, result_size
                ):
                    await _handle_storage_quota_exceeded(db, run, minio_path, minio)
                    await db.commit()
                    await msg.ack()
                    logger.warning(
                        "storage_quota_exceeded",
                        user_id=str(storage_user_id),
                        run_id=run_id,
                    )
                    return

        if run.batch_item_id is not None:
            await _handle_batch_result(
                db, run, worker_status, minio_path, error, nats_seq, msg.metadata.timestamp
            )
            if storage_user_id and result_size > 0 and worker_status == "completed":
                await increment_storage_bytes(storage_user_id, db, result_size)
        else:
            await _handle_job_result(
                db,
                run,
                js,
                minio,
                job_id,
                run_id,
                worker_status,
                minio_path,
                error,
                nats_seq,
                warnings,
                storage_user_id,
                result_size,
                msg.metadata.timestamp,
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
