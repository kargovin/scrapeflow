"""
LLM worker — entry point.

Startup sequence (spec §4.3):
  1. Load config from env vars (requires LLM_KEY_ENCRYPTION_KEY)
  2. Connect to NATS, verify SCRAPEFLOW stream exists
  3. Connect to MinIO, verify bucket exists
  4. Pull subscribe to scrapeflow.jobs.llm (durable: python-llm-worker)
  5. Run worker loop (concurrency capped by LLM_MAX_WORKERS)
"""

import asyncio
import signal

import nats
import nats.errors
import structlog
from miniopy_async import Minio

from .config import settings
from .worker import handle_message

log = structlog.get_logger()

LLM_SUBJECT = "scrapeflow.jobs.llm"
DURABLE_NAME = "python-llm-worker"
STREAM_NAME = "SCRAPEFLOW"


async def run() -> None:
    # ── NATS ─────────────────────────────────────────────────────────────────────
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
        await js.stream_info(STREAM_NAME)
    except Exception as exc:
        log.error("stream_not_found", stream=STREAM_NAME, error=str(exc))
        await nc.drain()
        return

    log.info("nats_connected", url=settings.nats_url)

    # ── MinIO ─────────────────────────────────────────────────────────────────────
    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    if not await minio.bucket_exists(settings.minio_bucket):
        await minio.make_bucket(settings.minio_bucket)
    log.info("minio_connected", bucket=settings.minio_bucket)

    # ── Pull subscription ─────────────────────────────────────────────────────────
    psub = await js.pull_subscribe(
        LLM_SUBJECT,
        durable=DURABLE_NAME,
        stream=STREAM_NAME,
    )
    log.info(
        "subscribed",
        subject=LLM_SUBJECT,
        durable=DURABLE_NAME,
        max_workers=settings.llm_max_workers,
    )

    # ── Worker loop ───────────────────────────────────────────────────────────────
    sem = asyncio.Semaphore(settings.llm_max_workers)

    async def handle_with_sem(msg):
        async with sem:
            await handle_message(msg, js, minio)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        available = sem._value
        if available == 0:
            await asyncio.sleep(0.1)
            continue

        try:
            msgs = await psub.fetch(batch=available, timeout=5)
        except nats.errors.TimeoutError:
            continue
        except Exception as exc:
            log.error("fetch_error", error=str(exc))
            await asyncio.sleep(1)
            continue

        for msg in msgs:
            asyncio.create_task(handle_with_sem(msg))

    # ── Graceful shutdown ─────────────────────────────────────────────────────────
    log.info("shutting_down")
    await nc.drain()


if __name__ == "__main__":
    asyncio.run(run())
