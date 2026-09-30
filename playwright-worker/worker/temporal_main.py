"""
Playwright worker — Temporal entry point (WORKER_MODE=temporal).

  1. Load config from env vars (requires CREDENTIALS_ENCRYPTION_KEY)
  2. Connect to Temporal
  3. Connect to MinIO, verify bucket exists
  4. Launch Chrome via Patchright (headed under Xvfb — same stealth config as main.py)
  5. Poll the `scrape-playwright` task queue for Scrape activity tasks (no workflows)
  6. On SIGTERM, wait up to playwright_graceful_shutdown_seconds for in-flight scrapes
"""

import asyncio
import signal
from datetime import timedelta

import structlog
from miniopy_async import Minio
from patchright.async_api import async_playwright
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from .activity import PlayWrightScrapeActivities
from .config import settings
from .contracts import SCRAPE_PLAYWRIGHT_QUEUE

log = structlog.get_logger()


async def run() -> None:
    # Installed before anything slow: as PID 1 the process ignores a SIGTERM it has no
    # handler for, so one arriving during startup would stall until the pod's grace
    # period ends in a SIGKILL.
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

    activities = PlayWrightScrapeActivities(minio, browser)
    worker = Worker(
        client,
        task_queue=SCRAPE_PLAYWRIGHT_QUEUE,
        activities=[activities.playwright_scrape],
        max_concurrent_activities=settings.playwright_max_workers,
        graceful_shutdown_timeout=timedelta(
            seconds=settings.playwright_graceful_shutdown_seconds
        ),
    )

    try:
        async with worker:
            log.info(
                "temporal_worker_started",
                address=settings.temporal_address,
                namespace=settings.temporal_namespace,
                task_queue=SCRAPE_PLAYWRIGHT_QUEUE,
                max_workers=settings.playwright_max_workers,
                graceful_shutdown_s=settings.playwright_graceful_shutdown_seconds,
            )
            await stop.wait()
            log.info("temporal_worker_stopping")
    finally:
        # After the worker has drained: an in-flight scrape still holds a context.
        await browser.close()
        await pw.stop()
    log.info("temporal_worker_stopped")
