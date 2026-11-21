"""M.12 — PRD-017 slice 1 and PRD-019's bucket criterion on compose, through the real path: the
destinations API and its test, PipelineWorkflow with sinks and rollback, the delivery worker against a
real S3 server (MinIO behind the gate fixture's TLS front), WebhookDeliveryWorkflow, monitor notices to
a webhook and an SMTP destination, and a known secret looked for in every API response and every
workflow history the gate made.

Run it with ``scripts/delivery-gate.sh`` (repo root): the driver brings the fixtures up, gives the
workers the gate CA, makes the MinIO users with a fresh secret (``GATE_SECRET``), runs this, then looks
for the secret in the containers' logs and runs the egress copies' rebinding test. Runs as a throwaway
user (an admin user beside it for the admin reads and the admin delete); its rows and stored objects
are removed afterwards. Exit status 1 on any mismatch.

Not covered here (the backlog's M.12 note says where): a webhook that gives up after its five tries
(≈2.9 h; here a webhook "refuses" by resolving to a private address, which is final), a real third-party
S3 service, the SPA, the NetworkPolicy (production's release gate).
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, func, select, update

from app.auth.api_key import generate_api_key, hash_api_key
from app.core import minio as minio_client
from app.core import monitor_schedules
from app.core.db import AsyncSessionLocal
from app.core.destinations import encrypt_secret
from app.core.ledger import release_user_objects
from app.models.api_key import ApiKey
from app.models.destination import Destination
from app.models.monitor import Monitor
from app.models.pipeline import PipelineRunBlock
from app.models.quota_views import quota_active_submissions, quota_run_units
from app.models.storage_object import StorageObject
from app.models.user import User
from app.models.user_quota import UserQuota
from app.settings import settings
from app.workflows.client import connect
from scripts.pipeline_gate import API, HOOKS, LLM, TERMINAL, Gate, blocks_by_id, item

SECRET = os.environ.get("GATE_SECRET", "")
STARTED = os.environ.get("GATE_STARTED", "")
S3 = "s3.deliveryfixture.com"
HOOKS_TLS = "https://hooks.deliveryfixture.com"
CONTROL = "http://mail.deliveryfixture.com"
BUCKET, OTHER_BUCKET = "delivery-gate", "delivery-gate-2"
PRIVATE = "https://minio:9000"
INLINE_SECRET = "inline-gate-secret"
RESPONSES: list[str] = []


async def _record(response: httpx.Response) -> None:
    await response.aread()
    RESPONSES.append(response.text)


def signed_with(post: dict, key: str) -> bool:
    mac = hmac.new(key.encode(), post["body"].encode(), hashlib.sha256).hexdigest()
    return post["headers"].get("x-scrapeflow-signature") == f"sha256={mac}"


class DeliveryGate(Gate):
    def __init__(self, api, admin, fixture, key_id, user_id, minio):
        super().__init__(api, fixture, key_id)
        self.admin, self.user_id, self.minio = admin, user_id, minio
        self.dest: dict[str, str] = {}
        self.kept: tuple[str, str] | None = None
        self.sessions_before = 0

    # --- destinations ------------------------------------------------------------------------

    @staticmethod
    def bucket_address(prefix="gate", bucket=BUCKET) -> dict:
        return {"endpoint": f"https://{S3}/", "bucket": bucket, "prefix": prefix}

    @staticmethod
    def bucket_secret(user: str) -> dict:
        return {"access_key": f"gate-{user}", "secret_key": f"{SECRET}-{user}"}

    async def create(self, body: dict) -> httpx.Response:
        return await self.api.post(
            "/destinations", json={"name": f"gate-{uuid.uuid4().hex[:6]}", **body}
        )

    async def insert(self, kind: str, address: dict, secret: dict, host: str) -> str:
        """A destination the API would refuse (a private address): written straight to the table."""
        async with AsyncSessionLocal() as db:
            row = Destination(
                user_id=self.user_id,
                name=f"gate-private-{uuid.uuid4().hex[:6]}",
                kind=kind,
                address=address,
                encrypted_secret=encrypt_secret(secret),
                display_host=host,
            )
            db.add(row)
            await db.commit()
            return str(row.id)

    # --- blocks ------------------------------------------------------------------------------

    @staticmethod
    def store(destination: str, id="store", input="fetch") -> dict:
        return {
            "id": id,
            "type": "bucket",
            "input": input,
            "config": {"destination_id": destination},
        }

    @staticmethod
    def hook(
        id="notify", input="store", destination=None, url=None, secret=None, events=None
    ) -> dict:
        config: dict = {"destination_id": destination} if destination else {"url": url}
        if secret:
            config["secret"] = secret
        if events is not None:
            config["events"] = events
        return {"id": id, "type": "webhook", "input": input, "config": config}

    def refusing(self, id="notify", input="store") -> dict:
        return self.hook(id=id, input=input, destination=self.dest["private_hook"])

    @staticmethod
    def alert(input: str) -> dict:
        return {
            "id": "alert",
            "type": "webhook",
            "input": input,
            "config": {"url": f"{HOOKS}/ok", "events": ["pipeline.failed"]},
        }

    # --- reads -------------------------------------------------------------------------------

    async def get(self, pid: str, rid: str) -> dict:
        return (await self.api.get(f"/pipelines/{pid}/runs/{rid}")).json()

    async def until(self, pid: str, rid: str, done, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            run = await self.get(pid, rid)
            if done(run) or time.monotonic() > deadline:
                return run
            await asyncio.sleep(0.5)

    async def start(self, pid: str, inputs: dict) -> str:
        resp = await self.trigger(pid, inputs)
        if resp.status_code != 202:
            raise RuntimeError(f"run {pid}: {resp.status_code} {resp.text}")
        return resp.json()["id"]

    async def exists(self, key: str, bucket=BUCKET) -> bool:
        try:
            await self.minio.stat_object(bucket, key)
            return True
        except Exception:
            return False

    async def objects(self, prefix: str, bucket=BUCKET) -> list[str]:
        return [
            o.object_name
            async for o in self.minio.list_objects(bucket, prefix=prefix, recursive=True)
        ]

    async def read(self, key: str) -> bytes:
        response = await self.minio.get_object(BUCKET, key)
        try:
            return await response.read()
        finally:
            response.close()
            await response.release()

    async def s3_log(self, key: str) -> list[tuple[str, str]]:
        log = (await self.fixture.get(f"{CONTROL}/_s3")).json()
        return [(e["method"], e["outcome"]) for e in log if e["path"] == f"/{BUCKET}/{key}"]

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

    async def ledger_bytes(self, rid: str) -> int:
        async with AsyncSessionLocal() as db:
            return await db.scalar(
                select(func.coalesce(func.sum(StorageObject.bytes), 0))
                .join(PipelineRunBlock, PipelineRunBlock.id == StorageObject.pipeline_run_block_id)
                .where(PipelineRunBlock.pipeline_run_id == uuid.UUID(rid))
            )

    async def holds_slot(self, rid: str) -> int:
        async with AsyncSessionLocal() as db:
            return await db.scalar(
                select(func.count())
                .select_from(quota_active_submissions)
                .where(quota_active_submissions.c.submission_id == uuid.UUID(rid))
            )

    async def quota(self, **values) -> None:
        async with AsyncSessionLocal() as db:
            if await db.get(UserQuota, self.user_id) is None:
                db.add(UserQuota(user_id=self.user_id))
                await db.flush()
            await db.execute(
                update(UserQuota).where(UserQuota.user_id == self.user_id).values(**values)
            )
            await db.commit()

    async def hook_posts(self, path: str) -> list[dict]:
        log = (await self.fixture.get(f"{HOOKS}/_log")).json()
        return [p for p in log if p["path"] == path]

    async def quick_run(self) -> dict:
        """Another run of the same user, while the first waits."""
        pid = self.dest.setdefault("quick", await self.pipeline("quick", [self.fetch()]))
        resp = await self.trigger(pid, {"url": item("10")})
        if resp.status_code != 202:
            return {"status": f"refused {resp.status_code}: {resp.text}"}
        return await self.wait(pid, resp.json()["id"], 60)


def entry(run: dict, block_id="store") -> dict:
    return {
        "block_id": block_id,
        "kind": "bucket",
        "location": blocks_by_id(run)[block_id]["delivery"]["location"],
    }


def location(run: dict, block_id="store") -> dict:
    return ((blocks_by_id(run)[block_id].get("delivery") or {}).get("location")) or {}


def undoing(block_id="store"):
    return lambda r: (
        r["status"] in TERMINAL or blocks_by_id(r)[block_id].get("undo_status") == "undoing"
    )


# --- scenarios ---------------------------------------------------------------------------------


async def destinations(g: DeliveryGate) -> None:
    print("R5/R8 — destinations through the API: the test before save, host only, refusals")
    form = {"kind": "bucket", "address": g.bucket_address(), "secret": g.bucket_secret("rw")}
    resp = await g.api.post("/destinations/test", json=form)
    checks = resp.json().get("checks", []) if resp.status_code == 200 else []
    g.check(
        resp.status_code == 200
        and resp.json()["passed"]
        and [(c["check"], c["outcome"]) for c in checks]
        == [("address", "passed"), ("tls", "passed"), ("write", "passed"), ("delete", "passed")],
        f"a bucket's form test passes address, tls, write, delete against a real S3 ({resp.status_code} {checks})",
    )
    resp = await g.create(form)
    g.check(
        resp.status_code == 201 and resp.json()["test"]["passed"],
        f"saved after its test ({resp.status_code})",
    )
    g.dest["rw"] = resp.json()["id"]
    g.check(await g.objects("gate/.scrapeflow-test") == [], "the test left nothing in the bucket")

    form = {"kind": "bucket", "address": g.bucket_address(), "secret": g.bucket_secret("putonly")}
    resp = await g.api.post("/destinations/test", json=form)
    warned = [c for c in resp.json().get("checks", []) if c["outcome"] == "warning"]
    g.check(
        resp.status_code == 200
        and resp.json()["passed"]
        and [c["check"] for c in warned] == ["delete"]
        and warned[0]["message"].startswith("delete refused — rollback to this bucket will fail"),
        f"a key that cannot delete: the test warns before save ({warned})",
    )
    resp = await g.create(form)
    g.check(resp.status_code == 201, f"…and it saves with the warning ({resp.status_code})")
    g.dest["putonly"] = resp.json()["id"]

    resp = await g.create(
        {"kind": "bucket", "address": g.bucket_address("moving"), "secret": g.bucket_secret("rw")}
    )
    g.dest["moving"] = resp.json()["id"]

    resp = await g.create(
        {"kind": "webhook", "secret": {"url": f"{HOOKS_TLS}/{SECRET}", "secret": f"{SECRET}-sign"}}
    )
    g.check(
        resp.status_code == 201 and resp.json()["test"]["passed"],
        f"a webhook destination passes its test ({resp.status_code})",
    )
    g.dest["hook"] = resp.json()["id"]
    own = (await g.api.get(f"/destinations/{g.dest['hook']}")).json()
    admin = (await g.admin.get(f"/admin/destinations/{g.dest['hook']}")).json()
    g.check(
        own.get("display_host") == admin.get("display_host") == "hooks.deliveryfixture.com"
        and "url" not in json.dumps(own)
        and "url" not in json.dumps(admin),
        "a webhook destination is shown by its host only, to the owner and to an admin",
    )

    ca = (await g.fixture.get(f"{CONTROL}/_ca")).text
    g.sessions_before = len((await g.fixture.get(f"{CONTROL}/_mail")).json()["sessions"])
    mail = {
        "kind": "smtp",
        "address": {
            "host": "mail.deliveryfixture.com",
            "port": 587,
            "tls_mode": "starttls",
            "from": "alerts@deliveryfixture.com",
            "to": ["ops@deliveryfixture.com"],
            "ca_certificate": ca,
        },
    }
    resp = await g.create({**mail, "secret": {"user": "gate-mailer", "password": "wrong-password"}})
    failed = [
        c["check"]
        for c in resp.json().get("detail", {}).get("checks", [])
        if c["outcome"] == "failed"
    ]
    g.check(
        resp.status_code == 422 and failed == ["login"],
        f"a wrong SMTP password is refused at save ({resp.status_code} {failed})",
    )
    resp = await g.create({**mail, "secret": {"user": "gate-mailer", "password": f"{SECRET}-smtp"}})
    checks = [(c["check"], c["outcome"]) for c in resp.json().get("test", {}).get("checks", [])]
    g.check(
        resp.status_code == 201
        and checks == [("address", "passed"), ("tls", "passed"), ("login", "passed")],
        f"an SMTP destination passes address, tls, login ({resp.status_code} {checks})",
    )
    g.dest["mail"] = resp.json().get("id")

    print("R5 — a private address is refused at save")
    for body, what in [
        (
            {
                "kind": "bucket",
                "address": {**g.bucket_address(), "endpoint": f"{PRIVATE}/"},
                "secret": g.bucket_secret("rw"),
            },
            "bucket endpoint",
        ),
        ({"kind": "webhook", "secret": {"url": f"{PRIVATE}/hook"}}, "webhook destination URL"),
    ]:
        resp = await g.create(body)
        g.check(
            resp.status_code == 400 and "private address" in resp.text,
            f"a private {what} ({resp.status_code})",
        )
    resp = await g.api.post(
        "/pipelines",
        json={
            "name": "gate-private",
            "definition": {
                "blocks": [g.fetch(), g.hook(input="fetch", url=f"{PRIVATE}/hook")],
                "inputs": {"url": {"type": "url"}},
            },
        },
    )
    g.check(
        resp.status_code == 400 and "private" in resp.text,
        f"a private inline webhook URL ({resp.status_code})",
    )
    resp = await g.create(
        {"kind": "webhook", "secret": {"url": f"http://hooks.deliveryfixture.com/{SECRET}"}}
    )
    g.check(
        resp.status_code == 422 and SECRET not in resp.text,
        "a refused form is answered without its values",
    )

    g.dest["private_hook"] = await g.insert(
        "webhook", {}, {"url": f"{PRIVATE}/hook", "secret": None}, "minio"
    )
    g.dest["private_bucket"] = await g.insert(
        "bucket",
        {**g.bucket_address(), "endpoint": f"{PRIVATE}/", "region": None},
        g.bucket_secret("rw"),
        "minio",
    )


async def delivered(g: DeliveryGate) -> None:
    print("R1/R4/R7 — scrape → extract → bucket → webhook: delivered, signed, not metered")
    pid = await g.pipeline(
        "delivered",
        [
            g.fetch(),
            g.extract(),
            g.store(g.dest["rw"], input="extract"),
            g.hook(destination=g.dest["hook"]),
            g.hook(id="twin", input="notify", url=f"{HOOKS_TLS}/inline", secret=INLINE_SECRET),
        ],
    )
    storage, units = await g.storage(), await g.units()
    run = await g.run(pid, {"url": item("80")})
    rid, blocks, loc = run["id"], blocks_by_id(run), location(run)
    key = f"gate/{pid}/{rid}/store.json"
    g.check(run["status"] == "completed", f"the run completes ({run['status']} {run.get('error')})")
    g.check(
        loc == {"endpoint": f"https://{S3}/", "bucket": BUCKET, "region": None, "key": key}
        and blocks["notify"]["status"] == "completed",
        f"the run reports the object at {loc.get('key')} and the webhook delivered",
    )
    extraction = (
        await g.api.get(f"/pipelines/{pid}/runs/{rid}/result", params={"block": "extract"})
    ).json()
    stored = json.loads(await g.read(key)) if await g.exists(key) else None
    g.check(
        stored is not None and stored == json.loads(extraction["content"]),
        "the object holds the extraction",
    )
    posts = await g.posts_after(rid, 2)
    dest = [p for p in posts if p["path"] == f"/{SECRET}"]
    twin = [p for p in posts if p["path"] == "/inline"]
    g.check(
        len(dest) == 1 and signed_with(dest[0], f"{SECRET}-sign"),
        "the destination webhook arrives, signed with its secret",
    )
    if dest and twin:
        a, b = json.loads(dest[0]["body"]), json.loads(twin[0]["body"])
        g.check(
            a.get("block_id") == "notify" and a.get("deliveries") == [entry(run)],
            f"the payload names its block and the bucket's key ({a.get('block_id')}, {a.get('deliveries')})",
        )
        # Each block stamps its own send time.
        differ = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
        g.check(
            differ == ["block_id", "timestamp"] and signed_with(twin[0], INLINE_SECRET),
            f"a destination webhook sends the same payload as an inline one (differs in {differ})",
        )
    used = await g.ledger_bytes(rid)
    g.check(
        await g.storage() - storage == used and await g.units() - units == 1,
        f"the bucket's bytes are not charged (usage moved by {await g.storage() - storage}, ScrapeFlow's copies {used}) and one monthly unit",
    )
    g.kept = (pid, key)

    resp = await g.api.patch(
        f"/destinations/{g.dest['hook']}", json={"secret": {"secret": f"{SECRET}-sign2"}}
    )
    g.check(
        resp.status_code == 200 and resp.json()["test"]["passed"],
        f"the signing secret is rotated ({resp.status_code})",
    )
    run = await g.run(pid, {"url": item("80")})
    dest = [p for p in await g.posts_after(run["id"], 2) if p["path"] == f"/{SECRET}"]
    g.check(
        len(dest) == 1 and signed_with(dest[0], f"{SECRET}-sign2"),
        "the next delivery is signed with the new secret",
    )


async def undone(g: DeliveryGate) -> None:
    print("R3 — the webhook refuses: undoing, slots free, the object deleted, the extraction kept")
    pid = await g.pipeline(
        "undone",
        [
            g.fetch(),
            g.extract(),
            g.store(g.dest["rw"], id="store_waitundo", input="extract"),
            g.refusing(input="store_waitundo"),
            g.alert("notify"),
        ],
    )
    await g.quota(concurrent_jobs_limit=1)
    try:
        rid = await g.start(pid, {"url": item("80")})
        run = await g.until(pid, rid, undoing("store_waitundo"), 60)
        g.check(
            run["status"] == "running" and run.get("rollback_deadline") is not None,
            f"while the undo waits the run is undoing, with an upper bound ({run['status']}, by {run.get('rollback_deadline')})",
        )
        g.check(await g.holds_slot(rid) == 0, "…and holds no concurrency slot")
        other = await g.quick_run()
        g.check(
            other["status"] == "completed",
            f"another run starts and ends meanwhile at a limit of 1 ({other['status']})",
        )
    finally:
        await g.quota(concurrent_jobs_limit=None)
    run = await g.wait(pid, rid, 90)
    blocks = blocks_by_id(run)
    key = location(run, "store_waitundo").get("key", "")
    g.check(
        (run["status"], run["failed_block_id"]) == ("failed", "notify"),
        f"the run fails naming the webhook ({run['status']}, {run['failed_block_id']}: {run.get('error')})",
    )
    g.check(
        (blocks["store_waitundo"]["status"], blocks["store_waitundo"]["undo_status"])
        == ("completed", "undone")
        and blocks["notify"]["status"] == "failed"
        and "ssrf_blocked" in (blocks["notify"]["error"] or ""),
        f"the bucket reads delivered then undone, the webhook failed at once ({blocks['notify']['error']})",
    )
    g.check(key != "" and not await g.exists(key), "the object is gone from the bucket")
    g.check(
        [m for m, _ in await g.s3_log(key) if m == "DELETE"] == ["DELETE", "DELETE"],
        "the undo was tried twice",
    )
    posts = [p for p in await g.posts_after(rid, 1) if p["path"] == "/ok"]
    rollback = json.loads(posts[0]["body"]).get("rollback") if posts else None
    g.check(
        rollback == {"undone": [entry(run, "store_waitundo")], "left_in_place": [], "kept": []},
        f"the alert carries what rollback did ({rollback})",
    )
    resp = await g.api.get(f"/pipelines/{pid}/runs/{rid}/result", params={"block": "extract"})
    g.check(
        resp.status_code == 200 and "price" in resp.json()["content"],
        "the extraction is still retrievable (D12)",
    )


async def undo_refused(g: DeliveryGate) -> None:
    print("R3 — a key that cannot delete: undo failed, left in place at the exact location")
    pid = await g.pipeline("refused", [g.fetch(), g.store(g.dest["putonly"]), g.refusing()])
    run = await g.run(pid, {"url": item("80")})
    store, loc = blocks_by_id(run)["store"], location(run)
    g.check(run["status"] == "failed", f"the run fails ({run['status']})")
    g.check(
        store["undo_status"] == "undo_failed"
        and loc.get("bucket") == BUCKET
        and loc.get("key") == f"gate/{pid}/{run['id']}/store.html"
        and "DestinationRefused" in (store["undo_error"] or ""),
        f"undo failed: left in place at {loc.get('bucket')}/{loc.get('key')} ({store['undo_error']})",
    )
    g.check(await g.exists(loc.get("key", "")), "the object is still there")


async def lost_reply(g: DeliveryGate) -> None:
    print("R2 — a write retried after a lost reply makes one object")
    pid = await g.pipeline("lost", [g.fetch(), g.store(g.dest["rw"], id="store_lostreply")])
    run = await g.run(pid, {"url": item("80")})
    key = location(run, "store_lostreply").get("key", "")
    g.check(run["status"] == "completed", f"the run completes ({run['status']} {run.get('error')})")
    g.check(
        await g.s3_log(key) == [("PUT", "reply dropped"), ("PUT", "passed")]
        and await g.objects(f"gate/{pid}/{run['id']}/") == [key],
        "two PUTs reached the bucket, one object is there",
    )


async def waiting_put(g: DeliveryGate) -> None:
    print("R6 — a bucket sink waiting on a down endpoint holds no slot")
    pid = await g.pipeline("waiting", [g.fetch(), g.store(g.dest["rw"], id="store_waitput")])
    await g.quota(concurrent_jobs_limit=1)
    try:
        rid = await g.start(pid, {"url": item("80")})
        run = await g.until(
            pid,
            rid,
            lambda r: blocks_by_id(r)["store_waitput"]["status"] in ("waiting", *TERMINAL),
            40,
        )
        store = blocks_by_id(run)["store_waitput"]
        g.check(
            store["status"] == "waiting", f"the sink waits ({store['status']}: {store['error']})"
        )
        g.check(await g.holds_slot(rid) == 0, "…holding no concurrency slot")
        other = await g.quick_run()
        g.check(
            other["status"] == "completed",
            f"another run starts and ends meanwhile at a limit of 1 ({other['status']})",
        )
    finally:
        await g.quota(concurrent_jobs_limit=None)
    run = await g.wait(pid, rid, 90)
    g.check(
        run["status"] == "completed"
        and await g.exists(location(run, "store_waitput").get("key", "")),
        "the next try delivers",
    )


async def gone_before_undo(g: DeliveryGate) -> None:
    print("R3 — an object the user already deleted reads undone")
    pid = await g.pipeline(
        "gone",
        [g.fetch(), g.store(g.dest["rw"], id="store_waitundo"), g.refusing(input="store_waitundo")],
    )
    rid = await g.start(pid, {"url": item("80")})
    run = await g.until(pid, rid, undoing("store_waitundo"), 60)
    key = location(run, "store_waitundo").get("key", "")
    await g.minio.remove_object(BUCKET, key)
    run = await g.wait(pid, rid, 90)
    g.check(
        blocks_by_id(run)["store_waitundo"]["undo_status"] == "undone",
        f"delivered, then undone ({blocks_by_id(run)['store_waitundo']['undo_status']})",
    )


async def moved_bucket(g: DeliveryGate) -> None:
    print("R3/F4 — the destination's bucket changed mid-run: the undo targets where it landed")
    pid = await g.pipeline(
        "moved",
        [
            g.fetch(),
            g.store(g.dest["moving"], id="store_waitundo"),
            g.refusing(input="store_waitundo"),
        ],
    )
    rid = await g.start(pid, {"url": item("80")})
    run = await g.until(pid, rid, undoing("store_waitundo"), 60)
    resp = await g.api.patch(
        f"/destinations/{g.dest['moving']}",
        json={"address": {"bucket": OTHER_BUCKET}, "secret": g.bucket_secret("other")},
    )
    g.check(
        resp.status_code == 200 and resp.json()["test"]["passed"],
        f"the destination moves to another bucket ({resp.status_code})",
    )
    run = await g.wait(pid, rid, 90)
    store, loc = blocks_by_id(run)["store_waitundo"], location(run, "store_waitundo")
    g.check(
        store["undo_status"] == "undo_failed"
        and loc.get("bucket") == BUCKET
        and await g.exists(loc.get("key", "")),
        f"undo failed, left in place at the original {loc.get('bucket')}/{loc.get('key')} ({store['undo_error']})",
    )


async def ordering(g: DeliveryGate) -> None:
    print("R2 — bucket → webhook → bucket is refused; bucket → bucket → webhook → webhook runs")
    resp = await g.api.post(
        "/pipelines",
        json={
            "name": "gate-order",
            "definition": {
                "inputs": {"url": {"type": "url"}},
                "blocks": [
                    g.fetch(),
                    g.store(g.dest["rw"]),
                    g.hook(url=f"{HOOKS}/ok"),
                    g.store(g.dest["rw"], id="store_2", input="notify"),
                ],
            },
        },
    )
    g.check(
        resp.status_code == 422 and "'store_2' cannot follow webhook 'notify'" in resp.text,
        f"refused at save naming both blocks ({resp.status_code} {resp.text[:200]})",
    )
    pid = await g.pipeline(
        "order",
        [
            g.fetch(),
            g.store(g.dest["rw"]),
            g.store(g.dest["rw"], id="store_2", input="store"),
            g.hook(input="store_2", url=f"{HOOKS}/ok"),
            g.hook(id="notify_2", input="notify", url=f"{HOOKS}/ok"),
        ],
    )
    run = await g.run(pid, {"url": item("80")})
    g.check(
        run["status"] == "completed"
        and await g.exists(location(run).get("key", ""))
        and await g.exists(location(run, "store_2").get("key", ""))
        and len(await g.posts_after(run["id"], 2)) == 2,
        f"two buckets and two webhooks deliver ({run['status']} {run.get('error')})",
    )


async def cancel_in_wait(g: DeliveryGate) -> None:
    print("R6 — a cancel during the webhook's retry wait: cancelled in seconds, the object stays")
    pid = await g.pipeline(
        "cancel", [g.fetch(), g.store(g.dest["rw"]), g.hook(url=f"{HOOKS}/down")]
    )
    rid = await g.start(pid, {"url": item("80")})
    run = await g.until(
        pid, rid, lambda r: blocks_by_id(r)["notify"]["status"] in ("waiting", *TERMINAL), 40
    )
    g.check(blocks_by_id(run)["notify"]["status"] == "waiting", "the webhook waits to retry")
    await g.api.post(f"/pipelines/{pid}/runs/{rid}/cancel", json={})
    t0 = time.monotonic()
    run = await g.wait(pid, rid, 60)
    took = time.monotonic() - t0
    g.check(
        run["status"] == "cancelled" and took < 10,
        f"cancelled after {took:.1f} s ({run['status']})",
    )
    key = location(run).get("key", "")
    g.check(
        key.startswith(f"gate/{pid}/{rid}/")
        and await g.exists(key)
        and blocks_by_id(run)["store"]["undo_status"] is None,
        f"the object stays and the run says where ({key})",
    )


async def stopped_after_bucket(g: DeliveryGate) -> None:
    print("PRD-019 — Scrape → Bucket → Validate (stop, delete) → Webhook")
    pid = await g.pipeline(
        "stop",
        [
            g.fetch(),
            g.store(g.dest["rw"]),
            {
                "id": "guard",
                "type": "validate",
                "input": "store",
                "config": {
                    "rules": [{"kind": "contains", "text": "Sold out"}],
                    "when_unmet": "stop",
                    "on_stop_outputs": "delete",
                },
            },
            g.hook(input="guard", url=f"{HOOKS}/ok"),
        ],
    )
    storage = await g.storage()
    run = await g.run(pid, {"url": item("80")})
    blocks = blocks_by_id(run)
    g.check(
        (run["status"], run["stopped_at_block_id"]) == ("completed", "guard")
        and (blocks["notify"]["status"], blocks["notify"]["status_reason"])
        == ("skipped", "stopped"),
        f"completed, stopped at guard, the webhook skipped ({run['status']} {run['stopped_at_block_id']})",
    )
    resp = await g.api.get(f"/pipelines/{pid}/runs/{run['id']}/result", params={"block": "fetch"})
    g.check(
        await g.storage() == storage and resp.status_code == 410,
        f"ScrapeFlow's copy of the page is deleted (usage back to {await g.storage()}, result {resp.status_code})",
    )
    g.check(await g.exists(location(run).get("key", "")), "the object stays in the user's bucket")
    g.check(await g.posts(run["id"]) == [], "nothing is sent")


async def announced(g: DeliveryGate) -> None:
    print("R3/D13 — two webhooks, the second fails: nothing is undone")
    pid = await g.pipeline(
        "announced",
        [
            g.fetch(),
            g.store(g.dest["rw"]),
            g.hook(url=f"{HOOKS}/ok"),
            g.refusing(id="notify_2", input="notify"),
        ],
    )
    run = await g.run(pid, {"url": item("80")})
    blocks = blocks_by_id(run)
    g.check(
        (run["status"], run["failed_block_id"]) == ("failed", "notify_2"),
        f"the run fails naming the second webhook ({run['failed_block_id']})",
    )
    g.check(
        (blocks["store"]["undo_status"], blocks["notify"]["undo_status"])
        == ("kept", "not_undoable")
        and run.get("rollback_deadline") is None,
        f"bucket kept (a webhook had announced it), first webhook could not be undone ({blocks['store']['undo_status']}, {blocks['notify']['undo_status']})",
    )
    g.check(await g.exists(location(run).get("key", "")), "the object stays")


async def private_at_delivery(g: DeliveryGate) -> None:
    print(
        "R5 — a bucket endpoint resolving to a private address is refused at delivery, not retried"
    )
    pid = await g.pipeline("private", [g.fetch(), g.store(g.dest["private_bucket"])])
    t0 = time.monotonic()
    run = await g.run(pid, {"url": item("80")})
    store = blocks_by_id(run)["store"]
    g.check(
        run["status"] == "failed"
        and (store["error"] or "").startswith("EgressBlocked")
        and "attempt" not in store["error"]
        and time.monotonic() - t0 < 20,
        f"failed at once ({store['error']}, {time.monotonic() - t0:.0f} s)",
    )


async def budget(g: DeliveryGate) -> None:
    print("R6 — a chain that cannot fit 24 h is refused at save, the undo reserve shown apart")
    blocks = [g.fetch(), g.store(g.dest["rw"], id="b1")]
    for n in range(2, 5):
        blocks.append(g.store(g.dest["rw"], id=f"b{n}", input=f"b{n - 1}"))
    blocks.append(g.hook(input="b4", url=f"{HOOKS}/ok"))
    resp = await g.api.post(
        "/pipelines",
        json={
            "name": "gate-budget",
            "definition": {"inputs": {"url": {"type": "url"}}, "blocks": blocks},
        },
    )
    g.check(
        resp.status_code == 422
        and "to undo 4 deliveries" in resp.text
        and "the limit is 24 h" in resp.text,
        f"refused with the reserve shown ({resp.status_code} {resp.text[:160]})",
    )


async def in_use(g: DeliveryGate) -> None:
    print("R5 — deleting a destination in use is refused, naming its pipelines")
    resp = await g.api.delete(f"/destinations/{g.dest['rw']}")
    detail = resp.json().get("detail", {}) if resp.status_code == 409 else {}
    g.check(
        resp.status_code == 409
        and detail.get("error") == "destination_in_use"
        and len(detail.get("pipelines", [])) >= 5,
        f"409 naming {len(detail.get('pipelines', []))} pipelines",
    )


async def notices(g: DeliveryGate) -> None:
    print("ADR-016 §8 — monitor notices: two emails in one delivery-worker life, then a webhook")
    pid = await g.pipeline("notices", [g.fetch()])
    resp = await g.api.post(
        "/monitors",
        json={
            "name": "gate-notices",
            "pipeline_id": pid,
            "inputs": {"url": item("80")},
            "schedule_cron": "0 3 1 1 *",
            "notice_destination_id": g.dest["mail"],
        },
    )
    if resp.status_code != 201:
        g.check(False, f"monitor created ({resp.status_code} {resp.text[:200]})")
        return
    mid = resp.json()["id"]
    client = await connect()
    schedule = client.get_schedule_handle(monitor_schedules.schedule_id(mid))

    async def situation() -> dict:
        before = {i["event"]["id"] for i in await history() if i["type"] == "event"}
        await schedule.trigger()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            new = [
                i["event"]
                for i in await history()
                if i["type"] == "event" and i["event"]["id"] not in before
            ]
            if new and (new[0].get("notice") or {}).get("status") in ("delivered", "exhausted"):
                return new[0]
            await asyncio.sleep(1)
        return new[0] if new else {}

    async def history() -> list[dict]:
        return (await g.api.get(f"/monitors/{mid}/history")).json()

    async def new_situation() -> None:
        for action in ("pause", "resume"):
            await g.api.post(f"/monitors/{mid}/{action}")

    await g.quota(monthly_runs_limit=0)
    try:
        events = [await situation()]
        await new_situation()
        events.append(await situation())
        await g.api.patch(f"/monitors/{mid}", json={"notice_destination_id": g.dest["hook"]})
        await new_situation()
        events.append(await situation())
    finally:
        await g.quota(monthly_runs_limit=None)
        await g.api.delete(f"/monitors/{mid}")
    states = [
        ((e.get("notice") or {}).get("kind"), (e.get("notice") or {}).get("status")) for e in events
    ]
    g.check(
        [e.get("kind") for e in events] == ["skipped_monthly_runs"] * 3
        and states == [("email", "delivered"), ("email", "delivered"), ("webhook", "delivered")],
        f"three situations, each notice delivered ({states})",
    )
    mail = (await g.fixture.get(f"{CONTROL}/_mail")).json()
    ids = {f"<{e.get('id')}@{settings.public_host}>" for e in events[:2]}
    mine = [m for m in mail["messages"] if m["message_id"] in ids]
    password = hashlib.sha256(f"{SECRET}-smtp".encode()).hexdigest()
    g.check(
        len(mine) == 2
        and len({m["session"] for m in mine}) == 2
        and all(m["password_sha256"] == password and m["user"] == "gate-mailer" for m in mine),
        f"two emails, one SMTP session each, logged in with the stored password ({len(mine)})",
    )
    sessions = [s["auth"] for s in mail["sessions"][g.sessions_before :]]
    g.check(
        sessions.count("accepted") == 3 and sessions.count("refused") == 1,
        f"four SMTP sessions to one delivery worker: the refused test, the passed test, both sends ({sessions})",
    )
    posts = [
        p
        for p in await g.hook_posts(f"/{SECRET}")
        if json.loads(p["body"]).get("monitor_id") == mid
    ]
    g.check(
        len(posts) == 1 and signed_with(posts[0], f"{SECRET}-sign2"),
        "the webhook notice arrives, signed with the destination's secret",
    )


def _payload_texts(value) -> list[str]:
    """Every payload's decoded bytes in a history's JSON form."""
    found = []
    if isinstance(value, dict):
        if "data" in value and "metadata" in value and isinstance(value["data"], str):
            try:
                found.append(base64.b64decode(value["data"]).decode("utf-8", "replace"))
            except ValueError:
                pass
        for v in value.values():
            found += _payload_texts(v)
    elif isinstance(value, list):
        for v in value:
            found += _payload_texts(v)
    return found


async def secrets(g: DeliveryGate) -> None:
    print("R5 — the known secret: in no API response, no workflow history")
    leaked = [r[:120] for r in RESPONSES if SECRET in r]
    g.check(
        not leaked and len(RESPONSES) > 50,
        f"{len(RESPONSES)} API responses, {len(leaked)} with the secret",
    )
    client = await connect()
    since = STARTED or (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    types: dict[str, int] = {}
    hits = []
    async for wf in client.list_workflows(f'StartTime >= "{since}"'):
        history = await client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history()
        text = history.to_json()
        types[wf.workflow_type] = types.get(wf.workflow_type, 0) + 1
        if SECRET in text or any(SECRET in t for t in _payload_texts(json.loads(text))):
            hits.append(wf.id)
    needed = {
        "PipelineWorkflow",
        "DestinationTestWorkflow",
        "WebhookDeliveryWorkflow",
        "MonitorCheckWorkflow",
    }
    g.check(
        not hits and needed <= set(types),
        f"{sum(types.values())} histories since {since} ({types}), {len(hits)} with the secret {hits[:3]}",
    )


async def admin_delete(g: DeliveryGate) -> None:
    print("R1 — an admin deletes the user: what was delivered stays in their bucket")
    resp = await g.admin.delete(f"/admin/users/{g.user_id}")
    g.check(resp.status_code == 204, f"the user is deleted ({resp.status_code} {resp.text[:120]})")
    g.check(
        g.kept is not None and await g.exists(g.kept[1]),
        "the delivered object is still in the bucket",
    )


SCENARIOS = {
    "destinations": destinations,
    "delivered": delivered,
    "undone": undone,
    "undo_refused": undo_refused,
    "lost_reply": lost_reply,
    "waiting_put": waiting_put,
    "gone_before_undo": gone_before_undo,
    "moved_bucket": moved_bucket,
    "ordering": ordering,
    "cancel_in_wait": cancel_in_wait,
    "stopped_after_bucket": stopped_after_bucket,
    "announced": announced,
    "private_at_delivery": private_at_delivery,
    "budget": budget,
    "in_use": in_use,
    "notices": notices,
    "secrets": secrets,
    "admin_delete": admin_delete,
}


async def _user(name: str, admin: bool = False) -> tuple[User, str]:
    raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        user = User(
            clerk_id=f"{name}_{uuid.uuid4().hex}",
            email=f"{name.replace('_', '-')}-{uuid.uuid4().hex[:8]}@scrapeflow.test",
            is_admin=admin,
        )
        db.add(user)
        await db.flush()
        db.add(ApiKey(user_id=user.id, key_hash=hash_api_key(raw), name=name))
        await db.commit()
        await db.refresh(user)
    return user, raw


async def _remove(user_id: uuid.UUID, minio) -> None:
    async with AsyncSessionLocal() as db:
        if await db.get(User, user_id) is None:
            return
        ids = list((await db.scalars(select(Monitor.id).where(Monitor.user_id == user_id))).all())
    await monitor_schedules.forget(connect, ids)
    async with AsyncSessionLocal() as db:
        await release_user_objects(db, minio, user_id, "delivery gate cleanup")
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def gate(only: list[str]) -> list[str]:
    if not SECRET:
        raise SystemExit("GATE_SECRET is not set — run scripts/delivery-gate.sh")
    user, raw = await _user("delivery_gate")
    admin_user, admin_raw = await _user("delivery_gate_admin", admin=True)
    print(f"gate user {user.id}, admin {admin_user.id}")
    minio = await minio_client.create_client()
    hooks = {"response": [_record]}
    try:
        async with (
            httpx.AsyncClient(
                base_url=API, headers={"X-API-Key": raw}, timeout=60, event_hooks=hooks
            ) as api,
            httpx.AsyncClient(
                base_url=API, headers={"X-API-Key": admin_raw}, timeout=60, event_hooks=hooks
            ) as admin,
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
            g = DeliveryGate(api, admin, fixture, key.json()["id"], user.id, minio)
            for name, scenario in SCENARIOS.items():
                if not only or name in only or name == "destinations":
                    await scenario(g)
            return g.problems
    finally:
        await _remove(user.id, minio)
        await _remove(admin_user.id, minio)
        await minio_client.close_client(minio)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "only", nargs="*", help="scenario names (destinations always runs); all when omitted"
    )
    args = parser.parse_args()
    problems = asyncio.run(gate(args.only))
    for problem in problems:
        print(f"MISMATCH  {problem}")
    print("GATE PASSED" if not problems else f"GATE FAILED ({len(problems)})")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
