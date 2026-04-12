"""
Playwright worker — entry point.

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
from typing import Any

import nats
import nats.errors
import structlog
from miniopy_async import Minio
from playwright.async_api import async_playwright

from .config import settings
from .formatter import format_output
from .models import JobMessage, ResultMessage
from .storage import upload

log = structlog.get_logger()

PLAYWRIGHT_SUBJECT = "scrapeflow.jobs.run.playwright"
RESULT_SUBJECT = "scrapeflow.jobs.result"
DURABLE_NAME = "python-playwright-worker"
STREAM_NAME = "SCRAPEFLOW"


async def _publish_result(js: Any, result: ResultMessage) -> None:
    await js.publish(RESULT_SUBJECT, result.to_nats_bytes())


async def _handle_message(
    msg: Any,
    js: Any,
    minio: Minio,
    browser: Any,
    default_timeout: int,
) -> None:
    """Full ADR-002 job lifecycle for a single Playwright job."""
    # --- Step 1: Parse the incoming job message ---
    try:
        job = JobMessage.model_validate_json(msg.data)
    except Exception as exc:
        log.error("malformed_message", error=str(exc), data=msg.data[:200])
        await msg.ack()
        return

    log.info("job_received", job_id=job.job_id, run_id=job.run_id, url=job.url)

    opts = job.playwright_options
    timeout_ms = (opts.timeout_seconds if opts else default_timeout) * 1000
    wait_state = opts.wait_strategy if opts else "load"

    # --- Step 2: Publish "running" with nats_stream_seq (ADR-002 §3) ---
    # The result consumer stores nats_stream_seq on job_runs so the MaxDeliver
    # advisory handler (Step 22) can identify stalled runs by sequence number alone.
    nats_seq = msg.metadata.sequence.stream
    await _publish_result(
        js,
        ResultMessage(
            job_id=job.job_id,
            run_id=job.run_id,
            status="running",
            nats_stream_seq=nats_seq,
        ),
    )

    # --- Steps 3–6: Render, format, upload ---
    context = await browser.new_context()
    page = await context.new_page()
    try:
        # Optional: block images/fonts/CSS to speed up non-visual scrapes
        if opts and opts.block_images:
            await page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}",
                lambda route: route.abort(),
            )

        await page.goto(job.url, timeout=timeout_ms)
        await page.wait_for_load_state(wait_state)

        html = await page.content()
        final_url = page.url

        content, ext = format_output(html, job.output_format, final_url)
        minio_path = await upload(minio, job.job_id, ext, content)

        # --- Step 7: Publish "completed" ---
        await _publish_result(
            js,
            ResultMessage(
                job_id=job.job_id,
                run_id=job.run_id,
                status="completed",
                minio_path=minio_path,
            ),
        )
        # --- Step 8: Ack only after MinIO write succeeds (ADR-002 §6) ---
        await msg.ack()
        log.info("job_completed", job_id=job.job_id, run_id=job.run_id, path=minio_path)

    except Exception as exc:
        log.error("job_failed", job_id=job.job_id, run_id=job.run_id, error=str(exc))
        await _publish_result(
            js,
            ResultMessage(
                job_id=job.job_id,
                run_id=job.run_id,
                status="failed",
                error=str(exc),
            ),
        )
        # Ack even on failure — the API already knows it failed via the result event.
        # Not acking would redeliver, but if the page is down/timing out, retry won't help.
        await msg.ack()

    finally:
        # Always discard the browser context — no session state leaks between jobs
        await context.close()


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
            await _handle_message(
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

    while not stop.is_set():
        # sem._value is the number of currently available slots
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
    await browser.close()
    await pw.stop()
    await nc.drain()


if __name__ == "__main__":
    asyncio.run(run())
