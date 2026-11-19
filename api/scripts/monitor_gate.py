"""L.12 — PRD-018 slice 1 on compose, through the real path: the monitors API, the monitor's Temporal
Schedule, MonitorCheckWorkflow, PipelineWorkflow and the three workers, against the fixture in
contracts/fixtures/pipeline-gate/ (its /watched/<key> page changes only when the gate says so).

The measured criterion (ADR-016 §16): 24 checks of a page that changes twice after the first — 23 by
Check now and 1 by a real firing of the monitor's Schedule — give exactly 3 LLM calls, 3 kept result
copies, 3 webhook messages and 24 monthly units, and storage moves only for the 3 kept checks.

Runs as a throwaway user with an API key; its objects, rows and Schedules are removed afterwards (--keep
to inspect them). Exit status 1 on any mismatch. Not covered here (unit-tested, or the backlog's L.12
note says where): restarts mid-schedule and a Temporal outage, the SPA, pipelines switched off, and
waiting for a slot.

Usage (from ./docker; the fixture up, the LLM worker built from this tree, the workflow worker
restarted after any app/ change):

    docker compose --profile gate up -d pipeline-fixture
    docker compose exec -T api uv run python -m scripts.monitor_gate
"""

import argparse
import asyncio
import json
import sys
import time
import uuid

import httpx
from sqlalchemy import delete, func, select

from app.auth.api_key import generate_api_key, hash_api_key
from app.core import minio as minio_client
from app.core import monitor_schedules
from app.core.db import AsyncSessionLocal
from app.core.ledger import release_user_objects
from app.models.api_key import ApiKey
from app.models.monitor import Monitor
from app.models.quota_views import quota_run_units
from app.models.user import User
from app.models.user_quota import UserQuota
from app.workflows.client import connect
from scripts.pipeline_gate import API, LLM, SHOP, TERMINAL, Gate

GATED = {"kind": "changed"}


def watched(key: str) -> str:
    return f"{SHOP}/watched/{key}"


class MonitorGate(Gate):
    def __init__(self, api, fixture, key_id, user_id):
        super().__init__(api, fixture, key_id)
        self.user_id = user_id

    def gate_block(self, input="clean") -> dict:
        return {
            "id": "gate",
            "type": "validate",
            "input": input,
            "config": {"rules": [GATED], "when_unmet": "stop"},
        }

    async def set_page(self, key: str, **state) -> None:
        resp = await self.fixture.post(f"{SHOP}/_watched/{key}", json=state)
        resp.raise_for_status()

    async def monitor(self, pid: str, inputs: dict, carried=(), cron="0 3 1 1 *") -> dict:
        resp = await self.api.post(
            "/monitors",
            json={
                "name": f"gate-{uuid.uuid4().hex[:6]}",
                "pipeline_id": pid,
                "inputs": inputs,
                "carried": list(carried),
                "schedule_cron": cron,
            },
        )
        if resp.status_code != 201:
            raise RuntimeError(f"monitor: {resp.status_code} {resp.text}")
        return resp.json()

    async def check_now(self, monitor: dict) -> dict:
        resp = await self.api.post(f"/monitors/{monitor['id']}/check")
        if resp.status_code != 202:
            raise RuntimeError(f"check now: {resp.status_code} {resp.text}")
        return await self.wait(monitor["pipeline_id"], resp.json()["id"])

    async def firing(self, monitor: dict) -> dict:
        """One real firing of the monitor's Schedule: MonitorCheckWorkflow and its admission."""
        before = {r["id"] for r in await self.checks(monitor)}
        client = await connect()
        await client.get_schedule_handle(monitor_schedules.schedule_id(monitor["id"])).trigger()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            new = [r for r in await self.checks(monitor) if r["id"] not in before]
            if new and new[0]["status"] in TERMINAL:
                return await self.wait(monitor["pipeline_id"], new[0]["id"])
            await asyncio.sleep(1)
        raise RuntimeError("the firing made no check")

    async def checks(self, monitor: dict) -> list[dict]:
        resp = await self.api.get(
            f"/pipelines/{monitor['pipeline_id']}/runs",
            params={"monitor_id": monitor["id"], "limit": 200},
        )
        return resp.json()

    async def llm_calls(self) -> int:
        return len((await self.fixture.get(f"{LLM}/_calls")).json())

    async def storage(self) -> int:
        async with AsyncSessionLocal() as db:
            quota = await db.get(UserQuota, self.user_id)
            return quota.storage_bytes_used if quota else 0

    async def units(self) -> int:
        async with AsyncSessionLocal() as db:
            return await db.scalar(
                select(func.count())
                .select_from(quota_run_units)
                .where(quota_run_units.c.user_id == self.user_id)
            )

    def watched_pipeline(self, extra=()) -> list[dict]:
        """fetch -> clean -> gate -> extract [-> extra...] -> notify."""
        blocks = [self.fetch(), self.clean(), self.gate_block(), self.extract(input="gate"), *extra]
        events = ["pipeline.result", "pipeline.failed"]
        return [*blocks, self.notify(input=blocks[-1]["id"], events=events)]


def payload(post: dict) -> dict:
    return json.loads(post["body"])


async def measured(g: MonitorGate) -> None:
    print("R3 measured — 24 checks of a page that changes twice: 3 LLM calls, 3 copies, 3 messages")
    key = uuid.uuid4().hex[:8]
    await g.set_page(key, price="80")
    pid = await g.pipeline("measured", g.watched_pipeline())
    monitor = await g.monitor(pid, {"url": watched(key)})
    llm, units, storage = await g.llm_calls(), await g.units(), await g.storage()

    runs, moved = [], []
    for n in range(1, 25):
        if n == 9:
            await g.set_page(key, price="70")
        if n == 17:
            await g.set_page(key, price="60")
        before = await g.storage()
        run = await (g.firing(monitor) if n == 17 else g.check_now(monitor))
        runs.append(run)
        moved.append(await g.storage() - before)

    kept = {1, 9, 17}
    statuses = [r["change_status"] for r in runs]
    expected = ["first_check"] + ["unchanged"] * 7 + ["changed"] + ["unchanged"] * 7
    expected += ["changed"] + ["unchanged"] * 7
    g.check(all(r["status"] == "completed" for r in runs), "every check completed")
    g.check(statuses == expected, f"first check, 7 unchanged, changed, 7, changed, 7 ({statuses})")
    g.check(await g.llm_calls() - llm == 3, f"3 LLM calls ({await g.llm_calls() - llm})")
    copies = [n for n, r in enumerate(runs, 1) if r["result_path"]]
    g.check(copies == sorted(kept), f"3 kept copies, the first and the two changes ({copies})")
    posts = [p for r in runs for p in await g.posts(r["id"])]
    g.check(len(posts) == 3, f"3 webhook messages ({len(posts)})")
    g.check(await g.units() - units == 24, f"24 monthly units ({await g.units() - units})")
    g.check(
        all((m > 0) == (n in kept) for n, m in enumerate(moved, 1)),
        f"storage moves only for the kept checks ({moved})",
    )
    g.check(await g.storage() - storage == sum(moved), "storage after = before + the kept checks")

    first, second, third = (runs[n - 1] for n in sorted(kept))
    g.check(
        all(r["baseline_run_id"] == first["id"] for r in runs[1:8]),
        "checks 2-8 are unchanged since the first",
    )
    g.check(second["baseline_run_id"] == first["id"], "the first change is compared with check 1")
    g.check(third["baseline_run_id"] == second["id"], "the second change, with the first change")
    page = (second["change_summary"] or {}).get("gate", {})
    lines = page.get("lines") or []
    g.check(
        any(line.startswith("-") and "£80" in line for line in lines)
        and any(line.startswith("+") and "£70" in line for line in lines),
        "the page diff shows the old and the new price",
    )
    extraction = (second["change_summary"] or {}).get("extract", {})
    changes = {c["path"]: (c["old"], c["new"]) for c in extraction.get("changes") or []}
    g.check(changes.get("/price") == (80, 70), f"the extraction diff: /price 80 → 70 ({changes})")
    diff = await g.api.get(f"/pipelines/{pid}/runs/{second['id']}/diff", params={"block": "gate"})
    g.check(
        diff.status_code == 200 and diff.json()["diff"]["kind"] == "page", "the full diff reads"
    )

    bodies = {payload(p)["run_id"]: payload(p) for p in posts}
    one, two = bodies.get(first["id"], {}), bodies.get(second["id"], {})
    g.check(
        one.get("change", {}).get("status") == "first_check" and one.get("diff_detected") is False,
        "the first check's message: change first_check, diff_detected false",
    )
    g.check(
        two.get("monitor_id") == monitor["id"]
        and two.get("change", {}).get("status") == "changed"
        and two.get("diff_detected") is True
        and two["change"].get("baseline_run_id") == first["id"],
        "a change's message: monitor_id, change.status changed, diff_detected true, its baseline",
    )
    history = (await g.api.get(f"/monitors/{monitor['id']}/history", params={"limit": 200})).json()
    g.check(
        len([i for i in history if i["type"] == "check"]) == 24,
        "the 24 checks are in the monitor's history",
    )
    listed = await g.checks(monitor)
    g.check(len(listed) == 24, "and in the pipeline's run list, marked as the monitor's")

    print("R8 — deleting the newest baseline: the next check compares with the one before")
    await g.api.delete(f"/pipelines/{pid}/runs/{third['id']}")
    later = (await g.api.get(f"/pipelines/{pid}/runs/{runs[17]['id']}")).json()
    g.check(
        later["baseline_run_id"] is None and later["change_status"] == "unchanged",
        "a check compared with it now reads 'since deleted'",
    )
    run = await g.check_now(monitor)
    g.check(
        run["change_status"] == "changed" and run["baseline_run_id"] == second["id"],
        f"the next check finds the change against check 9 ({run['change_status']})",
    )

    print("R3 — the same pipeline by hand reads 'no baseline'")
    hand = await g.run(pid, {"url": watched(key)})
    g.check(
        hand["change_status"] == "no_baseline" and hand["monitor_id"] is None,
        f"a hand-run passes the gate as no_baseline ({hand['change_status']})",
    )
    hand_posts = await g.posts_after(hand["id"], 1)
    g.check(
        len(hand_posts) == 1 and "change" not in payload(hand_posts[0]),
        "its message is today's payload",
    )

    print("R8 — deleting the monitor with its checks frees exactly their bytes")
    used = await g.storage()
    async with AsyncSessionLocal() as db:
        from app.models.pipeline import PipelineRun, PipelineRunBlock
        from app.models.storage_object import StorageObject

        held = await db.scalar(
            select(func.coalesce(func.sum(StorageObject.bytes), 0))
            .join(PipelineRunBlock, PipelineRunBlock.id == StorageObject.pipeline_run_block_id)
            .join(PipelineRun, PipelineRun.id == PipelineRunBlock.pipeline_run_id)
            .where(PipelineRun.monitor_id == uuid.UUID(monitor["id"]))
        )
    resp = await g.api.delete(f"/monitors/{monitor['id']}", params={"with_checks": "true"})
    g.check(resp.status_code == 204, "the delete succeeds")
    g.check(await g.storage() == used - held, f"storage drops by exactly {held} bytes")


async def carried(g: MonitorGate) -> None:
    print("R4 — the price-dropped pipeline with a carried last_price")
    key = uuid.uuid4().hex[:8]
    await g.set_page(key, price="80")
    moved = {
        "id": "moved",
        "type": "validate",
        "input": "extract",
        "config": {
            "rules": [
                {"kind": "compare", "path": "/price", "op": "lt", "value": {"$input": "last_price"}}
            ],
            "when_unmet": "stop",
        },
    }
    pid = await g.pipeline(
        "carried",
        g.watched_pipeline(extra=[moved]),
        inputs={"url": {"type": "url"}, "last_price": {"type": "number"}},
    )
    monitor = await g.monitor(
        pid,
        {"url": watched(key)},
        carried=[{"input": "last_price", "from_block": "extract", "path": "/price", "start": 100}],
    )
    llm = await g.llm_calls()

    first = await g.check_now(monitor)
    g.check(
        first["inputs"]["last_price"] == 100
        and first["carried_sources"]["last_price"]["from"] == "start",
        "the first check uses the start value",
    )
    g.check(len(await g.posts_after(first["id"], 1)) == 1, "and sends (80 < 100)")
    same = await g.check_now(monitor)
    g.check(
        same["change_status"] == "unchanged" and await g.llm_calls() - llm == 1,
        "an unchanged page makes no LLM call",
    )
    await g.set_page(key, price="80", name="Blue Kettle II")
    renamed = await g.check_now(monitor)
    g.check(
        renamed["change_status"] == "changed" and renamed["stopped_at_block_id"] == "moved",
        "a changed page with the same price stops at the Validate",
    )
    g.check(
        renamed["inputs"]["last_price"] == 80
        and renamed["carried_sources"]["last_price"]["from"] == "baseline",
        "carrying 80 from the first check",
    )
    await asyncio.sleep(2)
    g.check(await g.posts(renamed["id"]) == [], "and sends nothing")
    await g.set_page(key, price="70", name="Blue Kettle II")
    lower = await g.check_now(monitor)
    g.check(
        lower["status"] == "completed" and lower["stopped_at_block_id"] is None,
        "a lower price runs every block",
    )
    g.check(len(await g.posts_after(lower["id"], 1)) == 1, "and sends (70 < 80)")


async def self_pause(g: MonitorGate) -> None:
    print("R6 — a monitor on a dead site pauses itself after the 5th failed check")
    pid = await g.pipeline("dead", g.watched_pipeline())
    monitor = await g.monitor(pid, {"url": f"{SHOP}/broken"})
    for _ in range(5):
        run = await g.check_now(monitor)
    read = (await g.api.get(f"/monitors/{monitor['id']}")).json()
    g.check(run["status"] == "failed", "each check fails")
    g.check(
        (read["state"], read["consecutive_failures"]) == ("paused_itself", 5),
        f"the monitor paused itself ({read['state']}, {read['consecutive_failures']})",
    )
    history = (await g.api.get(f"/monitors/{monitor['id']}/history")).json()
    events = [i["event"]["kind"] for i in history if i["type"] == "event"]
    g.check(events == ["paused_itself"], f"one paused_itself situation in its history ({events})")


async def _forget_schedules(user_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        ids = list((await db.scalars(select(Monitor.id).where(Monitor.user_id == user_id))).all())
    await monitor_schedules.forget(connect, ids)


async def gate(keep: bool, only: list[str]) -> list[str]:
    raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        user = User(
            clerk_id=f"monitor_gate_{uuid.uuid4().hex}",
            email=f"monitor-gate-{uuid.uuid4().hex[:8]}@scrapeflow.test",
        )
        db.add(user)
        await db.flush()
        db.add(ApiKey(user_id=user.id, key_hash=hash_api_key(raw), name="monitor gate"))
        await db.commit()
        await db.refresh(user)
    print(f"gate user {user.id}" + (f" key {raw}" if keep else ""))
    try:
        async with (
            httpx.AsyncClient(base_url=API, headers={"X-API-Key": raw}, timeout=30) as api,
            httpx.AsyncClient(timeout=30) as fixture,
        ):
            key = await api.post(
                "/users/llm-keys",
                json={
                    "name": "fixture",
                    "provider": "openai_compatible",
                    "api_key": "fixture-key",
                    "base_url": f"{LLM}/v1",
                },
            )
            g = MonitorGate(api, fixture, key.json()["id"], user.id)
            scenarios = {"measured": measured, "carried": carried, "self_pause": self_pause}
            for name, scenario in scenarios.items():
                if not only or name in only:
                    await scenario(g)
            return g.problems
    finally:
        if not keep:
            await _forget_schedules(user.id)
            minio = await minio_client.create_client()
            async with AsyncSessionLocal() as db:
                await release_user_objects(db, minio, user.id, "monitor gate cleanup")
                await db.execute(delete(User).where(User.id == user.id))
                await db.commit()
            await minio_client.close_client(minio)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="keep the gate user and its runs")
    parser.add_argument("only", nargs="*", help="scenario names; all when omitted")
    args = parser.parse_args()
    problems = asyncio.run(gate(args.keep, args.only))
    for problem in problems:
        print(f"MISMATCH  {problem}")
    print("GATE PASSED" if not problems else f"GATE FAILED ({len(problems)})")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
