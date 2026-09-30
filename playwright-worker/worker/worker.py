"""
Per-job processing logic for the Playwright worker.

Separated from main.py so the job lifecycle can be unit-tested
without standing up a NATS pull consumer loop.
"""

from typing import Any

import structlog
from miniopy_async import Minio

from .config import settings
from .errors import TRANSIENT, classify, describe, retry_delay
from .models import JobMessage, ResultMessage
from .robots import is_disallowed
from .scrape import BlockedPage, decrypt_credentials, render

log = structlog.get_logger()

RESULT_SUBJECT = "scrapeflow.jobs.result"


async def publish_result(js: Any, result: ResultMessage) -> None:
    await js.publish(RESULT_SUBJECT, result.to_nats_bytes())


async def handle_message(
    msg: Any,
    js: Any,
    minio: Minio,
    browser: Any,
    default_timeout: int,
) -> None:
    """Full ADR-002 job lifecycle for a single Playwright job (schema_version 2)."""
    # --- Step 1: Parse the incoming job message ---
    try:
        job = JobMessage.model_validate_json(msg.data)
    except Exception as exc:
        log.error("malformed_message", error=str(exc), data=msg.data[:200])
        await msg.ack()
        return

    log.info(
        "job_received", artifact_id=job.artifact_id, run_id=job.run_id, url=job.url
    )

    # --- Step 2: robots.txt check (fires BEFORE publishing "running") ---
    # If the job is disallowed, we publish "failed" directly — no "running" event.
    # This mirrors the Go HTTP worker's ordering in handleMessage (Step 14).
    if job.options and job.options.respect_robots:
        try:
            blocked = await is_disallowed(job.url)
        except Exception:
            blocked = False  # fetch failure → proceed
        if blocked:
            log.info("robots_disallowed", artifact_id=job.artifact_id, url=job.url)
            await publish_result(
                js,
                ResultMessage(
                    run_id=job.run_id,
                    status="failed",
                    error="robots_txt_disallowed",
                    crawl_context=job.crawl_context,
                ),
            )
            await msg.ack()
            return

    # --- Step 3: Publish "running" with nats_stream_seq (ADR-002 §3) ---
    nats_seq = msg.metadata.sequence.stream
    await publish_result(
        js,
        ResultMessage(
            run_id=job.run_id,
            status="running",
            nats_stream_seq=nats_seq,
            crawl_context=job.crawl_context,
        ),
    )

    # --- Step 4: Decrypt credentials (Fernet ciphertext from NATS) ---
    proxy_url, cookies = decrypt_credentials(job.credentials)

    # --- Steps 5–12: Render, format, upload (scrape.render), publish, ack ---
    try:
        rendered = await render(
            browser,
            minio,
            artifact_id=job.artifact_id,
            url=job.url,
            output_format=job.output_format,
            playwright_options=job.playwright_options,
            actions=job.options.actions if job.options else None,
            proxy_url=proxy_url,
            cookies=cookies,
            default_timeout=default_timeout,
            log=log.bind(run_id=job.run_id),
        )

        # --- Step 11: Publish "completed" ---
        await publish_result(
            js,
            ResultMessage(
                run_id=job.run_id,
                status="completed",
                minio_path=rendered.minio_path,
                warnings=rendered.warnings or None,
                screenshot_paths=rendered.screenshot_paths or None,
                crawl_context=job.crawl_context,
            ),
        )
        # --- Step 12: Ack only after MinIO write succeeds (ADR-002 §6) ---
        await msg.ack()
        log.info(
            "job_completed",
            artifact_id=job.artifact_id,
            run_id=job.run_id,
            path=rendered.minio_path,
        )

    except BlockedPage as blocked:
        # A wall is a *failed* scrape, not a successful one — storing it as
        # `completed` both hands the user garbage and seeds a dedup baseline
        # that silently suppresses future change detection for that job.
        await publish_result(
            js,
            ResultMessage(
                run_id=job.run_id,
                status="failed",
                error=blocked.detection.error,
                crawl_context=job.crawl_context,
            ),
        )
        await msg.ack()

    except Exception as exc:
        # UF-003 3a. This branch used to ack unconditionally, which made every
        # failure permanent — a MinIO write fault (object store momentarily down)
        # was treated exactly like a genuine site failure, after the expensive
        # headed-Chrome render had already succeeded. Now transient infra failures
        # are naked back to JetStream for redelivery; only the final outcome is
        # published. Same posture as the LLM worker; see worker/errors.py.
        kind = classify(exc)
        attempt = msg.metadata.num_delivered
        detail = describe(exc)

        if kind == TRANSIENT and attempt < settings.playwright_max_delivery_attempts:
            delay = retry_delay(
                attempt,
                settings.playwright_retry_base_delay_seconds,
                settings.playwright_retry_max_delay_seconds,
            )
            log.warning(
                "job_transient_failure",
                artifact_id=job.artifact_id,
                run_id=job.run_id,
                error=detail,
                attempt=attempt,
                max_attempts=settings.playwright_max_delivery_attempts,
                retry_in=delay,
            )
            # Deliberately no "failed" publish: the API's terminal-status guard
            # would lock the run as failed and then discard the retry's
            # "completed". Only the final attempt reports an outcome. Redelivery
            # re-runs the whole scrape (there is no partial-progress checkpoint).
            await msg.nak(delay=delay)
            return

        # Terminal, or the last attempt of a transient failure. Report and ack —
        # redelivery cannot recover a bad key or a dead site, and we are out of retries.
        if kind == TRANSIENT:
            detail = f"{detail} (gave up after {attempt} attempts)"
        log.error(
            "job_failed",
            artifact_id=job.artifact_id,
            run_id=job.run_id,
            error=detail,
            kind=kind,
            attempt=attempt,
        )
        await publish_result(
            js,
            ResultMessage(
                run_id=job.run_id,
                status="failed",
                error=detail,
                crawl_context=job.crawl_context,
            ),
        )
        await msg.ack()
