"""
LLM worker — entry point.

  1. Load config from env vars (requires LLM_KEY_ENCRYPTION_KEY)
  2. Connect to Temporal
  3. Connect to MinIO, verify bucket exists
  4. Poll the `llm` task queue for LLMExtract and the `content` queue for Clean and
     Validate (no workflows)
  5. On SIGTERM, wait up to llm_graceful_shutdown_seconds for in-flight calls
"""

import signal
import sys

if __name__ == "__main__":
    # As PID 1 the process ignores a SIGTERM it has no handler for, and the imports below
    # take ~4 s (anthropic, openai). Nothing is in flight yet, so exit at once; run()
    # replaces this with its graceful handler.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

import asyncio  # noqa: E402
from datetime import timedelta  # noqa: E402

import structlog  # noqa: E402
from miniopy_async import Minio  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.contrib.pydantic import pydantic_data_converter  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from .activity import LLMActivities  # noqa: E402
from .config import settings  # noqa: E402
from .content import ContentActivities  # noqa: E402
from .contracts import CONTENT_QUEUE, LLM_QUEUE  # noqa: E402

log = structlog.get_logger()


async def run() -> None:
    # Installed before anything slow: as PID 1 the process ignores a SIGTERM it has no
    # handler for, so one arriving during startup would stall until the pod's grace
    # period (~420 s) ends in a SIGKILL.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )

    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    if not await minio.bucket_exists(settings.minio_bucket):
        await minio.make_bucket(settings.minio_bucket)
    log.info("minio_connected", bucket=settings.minio_bucket)

    activities = LLMActivities(minio)
    worker = Worker(
        client,
        task_queue=LLM_QUEUE,
        activities=[activities.llm_extract],
        max_concurrent_activities=settings.llm_max_workers,
        graceful_shutdown_timeout=timedelta(
            seconds=settings.llm_graceful_shutdown_seconds
        ),
    )

    content = ContentActivities(minio)
    content_worker = Worker(
        client,
        task_queue=CONTENT_QUEUE,
        activities=[content.clean, content.validate],
        max_concurrent_activities=settings.content_max_workers,
        graceful_shutdown_timeout=timedelta(
            seconds=settings.llm_graceful_shutdown_seconds
        ),
    )

    async with worker, content_worker:
        log.info(
            "temporal_worker_started",
            address=settings.temporal_address,
            namespace=settings.temporal_namespace,
            task_queues=[LLM_QUEUE, CONTENT_QUEUE],
            max_workers=settings.llm_max_workers,
            content_max_workers=settings.content_max_workers,
            graceful_shutdown_s=settings.llm_graceful_shutdown_seconds,
            warmup=settings.llm_warmup_enabled,
        )
        await stop.wait()
        log.info("temporal_worker_stopping")
    log.info("temporal_worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
