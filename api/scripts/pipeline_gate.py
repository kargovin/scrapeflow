"""J.10 — PRD-019's success criteria on compose, through the real path: the API, PipelineWorkflow, the
three workers and WebhookDeliveryWorkflow, against the fixture in contracts/fixtures/pipeline-gate/.

Runs as a throwaway user with an API key; its stored objects and rows are removed afterwards (--keep to
inspect them). Exit status 1 on any mismatch. Not covered here (the backlog's J.10 note says where):
the worker-kill check, a run that could not start, a time budget running out, the Webhook block's own
failure, the SPA, and the bucket criterion (M's gate).

Usage (from ./docker; the fixture up, the workflow worker restarted after any app/ change):

    docker compose --profile gate up -d pipeline-fixture
    docker compose exec -T api uv run python -m scripts.pipeline_gate
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import time
import uuid

import httpx
from sqlalchemy import delete

from app.auth.api_key import generate_api_key, hash_api_key
from app.core import minio as minio_client
from app.core.db import AsyncSessionLocal
from app.core.ledger import release_user_objects
from app.models.api_key import ApiKey
from app.models.user import User
from app.models.user_quota import UserQuota

API = "http://localhost:8000"
SHOP = "http://shop.pipelinefixture.com"
LLM = "http://llm.pipelinefixture.com"
HOOKS = "http://hooks.pipelinefixture.com"
SECRET = "gate-secret"
TERMINAL = {"completed", "failed", "cancelled"}
SCHEMA = {
    "type": "object",
    "properties": {
        "price": {"type": ["number", "null"]},
        "in_stock": {"type": "boolean"},
        "name": {"type": ["string", "null"]},
    },
}


def item(price="80", stock="in", name="Blue Kettle") -> str:
    return (
        f"{SHOP}/item?"
        + httpx.QueryParams({"name": name, "price": price, "stock": stock}).__str__()
    )


class Gate:
    def __init__(self, api: httpx.AsyncClient, fixture: httpx.AsyncClient, key_id: str):
        self.api, self.fixture, self.key_id = api, fixture, key_id
        self.problems: list[str] = []

    def check(self, ok: bool, what: str) -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            self.problems.append(what)

    # --- blocks ------------------------------------------------------------------------------

    @staticmethod
    def fetch() -> dict:
        return {"id": "fetch", "type": "scrape", "config": {"url": {"$input": "url"}}}

    def extract(self, input="fetch") -> dict:
        return {
            "id": "extract",
            "type": "llm_extract",
            "input": input,
            "config": {"llm_key_id": self.key_id, "model": "fixture", "output_schema": SCHEMA},
        }

    @staticmethod
    def guard(rules, input="extract", **config) -> dict:
        return {
            "id": "guard",
            "type": "validate",
            "input": input,
            "config": {"rules": rules, **config},
        }

    @staticmethod
    def notify(path="/ok", input="guard", events=None) -> dict:
        config = {"url": f"{HOOKS}{path}", "secret": SECRET}
        if events is not None:
            config["events"] = events
        return {"id": "notify", "type": "webhook", "input": input, "config": config}

    @staticmethod
    def clean(input="fetch") -> dict:
        return {"id": "clean", "type": "clean", "input": input}

    # --- calls -------------------------------------------------------------------------------

    async def pipeline(self, name: str, blocks: list[dict], inputs: dict | None = None) -> str:
        definition = {"blocks": blocks, "inputs": inputs or {"url": {"type": "url"}}}
        resp = await self.api.post(
            "/pipelines",
            json={"name": f"gate-{name}-{uuid.uuid4().hex[:6]}", "definition": definition},
        )
        if resp.status_code != 201:
            raise RuntimeError(f"save {name}: {resp.status_code} {resp.text}")
        return resp.json()["id"]

    async def trigger(self, pid: str, inputs: dict) -> httpx.Response:
        return await self.api.post(f"/pipelines/{pid}/runs", json={"inputs": inputs})

    async def run(self, pid: str, inputs: dict, timeout: float = 120) -> dict:
        resp = await self.trigger(pid, inputs)
        if resp.status_code != 202:
            raise RuntimeError(f"run {pid}: {resp.status_code} {resp.text}")
        return await self.wait(pid, resp.json()["id"], timeout)

    async def wait(self, pid: str, rid: str, timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            run = (await self.api.get(f"/pipelines/{pid}/runs/{rid}")).json()
            if run["status"] in TERMINAL or time.monotonic() > deadline:
                return run
            await asyncio.sleep(1)

    async def posts(self, run_id: str) -> list[dict]:
        log = (await self.fixture.get(f"{HOOKS}/_log")).json()
        return [p for p in log if json.loads(p["body"] or "{}").get("run_id") == run_id]

    async def posts_after(self, run_id: str, count: int, timeout: float = 30) -> list[dict]:
        deadline = time.monotonic() + timeout
        while len(found := await self.posts(run_id)) < count and time.monotonic() < deadline:
            await asyncio.sleep(1)
        return found

    async def llm_calls(self) -> int:
        return len((await self.fixture.get(f"{LLM}/_calls")).json())


def blocks_by_id(run: dict) -> dict[str, dict]:
    return {b["block_id"]: b for b in run["blocks"]}


def signed(post: dict) -> bool:
    mac = hmac.new(SECRET.encode(), post["body"].encode(), hashlib.sha256).hexdigest()
    return post["headers"].get("x-scrapeflow-signature") == f"sha256={mac}"


async def stopping(g: Gate) -> None:
    print("R1/R2/R4 — stop on a bound threshold (scrape → extract → validate(stop) → webhook)")
    rule = {"kind": "compare", "path": "/price", "op": "lt", "value": {"$input": "max_price"}}
    inputs = {"url": {"type": "url"}, "max_price": {"type": "number"}}
    pid = await g.pipeline(
        "stop",
        [
            g.fetch(),
            g.extract(),
            g.guard([rule], when_unmet="stop"),
            g.notify(events=["pipeline.result", "pipeline.failed"]),
        ],
        inputs,
    )

    run = await g.run(pid, {"url": item("80"), "max_price": 100})
    posts = await g.posts_after(run["id"], 1)
    g.check(
        run["status"] == "completed" and run["stopped_at_block_id"] is None, "£80 runs every block"
    )
    g.check(
        [json.loads(p["body"])["event"] for p in posts] == ["pipeline.result"] and signed(posts[0]),
        "£80 sends one signed pipeline.result",
    )

    llm_before = await g.llm_calls()
    run = await g.run(pid, {"url": item("120"), "max_price": 100})
    blocks = blocks_by_id(run)
    verdict = blocks["guard"]["verdict"] or {}
    g.check(
        (run["status"], run["stopped_at_block_id"], run["stop_reason"])
        == ("completed", "guard", "rule_not_met"),
        "£120 ends completed, stopped at guard, rule_not_met",
    )
    g.check(
        (verdict.get("rule_index"), verdict.get("found"), verdict.get("expected"))
        == (0, 120.0, 100)
        and verdict.get("rule", {}).get("value") == {"$input": "max_price"},
        f"the verdict names rule 0, found 120, expected 100, the rule as saved: {verdict}",
    )
    g.check(
        (
            blocks["notify"]["status"],
            blocks["notify"]["status_reason"],
            blocks["notify"]["reason_block_id"],
        )
        == ("skipped", "stopped", "guard"),
        "the Webhook block is skipped, stopped by guard",
    )
    await asyncio.sleep(3)
    g.check(await g.posts(run["id"]) == [], "a stop sends nothing (result or alert)")
    g.check(await g.llm_calls() == llm_before + 1, "the extraction ran once")
    result = await g.api.get(f"/pipelines/{pid}/runs/{run['id']}/result")
    g.check(
        result.status_code == 200 and result.json()["block_id"] == "extract",
        "with keep, the extraction stays retrievable",
    )
    listed = (await g.api.get(f"/pipelines/{pid}/runs")).json()
    labels = {r["id"]: r["stopped_at_block_id"] for r in listed}
    g.check(
        labels.get(run["id"]) == "guard" and list(labels.values()).count(None) == 1,
        "the run list labels the stopped run only",
    )

    run = await g.run(pid, {"url": item("80"), "max_price": 40})
    g.check(run["stopped_at_block_id"] == "guard", "the same pipeline stops at 40 with no edit")
    run = await g.run(pid, {"url": item("120"), "max_price": 200})
    g.check(run["stopped_at_block_id"] is None, "...and passes £120 at 200")

    for bad, why in (
        ({"url": item(), "max_price": "100"}, "a numeric string"),
        ({"url": item()}, "a missing input"),
    ):
        resp = await g.trigger(pid, bad)
        g.check(
            resp.status_code == 422, f"{why} is refused before anything runs ({resp.status_code})"
        )


async def failing_mode(g: Gate) -> None:
    print("R1 — fail mode, and a definition saved without the new fields, are today's behaviour")
    rule = {"kind": "compare", "path": "/price", "op": "lt", "value": 100}
    pid = await g.pipeline("old", [g.fetch(), g.extract(), g.guard([rule]), g.notify()])
    run = await g.run(pid, {"url": item("80")})
    posts = await g.posts_after(run["id"], 1)
    g.check(run["status"] == "completed" and len(posts) == 1, "£80 completes and sends its result")
    run = await g.run(pid, {"url": item("120")})
    blocks = blocks_by_id(run)
    g.check(
        run["status"] == "failed" and "rules[0] (compare)" in (run["error"] or ""),
        f"£120 fails naming the rule: {run['error']}",
    )
    g.check(
        (run["failed_block_id"], blocks["notify"]["status_reason"]) == ("guard", "upstream_failed"),
        "the run names its failed block; the Webhook is skipped, upstream failed",
    )
    await asyncio.sleep(3)
    g.check(await g.posts(run["id"]) == [], "a result-only Webhook sends no alert")


async def deleting(g: Gate, user_id: uuid.UUID) -> None:
    print("D10 — a stop set to delete frees exactly what the run stored")
    rule = {"kind": "compare", "path": "/price", "op": "lt", "value": 100}
    pid = await g.pipeline(
        "delete",
        [
            g.fetch(),
            g.extract(),
            g.guard([rule], when_unmet="stop", on_stop_outputs="delete"),
            g.notify(),
        ],
    )
    before = await _storage(user_id)
    run = await g.run(pid, {"url": item("120")})
    after = await _storage(user_id)
    blocks = blocks_by_id(run)
    g.check(
        run["stopped_at_block_id"] == "guard" and run["result_path"] is None,
        "stopped, no result path",
    )
    g.check(after == before, f"storage back where it was ({before} → {after})")
    g.check(
        all(blocks[b]["collected_at"] for b in ("fetch", "extract")),
        "the stored blocks read as collected",
    )
    g.check((blocks["guard"]["verdict"] or {}).get("found") == 120.0, "the verdict is still shown")
    result = await g.api.get(f"/pipelines/{pid}/runs/{run['id']}/result")
    g.check(result.status_code == 404, f"no result to read ({result.status_code})")


async def before_the_llm(g: Gate) -> None:
    print("R1 — a stop before the LLM block makes no LLM call; text rules and bound text")
    pid = await g.pipeline(
        "stock",
        [
            g.fetch(),
            g.clean(),
            g.guard([{"kind": "contains", "text": "In stock"}], input="clean", when_unmet="stop"),
            g.extract(input="guard"),
            g.notify(input="extract"),
        ],
    )
    calls = await g.llm_calls()
    run = await g.run(pid, {"url": item("80", stock="out")})
    g.check(run["stopped_at_block_id"] == "guard", "an out-of-stock page stops at guard")
    g.check(await g.llm_calls() == calls, "with no LLM call")
    run = await g.run(pid, {"url": item("80")})
    g.check(
        run["status"] == "completed" and await g.llm_calls() == calls + 1,
        "an in-stock page runs on",
    )

    pid = await g.pipeline(
        "phrase",
        [
            g.fetch(),
            g.clean(),
            g.guard(
                [{"kind": "contains", "text": {"$input": "phrase"}}],
                input="clean",
                when_unmet="stop",
            ),
            g.notify(input="guard"),
        ],
        {"url": {"type": "url"}, "phrase": {"type": "text"}},
    )
    run = await g.run(pid, {"url": item(), "phrase": "Blue Kettle"})
    g.check(run["stopped_at_block_id"] is None, "'Blue Kettle' passes on the kettle page")
    run = await g.run(pid, {"url": item(), "phrase": "Red Toaster"})
    g.check(run["stopped_at_block_id"] == "guard", "'Red Toaster' stops on the same page")

    pid = await g.pipeline(
        "absent",
        [
            g.fetch(),
            g.clean(),
            g.guard(
                [{"kind": "contains", "text": "Out of stock", "negate": True}],
                input="clean",
                when_unmet="stop",
            ),
            g.notify(input="guard"),
        ],
    )
    run = await g.run(pid, {"url": item(stock="out")})
    g.check(run["stopped_at_block_id"] == "guard", "a reversed contains stops on 'Out of stock'")
    run = await g.run(pid, {"url": item()})
    g.check(run["stopped_at_block_id"] is None, "...and passes an in-stock page")


async def null_price(g: Gate) -> None:
    print("R1 — [type /price number, compare /price < 100] stops at the first rule on a null price")
    rules = [
        {"kind": "type", "path": "/price", "type": "number"},
        {"kind": "compare", "path": "/price", "op": "lt", "value": 100},
    ]
    pid = await g.pipeline(
        "null", [g.fetch(), g.extract(), g.guard(rules, when_unmet="stop"), g.notify()]
    )
    run = await g.run(pid, {"url": item("null")})
    verdict = blocks_by_id(run)["guard"]["verdict"] or {}
    g.check(
        run["status"] == "completed" and verdict.get("rule_index") == 0,
        f"stopped at rule 0, not failed on rule 1: {run['status']} {verdict}",
    )
    pid = await g.pipeline(
        "unverifiable",
        [g.fetch(), g.extract(), g.guard(rules[1:], when_unmet="stop"), g.notify()],
    )
    run = await g.run(pid, {"url": item("null")})
    g.check(
        run["status"] == "failed" and run["failed_block_id"] == "guard",
        f"a compare that cannot be checked fails the run: {run['error']}",
    )


async def alerts(g: Gate) -> None:
    print("R5 — failure alerts")
    pid = await g.pipeline(
        "breaks-only", [g.fetch(), g.notify(input="fetch", events=["pipeline.failed"])]
    )
    run = await g.run(pid, {"url": item()})
    notify = blocks_by_id(run)["notify"]
    g.check(
        run["status"] == "completed"
        and (notify["status"], notify["status_reason"]) == ("skipped", "alert_only"),
        "breaks only, on a run that completes: skipped, alert only",
    )
    await asyncio.sleep(3)
    g.check(await g.posts(run["id"]) == [], "...and nothing is sent")

    run = await g.run(pid, {"url": f"{SHOP}/broken"})
    posts = await g.posts_after(run["id"], 1)
    payload = json.loads(posts[0]["body"]) if posts else {}
    g.check(
        run["status"] == "failed" and run["failed_block_id"] == "fetch",
        "a Scrape failure fails the run",
    )
    g.check(
        len(posts) == 1
        and signed(posts[0])
        and (payload.get("event"), payload.get("block_id"), payload.get("failed_block_id"))
        == ("pipeline.failed", "notify", "fetch")
        and payload.get("error", "").startswith("fetch: "),
        f"one signed pipeline.failed naming the block and the error: {payload}",
    )
    g.check(
        [(a["block_id"], a["status"]) for a in (await g.wait(pid, run["id"]))["alerts"]]
        == [("notify", "delivered")],
        "the run page lists the alert as delivered",
    )

    pid = await g.pipeline(
        "both", [g.fetch(), g.notify(input="fetch", events=["pipeline.result", "pipeline.failed"])]
    )
    ok = await g.run(pid, {"url": item()})
    broken = await g.run(pid, {"url": f"{SHOP}/broken"})
    sent = [
        json.loads(p["body"])["event"]
        for r in (ok, broken)
        for p in await g.posts_after(r["id"], 1)
    ]
    g.check(
        sent == ["pipeline.result", "pipeline.failed"],
        f"OK + breaks: result, then alert, same URL ({sent})",
    )

    pid = await g.pipeline(
        "receiver-down", [g.fetch(), g.notify("/down", input="fetch", events=["pipeline.failed"])]
    )
    started = time.monotonic()
    run = await g.run(pid, {"url": f"{SHOP}/broken"})
    g.check(
        run["status"] == "failed" and time.monotonic() - started < 60,
        "receiver down: the run fails at once",
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        alert = ((await g.wait(pid, run["id"]))["alerts"] or [{}])[0]
        if alert.get("attempts"):
            break
        await asyncio.sleep(1)
    g.check(
        (alert.get("status"), alert.get("attempts"), alert.get("last_error"))
        == ("pending", 1, "HTTP 503"),
        f"the alert waits to retry, its error shown: {alert}",
    )


async def cancelled(g: Gate) -> None:
    print("R5 — a cancelled run sends nothing")
    pid = await g.pipeline(
        "cancel",
        [
            g.fetch(),
            g.extract(),
            g.notify(input="extract", events=["pipeline.result", "pipeline.failed"]),
        ],
    )
    resp = await g.trigger(pid, {"url": item(name="SLOW Kettle")})
    rid = resp.json()["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = (await g.api.get(f"/pipelines/{pid}/runs/{rid}")).json()
        if blocks_by_id(run)["extract"]["status"] == "running":
            break
        await asyncio.sleep(0.5)
    await g.api.post(f"/pipelines/{pid}/runs/{rid}/cancel", json={})
    run = await g.wait(pid, rid)
    g.check(run["status"] == "cancelled", f"the run ends cancelled ({run['status']})")
    g.check(
        blocks_by_id(run)["notify"]["status_reason"] == "cancelled",
        "its unrun block is skipped, cancelled",
    )
    await asyncio.sleep(3)
    g.check(await g.posts(rid) == [] and run["alerts"] == [], "nothing is sent")


async def _storage(user_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as db:
        quota = await db.get(UserQuota, user_id)
        return quota.storage_bytes_used if quota else 0


async def gate(keep: bool, only: list[str]) -> list[str]:
    raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        user = User(
            clerk_id=f"pipeline_gate_{uuid.uuid4().hex}",
            email=f"pipeline-gate-{uuid.uuid4().hex[:8]}@scrapeflow.test",
        )
        db.add(user)
        await db.flush()
        db.add(ApiKey(user_id=user.id, key_hash=hash_api_key(raw), name="pipeline gate"))
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
            g = Gate(api, fixture, key.json()["id"])
            scenarios = {
                "stopping": stopping,
                "failing_mode": failing_mode,
                "deleting": lambda g: deleting(g, user.id),
                "before_the_llm": before_the_llm,
                "null_price": null_price,
                "alerts": alerts,
                "cancelled": cancelled,
            }
            for name, scenario in scenarios.items():
                if not only or name in only:
                    await scenario(g)
            return g.problems
    finally:
        if not keep:
            minio = await minio_client.create_client()
            async with AsyncSessionLocal() as db:
                await release_user_objects(db, minio, user.id, "pipeline gate cleanup")
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
