"""
Scrape on scrape-playwright — the Temporal activity for the Playwright engine (WORKER_MODE=temporal).

The render pipeline is scrape.render, shared with worker.handle_message; this module
adds the Temporal half: no "running" publish, no result publish, no ack/nak. Success is the return value; failure is a raised
ApplicationError, and the caller's RetryPolicy decides when to stop.
"""

import asyncio
import contextlib
from typing import Any

import structlog
import xxhash
from miniopy_async import Minio
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .config import settings
from .contracts import SCRAPE_ACTIVITY, ScrapeInput, ScrapeOutput, StoredObject
from .errors import TERMINAL, classify, describe
from .robots import is_disallowed
from .scrape import BlockedPage, decrypt_credentials, render

log = structlog.get_logger()

# ApplicationError.type values — Go's names (internal/activity/scrape.go) where one exists.
ROBOTS_DISALLOWED = "RobotsDisallowed"  # non-retryable
BLOCKED = "Blocked"  # non-retryable
SCRAPE_FAILED = "ScrapeFailed"  # non-retryable
STORAGE_TRANSIENT = "StorageTransient"


def to_application_error(exc: Exception) -> ApplicationError:
    # The SDK retries any exception that is not an ApplicationError, so every failure must
    # pass through classify() or a dead site is re-rendered on every attempt.
    detail = describe(exc)
    if classify(exc) == TERMINAL:
        return ApplicationError(detail, type=SCRAPE_FAILED, non_retryable=True)
    return ApplicationError(detail, type=STORAGE_TRANSIENT)


async def _heartbeat() -> None:
    while True:
        activity.heartbeat()
        await asyncio.sleep(settings.playwright_heartbeat_seconds)


class PlayWrightScrapeActivities:
    def __init__(self, minio: Minio, browser: Any) -> None:
        self.minio = minio
        self.browser = browser

    @activity.defn(name=SCRAPE_ACTIVITY)
    async def playwright_scrape(self, job: ScrapeInput) -> ScrapeOutput:
        """Render the page in headed Chrome, write history/{artifact_id}/scrape.{ext}.

        Caller requirements:
          - start_to_close >= 2 x playwright_options.timeout_seconds (goto and
            wait_for_load_state each get the full budget, BUG-015) + action time + upload.
            Per job — derive it from the input.
          - heartbeat_timeout > playwright_heartbeat_seconds (30 s).
          - A bot wall and a robots.txt disallow are non-retryable; only StorageTransient retries.
        """
        # Step 1: input validation is the data converter's (pydantic) — a decode failure
        # never reaches this body.
        attempt = activity.info().attempt
        log.info(
            "job_received", artifact_id=job.artifact_id, url=job.url, attempt=attempt
        )

        # Step 2: robots.txt. Fetch failure → proceed, as the NATS path and Go do.
        if job.options and job.options.respect_robots:
            try:
                blocked = await is_disallowed(job.url)
            except Exception:
                blocked = False

            if blocked:
                log.info("robots_disallowed", artifact_id=job.artifact_id, url=job.url)
                raise ApplicationError(
                    "robots_txt_disallowed", type=ROBOTS_DISALLOWED, non_retryable=True
                )

        hb = asyncio.create_task(_heartbeat())
        try:
            # Inside the try so InvalidToken reaches classify().
            proxy_url, cookies = decrypt_credentials(job.credentials)

            rendered = await render(
                self.browser,
                self.minio,
                artifact_id=job.artifact_id,
                url=job.url,
                output_format=job.output_format,
                playwright_options=job.playwright_options,
                actions=job.options.actions if job.options else None,
                proxy_url=proxy_url,
                cookies=cookies,
                default_timeout=settings.playwright_default_timeout_seconds,
                log=log.bind(attempt=attempt),
            )

            screenshots = [
                await self._stored(path) for path in rendered.screenshot_paths
            ]
        except BlockedPage as blocked:
            raise ApplicationError(
                blocked.detection.error, type=BLOCKED, non_retryable=True
            ) from None
        except Exception as exc:
            # CancelledError is a BaseException and passes through: it is how a cancel or a
            # shutdown past the grace period reaches the activity.
            err = to_application_error(exc)
            log_fn = log.error if err.non_retryable else log.warning
            log_fn(
                "job_failed" if err.non_retryable else "job_transient_failure",
                artifact_id=job.artifact_id,
                error=err.message,
                type=err.type,
                attempt=attempt,
            )
            raise err from exc
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb

        log.info("job_completed", artifact_id=job.artifact_id, path=rendered.minio_path)
        return ScrapeOutput(
            result=StoredObject(path=rendered.minio_path, size=len(rendered.content)),
            content_hash=xxhash.xxh64(rendered.content).hexdigest(),
            warnings=rendered.warnings,
            screenshots=screenshots,
        )

    async def _stored(self, path: str) -> StoredObject:
        # execute_actions returns paths only; the size comes from the object itself.
        bucket, key = path.split("/", 1)
        stat = await self.minio.stat_object(bucket, key)
        return StoredObject(path=path, size=stat.size)
