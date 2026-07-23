"""
Playwright worker — entry point (Python/asyncio, Chromium via Playwright).

Startup sequence (spec §4.2):
  1. Load config from env vars
  2. Connect to NATS, verify SCRAPEFLOW stream exists
  3. Connect to MinIO, verify bucket exists
  4. Launch Chrome via Patchright (headed under Xvfb by default — stealth config)
  5. Create pull consumer on scrapeflow.jobs.run.playwright
  6. Run worker loop (concurrency capped by PLAYWRIGHT_MAX_WORKERS)
"""

import asyncio
import signal

import nats
import nats.errors
import structlog
from miniopy_async import Minio
from nats.js.api import ConsumerConfig

# Patchright is a drop-in Playwright fork: it patches the CDP Runtime.enable leak
# and removes navigator.webdriver, which are the automation tells that were failing
# bot detection. The async API is import-compatible with playwright.async_api.
from patchright.async_api import async_playwright

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
    launch_args: list[str] = []
    if settings.playwright_no_sandbox:
        launch_args.append("--no-sandbox")
    if settings.playwright_disable_dev_shm:
        launch_args.append("--disable-dev-shm-usage")
    if settings.playwright_disable_automation:
        launch_args.append("--disable-blink-features=AutomationControlled")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        channel=settings.playwright_channel or None,
        headless=settings.playwright_headless,
        args=launch_args,
    )
    log.info(
        "browser_launched",
        channel=settings.playwright_channel,
        headless=settings.playwright_headless,
        args=launch_args,
    )

    # ── Pull subscription ─────────────────────────────────────────────────────────
    # Explicit ack_wait: the JetStream default (30s) is shorter than a headed-Chrome
    # scrape, so messages were redelivered mid-job and looped forever (see config.py).
    # NOTE: JetStream does not update ack_wait on an already-existing durable consumer,
    # so changing this value requires updating/recreating the consumer out-of-band.
    # max_deliver caps redelivery as the consumer-side backstop to the worker's own
    # attempt cap (metadata.num_delivered in worker.py). It matters only if the worker
    # dies before acking; in the normal path the worker publishes a terminal "failed"
    # and acks on the last attempt. NOTE: like ack_wait, JetStream will NOT apply a
    # changed max_deliver to an existing durable — the live consumer stays at its old
    # value (unlimited) until recreated out-of-band (nats consumer rm + rollout restart).
    psub = await js.pull_subscribe(
        PLAYWRIGHT_SUBJECT,
        durable=DURABLE_NAME,
        stream=STREAM_NAME,
        config=ConsumerConfig(
            ack_wait=settings.playwright_ack_wait_seconds,
            max_deliver=settings.playwright_max_delivery_attempts,
        ),
    )
    log.info(
        "subscribed",
        subject=PLAYWRIGHT_SUBJECT,
        durable=DURABLE_NAME,
        max_workers=settings.playwright_max_workers,
        ack_wait=settings.playwright_ack_wait_seconds,
    )

    # ── Worker loop ───────────────────────────────────────────────────────────────
    # Semaphore caps concurrent jobs to PLAYWRIGHT_MAX_WORKERS.
    # We only fetch as many messages as we have free slots — same reasoning as the
    # Go worker: fetching more starts AckWait timers on messages we can't process yet,
    # causing spurious NATS redeliveries before the job even starts.
    sem = asyncio.Semaphore(settings.playwright_max_workers)

    async def _heartbeat(msg):
        # Reset the NATS ack timer while the job runs so a scrape longer than
        # ack_wait never triggers redelivery. Errors (e.g. msg already acked) are
        # ignored; the task is cancelled when the job finishes.
        try:
            while True:
                await asyncio.sleep(settings.playwright_heartbeat_seconds)
                try:
                    await msg.in_progress()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def handle_with_sem(msg):
        async with sem:
            hb = asyncio.create_task(_heartbeat(msg))
            try:
                await handle_message(
                    msg,
                    js,
                    minio,
                    browser,
                    settings.playwright_default_timeout_seconds,
                )
            finally:
                hb.cancel()
                try:
                    await hb
                except asyncio.CancelledError:
                    pass

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
