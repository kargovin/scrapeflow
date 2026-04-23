"""
get_result tool — fetch the stored result content for a completed job.
"""

import structlog

from client import make_client
from config import settings

log = structlog.get_logger()


async def get_result(job_id: str) -> dict:
    async with make_client() as http:
        resp = await http.get(f"/jobs/{job_id}/result")
        if resp.status_code == 404:
            return {"error": "job not found or result not available", "job_id": job_id}
        if resp.status_code != 200:
            return {"error": f"result fetch failed: {resp.status_code}", "job_id": job_id}

        result = resp.json()
        content: str = result.get("content", "")
        truncated = False
        if len(content.encode()) > settings.mcp_max_content_bytes:
            content = content.encode()[: settings.mcp_max_content_bytes].decode(
                "utf-8", errors="ignore"
            )
            truncated = True

        payload: dict = {
            "job_id": job_id,
            "output_format": result.get("output_format"),
            "content": content,
        }
        if truncated:
            payload["notice"] = f"Content truncated to {settings.mcp_max_content_bytes} bytes."
        if result.get("warnings"):
            payload["warnings"] = result["warnings"]
        return payload
