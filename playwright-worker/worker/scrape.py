"""
The render pipeline activity.PlayWrightScrapeActivities runs. Counterpart of the Go
worker's internal/scrape.

render() owns one browser context end to end: cookies, routes, navigation, actions,
bot-wall detection, formatting and the upload. The activity keeps the robots.txt check
and failure handling.
"""

import contextlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

import structlog
from cryptography.fernet import Fernet
from miniopy_async import Minio

from .actions import execute_actions
from .blocking import BlockDetection, detect_block
from .config import settings
from .egress import CONNECT_REFUSED, REFUSAL_HEADER, AddressRefused, pinned_proxy_server
from .formatter import format_output
from .storage import upload

_log = structlog.get_logger()


class BlockedPage(Exception):
    """The rendered page is a bot wall (BUG-003). Carries the detection; nothing was uploaded."""

    def __init__(self, detection: BlockDetection) -> None:
        super().__init__(detection.error)
        self.detection = detection


@dataclass
class Rendered:
    minio_path: str
    content: bytes  # the uploaded bytes
    warnings: list[str]
    screenshot_paths: list[str]


def decrypt_credentials(credentials: Any) -> tuple[str | None, list[dict] | None]:
    """Fernet-decrypt the per-job proxy URL and cookies. Raises InvalidToken on a bad key."""
    proxy_url: str | None = None
    cookies: list[dict] | None = None
    if credentials:
        f = Fernet(settings.credentials_encryption_key)
        if credentials.encrypted_proxy_url:
            proxy_url = f.decrypt(credentials.encrypted_proxy_url.encode()).decode()
        if credentials.encrypted_cookies:
            cookies = json.loads(
                f.decrypt(credentials.encrypted_cookies.encode()).decode()
            )
    return proxy_url, cookies


async def render(
    browser: Any,
    minio: Minio,
    *,
    artifact_id: str,
    url: str,
    output_format: str,
    playwright_options: Any,
    actions: list[dict] | None,
    proxy_url: str | None,
    cookies: list[dict] | None,
    default_timeout: int,
    log: Any = _log,
) -> Rendered:
    """Render url in a fresh context and upload history/{artifact_id}/scrape.{ext}.

    Raises BlockedPage on a bot wall, AddressRefused when the egress proxy refused the page (a
    redirect or a script navigation included) or the job's proxy is not public; any other failure
    propagates for the caller to classify.
    """
    opts = playwright_options
    timeout_ms = (opts.timeout_seconds if opts else default_timeout) * 1000
    wait_state = opts.wait_strategy if opts else "load"

    # Proxy is set at context level so all traffic routes through it.
    # Chromium silently drops userinfo from proxy URLs; split into server/username/password.
    # urlparse leaves userinfo percent-encoded, so unquote() both fields (BUG-017): the Go
    # http-worker decodes them via net/url, and a password with a reserved character must
    # authenticate the same way on both engines.
    #
    # no_viewport=True lets the page use the real browser window size instead of a
    # forced viewport. A forced viewport goes through CDP Emulation.setDeviceMetricsOverride,
    # which is itself a bot-detection signal; deferring to the window (Xvfb screen size)
    # avoids that tell. Deliberately NOT setting user_agent — with channel="chrome" the
    # UA is already a genuine Chrome, and Patchright advises against overriding it.
    #
    # Without a job proxy the context inherits the browser's: the egress proxy (BUG-010 part 2).
    # A job proxy replaces it, so Chrome gets the proxy's checked address, not its name.
    context_kwargs: dict = {"no_viewport": True}
    if proxy_url:
        parsed_proxy = urlparse(proxy_url)
        proxy_config: dict = {"server": await pinned_proxy_server(proxy_url)}
        if parsed_proxy.username:
            proxy_config["username"] = unquote(parsed_proxy.username)
        if parsed_proxy.password:
            proxy_config["password"] = unquote(parsed_proxy.password)
        context_kwargs["proxy"] = proxy_config

    context = await browser.new_context(**context_kwargs)
    try:
        page = await context.new_page()
        refused = _watch_for_refusal(page, through_egress=not proxy_url)

        # --- Cookie injection (before goto so cookies are live on first load) ---
        if cookies:
            parsed = urlparse(url)
            cookies_to_add = []
            for cookie in cookies:
                c = dict(cookie)
                if "domain" not in c or not c["domain"]:
                    c["domain"] = parsed.hostname
                cookies_to_add.append(c)
            await context.add_cookies(cookies_to_add)

        # Optional: block images/fonts to speed up non-visual scrapes.
        # Never CSS (BUG-016): a missing font falls back silently, but an
        # aborted stylesheet chunk rejects a lazy-loaded SPA route's import()
        # and the page renders React's error boundary instead of the content.
        if opts and opts.block_images:
            await page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}",
                lambda route: route.abort(),
            )

        # --- CSP injection (before goto; restricts execute_js exfil) ---
        # Injected via page.route so the CSP arrives as a *response* header that
        # Chromium actually enforces. set_extra_http_headers sends *request* headers
        # which are ignored by the browser for CSP purposes.
        if actions:
            parsed = urlparse(url)
            target_origin = f"{parsed.scheme}://{parsed.netloc}"
            csp = (
                f"connect-src 'self' {target_origin}; "
                f"img-src 'self' {target_origin}; "
                f"form-action 'none'; "
                f"frame-src 'none'"
            )

            async def _inject_csp(route: Any) -> None:
                if route.request.resource_type == "document":
                    try:
                        response = await route.fetch()
                    except Exception:
                        # A fetch error raised here would surface at the page's next call
                        # (page.content) and fail the run; as a network error, Chrome handles it
                        # as it would unrouted.
                        await route.abort("connectionfailed")
                        return
                    if response.status != 407 and response.headers.get(REFUSAL_HEADER):
                        # The egress proxy could not connect (a 502 carrying its header): the
                        # network error Chrome would see unproxied, so an https upgrade of an
                        # http-only site still falls back to http.
                        await route.abort("connectionfailed")
                        return
                    headers = dict(response.headers)
                    headers["content-security-policy"] = csp
                    await route.fulfill(response=response, headers=headers)
                else:
                    await route.fallback()

            await page.route("**", _inject_csp)

        # --- Navigate ---
        # Keep the Response: its status is a block signal (BUG-003). It is None
        # for same-document navigations, which is not itself evidence.
        try:
            response = await page.goto(url, timeout=timeout_ms)
            # Same budget as goto (BUG-015): without it the wait gets Playwright's
            # 30s default, and a networkidle page that never goes quiet fails at
            # goto elapsed + 30s regardless of what timeout_seconds asked for.
            await page.wait_for_load_state(wait_state, timeout=timeout_ms)
        except Exception:
            if refused:
                raise refused[0] from None
            raise
        if refused:
            raise refused[0]

        # --- Execute actions (partial failures collected as warnings) ---
        warnings: list[str] = []
        screenshot_paths: list[str] = []
        if actions:
            warnings, screenshot_paths = await execute_actions(
                page, minio, artifact_id, actions
            )

        if refused:  # a script or an action navigated somewhere refused
            raise refused[0]
        html = await page.content()
        final_url = page.url

        # --- Bot-wall detection (BUG-003) ---
        # Must run on the raw HTML, before format_output: markdown conversion
        # strips every HTML-level signal (scripts, meta, link tags).
        #
        # The wall is NOT uploaded: a failed run persists no minio_path, so the
        # object would be orphaned. The `signals` on the log line carry the
        # debugging value.
        status = response.status if response else None
        detection = detect_block(html, status=status, final_url=final_url)
        if detection.blocked:
            log.warning(
                "block_detected",
                artifact_id=artifact_id,
                url=url,
                final_url=final_url,
                vendor=detection.vendor,
                tier=detection.tier,
                signals=detection.signals,
                body_bytes=len(html.encode()),
                status=status,
            )
            raise BlockedPage(detection)

        content, ext = format_output(html, output_format, final_url)
        minio_path = await upload(minio, artifact_id, ext, content)
        return Rendered(minio_path, content, warnings, screenshot_paths)

    finally:
        # A close failure must not replace the outcome — success or the real error.
        with contextlib.suppress(Exception):
            await context.close()


def _watch_for_refusal(page: Any, *, through_egress: bool) -> list[AddressRefused]:
    """Records the egress proxy refusing a main-frame navigation: a plain-http one arrives as a 407
    page carrying the proxy's header, an https one fails with Chrome's CONNECT error (which only
    the egress proxy can cause when the context has no proxy of its own)."""
    refused: list[AddressRefused] = []

    def on_response(response: Any) -> None:
        reason = response.headers.get(REFUSAL_HEADER)
        if (
            reason
            and response.status == 407
            and response.frame == page.main_frame
            and response.request.is_navigation_request()
        ):
            refused.append(AddressRefused(urlparse(response.url).netloc, reason))

    def on_request_failed(request: Any) -> None:
        if (
            through_egress
            and request.failure == CONNECT_REFUSED
            and request.frame == page.main_frame
            and request.is_navigation_request()
        ):
            refused.append(
                AddressRefused(urlparse(request.url).netloc, CONNECT_REFUSED)
            )

    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    return refused
