"""BUG-010 part 2 — the egress gate: real jobs through the API, the workers and the egress proxy.

A public page must still scrape; a public page that redirects to an internal address must fail
as refused, on each engine, with nothing stored. Runs as a throwaway user, removed afterwards.
Exit status 1 on any mismatch.

Usage (from ./docker; needs the gate profile's pipeline-fixture and crawl-fixture):

    docker compose --profile gate up -d pipeline-fixture crawl-fixture
    docker compose exec -T api uv run python -m scripts.egress_gate [--engine http|playwright]
"""

import argparse
import asyncio
import sys
import time
import uuid
from urllib.parse import quote

import httpx
from sqlalchemy import delete, func, select

from app.auth.api_key import generate_api_key, hash_api_key
from app.core.db import AsyncSessionLocal
from app.models.api_key import ApiKey
from app.models.storage_object import StorageObject
from app.models.user import User

API = "http://localhost:8000"
TERMINAL = {"completed", "failed", "cancelled"}
SHOP = "http://shop.pipelinefixture.com"

# (name, url, expected status, text the error must contain)
CASES = [
    ("public page", f"{SHOP}/item?name=Kettle", "completed", None),
    (
        "redirect to a private name",
        f"{SHOP}/redirect?to={quote('http://internal.crawlfixture.com/', safe='')}",
        "failed",
        "address refused",
    ),
    (
        "redirect to MinIO",
        f"{SHOP}/redirect?to={quote('http://minio:9000/minio/health/live', safe='')}",
        "failed",
        "address refused",
    ),
    (
        "redirect to link-local",
        f"{SHOP}/redirect?to={quote('http://169.254.169.254/latest/meta-data/', safe='')}",
        "failed",
        "address refused",
    ),
]


async def _gate_user() -> tuple[User, str]:
    raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        user = User(
            clerk_id=f"egress_gate_{uuid.uuid4().hex}",
            email=f"egress-gate-{uuid.uuid4().hex[:8]}@scrapeflow.test",
        )
        db.add(user)
        await db.flush()
        db.add(ApiKey(user_id=user.id, key_hash=hash_api_key(raw), name="egress gate"))
        await db.commit()
        await db.refresh(user)
    return user, raw


async def _stored(user_id) -> int:
    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(func.count()).select_from(StorageObject).where(StorageObject.user_id == user_id)
        )


async def gate(engine: str, timeout: float) -> list[str]:
    problems: list[str] = []
    user, key = await _gate_user()
    try:
        async with httpx.AsyncClient(base_url=API, headers={"X-API-Key": key}, timeout=30) as api:
            jobs = {}
            for name, url, _, _ in CASES:
                resp = await api.post(
                    "/jobs", json={"url": url, "engine": engine, "output_format": "html"}
                )
                if resp.status_code != 201:
                    problems.append(f"{name}: POST /jobs → {resp.status_code}: {resp.text}")
                    continue
                jobs[name] = resp.json()["id"]

            deadline = time.monotonic() + timeout
            final: dict[str, dict] = {}
            while len(final) < len(jobs) and time.monotonic() < deadline:
                for name, job_id in jobs.items():
                    if name not in final:
                        job = (await api.get(f"/jobs/{job_id}")).json()
                        if job["status"] in TERMINAL:
                            final[name] = job
                await asyncio.sleep(1)

            for name, _, want_status, want_error in CASES:
                if name not in jobs:
                    continue
                job = final.get(name)
                if job is None:
                    problems.append(f"{name}: not finished in {timeout} s")
                    continue
                print(f"{name}: {job['status']} {job['error'] or ''}")
                if job["status"] != want_status:
                    problems.append(f"{name}: {job['status']}, expected {want_status}")
                if want_error and want_error not in (job["error"] or ""):
                    problems.append(f"{name}: error {job['error']!r} lacks {want_error!r}")
                if "Deny:" in (job["error"] or ""):
                    problems.append(
                        f"{name}: the error names the internal address: {job['error']!r}"
                    )

            stored = await _stored(user.id)
            if stored != 1:
                problems.append(f"{stored} objects stored, expected 1 (the public page)")

            for job_id in jobs.values():
                await api.delete(f"/jobs/{job_id}", params={"permanent": "true"})
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=["http", "playwright"], default="http")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    problems = asyncio.run(gate(args.engine, args.timeout))
    for p in problems:
        print(f"FAIL {p}")
    print("PASS" if not problems else f"{len(problems)} problem(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
