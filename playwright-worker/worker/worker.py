"""
Per-job processing logic for the Playwright worker.

Separated from main.py so the job lifecycle can be unit-tested
without standing up a NATS pull consumer loop.
"""

import json
from typing import Any
from urllib.parse import urlparse

import structlog
from cryptography.fernet import Fernet
from miniopy_async import Minio

from .actions import execute_actions
from .blocking import detect_block
from .config import settings
from .formatter import format_output
from .models import JobMessage, ResultMessage
from .robots import is_disallowed
from .storage import upload

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

    log.info("job_received", job_id=job.job_id, run_id=job.run_id, url=job.url)

    opts = job.playwright_options
    timeout_ms = (opts.timeout_seconds if opts else default_timeout) * 1000
    wait_state = opts.wait_strategy if opts else "load"

    # --- Step 2: robots.txt check (fires BEFORE publishing "running") ---
    # If the job is disallowed, we publish "failed" directly — no "running" event.
    # This mirrors the Go HTTP worker's ordering in handleMessage (Step 14).
    if job.options and job.options.respect_robots:
        try:
            blocked = await is_disallowed(job.url)
        except Exception:
            blocked = False  # fetch failure → proceed
        if blocked:
            log.info("robots_disallowed", job_id=job.job_id, url=job.url)
            await publish_result(
                js,
                ResultMessage(
                    job_id=job.job_id,
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
            job_id=job.job_id,
            run_id=job.run_id,
            status="running",
            nats_stream_seq=nats_seq,
            crawl_context=job.crawl_context,
        ),
    )

    # --- Step 4: Decrypt credentials (Fernet ciphertext from NATS) ---
    proxy_url: str | None = None
    cookies: list[dict] | None = None
    if job.credentials:
        f = Fernet(settings.credentials_encryption_key)
        if job.credentials.encrypted_proxy_url:
            proxy_url = f.decrypt(job.credentials.encrypted_proxy_url.encode()).decode()
        if job.credentials.encrypted_cookies:
            cookies = json.loads(
                f.decrypt(job.credentials.encrypted_cookies.encode()).decode()
            )

    # --- Steps 5–12: Render, format, upload ---
    # Build context options — proxy is set at context level so all traffic routes through it.
    # Chromium silently drops userinfo from proxy URLs; split into server/username/password.
    #
    # no_viewport=True lets the page use the real browser window size instead of a
    # forced viewport. A forced viewport goes through CDP Emulation.setDeviceMetricsOverride,
    # which is itself a bot-detection signal; deferring to the window (Xvfb screen size)
    # avoids that tell. Deliberately NOT setting user_agent — with channel="chrome" the
    # UA is already a genuine Chrome, and Patchright advises against overriding it.
    context_kwargs: dict = {"no_viewport": True}
    if proxy_url:
        parsed_proxy = urlparse(proxy_url)
        proxy_config: dict = {
            "server": f"{parsed_proxy.scheme}://{parsed_proxy.hostname}:{parsed_proxy.port}",
        }
        if parsed_proxy.username:
            proxy_config["username"] = parsed_proxy.username
        if parsed_proxy.password:
            proxy_config["password"] = parsed_proxy.password
        context_kwargs["proxy"] = proxy_config

    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()
    try:
        # --- Step 5: Cookie injection (before goto so cookies are live on first load) ---
        if cookies:
            parsed = urlparse(job.url)
            cookies_to_add = []
            for cookie in cookies:
                c = dict(cookie)
                if "domain" not in c or not c["domain"]:
                    c["domain"] = parsed.hostname
                cookies_to_add.append(c)
            await context.add_cookies(cookies_to_add)

        # Optional: block images/fonts/CSS to speed up non-visual scrapes
        if opts and opts.block_images:
            await page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}",
                lambda route: route.abort(),
            )

        # --- Step 6: CSP injection (before goto; restricts execute_js exfil) ---
        # Injected via page.route so the CSP arrives as a *response* header that
        # Chromium actually enforces. set_extra_http_headers sends *request* headers
        # which are ignored by the browser for CSP purposes.
        actions_list = job.options.actions if job.options else None
        if actions_list:
            parsed = urlparse(job.url)
            target_origin = f"{parsed.scheme}://{parsed.netloc}"
            csp = (
                f"connect-src 'self' {target_origin}; "
                f"img-src 'self' {target_origin}; "
                f"form-action 'none'; "
                f"frame-src 'none'"
            )

            async def _inject_csp(route: Any) -> None:
                if route.request.resource_type == "document":
                    response = await route.fetch()
                    headers = dict(response.headers)
                    headers["content-security-policy"] = csp
                    await route.fulfill(response=response, headers=headers)
                else:
                    await route.fallback()

            await page.route("**", _inject_csp)

        # --- Step 7: Navigate ---
        # Keep the Response: its status is a block signal (BUG-003). It is None
        # for same-document navigations, which is not itself evidence.
        response = await page.goto(job.url, timeout=timeout_ms)
        await page.wait_for_load_state(wait_state)

        # --- Step 8: Execute actions (partial failures collected as warnings) ---
        warnings: list[str] = []
        screenshot_paths: list[str] = []
        if actions_list:
            warnings, screenshot_paths = await execute_actions(
                page, minio, job.job_id, actions_list
            )

        html = await page.content()
        final_url = page.url

        # --- Step 9: Bot-wall detection (BUG-003) ---
        # Must run on the raw HTML, before format_output: markdown conversion
        # strips every HTML-level signal (scripts, meta, link tags).
        #
        # A wall is a *failed* scrape, not a successful one — storing it as
        # `completed` both hands the user garbage and seeds a dedup baseline
        # that silently suppresses future change detection for that job.
        #
        # We deliberately do NOT upload the wall: the result consumer's
        # `failed` branch does not persist `minio_path`, so the object would be
        # orphaned. The `signals` on the log line carry the debugging value.
        detection = detect_block(
            html,
            status=response.status if response else None,
            final_url=final_url,
        )
        if detection.blocked:
            log.warning(
                "block_detected",
                job_id=job.job_id,
                run_id=job.run_id,
                url=job.url,
                final_url=final_url,
                vendor=detection.vendor,
                tier=detection.tier,
                signals=detection.signals,
                body_bytes=len(html.encode()),
                status=response.status if response else None,
            )
            await publish_result(
                js,
                ResultMessage(
                    job_id=job.job_id,
                    run_id=job.run_id,
                    status="failed",
                    error=detection.error,
                    crawl_context=job.crawl_context,
                ),
            )
            await msg.ack()
            return

        content, ext = format_output(html, job.output_format, final_url)
        minio_path = await upload(minio, job.job_id, ext, content)

        # --- Step 11: Publish "completed" ---
        await publish_result(
            js,
            ResultMessage(
                job_id=job.job_id,
                run_id=job.run_id,
                status="completed",
                minio_path=minio_path,
                warnings=warnings or None,
                screenshot_paths=screenshot_paths or None,
                crawl_context=job.crawl_context,
            ),
        )
        # --- Step 12: Ack only after MinIO write succeeds (ADR-002 §6) ---
        await msg.ack()
        log.info("job_completed", job_id=job.job_id, run_id=job.run_id, path=minio_path)

    except Exception as exc:
        log.error("job_failed", job_id=job.job_id, run_id=job.run_id, error=str(exc))
        await publish_result(
            js,
            ResultMessage(
                job_id=job.job_id,
                run_id=job.run_id,
                status="failed",
                error=str(exc),
                crawl_context=job.crawl_context,
            ),
        )
        await msg.ack()

    finally:
        await context.close()
