"""The real server over stdio: an MCP client's handshake and tool listing."""

import json
import os
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server.py"


def _rpc(*messages: dict) -> dict[int, dict]:
    """Send messages, keeping stdin open until every request has its reply."""
    wanted = {m["id"] for m in messages if "id" in m}
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "SCRAPEFLOW_API_KEY": "test-key"},
    )
    replies: dict[int, dict] = {}
    try:
        for m in messages:
            proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        while wanted - replies.keys():
            line = proc.stdout.readline()
            if not line:
                proc.wait(timeout=5)
                raise AssertionError(f"server exited: {proc.stderr.read()}")
            reply = json.loads(line)
            if "id" in reply:
                replies[reply["id"]] = reply
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return replies


def test_handshake_and_tool_list():
    by_id = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )

    assert by_id[1]["result"]["serverInfo"]["name"] == "scrapeflow"
    assert "tools" in by_id[1]["result"]["capabilities"]
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert names == {"scrape_url", "get_result", "get_job_status", "list_jobs"}
