import asyncio
import json
import logging

from nats.aio.msg import Msg

from app.constants import NATS_JOBS_RESULT_SUBJECT
from app.core.db import AsyncSessionLocal
from app.core.nats import get_jetstream
from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)

_DURABLE_NAME = "api-result-consumer"


async def _handle_result(msg: Msg) -> None:
    """Process a single job result message from the worker (ADR-001)."""
    try:
        data = json.loads(msg.data.decode())
        job_id = data["job_id"]
        worker_status = data["status"]
        minio_path = data.get("minio_path")
        error = data.get("error")
    except (KeyError, json.JSONDecodeError) as e:
        # Malformed message — ack to prevent infinite redelivery, log and discard
        logger.error("Malformed result message, discarding", error=str(e), data=msg.data)
        await msg.ack()
        return

    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)

        if job is None:
            logger.warning("Received result for unknown job, discarding", job_id=job_id)
            await msg.ack()
            return

        # Cancellation enforcement (ADR-001): discard results for cancelled jobs
        if job.status == JobStatus.cancelled:
            logger.info("Job was cancelled, discarding worker result", job_id=job_id)
            await msg.ack()
            return

        if worker_status == "running":
            job.status = JobStatus.running
        elif worker_status == "completed":
            job.status = JobStatus.completed
            job.result_path = minio_path
        else:
            job.status = JobStatus.failed
            job.error = error
        await db.commit()

    await msg.ack()
    logger.info("Job result processed", job_id=job_id, status=worker_status)


async def start_result_consumer() -> asyncio.Task:
    """Subscribe to the result subject and return the background task."""
    js = get_jetstream()
    sub = await js.subscribe(
        NATS_JOBS_RESULT_SUBJECT,
        durable=_DURABLE_NAME,
        cb=_handle_result,
    )

    async def _run() -> None:
        try:
            await asyncio.Future()  # run until cancelled
        except asyncio.CancelledError:
            await sub.unsubscribe()

    return asyncio.create_task(_run())
