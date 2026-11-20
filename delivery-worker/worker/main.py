"""
Delivery worker — entry point (ADR-015 §4).

  1. Load config from env vars (requires DESTINATION_ENCRYPTION_KEY)
  2. Connect to Temporal; a MinIO client for ScrapeFlow's copies (read-only user: no bucket check)
  3. Poll the `delivery` task queue, at most MAX_CONCURRENT_ACTIVITIES at once (no workflows)
  4. On SIGTERM, wait up to delivery_graceful_shutdown_seconds for in-flight activities
"""

import signal
import sys

if __name__ == "__main__":
    # As PID 1 the process ignores a SIGTERM it has no handler for; run() replaces this with its
    # graceful handler.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

import asyncio  # noqa: E402
from datetime import timedelta  # noqa: E402

import aiohttp  # noqa: E402
import structlog  # noqa: E402
from miniopy_async import Minio  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.contrib.pydantic import pydantic_data_converter  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from .bucket import BucketActivities  # noqa: E402
from .checks import test_bucket, test_webhook  # noqa: E402
from .config import settings  # noqa: E402
from .contracts import DELIVERY_QUEUE  # noqa: E402
from .source import Source  # noqa: E402

log = structlog.get_logger()

# Bounds memory: ≈ 8 × the largest in-memory payload (ADR-015 §7).
MAX_CONCURRENT_ACTIVITIES = 8



def activities(source: Source) -> list:
    bucket = BucketActivities(source)
    return [test_webhook, test_bucket, bucket.deliver_bucket, bucket.undo_bucket]


def minio_client() -> Minio:
    # A plain session: miniopy-async's default retries 5xx itself, under the workflow's retries.
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
    )
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        session=session,
    )


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    minio = minio_client()
    worker = Worker(
        client,
        task_queue=DELIVERY_QUEUE,
        activities=activities(Source(minio, settings.minio_bucket)),
        max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
        graceful_shutdown_timeout=timedelta(seconds=settings.delivery_graceful_shutdown_seconds),
    )
    async with worker:
        log.info(
            "temporal_worker_started",
            address=settings.temporal_address,
            namespace=settings.temporal_namespace,
            task_queue=DELIVERY_QUEUE,
            max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
            graceful_shutdown_s=settings.delivery_graceful_shutdown_seconds,
        )
        await stop.wait()
        log.info("temporal_worker_stopping")
    await minio.close_session()
    log.info("temporal_worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
