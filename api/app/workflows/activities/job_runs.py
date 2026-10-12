"""Job-lane app-DB activities — the v2 half of ``core/result_consumer.py``'s job path.

Two writers own ``job_runs.status``: the API's cancel and these mirrors. A mirror never moves a run
out of a terminal state it did not set; every write notifies ``job_status`` (positional payload,
``{job_id}:{run_id}:{status}`` — the WS route parses it) in its own transaction.
"""

import uuid
from datetime import UTC, datetime

import structlog
from miniopy_async import Minio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from app.core import minio as minio_client
from app.core.db import AsyncSessionLocal
from app.core.ledger import record_object
from app.core.quota import check_storage_quota, handle_storage_quota_exceeded
from app.core.storage import delete_minio_object
from app.core.webhooks import create_webhook_delivery
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey
from app.models.storage_object import StorageObject
from app.workflows.job import (
    FINISH_JOB_RUN_ACTIVITY,
    JOB_RUN_PROCESSING_ACTIVITY,
    JOB_RUN_STARTED_ACTIVITY,
    LOAD_JOB_LLM_ACTIVITY,
    RECORD_JOB_OBJECTS_ACTIVITY,
    FinishJobRunInput,
    JobLLM,
    JobRunRef,
    RecordJobObjectsInput,
)

logger = structlog.get_logger()

GONE = "gone"
_ACTIVE = ("pending", "running", "processing")

_minio: Minio | None = None


async def get_minio() -> Minio:
    global _minio
    if _minio is None:
        _minio = await minio_client.create_client()
    return _minio


async def _lock(db: AsyncSession, run_id: str) -> JobRun | None:
    return await db.scalar(select(JobRun).where(JobRun.id == uuid.UUID(run_id)).with_for_update())


async def _notify(db: AsyncSession, run: JobRun) -> None:
    await db.execute(
        text("SELECT pg_notify('job_status', :p)"), {"p": f"{run.job_id}:{run.id}:{run.status}"}
    )


async def _discard_unrecorded(db: AsyncSession, paths: list[str], label: str) -> None:
    """Deletes the objects no ledger row holds; a recorded one is charged and stays."""
    if not paths:
        return
    recorded = set(
        (
            await db.scalars(
                select(StorageObject.object_key).where(StorageObject.object_key.in_(paths))
            )
        ).all()
    )
    minio = await get_minio()
    for path in paths:
        if path not in recorded:
            await delete_minio_object(minio, path, label)


async def _move(db: AsyncSession, run: JobRun, frm: str, to: str) -> str:
    if run.status == frm:
        run.status = to
        if to == "running":
            run.started_at = datetime.now(UTC)
        await _notify(db, run)
        await db.commit()
    return run.status


@activity.defn(name=JOB_RUN_STARTED_ACTIVITY)
async def job_run_started(input: JobRunRef) -> str:
    """pending → running. ``running`` now means *scheduled*: no worker reports it on v2."""
    async with AsyncSessionLocal() as db:
        run = await _lock(db, input.run_id)
        if run is None:
            return GONE
        return await _move(db, run, "pending", "running")


@activity.defn(name=JOB_RUN_PROCESSING_ACTIVITY)
async def job_run_processing(input: JobRunRef) -> str:
    async with AsyncSessionLocal() as db:
        run = await _lock(db, input.run_id)
        if run is None:
            return GONE
        return await _move(db, run, "running", "processing")


@activity.defn(name=LOAD_JOB_LLM_ACTIVITY)
async def load_job_llm(input: JobRunRef) -> JobLLM:
    """The job's LLM step as it is now — v1 also read it when the scrape result arrived."""
    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, uuid.UUID(input.run_id))
        job = await db.get(Job, run.job_id) if run is not None else None
        if job is None or not job.llm_config:
            return JobLLM()
        key = await db.get(UserLLMKey, job.llm_config["llm_key_id"])
        if key is None:
            return JobLLM(error="LLM key not found or deleted")
        return JobLLM(
            provider=key.provider,
            encrypted_api_key=key.encrypted_api_key,
            base_url=key.base_url,
            model=job.llm_config["model"],
            output_schema=job.llm_config["output_schema"],
        )


@activity.defn(name=RECORD_JOB_OBJECTS_ACTIVITY)
async def record_job_objects(input: RecordJobObjectsInput) -> str:
    """Charges a stage's objects to the job's owner; idempotent on object key (a retry is a no-op).

    Returns the run's status. Over the storage limit the objects are deleted and the run fails
    (v1's job-lane rule, not the pipeline's finish-and-charge); on a run no longer active the
    objects are dropped uncharged.
    """
    paths = [o.path for o in input.objects]
    async with AsyncSessionLocal() as db:
        run = await _lock(db, input.run_id)
        if run is None or run.status not in _ACTIVE:
            await _discard_unrecorded(db, paths, "job object on a stopped run")
            return GONE if run is None else run.status
        job = await db.get(Job, run.job_id)
        total = sum(o.size for o in input.objects)
        if total > 0 and not await check_storage_quota(job.user_id, db, total):
            minio = await get_minio()
            await handle_storage_quota_exceeded(db, run, paths[0], minio, extra_paths=paths[1:])
            await db.commit()
            logger.warning("storage_quota_exceeded", user_id=str(job.user_id), run_id=str(run.id))
            return run.status
        for obj in input.objects:
            await record_object(
                db, user_id=job.user_id, object_key=obj.path, size=obj.size, job_run_id=run.id
            )
        if input.content_hash is not None:
            run.content_hash = input.content_hash
        await db.commit()
        return run.status


@activity.defn(name=FINISH_JOB_RUN_ACTIVITY)
async def finish_job_run(input: FinishJobRunInput) -> str:
    """Moves an active run to its outcome and writes the webhook row in the same transaction.

    Returns the status the run now holds; a run already terminal is left as it is (the API's
    cancel wins). ``discard`` is deleted unless the ledger holds it, whatever the outcome.
    """
    async with AsyncSessionLocal() as db:
        run = await _lock(db, input.run_id)
        await _discard_unrecorded(db, input.discard, "job object the run will not charge")
        if run is None:
            return GONE
        if run.status not in _ACTIVE:
            return run.status

        run.status = input.status
        run.completed_at = datetime.now(UTC)
        if input.status == "completed":
            run.result_path = input.result_path
            run.warnings = input.warnings
        elif input.status == "failed":
            run.error = input.error

        job = await db.get(Job, run.job_id)
        event = {"completed": "job.completed", "failed": "job.failed"}.get(input.status)
        if (
            event is not None
            and job.webhook_url
            and (not job.webhook_events or event in job.webhook_events)
        ):
            # No diff on v2: change detection is not ported (owner, 2026-10-12).
            await create_webhook_delivery(
                db, job, run.id, event=event, minio_path=input.result_path, error=input.error
            )
        await _notify(db, run)
        await db.commit()
        return run.status
