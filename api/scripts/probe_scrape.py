"""Run one URL through ScrapeProbeWorkflow (the Go `Scrape` activity), print the output, then
delete the objects it wrote.

Usage:

    # local (from ./docker)
    docker compose exec api uv run python -m scripts.probe_scrape https://example.com [--format html]

    # prod
    kubectl -n scrapeflow exec deploy/scrapeflow-api -c api -- \\
        /app/.venv/bin/python -m scripts.probe_scrape https://example.com
"""

import argparse
import asyncio
import json
import sys
import uuid

from app.core.minio import close_client, create_client
from app.core.storage import delete_minio_object
from app.workflows.activities.contracts import ScrapeInput
from app.workflows.client import connect
from app.workflows.probe import ScrapeProbeWorkflow
from app.workflows.queues import WORKFLOW_QUEUE


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("url")
    parser.add_argument("--format", choices=["html", "markdown", "json"], default="html")
    args = parser.parse_args()

    artifact_id = str(uuid.uuid4())
    client = await connect()
    output = await client.execute_workflow(
        ScrapeProbeWorkflow.run,
        ScrapeInput(artifact_id=artifact_id, url=args.url, output_format=args.format),
        id=f"scrape-probe-{artifact_id}",
        task_queue=WORKFLOW_QUEUE,
    )

    print(json.dumps({"artifact_id": artifact_id, **output.model_dump()}, indent=2))

    paths = [output.result.path, *(s.path for s in output.screenshots)]
    minio = await create_client()
    try:
        results = [await delete_minio_object(minio, p, label="probe") for p in paths]
    finally:
        await close_client(minio)

    for path, ok in zip(paths, results, strict=True):
        print(f"{'deleted' if ok else 'DELETE FAILED'}: {path}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
