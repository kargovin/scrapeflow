"""
get_job_status tool — fetch current status for a single job.
"""

import structlog

from client import make_client

log = structlog.get_logger()


async def get_job_status(job_id: str) -> dict:
    async with make_client() as http:
        resp = await http.get(f"/jobs/{job_id}")
        if resp.status_code == 404:
            return {"error": "job not found", "job_id": job_id}
        if resp.status_code != 200:
            return {"error": f"request failed: {resp.status_code}", "job_id": job_id}

        data = resp.json()
        return {
            "job_id": job_id,
            "status": data.get("status"),
            "url": data.get("url"),
            "output_format": data.get("output_format"),
            "engine": data.get("engine"),
            "created_at": data.get("created_at"),
            "completed_at": data.get("completed_at"),
            "error": data.get("error"),
        }
