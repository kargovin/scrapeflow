"""
Coordinator entrypoint.

Startup sequence:
  1. Connect to NATS, assert SCRAPEFLOW stream exists
  2. Connect to MinIO, assert bucket exists
  3. Re-enqueue stalled dispatched items (crash recovery)
  4. Run two concurrent async loops: dispatch_loop + result_handler_loop
"""

import asyncio
import signal

import nats
import structlog
from miniopy_async import Minio

from coordinator.dispatcher import dispatch_loop, reenqueue_stalled
from coordinator.config import settings
from coordinator.constants import NATS_STREAM_NAME
from coordinator.db import AsyncSessionLocal
from coordinator.result_handler import result_handler_loop

log = structlog.get_logger()


async def run() -> None:
    async def _on_disconnect():
        log.warning("nats_disconnected")

    async def _on_reconnect():
        log.info("nats_reconnected")

    nc = await nats.connect(
        settings.nats_url,
        max_reconnect_attempts=-1,
        reconnect_time_wait=2,
        disconnected_cb=_on_disconnect,
        reconnected_cb=_on_reconnect,
    )
    js = nc.jetstream()

    try:
        await js.stream_info(NATS_STREAM_NAME)
    except Exception as exc:
        log.error("stream_not_found", stream=NATS_STREAM_NAME, error=str(exc))
        await nc.drain()
        return

    log.info("nats_connected", url=settings.nats_url)

    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    if not await minio.bucket_exists(settings.minio_bucket):
        await minio.make_bucket(settings.minio_bucket)
    log.info("minio_connected", bucket=settings.minio_bucket)

    # Crash recovery: reset stalled dispatched items before loops start.
    async with AsyncSessionLocal() as db:
        await reenqueue_stalled(db)
        await db.commit()

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    dispatch_task = asyncio.create_task(dispatch_loop(js))
    handler_task = asyncio.create_task(result_handler_loop(js, minio))

    log.info("coordinator_started")

    await stop.wait()

    log.info("coordinator_shutting_down")
    dispatch_task.cancel()
    handler_task.cancel()
    await asyncio.gather(dispatch_task, handler_task, return_exceptions=True)
    await nc.drain()


if __name__ == "__main__":
    asyncio.run(run())
