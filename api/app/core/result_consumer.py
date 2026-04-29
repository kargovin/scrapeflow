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
from app.core.quota import (
    check_storage_quota,
    handle_storage_quota_exceeded,
    increment_storage_bytes,
)
from app.core.storage import delete_minio_object, stat_minio_size
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


async def _try_increment_storage(db, run: JobRun, user_id: uuid.UUID, size: int) -> bool:
    """Increment storage in a savepoint; returns False on failure (outer tx stays clean).

    Idempotent: if run.storage_accounted_at is already set, the increment was recorded
    on a prior delivery and is skipped to prevent double-counting on NATS redelivery.
    """
    if run.storage_accounted_at is not None:
        return True
    try:
        async with db.begin_nested():
            await increment_storage_bytes(user_id, db, size)
        run.storage_accounted_at = datetime.now(UTC)
        return True
    except Exception:
        logger.error("storage_increment_failed", user_id=str(user_id), size=size)
        return False


async def _handle_batch_result(
    db,
    run: JobRun,
    js: JetStreamContext,
    minio: Minio,
    worker_status: str,
    minio_path,
    error,
    nats_seq,
    started_at,
    storage_user_id: uuid.UUID | None,
    result_size: int,
    source: str = "scrape",
) -> None:
    """Update batch item + batch counters, notify, and fire batch webhook on completion.

    Caller owns the commit. pg_notify is emitted for all status transitions including
    'running' so WebSocket subscribers see the full lifecycle.

    LLM flow: when batch.llm_config is set and the scrape worker completes, the run
    transitions to 'processing' and a message is dispatched to the LLM subject. The
    counter increment and batch-done check are deferred until the LLM result arrives
    (detected by run.status == 'processing' on the second completed message).
    """
    item = await db.get(BatchItem, run.batch_item_id)
    if item is None:
        return

    batch = await db.get(Batch, item.batch_id)
    if batch is None:
        return

    # Idempotency guard: NATS redelivery after commit-before-ack must be a no-op.
    if run.status in ("completed", "failed"):
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

    if worker_status == "completed" and run.status == "running":
        if batch.llm_config:
            llm_key = await db.get(UserLLMKey, batch.llm_config["llm_key_id"])
            if llm_key is None:
                if minio_path:
                    await delete_minio_object(
                        minio, minio_path, "orphaned batch object on LLM key miss"
                    )
                run.status = "failed"
                run.error = "LLM key not found or deleted"
                run.completed_at = now
                item.status = "failed"
                item.error = run.error
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
            else:
                accounting_ok = True
                if storage_user_id and result_size > 0:
                    accounting_ok = await _try_increment_storage(
                        db, run, storage_user_id, result_size
                    )
                if not accounting_ok:
                    run.status = "failed"
                    run.error = "storage_accounting_failed"
                    run.completed_at = now
                    item.status = "failed"
                    item.error = "storage_accounting_failed"
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
                else:
                    run.status = "processing"
                    llm_payload: dict[str, Any] = {
                        "job_id": None,
                        "run_id": str(run.id),
                        "raw_minio_path": minio_path,
                        "provider": llm_key.provider,
                        "encrypted_api_key": llm_key.encrypted_api_key,
                        "base_url": llm_key.base_url,
                        "model": batch.llm_config["model"],
                        "output_schema": batch.llm_config["output_schema"],
                    }
                    await js.publish(NATS_JOBS_LLM_SUBJECT, json.dumps(llm_payload).encode())
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
                                    "item_status": "processing",
                                }
                            )
                        },
                    )
                    return  # counters updated when LLM result arrives

        else:
            accounting_ok = True
            if storage_user_id and result_size > 0:
                accounting_ok = await _try_increment_storage(db, run, storage_user_id, result_size)
            if accounting_ok:
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
                run.status = "failed"
                run.error = "storage_accounting_failed"
                run.completed_at = now
                item.status = "failed"
                item.error = "storage_accounting_failed"
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

    elif worker_status == "completed" and run.status == "processing" and source == "llm":
        accounting_ok = True
        if storage_user_id and result_size > 0:
            accounting_ok = await _try_increment_storage(db, run, storage_user_id, result_size)
        if accounting_ok:
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
            run.status = "failed"
            run.error = "storage_accounting_failed"
            run.completed_at = now
            item.status = "failed"
            item.error = "storage_accounting_failed"
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

    elif worker_status == "failed":
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

    else:
        # Unhandled combination (e.g. redelivered scrape "completed" on a "processing" item) — ignore.
        return

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
            await create_batch_webhook_delivery(db, batch, run.id, event="batch.completed")

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
            if prev and prev.content_hash == content_hash and prev.result_path:
                run.status = "completed"
                run.completed_at = datetime.now(UTC)
                run.diff_detected = False
                run.warnings = warnings
                run.result_path = prev.result_path
                await delete_minio_object(minio, minio_path, "history object on content dedup")
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
            if minio_path:
                await delete_minio_object(minio, minio_path, "orphaned object on LLM key miss")
            run.status = "failed"
            run.error = "LLM key not found or deleted"
            run.completed_at = datetime.now(UTC)
            if job.webhook_url and (not job.webhook_events or "job.failed" in job.webhook_events):
                await create_webhook_delivery(
                    db, job, run_id, event="job.failed", minio_path=None, error=run.error
                )
        else:
            if storage_user_id and result_size > 0:
                if not await _try_increment_storage(db, run, storage_user_id, result_size):
                    run.status = "failed"
                    run.error = "storage_accounting_failed"
                    run.completed_at = datetime.now(UTC)
                    if job.webhook_url and (
                        not job.webhook_events or "job.failed" in job.webhook_events
                    ):
                        await create_webhook_delivery(
                            db, job, run_id, event="job.failed", minio_path=None, error=run.error
                        )
                    return
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
            await db.execute(
                text("SELECT pg_notify('job_status', :p)"),
                {"p": f"{job_id}:{run_id}:processing"},
            )
    else:
        if storage_user_id and result_size > 0:
            if not await _try_increment_storage(db, run, storage_user_id, result_size):
                run.status = "failed"
                run.error = "storage_accounting_failed"
                run.completed_at = datetime.now(UTC)
                if (
                    job is not None
                    and job.webhook_url
                    and (not job.webhook_events or "job.failed" in job.webhook_events)
                ):
                    await create_webhook_delivery(
                        db, job, run_id, event="job.failed", minio_path=None, error=run.error
                    )
                return
        run.status = "completed"
        run.result_path = minio_path
        run.completed_at = datetime.now(UTC)
        run.warnings = warnings
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
            await create_webhook_delivery(
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
    if storage_user_id and result_size > 0:
        if not await _try_increment_storage(db, run, storage_user_id, result_size):
            run.status = "failed"
            run.error = "storage_accounting_failed"
            run.completed_at = datetime.now(UTC)
            job = await db.get(Job, job_id)
            if (
                job
                and job.webhook_url
                and (not job.webhook_events or "job.failed" in job.webhook_events)
            ):
                await create_webhook_delivery(
                    db, job, run_id, event="job.failed", minio_path=None, error=run.error
                )
            return
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
    if (
        job is not None
        and job.webhook_url
        and (not job.webhook_events or "job.completed" in job.webhook_events)
    ):
        await create_webhook_delivery(
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
    source: str = "scrape",
) -> None:
    """Handle a result message for a regular (non-batch) job run. Caller owns commit.

    Emits pg_notify('job_status') for every status transition so WebSocket
    subscribers always see the final run.status regardless of the path taken.
    """
    # Idempotency guard: NATS redelivery after commit-before-ack must be a no-op.
    if run.status in ("completed", "failed"):
        return

    if worker_status == "running":
        run.status = "running"
        run.started_at = started_at
        run.nats_stream_seq = nats_seq

    elif worker_status == "completed" and run.status == "running":
        await _handle_scrape_completed(
            db, run, js, minio, job_id, run_id, minio_path, warnings, storage_user_id, result_size
        )

    elif worker_status == "completed" and run.status == "processing" and source == "llm":
        await _handle_llm_completed(
            db, run, minio, job_id, run_id, minio_path, storage_user_id, result_size
        )

    elif worker_status == "failed":
        run.status = "failed"
        run.error = error
        run.completed_at = datetime.now(UTC)
        job = await db.get(Job, job_id)
        if (
            job is not None
            and job.webhook_url
            and (not job.webhook_events or "job.failed" in job.webhook_events)
        ):
            await create_webhook_delivery(
                db, job, run_id, event="job.failed", minio_path=None, error=error
            )
    # else: unhandled combination (e.g. redelivered scrape "completed" on a "processing" run) — ignore

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
        source = data.get("source", "scrape")
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

        storage_user_id: uuid.UUID | None = None
        result_size: int = 0
        if worker_status == "completed" and minio_path:
            storage_user_id = await _get_user_id_for_run(run, db)
            if storage_user_id:
                result_size = await stat_minio_size(minio, minio_path)
                if result_size > 0 and not await check_storage_quota(
                    storage_user_id, db, result_size
                ):
                    await handle_storage_quota_exceeded(db, run, minio_path, minio)
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
                db,
                run,
                js,
                minio,
                worker_status,
                minio_path,
                error,
                nats_seq,
                msg.metadata.timestamp,
                storage_user_id,
                result_size,
                source=source,
            )
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
                source=source,
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
