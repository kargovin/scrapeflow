"""Run one URL through LLMProbeWorkflow (Go `Scrape`, then `LLMExtract`), print the outputs and
the extraction, then delete every object under the probe's artifact prefix.

The API key is read from PROBE_LLM_API_KEY, else prompted for; the script encrypts it with
LLM_KEY_ENCRYPTION_KEY, as the API does for a stored key. It is never printed.

Usage:

    # local (from ./docker)
    docker compose exec api uv run python -m scripts.probe_llm https://example.com \\
        --provider anthropic --model claude-haiku-4-5-20251001

    # prod — -it for the key prompt
    kubectl -n scrapeflow exec -it deploy/scrapeflow-api -c api -- \\
        /app/.venv/bin/python -m scripts.probe_llm https://example.com \\
        --provider openai_compatible --base-url https://<endpoint>/v1 --model <model>
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
import uuid

from cryptography.fernet import Fernet
from temporalio.exceptions import ApplicationError

from app.core.minio import close_client, create_client
from app.core.storage import delete_minio_object
from app.settings import settings
from app.workflows.activities.contracts import ScrapeInput
from app.workflows.client import connect
from app.workflows.probe import LLMProbeInput, LLMProbeWorkflow
from app.workflows.queues import WORKFLOW_QUEUE

DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["title", "summary"],
    "additionalProperties": False,
}


async def _read_json(minio, path: str):
    bucket, _, key = path.partition("/")
    response = await minio.get_object(bucket, key)
    try:
        return json.loads(await response.read())
    finally:
        response.close()
        await response.release()


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("url")
    parser.add_argument("--provider", choices=["anthropic", "openai_compatible"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--format", choices=["html", "markdown", "json"], default="markdown")
    parser.add_argument("--schema", default=None, help="JSON Schema as a JSON string")
    args = parser.parse_args()

    api_key = os.environ.get("PROBE_LLM_API_KEY") or getpass.getpass("LLM API key: ")
    encrypted = Fernet(settings.llm_key_encryption_key).encrypt(api_key.encode()).decode()
    schema = json.loads(args.schema) if args.schema else DEFAULT_SCHEMA

    artifact_id = str(uuid.uuid4())
    client = await connect()
    minio = await create_client()
    exit_code = 0
    try:
        try:
            output = await client.execute_workflow(
                LLMProbeWorkflow.run,
                LLMProbeInput(
                    scrape=ScrapeInput(
                        artifact_id=artifact_id, url=args.url, output_format=args.format
                    ),
                    provider=args.provider,
                    encrypted_api_key=encrypted,
                    base_url=args.base_url,
                    model=args.model,
                    output_schema=schema,
                ),
                id=f"llm-probe-{artifact_id}",
                task_queue=WORKFLOW_QUEUE,
            )
        except Exception as exc:
            # WorkflowFailureError → ActivityError → the activity's ApplicationError (type =
            # LLMFailed etc.) → the original exception, also sent as an ApplicationError.
            err = exc
            while err.__cause__ is not None and not isinstance(err, ApplicationError):
                err = err.__cause__
            print(f"probe failed: {err}")
            exit_code = 1
        else:
            print(json.dumps({"artifact_id": artifact_id, **output.model_dump()}, indent=2))
            print("extraction:", json.dumps(await _read_json(minio, output.llm.result.path)))

        # By prefix, not by returned path: a failed LLM step still leaves the scrape behind.
        prefix = f"history/{artifact_id}/"
        paths = [
            f"{settings.minio_bucket}/{obj.object_name}"
            async for obj in minio.list_objects(
                settings.minio_bucket, prefix=prefix, recursive=True
            )
        ]
        for path in paths:
            ok = await delete_minio_object(minio, path, label="probe")
            print(f"{'deleted' if ok else 'DELETE FAILED'}: {path}")
            exit_code = exit_code or (0 if ok else 1)
    finally:
        await close_client(minio)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
