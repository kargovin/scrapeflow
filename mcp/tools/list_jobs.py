"""
list_jobs tool — paginated list of the authenticated user's jobs.
"""

import structlog

from client import make_client

log = structlog.get_logger()


async def list_jobs(limit: int = 20, offset: int = 0) -> dict:
    async with make_client() as http:
        resp = await http.get("/jobs", params={"limit": limit, "offset": offset})
        if resp.status_code != 200:
            return {"error": f"request failed: {resp.status_code}"}

        jobs = resp.json()
        return {
            "jobs": [
                {
                    "job_id": j["id"],
                    "url": j["url"],
                    "status": j["status"],
                    "output_format": j["output_format"],
                    "engine": j.get("engine"),
                    "created_at": j["created_at"],
                    "completed_at": j.get("completed_at"),
                }
                for j in jobs
            ],
            "count": len(jobs),
            "offset": offset,
        }
