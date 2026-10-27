"""E.6 — the crawl lane's compensating gate: one crawl of the fixture site, judged against the
expected result written down beside it (contracts/fixtures/crawl-site/).

No v1 crawl ever completed (ADR-009 13a), so there is no reference run to diff against; the
fixture is the reference. The crawl goes through the real path — POST /crawls, the
CrawlWorkflow, both scrape workers, the crawl queue — as a throwaway user, and must produce
exactly the expected page set, outcomes, refusals and counters, and charge exactly the bytes
the fixture served for its completed pages. The user and everything it stored are removed
afterwards (--keep to inspect them). Exit status 1 on any mismatch.

Usage (from ./docker; the fixture must be up — its README):

    docker compose exec -T api uv run python -m scripts.crawl_gate \\
        < ../contracts/fixtures/crawl-site/expected.json
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, select

from app.auth.api_key import generate_api_key, hash_api_key
from app.core.db import AsyncSessionLocal
from app.models.api_key import ApiKey
from app.models.crawl import CrawlPage
from app.models.storage_object import StorageObject
from app.models.user import User
from app.models.user_quota import UserQuota

API = "http://localhost:8000"
TERMINAL = {"completed", "failed", "cancelled"}


async def _gate_user() -> tuple[User, str]:
    raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        user = User(
            clerk_id=f"crawl_gate_{uuid.uuid4().hex}",
            email=f"crawl-gate-{uuid.uuid4().hex[:8]}@scrapeflow.test",
        )
        db.add(user)
        await db.flush()
        db.add(ApiKey(user_id=user.id, key_hash=hash_api_key(raw), name="crawl gate"))
        await db.commit()
        await db.refresh(user)
    return user, raw


async def _charged(user_id, crawl_id) -> tuple[int, int, dict[str, int]]:
    """(the user's storage counter, the ledger's sum for the crawl, bytes per page URL)."""
    async with AsyncSessionLocal() as db:
        quota = await db.get(UserQuota, user_id)
        rows = (
            await db.execute(
                select(CrawlPage.url, func.sum(StorageObject.bytes))
                .join(StorageObject, StorageObject.crawl_page_id == CrawlPage.id)
                .where(CrawlPage.crawl_id == crawl_id)
                .group_by(CrawlPage.url)
            )
        ).all()
    per_page = {url: int(n) for url, n in rows}
    return (quota.storage_bytes_used if quota else 0), sum(per_page.values()), per_page


async def gate(expected: dict, timeout: float, keep: bool) -> list[str]:
    problems: list[str] = []
    user, key = await _gate_user()
    headers = {"X-API-Key": key}
    async with httpx.AsyncClient(base_url=API, headers=headers, timeout=30) as api:
        resp = await api.post(
            "/crawls",
            json={"seed_url": expected["seed_url"], "ignore_sitemap": False, **expected["request"]},
        )
        if resp.status_code != 201:
            return [f"POST /crawls → {resp.status_code}: {resp.text}"]
        crawl_id = resp.json()["id"]
        print(f"crawl {crawl_id} (user {user.id})")

        deadline = time.monotonic() + timeout
        while True:
            crawl = (await api.get(f"/crawls/{crawl_id}")).json()
            if crawl["status"] in TERMINAL or time.monotonic() > deadline:
                break
            await asyncio.sleep(1)
        print(f"→ {crawl['status']} in {timeout - (deadline - time.monotonic()):.1f} s")
        pages = (await api.get(f"/crawls/{crawl_id}/pages", params={"limit": 200})).json()

        for field in ("status", "total_queued", "total_completed", "total_failed", "dropped"):
            if crawl[field] != expected[field]:
                problems.append(f"{field}: got {crawl[field]!r}, expected {expected[field]!r}")

        got = {(p["url"], p["depth"], p["status"], p["error"]) for p in pages}
        want = {(p["url"], p["depth"], p["status"], p.get("error")) for p in expected["pages"]}
        for extra in sorted(got - want):
            problems.append(f"unexpected page {extra}")
        for missing in sorted(want - got):
            problems.append(f"missing page {missing}")

        # What the crawl must have charged: the bytes the site served (http stores them as
        # served), or the stored object's size (playwright stores the rendered DOM).
        served: dict[str, int] = {}
        async with httpx.AsyncClient(timeout=10) as site:
            for page in expected["pages"]:
                if page["status"] != "completed":
                    continue
                if expected["request"]["engine"] == "http":
                    served[page["url"]] = len((await site.get(page["url"])).content)
                else:
                    by_url = {p["url"]: p["id"] for p in pages}
                    if page["url"] in by_url:
                        result = await api.get(
                            f"/crawls/{crawl_id}/pages/{by_url[page['url']]}/result"
                        )
                        served[page["url"]] = len(result.json()["content"].encode())
        counter, ledger, per_page = await _charged(user.id, uuid.UUID(crawl_id))
        if per_page != served:
            problems.append(f"charged per page {per_page}, stored {served}")
        if not (counter == ledger == sum(served.values())):
            problems.append(
                f"bytes: counter {counter}, ledger {ledger}, stored {sum(served.values())}"
            )
        print(f"charged {ledger} bytes for {len(per_page)} pages; counter {counter}")

        for page in pages:
            host = urlsplit(page["url"]).hostname
            if host == "internal.crawlfixture.com":
                problems.append(f"the SSRF bait was crawled: {page['url']}")

        if not keep:
            resp = await api.delete(f"/crawls/{crawl_id}", params={"permanent": "true"})
            if resp.status_code != 204:
                problems.append(f"permanent delete → {resp.status_code}: {resp.text}")
    if not keep:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--keep", action="store_true", help="keep the gate user and its crawl")
    parser.add_argument("--engine", choices=["http", "playwright"], help="override expected.json's")
    args = parser.parse_args()
    expected = json.load(sys.stdin)
    if args.engine:
        expected["request"]["engine"] = args.engine
    problems = asyncio.run(gate(expected, args.timeout, args.keep))
    for problem in problems:
        print(f"MISMATCH  {problem}")
    print("GATE PASSED" if not problems else f"GATE FAILED ({len(problems)})")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
