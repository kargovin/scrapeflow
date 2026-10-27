"""E.10 — batch and crawl reads: owner lists, result content, and their admin twins."""

import io
import uuid

import pytest
import pytest_asyncio

from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.batch import Batch, BatchItem
from app.models.crawl import Crawl, CrawlPage
from app.settings import settings
from tests.test_admin import admin_api_key, admin_headers, admin_user  # noqa: F401

BUCKET = settings.minio_bucket


@pytest.fixture
def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


async def _put(content: bytes, ext: str) -> str:
    key = f"history/{uuid.uuid4()}/scrape.{ext}"
    await app.state.minio.put_object(BUCKET, key, io.BytesIO(content), len(content))
    return f"{BUCKET}/{key}"


async def _batch(user_id, *, status="completed", path=None) -> tuple[Batch, BatchItem, BatchItem]:
    async with AsyncSessionLocal() as db:
        batch = Batch(user_id=user_id, status=status, total=2, completed=1, failed=1)
        db.add(batch)
        await db.flush()
        done = BatchItem(batch_id=batch.id, url="https://a.example/", status="completed")
        done.result_path = path
        failed = BatchItem(batch_id=batch.id, url="https://b.example/", status="failed", error="e")
        db.add_all([done, failed])
        await db.commit()
    return batch, done, failed


async def _crawl(user_id, *, status="completed", path=None) -> tuple[Crawl, CrawlPage, CrawlPage]:
    async with AsyncSessionLocal() as db:
        crawl = Crawl(user_id=user_id, seed_url="https://c.example/", status=status)
        db.add(crawl)
        await db.flush()
        done = CrawlPage(crawl_id=crawl.id, url="https://c.example/", depth=0, status="completed")
        done.result_path = path
        running = CrawlPage(crawl_id=crawl.id, url="https://c.example/x", depth=1, status="running")
        db.add_all([done, running])
        await db.commit()
    return crawl, done, running


@pytest_asyncio.fixture
async def mine(db_user):
    batches = [(await _batch(db_user.id, path=await _put(b"<p>a</p>", "html")))[0]]
    batches.append((await _batch(db_user.id, status="running"))[0])
    crawl, page, running = await _crawl(db_user.id, path=await _put(b"# md", "md"))
    return batches, crawl, page, running


# ---------------------------------------------------------------------------
# Owner reads
# ---------------------------------------------------------------------------


async def test_list_batches_is_owner_scoped_newest_first(client, headers, mine, other_user):
    await _batch(other_user.id)
    resp = await client.get("/batch", headers=headers)
    assert resp.status_code == 200
    batches, *_ = mine
    assert [b["id"] for b in resp.json()] == [str(b.id) for b in reversed(batches)]


async def test_batch_item_result_reads_the_stored_object(client, headers, db_user):
    path = await _put(b"<p>a</p>", "html")
    batch, done, failed = await _batch(db_user.id, path=path)

    resp = await client.get(f"/batch/{batch.id}/items/{done.id}/result", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "content": "<p>a</p>",
        "output_format": "html",
        "result_path": path,
        "warnings": None,
        # A job run's fields; a batch item has none.
        "run_id": None,
        "completed_at": None,
    }
    not_done = await client.get(f"/batch/{batch.id}/items/{failed.id}/result", headers=headers)
    assert not_done.status_code == 404


async def test_batch_item_result_of_an_llm_step_is_json(client, headers, db_user):
    path = await _put(b'{"title": "x"}', "json")
    batch, done, _ = await _batch(db_user.id, path=path)
    resp = await client.get(f"/batch/{batch.id}/items/{done.id}/result", headers=headers)
    assert resp.json()["output_format"] == "json"


async def test_batch_reads_404_across_tenants(client, headers, other_user, db_user):
    batch, done, _ = await _batch(other_user.id, path=await _put(b"x", "html"))
    mine, _, _ = await _batch(db_user.id)
    for path in (
        f"/batch/{batch.id}",
        f"/batch/{batch.id}/items",
        f"/batch/{batch.id}/items/{done.id}/result",
        # Another batch's item through my batch.
        f"/batch/{mine.id}/items/{done.id}/result",
    ):
        assert (await client.get(path, headers=headers)).status_code == 404, path


async def test_list_crawls_is_owner_scoped_and_carries_drops(client, headers, mine, other_user):
    await _crawl(other_user.id)
    _, crawl, _, _ = mine
    async with AsyncSessionLocal() as db:
        row = await db.get(Crawl, crawl.id)
        row.dropped = {"ssrf": 1}
        await db.commit()

    resp = await client.get("/crawls", headers=headers)
    assert resp.status_code == 200
    [only] = resp.json()
    assert (only["id"], only["dropped"], only["user_email"]) == (str(crawl.id), {"ssrf": 1}, None)


async def test_crawl_page_result_reads_the_stored_object(client, headers, mine):
    _, crawl, page, running = mine
    resp = await client.get(f"/crawls/{crawl.id}/pages/{page.id}/result", headers=headers)
    assert resp.status_code == 200
    assert (resp.json()["content"], resp.json()["output_format"]) == ("# md", "markdown")

    not_done = await client.get(f"/crawls/{crawl.id}/pages/{running.id}/result", headers=headers)
    assert not_done.status_code == 404


async def test_crawl_reads_404_across_tenants(client, headers, other_user, mine):
    crawl, page, _ = await _crawl(other_user.id, path=await _put(b"x", "html"))
    _, my_crawl, _, _ = mine
    for path in (
        f"/crawls/{crawl.id}",
        f"/crawls/{crawl.id}/pages",
        f"/crawls/{crawl.id}/pages/{page.id}/result",
        f"/crawls/{my_crawl.id}/pages/{page.id}/result",
    ):
        assert (await client.get(path, headers=headers)).status_code == 404, path


# ---------------------------------------------------------------------------
# Admin twins
# ---------------------------------------------------------------------------


async def test_admin_lane_reads_are_403_for_a_non_admin(client, headers):
    fake = uuid.uuid4()
    for path in (
        "/admin/batches",
        f"/admin/batches/{fake}",
        f"/admin/batches/{fake}/items",
        f"/admin/batches/{fake}/items/{fake}/result",
        "/admin/crawls",
        f"/admin/crawls/{fake}",
        f"/admin/crawls/{fake}/pages",
        f"/admin/crawls/{fake}/pages/{fake}/result",
    ):
        assert (await client.get(path, headers=headers)).status_code == 403, path


async def test_admin_sees_every_users_batches_with_their_email(
    client,
    admin_headers,  # noqa: F811
    db_user,
    other_user,
):
    path = await _put(b"<p>b</p>", "html")
    mine, done, _ = await _batch(db_user.id, path=path)
    theirs, _, _ = await _batch(other_user.id, status="running")

    listed = await client.get("/admin/batches", headers=admin_headers, params={"limit": 200})
    by_id = {b["id"]: b for b in listed.json()}
    assert by_id[str(mine.id)]["user_email"] == db_user.email
    assert by_id[str(theirs.id)]["user_email"] == other_user.email

    running = await client.get(
        "/admin/batches",
        headers=admin_headers,
        params={"status": "running", "user_id": str(other_user.id)},
    )
    assert [b["id"] for b in running.json()] == [str(theirs.id)]

    one = await client.get(f"/admin/batches/{mine.id}", headers=admin_headers)
    assert one.json()["user_email"] == db_user.email
    items = await client.get(f"/admin/batches/{mine.id}/items", headers=admin_headers)
    assert len(items.json()) == 2
    result = await client.get(
        f"/admin/batches/{mine.id}/items/{done.id}/result", headers=admin_headers
    )
    assert result.json()["content"] == "<p>b</p>"
    assert (
        await client.get(f"/admin/batches/{uuid.uuid4()}", headers=admin_headers)
    ).status_code == 404


async def test_admin_sees_every_users_crawls_with_their_email(
    client,
    admin_headers,  # noqa: F811
    other_user,
):
    crawl, page, _ = await _crawl(other_user.id, path=await _put(b"<p>c</p>", "html"))

    one = await client.get(f"/admin/crawls/{crawl.id}", headers=admin_headers)
    assert one.json()["user_email"] == other_user.email
    listed = await client.get(
        "/admin/crawls", headers=admin_headers, params={"user_id": str(other_user.id)}
    )
    assert [c["id"] for c in listed.json()] == [str(crawl.id)]
    pages = await client.get(
        f"/admin/crawls/{crawl.id}/pages", headers=admin_headers, params={"status": "completed"}
    )
    assert [p["id"] for p in pages.json()] == [str(page.id)]
    result = await client.get(
        f"/admin/crawls/{crawl.id}/pages/{page.id}/result", headers=admin_headers
    )
    assert result.json()["content"] == "<p>c</p>"
    assert (
        await client.get(f"/admin/crawls/{uuid.uuid4()}/pages", headers=admin_headers)
    ).status_code == 404
