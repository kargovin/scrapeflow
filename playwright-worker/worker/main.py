"""
Playwright worker — entry point (Python/asyncio, Chromium via Playwright).

Startup sequence (spec §4.2):
  1. Load config from env vars
  2. Connect to NATS, verify SCRAPEFLOW stream exists
  3. Connect to MinIO, verify bucket exists
  4. Launch Chromium browser (headless)
  5. Create pull consumer on scrapeflow.jobs.run.playwright
  6. Run worker loop (concurrency capped by PLAYWRIGHT_MAX_WORKERS)
"""

import asyncio
import signal

import nats
import nats.errors
import structlog
from miniopy_async import Minio
from playwright.async_api import async_playwright

from .config import settings
from .worker import handle_message

log = structlog.get_logger()

PLAYWRIGHT_SUBJECT = "scrapeflow.jobs.run.playwright"
DURABLE_NAME = "python-playwright-worker"
STREAM_NAME = "SCRAPEFLOW"


async def run() -> None:
    # ── NATS ────────────────────────────────────────────────────────────────────
    # nats-py requires callbacks to be coroutine functions (async def), not plain lambdas
    async def _on_disconnect():
        log.warning("nats_disconnected")

    async def _on_reconnect():
        log.info("nats_reconnected")

    nc = await nats.connect(
        settings.nats_url,
        max_reconnect_attempts=-1,  # retry forever on disconnect
        reconnect_time_wait=2,
        disconnected_cb=_on_disconnect,
        reconnected_cb=_on_reconnect,
    )
    js = nc.jetstream()

    # Assert the SCRAPEFLOW stream exists — fail fast if infra is missing.
    # js.stream_info() raises an exception if the stream is not found.
    try:
        await js.stream_info(STREAM_NAME)
    except Exception as exc:
        log.error("stream_not_found", stream=STREAM_NAME, error=str(exc))
        await nc.drain()
        return

    log.info("nats_connected", url=settings.nats_url)

    # ── MinIO ────────────────────────────────────────────────────────────────────
    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    if not await minio.bucket_exists(settings.minio_bucket):
        await minio.make_bucket(settings.minio_bucket)
    log.info("minio_connected", bucket=settings.minio_bucket)

    # ── Playwright browser ────────────────────────────────────────────────────────
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    log.info("browser_launched")

    # ── Pull subscription ─────────────────────────────────────────────────────────
    psub = await js.pull_subscribe(
        PLAYWRIGHT_SUBJECT,
        durable=DURABLE_NAME,
        stream=STREAM_NAME,
    )
    log.info(
        "subscribed",
        subject=PLAYWRIGHT_SUBJECT,
        durable=DURABLE_NAME,
        max_workers=settings.playwright_max_workers,
    )

    # ── Worker loop ───────────────────────────────────────────────────────────────
    # Semaphore caps concurrent jobs to PLAYWRIGHT_MAX_WORKERS.
    # We only fetch as many messages as we have free slots — same reasoning as the
    # Go worker: fetching more starts AckWait timers on messages we can't process yet,
    # causing spurious NATS redeliveries before the job even starts.
    sem = asyncio.Semaphore(settings.playwright_max_workers)

    async def handle_with_sem(msg):
        async with sem:
            await handle_message(
                msg,
                js,
                minio,
                browser,
                settings.playwright_default_timeout_seconds,
            )

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    fetch_backoff = (
        2  # seconds; doubles on consecutive non-timeout errors, capped at 60
    )
    while not stop.is_set():
        # sem._value is the number of currently available slots
        available = sem._value
        if available == 0:
            await asyncio.sleep(0.1)
            continue

        try:
            msgs = await psub.fetch(batch=available, timeout=5)
            fetch_backoff = 2  # reset on success
        except nats.errors.TimeoutError:
            continue
        except Exception as exc:
            log.error("fetch_error", error=str(exc), backoff=fetch_backoff)
            await asyncio.sleep(fetch_backoff)
            fetch_backoff = min(fetch_backoff * 2, 60)
            continue

        for msg in msgs:
            asyncio.create_task(handle_with_sem(msg))

    # ── Graceful shutdown ─────────────────────────────────────────────────────────
    log.info("shutting_down")
    await browser.close()
    await pw.stop()
    await nc.drain()


if __name__ == "__main__":
    asyncio.run(run())
