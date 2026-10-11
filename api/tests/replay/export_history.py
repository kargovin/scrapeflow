"""Adds a workflow history to the replay corpus (``tests/test_workflow_replay.py``).

Run inside the api container against the compose Temporal, on the code that produced the run:

    docker compose exec api uv run python -m tests.replay.export_history <workflow_id> <name>

Never overwrite or regenerate a history: each file is what an older version of the code recorded,
and replaying it against today's code is the point.
"""

import asyncio
import sys
from pathlib import Path

from app.workflows.client import connect

HISTORIES = Path(__file__).parent / "histories"


async def export(workflow_id: str, name: str) -> Path:
    path = HISTORIES / f"{name}.json"
    if path.exists():
        raise SystemExit(f"{path} exists — histories are never overwritten")
    client = await connect()
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    path.write_text(history.to_json() + "\n")
    return path


if __name__ == "__main__":
    workflow_id, name = sys.argv[1], sys.argv[2]
    print(asyncio.run(export(workflow_id, name)))
