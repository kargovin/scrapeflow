"""
Playwright worker — entry point. entrypoint.sh execs this module (the Xvfb/pid-1 contract).

  1. Load config from env vars (requires CREDENTIALS_ENCRYPTION_KEY)
  2. Connect to Temporal
  3. Connect to MinIO, verify bucket exists
  4. Launch Chrome via Patchright (headed under Xvfb — the stealth config in config.py)
  5. Poll the `scrape-playwright` task queue for Scrape activity tasks (no workflows), and the
     `crawl` queue for the crawl's robots/sitemap/link activities (no browser)
  6. On SIGTERM, wait up to playwright_graceful_shutdown_seconds for in-flight scrapes
"""

import signal
import sys

if __name__ == "__main__":
    # As PID 1 the process ignores a SIGTERM it has no handler for, and the imports below
    # take seconds. Nothing is in flight yet, so exit at once; run() replaces this with
    # its graceful handler.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

import asyncio  # noqa: E402
from datetime import timedelta  # noqa: E402

import structlog  # noqa: E402
from miniopy_async import Minio  # noqa: E402

# Patchright is a drop-in Playwright fork: it patches the CDP Runtime.enable leak
# and removes navigator.webdriver, which are the automation tells that were failing
# bot detection. The async API is import-compatible with playwright.async_api.
from patchright.async_api import async_playwright  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.contrib.pydantic import pydantic_data_converter  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from .activity import PlayWrightScrapeActivities  # noqa: E402
from .config import settings  # noqa: E402
from .contracts import CRAWL_QUEUE, SCRAPE_PLAYWRIGHT_QUEUE  # noqa: E402
from .crawl import CrawlActivities, discover_sitemaps, fetch_sitemap  # noqa: E402

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

    crawl_worker = Worker(
        client,
        task_queue=CRAWL_QUEUE,
        activities=[
            discover_sitemaps,
            fetch_sitemap,
            CrawlActivities(minio).extract_links,
        ],
        max_concurrent_activities=settings.crawl_max_activities,
        # Each fetch is bounded by its 10 s client timeout.
        graceful_shutdown_timeout=timedelta(seconds=30),
    )

    try:
        async with worker, crawl_worker:
            log.info(
                "temporal_worker_started",
                address=settings.temporal_address,
                namespace=settings.temporal_namespace,
                task_queue=SCRAPE_PLAYWRIGHT_QUEUE,
                max_workers=settings.playwright_max_workers,
                graceful_shutdown_s=settings.playwright_graceful_shutdown_seconds,
                crawl_queue=CRAWL_QUEUE,
                crawl_max_activities=settings.crawl_max_activities,
            )
            await stop.wait()
            log.info("temporal_worker_stopping")
    finally:
        # After the worker has drained: an in-flight scrape still holds a context.
        await browser.close()
        await pw.stop()
    log.info("temporal_worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
