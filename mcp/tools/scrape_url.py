"""
scrape_url tool — submit a single-URL scrape job and optionally wait for the result.

wait_for_result=True (default): polls until terminal status, then returns content.
wait_for_result=False: returns immediately with the job_id for later lookup.
"""

import asyncio

import structlog

from client import make_client
from config import settings

log = structlog.get_logger()

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


async def scrape_url(
    url: str,
    output_format: str = "html",
    engine: str = "http",
    wait_for_result: bool = True,
) -> dict:
    async with make_client() as http:
        resp = await http.post(
            "/jobs",
            json={"url": url, "output_format": output_format, "engine": engine},
        )
        if resp.status_code != 200:
            return {"error": f"job creation failed: {resp.status_code}", "detail": resp.text}

        job = resp.json()
        job_id = job["id"]
        log.info("job_submitted", job_id=job_id, url=url)

        if not wait_for_result:
            return {"job_id": job_id, "status": job.get("status")}

        elapsed = 0.0
        while elapsed < settings.mcp_max_wait_seconds:
            await asyncio.sleep(settings.mcp_poll_interval_seconds)
            elapsed += settings.mcp_poll_interval_seconds

            poll = await http.get(f"/jobs/{job_id}")
            if poll.status_code != 200:
                return {"error": f"status poll failed: {poll.status_code}", "job_id": job_id}

            data = poll.json()
            status = data.get("status")
            log.debug("job_polled", job_id=job_id, status=status, elapsed=elapsed)

            if status not in TERMINAL_STATUSES:
                continue

            if status == "completed":
                result_resp = await http.get(f"/jobs/{job_id}/result")
                if result_resp.status_code != 200:
                    return {
                        "job_id": job_id,
                        "status": "completed",
                        "error": f"result fetch failed: {result_resp.status_code}",
                    }
                result = result_resp.json()
                content: str = result.get("content", "")
                truncated = False
                if len(content.encode()) > settings.mcp_max_content_bytes:
                    content = content.encode()[: settings.mcp_max_content_bytes].decode(
                        "utf-8", errors="ignore"
                    )
                    truncated = True

                payload: dict = {
                    "job_id": job_id,
                    "status": "completed",
                    "output_format": result.get("output_format"),
                    "content": content,
                }
                if truncated:
                    payload["notice"] = (
                        f"Content truncated to {settings.mcp_max_content_bytes} bytes."
                    )
                if result.get("warnings"):
                    payload["warnings"] = result["warnings"]
                return payload

            return {"job_id": job_id, "status": status, "error": data.get("error")}

        return {
            "job_id": job_id,
            "status": "timeout",
            "notice": (
                f"Job did not complete within {settings.mcp_max_wait_seconds}s. "
                f"Use get_job_status(job_id='{job_id}') to check later."
            ),
        }
