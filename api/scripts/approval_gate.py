"""N.5 — PRD-018's approval criteria (R5, D8-D10; R6's one-thread rule; R8's superseded check) on
compose, through the real path: the pipelines and monitors API, the answer route, PipelineWorkflow
(patch ``approval``), MonitorCheckWorkflow (patch ``approval-settled``), the monitor's Schedule,
WebhookDeliveryWorkflow, the delivery worker (an SMTP destination, a bucket behind TLS) and the
fixtures in contracts/fixtures/pipeline-gate/ and delivery-gate/.

Run it with ``scripts/approval-gate.sh`` (repo root): the driver brings the fixtures up, gives the
workers the gate CA and makes the MinIO users from a fresh secret (``GATE_SECRET``), as the delivery
gate does. Runs as a throwaway user beside a second user and an admin; their rows, objects and
Schedules are removed afterwards and every run still waiting is cancelled first. Exit status 1 on any
mismatch.

Three scenarios wait out real time (≈ 62 min, while the others run): an approval expiring at its
1-hour deadline with an answer racing it, a 3-hour approval's reminder (deadline - 2 h), and an
answer whose signal is lost found at that reminder. ``--no-waits`` skips them.

A lost signal is the answer route minus its wake-up: ``approvals.record_answer`` under the run lock,
committed, no signal sent.
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus

from app.core import approvals, monitor_schedules
from app.core import minio as minio_client
from app.core.db import AsyncSessionLocal
from app.models.pipeline import PipelineRun
from app.settings import settings
from app.workflows.client import connect
from scripts.delivery_gate import (
    CONTROL,
    HOOKS_TLS,
    PRIVATE,
    SECRET,
    DeliveryGate,
    _remove,
    _user,
    entry,
    location,
    signed_with,
)
from scripts.monitor_gate import MonitorGate, watched
from scripts.pipeline_gate import API, HOOKS, LLM, TERMINAL, blocks_by_id

APPROVAL_HOOK = f"/{SECRET}-approval"
NOTICE_HOOK = f"/{SECRET}-notice"
HOOK_SIGNING = f"{SECRET}-approval-sign"
ANSWERED = ("approved", "rejected", "expired", "superseded")


def payload(post: dict) -> dict:
    return json.loads(post["body"])


def approval_of(run: dict, block_id: str = "approve") -> dict:
    return blocks_by_id(run).get(block_id) or {}


def asked(run: dict) -> bool:
    block = approval_of(run)
    return run["status"] in TERMINAL or (
        block.get("status") == "waiting" and block.get("approval_deadline") is not None
    )


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ApprovalGate(DeliveryGate):
    set_page = MonitorGate.set_page
    gate_block = MonitorGate.gate_block

    def __init__(self, api, admin, other, fixture, key_id, user_id, minio):
        super().__init__(api, admin, fixture, key_id, user_id, minio)
        self.other = other

    # --- blocks ------------------------------------------------------------------------------

    @staticmethod
    def approve(input: str, destination=None, hours=24, id="approve") -> dict:
        config: dict = {"deadline_hours": hours}
        if destination is not None:
            config["destination_id"] = destination
        return {"id": id, "type": "approval", "input": input, "config": config}

    @staticmethod
    def rule(id: str, limit: int, mode: str) -> dict:
        return {
            "id": id,
            "type": "validate",
            "input": "extract" if id == "sane" else "sane",
            "config": {
                "rules": [{"kind": "compare", "path": "/price", "op": "lt", "value": limit}],
                "when_unmet": mode,
            },
        }

    def watched_pipeline(self, hours=24) -> list[dict]:
        """fetch → clean → gate → extract → sane (fail ≥ 1000) → cheap (stop ≥ 500) → store →
        approve → notify."""
        return [
            self.fetch(),
            self.clean(),
            self.gate_block(),
            self.extract(input="gate"),
            self.rule("sane", 1000, "fail"),
            self.rule("cheap", 500, "stop"),
            self.store(self.dest["rw"], input="cheap"),
            self.approve("store", self.dest["hook"], hours),
            self.tail("approve"),
        ]

    @staticmethod
    def tail(input: str) -> dict:
        """The send held back: a result, or an alert when the run breaks."""
        return {
            "id": "notify",
            "type": "webhook",
            "input": input,
            "config": {"url": f"{HOOKS}/ok", "events": ["pipeline.result", "pipeline.failed"]},
        }

    # --- monitors ----------------------------------------------------------------------------

    async def monitor(self, pid: str, key: str, notice: str | None = None) -> dict:
        body = {
            "name": f"gate-{uuid.uuid4().hex[:6]}",
            "pipeline_id": pid,
            "inputs": {"url": watched(key)},
            "schedule_cron": "0 3 1 1 *",
        }
        if notice is not None:
            body["notice_destination_id"] = notice
        resp = await self.api.post("/monitors", json=body)
        if resp.status_code != 201:
            raise RuntimeError(f"monitor: {resp.status_code} {resp.text}")
        return resp.json()

    async def check_now(self, monitor: dict) -> dict:
        """A Check now, read once it has asked for approval or ended."""
        resp = await self.api.post(f"/monitors/{monitor['id']}/check")
        if resp.status_code != 202:
            raise RuntimeError(f"check now: {resp.status_code} {resp.text}")
        return await self.until(monitor["pipeline_id"], resp.json()["id"], asked)

    async def checks(self, monitor: dict) -> list[dict]:
        resp = await self.api.get(
            f"/pipelines/{monitor['pipeline_id']}/runs",
            params={"monitor_id": monitor["id"], "limit": 200},
        )
        return resp.json()

    async def firing(self, monitor: dict) -> dict | None:
        """One real firing of the monitor's Schedule; the check it made, once asked or ended, or
        None when it made none in 60 s."""
        before = {r["id"] for r in await self.checks(monitor)}
        client = await connect()
        await client.get_schedule_handle(monitor_schedules.schedule_id(monitor["id"])).trigger()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            new = [r for r in await self.checks(monitor) if r["id"] not in before]
            if new:
                return await self.until(monitor["pipeline_id"], new[0]["id"], asked)
            await asyncio.sleep(1)
        return None

    async def history(self, monitor: dict) -> list[dict]:
        return (await self.api.get(f"/monitors/{monitor['id']}/history")).json()

    async def waiting_for(self, monitor: dict) -> list[str]:
        """The run ids of the user's approvals waiting under this monitor."""
        listed = (await self.api.get("/approvals")).json()
        return [a["run_id"] for a in listed if a["monitor_id"] == monitor["id"]]

    # --- approvals ---------------------------------------------------------------------------

    async def answer(self, run: dict, decision: str, client=None) -> httpx.Response:
        return await (client or self.api).post(
            f"/pipelines/{run['pipeline_id']}/runs/{run['id']}/approval",
            json={"decision": decision},
        )

    async def lose(self, run: dict, decision: str = "approved") -> bool:
        """The answer route's write without its wake-up (a lost signal)."""
        async with AsyncSessionLocal() as db:
            row = await db.scalar(
                select(PipelineRun).where(PipelineRun.id == uuid.UUID(run["id"])).with_for_update()
            )
            block = await approvals.approval_block(db, row.id)
            ok = await approvals.record_answer(
                db, block, decision, answered_by=self.user_id, open_until_deadline=True
            )
            await db.commit()
            return ok

    async def messages(self, rid: str, count: int = 0, path=APPROVAL_HOOK) -> list[dict]:
        """The approval messages that reached ``path`` for a run (waits for ``count``)."""
        deadline = time.monotonic() + 40
        while True:
            found = [p for p in await self.hook_posts(path) if payload(p).get("run_id") == rid]
            if len(found) >= count or time.monotonic() > deadline:
                return found
            await asyncio.sleep(1)

    async def tail_posts(self, rid: str, count: int = 0) -> list[dict]:
        """What the tail webhook (``HOOKS/ok``) sent for a run (waits for ``count``)."""
        return await self.messages(rid, count, path="/ok")

    async def events(self, rid: str, count: int = 0) -> list[str]:
        return [payload(p)["event"] for p in await self.messages(rid, count)]

    async def eligible(self, rid: str) -> bool:
        async with AsyncSessionLocal() as db:
            run = await db.get(PipelineRun, uuid.UUID(rid))
            return bool(run and run.baseline_eligible)

    async def workflow_id(self, rid: str) -> str:
        async with AsyncSessionLocal() as db:
            return (await db.get(PipelineRun, uuid.UUID(rid))).workflow_id

    async def cancel(self, run: dict) -> httpx.Response:
        return await self.api.post(
            f"/pipelines/{run['pipeline_id']}/runs/{run['id']}/cancel", json={}
        )


def ended(run: dict, reason: str) -> bool:
    return (run["status"], run["stopped_at_block_id"], run["stop_reason"]) == (
        "completed",
        "approve",
        reason,
    )


def price_change(run: dict) -> tuple | None:
    changes = (run.get("change_summary") or {}).get("extract", {}).get("changes") or []
    found = {c["path"]: (c["old"], c["new"]) for c in changes}
    return found.get("/price")


# --- setup -------------------------------------------------------------------------------------


async def destinations(g: ApprovalGate) -> None:
    print("setup — a bucket, two webhook destinations, an SMTP destination, a refusing webhook")
    resp = await g.create(
        {"kind": "bucket", "address": g.bucket_address(), "secret": g.bucket_secret("rw")}
    )
    g.dest["rw"] = resp.json()["id"]
    for name, path, signing in (
        ("hook", APPROVAL_HOOK, HOOK_SIGNING),
        ("notice", NOTICE_HOOK, None),
    ):
        secret = {"url": f"{HOOKS_TLS}{path}"} | ({"secret": signing} if signing else {})
        resp = await g.create({"kind": "webhook", "secret": secret})
        g.dest[name] = resp.json()["id"]
    ca = (await g.fixture.get(f"{CONTROL}/_ca")).text
    resp = await g.create(
        {
            "kind": "smtp",
            "address": {
                "host": "mail.deliveryfixture.com",
                "port": 587,
                "tls_mode": "starttls",
                "from": "alerts@deliveryfixture.com",
                "to": ["ops@deliveryfixture.com"],
                "ca_certificate": ca,
            },
            "secret": {"user": "gate-mailer", "password": f"{SECRET}-smtp"},
        }
    )
    g.dest["mail"] = resp.json().get("id")
    g.dest["private_hook"] = await g.insert(
        "webhook", {}, {"url": f"{PRIVATE}/hook", "secret": None}, "minio"
    )
    g.check(
        all(g.dest.get(k) for k in ("rw", "hook", "notice", "mail", "private_hook")),
        f"destinations saved ({sorted(g.dest)})",
    )


# --- quick scenarios ---------------------------------------------------------------------------


async def save_rules(g: ApprovalGate) -> None:
    print("R5/ADR-016 §9a — save-time rules: not first, not last, one, the deadline, the kind")

    async def refused(name: str, blocks: list[dict], *expect: str) -> None:
        resp = await g.api.post(
            "/pipelines",
            json={
                "name": f"gate-{name}",
                "definition": {"blocks": blocks, "inputs": {"url": {"type": "url"}}},
            },
        )
        g.check(
            resp.status_code in (400, 404, 422) and all(e in resp.text for e in expect),
            f"{name}: refused naming {expect} ({resp.status_code} {resp.text[:160]})",
        )

    first = {"id": "approve", "type": "approval", "config": {}}
    await refused("first", [first, {**g.fetch(), "input": "approve"}], "approve")
    await refused("last", [g.fetch(), g.approve("fetch")], "approve")
    await refused(
        "two",
        [g.fetch(), g.approve("fetch"), g.approve("approve", id="approve_2"), g.tail("approve_2")],
        "approve_2",
    )
    await refused(
        "deadline-0", [g.fetch(), g.approve("fetch", hours=0), g.tail("approve")], "approve"
    )
    limit = settings.approval_max_deadline_hours
    await refused(
        "deadline-over",
        [g.fetch(), g.approve("fetch", hours=limit + 1), g.tail("approve")],
        str(limit + 1),
        str(limit),
    )
    await refused(
        "bucket-destination",
        [g.fetch(), g.approve("fetch", g.dest["rw"]), g.tail("approve")],
        "approve",
    )
    theirs = await g.other.post(
        "/destinations",
        json={"name": "gate-theirs", "kind": "webhook", "secret": {"url": f"{HOOKS_TLS}/theirs"}},
    )
    await refused(
        "another-users-destination",
        [g.fetch(), g.approve("fetch", theirs.json()["id"]), g.tail("approve")],
    )
    pid = await g.pipeline(
        "longest", [g.fetch(), g.approve("fetch", hours=limit), g.tail("approve")]
    )
    g.check(bool(pid), f"a {limit} h deadline saves (the run ceiling does not include the wait)")


async def approve(g: ApprovalGate) -> None:
    print("R5 — bucket → Approval → webhook: waits, asks, holds no slot; approve → the tail runs")
    pid = await g.pipeline(
        "approve",
        [g.fetch(), g.store(g.dest["rw"]), g.approve("store", g.dest["hook"]), g.tail("approve")],
    )
    rid = await g.start(pid, {"url": f"{watched(uuid.uuid4().hex[:8])}"})
    t0 = datetime.now(UTC)
    run = await g.until(pid, rid, asked)
    block = approval_of(run)
    deadline = stamp(block["approval_deadline"]) if block.get("approval_deadline") else None
    g.check(
        run["status"] == "running"
        and (block.get("status"), block.get("status_reason")) == ("waiting", "awaiting_approval")
        and deadline is not None
        and abs((deadline - t0).total_seconds() - 24 * 3600) < 120,
        f"the run waits at approve, awaiting_approval, deadline 24 h out ({block.get('status')} {deadline})",
    )
    g.check(await g.holds_slot(rid) == 0, "the waiting run holds no concurrency slot")
    await g.quota(concurrent_jobs_limit=1)
    try:
        other = await g.quick_run()
        g.check(
            other["status"] == "completed",
            f"another run starts and ends at a limit of 1 ({other['status']})",
        )
    finally:
        await g.quota(concurrent_jobs_limit=None)

    listed = [a for a in (await g.api.get("/approvals")).json() if a["run_id"] == rid]
    g.check(
        len(listed) == 1
        and listed[0]["state"] == "waiting"
        and listed[0]["deliveries"] == [entry(run)],
        f"GET /approvals lists it with what was delivered ({listed[:1]})",
    )
    admin = (await g.admin.get("/admin/approvals", params={"user_id": str(g.user_id)})).json()
    seen = [a for a in admin if a["run_id"] == rid]
    g.check(
        len(seen) == 1 and seen[0]["user_id"] == str(g.user_id) and seen[0]["user_email"],
        "an admin sees it, with the owner",
    )
    posts = await g.messages(rid, 1)
    body = payload(posts[0]) if posts else {}
    g.check(
        len(posts) == 1
        and body.get("event") == "pipeline.approval_requested"
        and signed_with(posts[0], HOOK_SIGNING),
        "the request reaches the block's webhook destination, signed with its secret",
    )
    g.check(
        body.get("deliveries") == [entry(run)]
        and "removed only if this run later fails" in body.get("deliveries_note", "")
        and body.get("deadline") is not None
        and stamp(body["deadline"]) == deadline
        and body.get("run_url")
        == f"{settings.public_base_url}/app/dashboard/pipelines/{pid}/runs/{rid}",
        f"it names what was delivered (with the note), the deadline and the run's page ({body.get('run_url')})",
    )
    links = [v for k, v in body.items() if isinstance(v, str) and v.startswith("http")]
    g.check(links == [body.get("run_url")], "and carries no answer link")

    for who, client in (("another user", g.other), ("an admin", g.admin)):
        resp = await g.answer(run, "approved", client)
        g.check(resp.status_code == 404, f"{who} answering → 404 ({resp.status_code})")
    run = await g.get(pid, rid)
    g.check(approval_of(run).get("approval_answer") is None, "…and nothing is recorded")

    resp = await g.answer(run, "approved")
    g.check(
        resp.status_code == 200 and resp.json()["state"] == "approved",
        f"the owner approves ({resp.status_code})",
    )
    run = await g.wait(pid, rid)
    block, notify = approval_of(run), blocks_by_id(run)["notify"]
    g.check(
        run["status"] == "completed"
        and run["stopped_at_block_id"] is None
        and block["status"] == "completed"
        and (block["approval_answer"], block["answered_by"]) == ("approved", str(g.user_id))
        and block["answered_at"] is not None
        and notify["status"] == "completed",
        f"the run carries on and completes; approve records who and when ({run['status']})",
    )
    sent = [payload(p)["event"] for p in await g.tail_posts(rid, 1)]
    g.check(sent == ["pipeline.result"], f"the held-back webhook sends ({sent})")
    resp = await g.answer(run, "rejected")
    detail = resp.json().get("detail", {}) if resp.status_code == 409 else {}
    g.check(
        resp.status_code == 409 and detail.get("approval", {}).get("state") == "approved",
        f"a second answer → 409 with the state that stands ({resp.status_code} {detail.get('message')})",
    )
    g.check(
        approval_of(await g.get(pid, rid))["approval_answer"] == "approved",
        "…and changes nothing",
    )
    g.check(
        rid not in [a["run_id"] for a in (await g.api.get("/approvals")).json()],
        "it leaves GET /approvals",
    )
    rows = [(m["event"], m["status"]) for m in run["approval_messages"]]
    g.check(
        rows == [("pipeline.approval_requested", "delivered")] and run["alerts"] == [],
        f"the run lists the request, delivered, and no alert ({rows})",
    )


async def reject(g: ApprovalGate) -> None:
    print("R5/D9 — reject: completed, the tail skipped, the bucket file stays, no failure alert")
    pid = await g.pipeline(
        "reject",
        [g.fetch(), g.store(g.dest["rw"]), g.approve("store", g.dest["hook"]), g.tail("approve")],
    )
    rid = await g.start(pid, {"url": f"{watched(uuid.uuid4().hex[:8])}"})
    run = await g.until(pid, rid, asked)
    resp = await g.answer(run, "rejected")
    g.check(resp.status_code == 200, f"the owner rejects ({resp.status_code})")
    run = await g.wait(pid, rid)
    blocks = blocks_by_id(run)
    g.check(
        ended(run, "approval_rejected")
        and (blocks["notify"]["status"], blocks["notify"]["status_reason"])
        == ("skipped", "stopped"),
        f"completed, stopped at approve, approval_rejected, the tail skipped ({run['status']} {run['stop_reason']})",
    )
    key = location(run).get("key", "")
    g.check(
        key == f"gate/{pid}/{rid}/store.html"
        and await g.exists(key)
        and blocks["store"]["undo_status"] is None,
        f"the bucket file stays ({key})",
    )
    await asyncio.sleep(3)
    g.check(
        await g.tail_posts(rid) == [] and run["alerts"] == [],
        "nothing is sent and no failure alert is queued",
    )
    resp = await g.answer(run, "approved")
    g.check(
        resp.status_code == 409 and resp.json()["detail"]["approval"]["state"] == "rejected",
        f"approving after the reject → 409 ({resp.status_code})",
    )


async def email_and_cancel(g: ApprovalGate) -> None:
    print("R5 — a request by email; cancel while waiting ends the run at once")
    name = f"gate-mail-{uuid.uuid4().hex[:6]}"
    resp = await g.api.post(
        "/pipelines",
        json={
            "name": name,
            "definition": {
                "blocks": [g.fetch(), g.approve("fetch", g.dest["mail"]), g.tail("approve")],
                "inputs": {"url": {"type": "url"}},
            },
        },
    )
    pid = resp.json()["id"]
    rid = await g.start(pid, {"url": f"{watched(uuid.uuid4().hex[:8])}"})
    run = await g.until(pid, rid, asked)
    run = await g.until(
        pid, rid, lambda r: any(m["status"] == "delivered" for m in r["approval_messages"]), 40
    )
    rows = [(m["kind"], m["event"], m["status"]) for m in run["approval_messages"]]
    g.check(
        rows == [("email", "pipeline.approval_requested", "delivered")],
        f"the request is an email row, delivered ({rows})",
    )
    mail = (await g.fixture.get(f"{CONTROL}/_mail")).json()["messages"]
    mine = [
        m
        for m in mail
        if m["message_id"] == f"<{rid}.approve.approval_requested@{settings.public_host}>"
    ]
    g.check(
        len(mine) == 1
        and mine[0]["subject"] == f"[ScrapeFlow] {name}: pipeline.approval_requested"
        and f"/runs/{rid}" in mine[0]["body"],
        f"the email arrives with the event in its subject and the run's link ({len(mine)})",
    )
    t0 = time.monotonic()
    resp = await g.cancel(run)
    still = resp.json().get("still_running") if resp.status_code == 202 else None
    run = await g.wait(pid, rid, 30)
    took = time.monotonic() - t0
    block = approval_of(run)
    g.check(
        still == [] and run["status"] == "cancelled" and took < 10,
        f"cancel while waiting: nothing still running, cancelled after {took:.1f} s ({run['status']})",
    )
    g.check(
        (block["status"], block["error"]) == ("failed", "cancelled while waiting for approval"),
        f"the Approval block reads cancelled while waiting ({block['status']} {block['error']})",
    )
    resp = await g.answer(run, "approved")
    g.check(resp.status_code == 409, f"answering a cancelled run → 409 ({resp.status_code})")


async def rollback_after_approval(g: ApprovalGate) -> None:
    print("D9 — approve, then the next step fails: the bucket delivery before Approval is undone")
    pid = await g.pipeline(
        "rollback",
        [
            g.fetch(),
            g.store(g.dest["rw"]),
            g.approve("store", g.dest["hook"]),
            g.refusing(input="approve"),
        ],
    )
    rid = await g.start(pid, {"url": f"{watched(uuid.uuid4().hex[:8])}"})
    run = await g.until(pid, rid, asked)
    key = location(run).get("key", "")
    requested = await g.events(rid, 1)
    g.check(
        await g.exists(key) and requested == ["pipeline.approval_requested"],
        "delivered to the bucket, and the request has gone out",
    )
    await g.answer(run, "approved")
    run = await g.wait(pid, rid, 90)
    blocks = blocks_by_id(run)
    g.check(
        (run["status"], run["failed_block_id"]) == ("failed", "notify")
        and blocks["approve"]["approval_answer"] == "approved",
        f"the run fails at the webhook after the approval ({run['status']} {run['failed_block_id']})",
    )
    g.check(
        blocks["store"]["undo_status"] == "undone" and not await g.exists(key),
        f"the bucket file is undone — the request was not an announcement ({blocks['store']['undo_status']})",
    )


async def before_gate(g: ApprovalGate) -> None:
    print("ADR-016 §9e — an Approval before the change gate: SPA only, and the monitor waits on it")
    key = uuid.uuid4().hex[:8]
    pid = await g.pipeline(
        "before-gate",
        [g.fetch(), g.approve("fetch"), g.clean(input="approve"), g.gate_block(), g.tail("gate")],
    )
    monitor = await g.monitor(pid, key)
    run = await g.check_now(monitor)
    g.check(
        approval_of(run).get("status") == "waiting" and run["approval_messages"] == [],
        "the check waits; no destination, no message rows",
    )
    resp = await g.api.post(f"/monitors/{monitor['id']}/check")
    g.check(
        resp.status_code == 409 and resp.json()["detail"]["run_id"] == run["id"],
        f"Check now while it waits → 409 naming it ({resp.status_code})",
    )
    await g.cancel(run)
    run = await g.wait(pid, run["id"], 30)
    g.check(run["status"] == "cancelled", f"cancelled ({run['status']})")


async def monitor_thread(g: ApprovalGate) -> None:
    print(
        "R5/D10/R8 — a monitor's approvals: the tick closes, matching, supersede x3, reject, pause, delete"
    )
    key = uuid.uuid4().hex[:8]
    await g.set_page(key, price="80")
    pid = await g.pipeline("watch", g.watched_pipeline())
    monitor = await g.monitor(pid, key, notice=g.dest["notice"])
    llm = await g.llm_calls()

    c1 = await g.firing(monitor)
    if c1 is None:
        g.check(False, "the firing made a check")
        return
    g.check(
        c1["change_status"] == "first_check" and approval_of(c1).get("status") == "waiting",
        f"a real firing: the first check asks ({c1['change_status']})",
    )
    client = await connect()
    child = await client.get_workflow_handle(await g.workflow_id(c1["id"])).describe()
    tick = client.get_workflow_handle(child.parent_id, run_id=child.parent_run_id)
    deadline = time.monotonic() + 30
    while (desc := await tick.describe()).status == WorkflowExecutionStatus.RUNNING:
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(1)
    result = await tick.result() if desc.status == WorkflowExecutionStatus.COMPLETED else None
    g.check(
        result == "awaiting_approval" and child.status == WorkflowExecutionStatus.RUNNING,
        f"its tick closed on check_settled ({desc.status.name}, {result!r}); the check runs on",
    )
    c2 = await g.firing(monitor)
    g.check(
        c2 is not None
        and c2["change_status"] == "unchanged"
        and blocks_by_id(c2)["gate"]["verdict"].get("matched") == "waiting"
        and await g.llm_calls() - llm == 1,
        f"the next firing is admitted, matches the waiting content, no LLM call ({c2 and c2['change_status']})",
    )
    history = [i["event"]["kind"] for i in await g.history(monitor) if i["type"] == "event"]
    g.check("skipped_check_in_progress" not in history, f"no firing skipped ({history})")
    g.check(await g.waiting_for(monitor) == [c1["id"]], "c1 still waits, untouched")
    await g.answer(c1, "approved")
    c1 = await g.wait(pid, c1["id"])
    g.check(
        c1["status"] == "completed" and await g.eligible(c1["id"]),
        "c1 approved: completed, the baseline",
    )

    await g.set_page(key, price="70")
    c3 = await g.check_now(monitor)
    g.check(
        c3["change_status"] == "changed"
        and c3["baseline_run_id"] == c1["id"]
        and price_change(c3) == (80, 70),
        f"c3 (£70) changed against c1, waits ({price_change(c3)})",
    )
    request = [payload(p) for p in await g.messages(c3["id"], 1)]
    g.check(
        request
        and request[0]["event"] == "pipeline.approval_requested"
        and request[0]["monitor_id"] == monitor["id"]
        and request[0]["change_status"] == "changed"
        and request[0]["deliveries"] == [entry(c3)],
        "its request names the monitor, the change and the bucket file",
    )
    calls = await g.llm_calls()
    c4 = await g.check_now(monitor)
    g.check(
        c4["change_status"] == "unchanged"
        and blocks_by_id(c4)["gate"]["verdict"].get("matched") == "waiting"
        and await g.llm_calls() == calls
        and await g.waiting_for(monitor) == [c3["id"]]
        and await g.events(c3["id"]) == ["pipeline.approval_requested"],
        "c4 (£70 again): unchanged since c3, no LLM call, no new request; c3 untouched",
    )

    await g.set_page(key, price="65")
    c5 = await g.check_now(monitor)
    c3 = await g.wait(pid, c3["id"], 30)
    g.check(
        ended(c3, "approval_superseded")
        and approval_of(c3)["superseded_by_run_id"] == c5["id"]
        and not await g.eligible(c3["id"]),
        f"c5 (£65) supersedes c3: completed, approval_superseded, linked, not a baseline ({c3['stop_reason']})",
    )
    g.check(
        await g.events(c3["id"], 2)
        == ["pipeline.approval_requested", "pipeline.approval_superseded"],
        "c3's destination is told it was superseded",
    )
    resp = await g.api.get(f"/pipelines/{pid}/runs/{c3['id']}/result", params={"block": "extract"})
    g.check(
        resp.status_code == 200 and await g.exists(location(c3).get("key", "")),
        "c3's copies are kept and its bucket file stays",
    )
    g.check(
        c5["baseline_run_id"] == c1["id"]
        and price_change(c5) == (80, 65)
        and await g.waiting_for(monitor) == [c5["id"]],
        f"c5 compares with the baseline, not c3 ({price_change(c5)}), and is the one waiting",
    )

    await g.set_page(key, price="600")
    c6 = await g.check_now(monitor)
    c5 = await g.wait(pid, c5["id"], 30)
    g.check(
        (c6["status"], c6["stopped_at_block_id"], c6["stop_reason"])
        == ("completed", "cheap", "rule_not_met")
        and ended(c5, "approval_superseded")
        and approval_of(c5)["superseded_by_run_id"] == c6["id"]
        and await g.waiting_for(monitor) == [],
        f"c6 (£600) supersedes c5 and stops at a Validate; nothing waits ({c6['stop_reason']})",
    )

    await g.set_page(key, price="70")
    c7 = await g.check_now(monitor)
    await g.set_page(key, price="2000")
    c8 = await g.check_now(monitor)
    c7 = await g.wait(pid, c7["id"], 30)
    g.check(
        (c8["status"], c8["failed_block_id"]) == ("failed", "sane")
        and ended(c7, "approval_superseded")
        and approval_of(c7)["superseded_by_run_id"] == c8["id"],
        f"c8 (£2000) supersedes c7 and then fails ({c8['status']} {c8['failed_block_id']})",
    )
    await g.set_page(key, price="70")
    c9 = await g.check_now(monitor)
    g.check(
        c9["change_status"] == "changed"
        and c9["baseline_run_id"] == c6["id"]
        and price_change(c9) == (600, 70),
        f"c9 (£70) compares with c6: a superseded check is never the baseline ({c9['baseline_run_id']})",
    )

    resp = await g.answer(c9, "rejected")
    c9 = await g.wait(pid, c9["id"])
    await asyncio.sleep(2)
    g.check(
        ended(c9, "approval_rejected")
        and await g.eligible(c9["id"])
        and await g.tail_posts(c9["id"]) == []
        and await g.exists(location(c9).get("key", "")),
        "c9 rejected: completed, the baseline, nothing sent, its bucket file stays",
    )
    await g.set_page(key, price="60")
    c10 = await g.check_now(monitor)
    g.check(
        c10["baseline_run_id"] == c9["id"] and price_change(c10) == (70, 60),
        f"c10 (£60) diffs against the rejected c9 ({price_change(c10)})",
    )

    await g.api.post(f"/monitors/{monitor['id']}/pause")
    await asyncio.sleep(2)
    g.check(await g.waiting_for(monitor) == [c10["id"]], "pausing the monitor leaves c10 waiting")
    resp = await g.answer(c10, "approved")
    c10 = await g.wait(pid, c10["id"])
    g.check(
        resp.status_code == 200 and c10["status"] == "completed" and c10["stop_reason"] is None,
        f"…and it can be approved while paused ({c10['status']})",
    )
    await g.api.post(f"/monitors/{monitor['id']}/resume")

    await g.set_page(key, price="55")
    c11 = await g.check_now(monitor)
    g.check(approval_of(c11).get("status") == "waiting", "c11 (£55) waits")
    t0 = time.monotonic()
    resp = await g.api.delete(f"/monitors/{monitor['id']}")
    c11 = await g.wait(pid, c11["id"], 30)
    g.check(
        resp.status_code == 204 and c11["status"] == "cancelled" and time.monotonic() - t0 < 10,
        f"deleting the monitor cancels the waiting check ({c11['status']})",
    )

    g.check(
        await g.llm_calls() - llm == 9,
        f"9 LLM calls for 11 checks: none for the two matching the waiting one ({await g.llm_calls() - llm})",
    )
    expected = sorted(
        [(c["id"], "requested") for c in (c1, c3, c5, c7, c9, c10, c11)]
        + [(c["id"], "superseded") for c in (c3, c5, c7)]
    )
    until = time.monotonic() + 40
    while True:
        sent = sorted(
            (payload(p)["run_id"], payload(p)["event"].removeprefix("pipeline.approval_"))
            for p in await g.hook_posts(APPROVAL_HOOK)
            if payload(p).get("monitor_id") == monitor["id"]
        )
        if sent == expected or time.monotonic() > until:
            break
        await asyncio.sleep(1)
    g.check(
        sent == expected, f"the block's destination got 7 requests and 3 superseded ({len(sent)})"
    )
    g.check(
        await g.hook_posts(NOTICE_HOOK) == [],
        "the monitor's notice destination got no approval message",
    )


async def lost_signal(g: ApprovalGate) -> None:
    print("ADR-016 §9b — an answer whose signal is lost: the next firing or Check now wakes it")
    key = uuid.uuid4().hex[:8]
    await g.set_page(key, price="80")
    pid = await g.pipeline("lost", g.watched_pipeline())
    monitor = await g.monitor(pid, key)

    c1 = await g.check_now(monitor)
    g.check(await g.lose(c1), "c1 approved without a signal")
    await asyncio.sleep(3)
    still = await g.get(pid, c1["id"])
    g.check(
        approval_of(still)["status"] == "waiting"
        and approval_of(still)["approval_answer"] == "approved"
        and c1["id"] not in await g.waiting_for(monitor),
        "the run does not wake; the answer is recorded and it leaves GET /approvals",
    )
    before = {r["id"] for r in await g.checks(monitor)}
    client = await connect()
    await client.get_schedule_handle(monitor_schedules.schedule_id(monitor["id"])).trigger()
    c1 = await g.wait(pid, c1["id"], 30)
    history = [i["event"]["kind"] for i in await g.history(monitor) if i["type"] == "event"]
    g.check(
        c1["status"] == "completed"
        and c1["stop_reason"] is None
        and "skipped_check_in_progress" in history
        and {r["id"] for r in await g.checks(monitor)} == before,
        f"a firing is skipped as in progress and wakes it: completed ({c1['status']}, {history})",
    )

    await g.set_page(key, price="70")
    c2 = await g.check_now(monitor)
    g.check(await g.lose(c2), "c2 approved without a signal")
    t0 = time.monotonic()
    resp = await g.api.post(f"/monitors/{monitor['id']}/check")
    c2 = await g.wait(pid, c2["id"], 30)
    g.check(
        resp.status_code == 409
        and c2["status"] == "completed"
        and c2["stop_reason"] is None
        and time.monotonic() - t0 < 15,
        f"Check now → 409 and wakes it: completed in {time.monotonic() - t0:.1f} s ({resp.status_code})",
    )
    g.check(
        [await g.events(c["id"], 1) for c in (c1, c2)] == [["pipeline.approval_requested"]] * 2,
        "one request each, none asked again",
    )
    await g.api.delete(f"/monitors/{monitor['id']}")


# --- the scenarios that wait out real time -------------------------------------------------------


async def waits_start(g: ApprovalGate) -> dict:
    print(
        "waits (start) — a 1 h approval to expire, a 3 h one to remind, a 3 h one answered unheard"
    )
    s: dict = {}
    key = uuid.uuid4().hex[:8]
    await g.set_page(key, price="80")
    pid = await g.pipeline("expiry", g.watched_pipeline(hours=1))
    monitor = await g.monitor(pid, key, notice=g.dest["notice"])
    c0 = await g.check_now(monitor)
    await g.answer(c0, "approved")
    c0 = await g.wait(pid, c0["id"])
    await g.set_page(key, price="70")
    c1 = await g.check_now(monitor)
    calls = await g.llm_calls()
    c2 = await g.check_now(monitor)
    g.check(
        c0["status"] == "completed"
        and approval_of(c1).get("status") == "waiting"
        and c2["change_status"] == "unchanged"
        and await g.llm_calls() == calls,
        "expiry: c0 approved, c1 (£70) waits for 1 h, c2 matches it with no LLM call",
    )
    s["expiry"] = {"pid": pid, "monitor": monitor, "c0": c0, "c1": c1}

    for name in ("reminder", "unheard"):
        # unheard's tail is a Validate: a webhook's own need (its retry window) outlasts the wait.
        tail = (
            g.guard([{"kind": "contains", "text": "Kettle"}], input="approve")
            if name == "unheard"
            else g.tail("approve")
        )
        blocks = [g.fetch(), g.approve("fetch", g.dest["hook"], hours=3), tail]
        definition = {"blocks": blocks, "inputs": {"url": {"type": "url"}}}
        budget = None
        if name == "unheard":
            # The tightest ceiling the blocks allow: the wait must not count against it (§9d).
            resp = await g.api.post(
                "/pipelines",
                json={
                    "name": "gate-budget-probe",
                    "definition": definition | {"time_budget_seconds": 1},
                },
            )
            budget = int(resp.text.split("blocks need: ")[1].split(" s")[0])
        resp = await g.api.post(
            "/pipelines",
            json={
                "name": f"gate-{name}-{uuid.uuid4().hex[:6]}",
                "definition": definition | {"time_budget_seconds": budget},
            },
        )
        pid = resp.json()["id"]
        rid = await g.start(pid, {"url": watched(uuid.uuid4().hex[:8])})
        run = await g.until(pid, rid, asked)
        s[name] = {"pid": pid, "run": run, "budget": budget}
    unheard = s["unheard"]
    g.check(
        await g.lose(unheard["run"]),
        f"unheard: approved without a signal (budget {unheard['budget']} s)",
    )
    g.check(
        all(approval_of(s[n]["run"]).get("status") == "waiting" for n in ("reminder", "unheard")),
        "reminder, unheard: both wait, deadlines 3 h out",
    )
    return s


async def waits_finish(g: ApprovalGate, s: dict) -> None:
    print("waits (finish) — the 1 h deadline, then the reminder point of the 3 h ones")
    expiry = s["expiry"]
    pid, c1 = expiry["pid"], expiry["c1"]
    deadline = stamp(approval_of(c1)["approval_deadline"])
    lead = (deadline - datetime.now(UTC)).total_seconds() - 8
    print(f"  waiting {lead / 60:.0f} min for c1's deadline ({deadline:%H:%M:%S} UTC)")
    await asyncio.sleep(max(lead, 0))

    # An answer arriving before the deadline but held on the run lock until after it.
    async with AsyncSessionLocal() as db:
        await db.execute(
            select(PipelineRun).where(PipelineRun.id == uuid.UUID(c1["id"])).with_for_update()
        )
        racing = asyncio.create_task(g.answer(c1, "approved"))
        await asyncio.sleep(12)
        await db.rollback()
    resp = await racing
    g.check(
        resp.status_code == 409,
        f"an answer sent 8 s before the deadline, held on the run lock past it → 409 ({resp.status_code})",
    )
    c1 = await g.wait(pid, c1["id"], 60)
    g.check(
        ended(c1, "approval_expired")
        and approval_of(c1)["approval_answer"] == "expired"
        and approval_of(c1)["answered_at"] is None
        and not await g.eligible(c1["id"]),
        f"c1 expired at its deadline: completed, approval_expired, not a baseline ({c1['stop_reason']})",
    )
    g.check(
        await g.events(c1["id"], 2) == ["pipeline.approval_requested", "pipeline.approval_expired"],
        "its destination is told: requested, expired (a 1 h deadline has no reminder)",
    )
    resp = await g.answer(c1, "approved")
    g.check(
        resp.status_code == 409 and resp.json()["detail"]["approval"]["state"] == "expired",
        f"answering after the deadline → 409, expired ({resp.status_code})",
    )
    c3 = await g.check_now(expiry["monitor"])
    g.check(
        c3["change_status"] == "changed"
        and c3["baseline_run_id"] == expiry["c0"]["id"]
        and approval_of(c3).get("status") == "waiting",
        f"the next check compares with c0 and asks again ({c3['change_status']})",
    )
    history = [
        i["event"]["kind"] for i in await g.history(expiry["monitor"]) if i["type"] == "event"
    ]
    g.check(
        history == [] and await g.hook_posts(NOTICE_HOOK) == [],
        f"no monitor notice for an expiry (ADR-016 §8) ({history})",
    )

    remind_at = max(
        stamp(approval_of(s[n]["run"])["approval_deadline"]) for n in ("reminder", "unheard")
    )
    lead = (remind_at - datetime.now(UTC)).total_seconds() - 2 * 3600 + 5
    print(f"  waiting {max(lead, 0):.0f} s for the 3 h approvals' reminder point")
    await asyncio.sleep(max(lead, 0))

    reminder = s["reminder"]
    run = reminder["run"]
    sent = await g.events(run["id"], 2)
    now = await g.get(reminder["pid"], run["id"])
    g.check(
        sent == ["pipeline.approval_requested", "pipeline.approval_expiring"]
        and approval_of(now)["status"] == "waiting",
        f"reminder: at deadline - 2 h the destination gets approval_expiring; it still waits ({sent})",
    )
    await g.answer(run, "approved")
    run = await g.wait(reminder["pid"], run["id"])
    g.check(run["status"] == "completed", f"…and is approved after it ({run['status']})")

    unheard = s["unheard"]
    run = await g.wait(unheard["pid"], unheard["run"]["id"], 60)
    waited = (stamp(run["finished_at"]) - stamp(run["created_at"])).total_seconds()
    g.check(
        run["status"] == "completed"
        and run["stop_reason"] is None
        and blocks_by_id(run)["guard"]["status"] == "completed"
        and waited > unheard["budget"],
        f"unheard: the answer is found at the reminder; the step after it runs after {waited:.0f} s on a {unheard['budget']} s budget (§9d)",
    )
    g.check(
        await g.events(run["id"], 1) == ["pipeline.approval_requested"],
        "no reminder is sent for an answer already recorded",
    )


QUICK = {
    "save_rules": save_rules,
    "approve": approve,
    "reject": reject,
    "email_and_cancel": email_and_cancel,
    "rollback_after_approval": rollback_after_approval,
    "before_gate": before_gate,
    "monitor_thread": monitor_thread,
    "lost_signal": lost_signal,
}


async def _cancel_waiting(api: httpx.AsyncClient, user_id: uuid.UUID) -> None:
    """Every run of the gate user still running is cancelled, so no workflow outlives the gate."""
    async with AsyncSessionLocal() as db:
        runs = (
            await db.execute(
                select(PipelineRun.pipeline_id, PipelineRun.id).where(
                    PipelineRun.user_id == user_id, PipelineRun.status == "running"
                )
            )
        ).all()
    for pid, rid in runs:
        await api.post(f"/pipelines/{pid}/runs/{rid}/cancel", json={})
    deadline = time.monotonic() + 30
    while runs and time.monotonic() < deadline:
        async with AsyncSessionLocal() as db:
            left = await db.scalar(
                select(PipelineRun.id).where(
                    PipelineRun.user_id == user_id, PipelineRun.status == "running"
                )
            )
        if left is None:
            return
        await asyncio.sleep(1)


async def gate(only: list[str], waits: bool) -> list[str]:
    if not SECRET:
        raise SystemExit("GATE_SECRET is not set — run scripts/approval-gate.sh")
    user, raw = await _user("approval_gate")
    other_user, other_raw = await _user("approval_gate_other")
    admin_user, admin_raw = await _user("approval_gate_admin", admin=True)
    print(f"gate user {user.id}, other {other_user.id}, admin {admin_user.id}")
    minio = await minio_client.create_client()
    try:
        async with (
            httpx.AsyncClient(base_url=API, headers={"X-API-Key": raw}, timeout=60) as api,
            httpx.AsyncClient(base_url=API, headers={"X-API-Key": admin_raw}, timeout=60) as admin,
            httpx.AsyncClient(base_url=API, headers={"X-API-Key": other_raw}, timeout=60) as other,
            httpx.AsyncClient(timeout=30) as fixture,
        ):
            try:
                key = await api.post(
                    "/users/llm-keys",
                    json={
                        "name": "fixture",
                        "provider": "openai_compatible",
                        "api_key": "fixture-key",
                        "base_url": f"{LLM}/v1",
                    },
                )
                g = ApprovalGate(api, admin, other, fixture, key.json()["id"], user.id, minio)
                await destinations(g)
                started = await waits_start(g) if waits else None
                for name, scenario in QUICK.items():
                    if not only or name in only:
                        await scenario(g)
                if started is not None:
                    await waits_finish(g, started)
                return g.problems
            finally:
                await _cancel_waiting(api, user.id)
    finally:
        for u in (user, other_user, admin_user):
            await _remove(u.id, minio)
        await minio_client.close_client(minio)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-waits", action="store_true", help="skip the ≈ 62 min scenarios")
    parser.add_argument("only", nargs="*", help="quick scenario names; all when omitted")
    args = parser.parse_args()
    problems = asyncio.run(gate(args.only, not args.no_waits))
    for problem in problems:
        print(f"MISMATCH  {problem}")
    print("GATE PASSED" if not problems else f"GATE FAILED ({len(problems)})")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
