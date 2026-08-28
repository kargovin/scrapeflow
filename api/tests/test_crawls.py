import uuid

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.crawl import Crawl, CrawlPage, CrawlQueueItem


@pytest.fixture
def auth_headers(mock_clerk_auth):
    return {"Authorization": "Bearer fake.jwt.token"}


@pytest.fixture
def bypass_ssrf(monkeypatch):
    """Skip SSRF DNS resolution in crawl tests — SSRF behaviour is tested separately below."""
    monkeypatch.setattr("app.routers.crawls.validate_no_ssrf", lambda _: None)


# ---------------------------------------------------------------------------
# POST /crawls
# ---------------------------------------------------------------------------


async def test_create_crawl_creates_rows(client, auth_headers, bypass_ssrf):
    """POST /crawls creates a crawl row and seeds crawl_queue with depth=0."""
    seed = f"https://{uuid.uuid4().hex}.local/"
    response = await client.post(
        "/crawls",
        json={"seed_url": seed},
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "queued"
    assert data["seed_url"] == seed
    assert data["max_depth"] == 3
    assert data["max_pages"] == 100
    assert data["respect_robots"] is True
    assert "id" in data

    crawl_id = uuid.UUID(data["id"])
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CrawlQueueItem).where(CrawlQueueItem.crawl_id == crawl_id))
        items = result.scalars().all()
    assert len(items) == 1
    assert items[0].url == seed
    assert items[0].depth == 0
    assert items[0].status == "pending"


async def test_create_crawl_ssrf_rejected(client, auth_headers):
    """POST /crawls with a link-local seed_url is rejected before any rows are created."""
    response = await client.post(
        "/crawls",
        json={"seed_url": "http://169.254.169.254/latest/meta-data/"},
        headers=auth_headers,
    )
    assert response.status_code in (400, 422)


async def test_create_crawl_duplicate_domain_returns_409(client, auth_headers, bypass_ssrf):
    """POST /crawls for a domain with an active crawl returns 409 with crawl_id."""
    domain = f"https://{uuid.uuid4().hex}.local"
    first = await client.post(
        "/crawls",
        json={"seed_url": f"{domain}/"},
        headers=auth_headers,
    )
    assert first.status_code == 201
    existing_crawl_id = first.json()["id"]

    second = await client.post(
        "/crawls",
        json={"seed_url": f"{domain}/other-page"},
        headers=auth_headers,
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "active_crawl_exists"
    assert detail["crawl_id"] == existing_crawl_id


async def test_create_crawl_completed_domain_allows_new(client, auth_headers, bypass_ssrf):
    """POST /crawls for a domain whose previous crawl is completed is allowed."""
    seed = f"https://{uuid.uuid4().hex}.local/"
    create_resp = await client.post(
        "/crawls",
        json={"seed_url": seed},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    crawl_id = uuid.UUID(create_resp.json()["id"])

    async with AsyncSessionLocal() as db:
        crawl = await db.get(Crawl, crawl_id)
        assert crawl is not None
        crawl.status = "completed"
        await db.commit()

    second = await client.post(
        "/crawls",
        json={"seed_url": seed},
        headers=auth_headers,
    )
    assert second.status_code == 201


async def test_create_crawl_unauthenticated(client):
    """POST /crawls without auth returns 401."""
    response = await client.post("/crawls", json={"seed_url": "https://example.com/"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /crawls/{id}
# ---------------------------------------------------------------------------


async def test_get_crawl_returns_progress_counters(client, auth_headers, bypass_ssrf):
    """GET /crawls/{id} returns counters and status fields."""
    create_resp = await client.post(
        "/crawls",
        json={"seed_url": f"https://{uuid.uuid4().hex}.local/"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    crawl_id = create_resp.json()["id"]

    response = await client.get(f"/crawls/{crawl_id}", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == crawl_id
    assert data["status"] == "queued"
    assert data["total_queued"] == 0
    assert data["total_completed"] == 0
    assert data["total_failed"] == 0


async def test_get_crawl_other_user_returns_404(client, auth_headers, db_user):
    """GET /crawls/{id} returns 404 for a crawl belonging to another user."""
    crawl = Crawl(
        user_id=db_user.id,
        seed_url="https://other-user.local/",
        status="queued",
        output_format="markdown",
        engine="http",
        max_depth=3,
        max_pages=100,
        ignore_sitemap=False,
        respect_robots=True,
    )
    async with AsyncSessionLocal() as db:
        db.add(crawl)
        await db.commit()
        await db.refresh(crawl)

    response = await client.get(f"/crawls/{crawl.id}", headers=auth_headers)
    assert response.status_code == 404

    async with AsyncSessionLocal() as db:
        c = await db.get(Crawl, crawl.id)
        if c:
            await db.delete(c)
            await db.commit()


async def test_get_crawl_not_found(client, auth_headers):
    """GET /crawls/{id} for a nonexistent crawl returns 404."""
    response = await client.get(f"/crawls/{uuid.uuid4()}", headers=auth_headers)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /crawls/{id}/pages
# ---------------------------------------------------------------------------


async def test_list_crawl_pages_empty(client, auth_headers, bypass_ssrf):
    """GET /crawls/{id}/pages returns empty list before coordinator dispatches."""
    create_resp = await client.post(
        "/crawls",
        json={"seed_url": f"https://{uuid.uuid4().hex}.local/"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    crawl_id = create_resp.json()["id"]

    response = await client.get(f"/crawls/{crawl_id}/pages", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


async def test_list_crawl_pages_other_user_returns_404(client, auth_headers, db_user):
    """GET /crawls/{id}/pages returns 404 for another user's crawl."""
    crawl = Crawl(
        user_id=db_user.id,
        seed_url="https://other-user-pages.local/",
        status="queued",
        output_format="markdown",
        engine="http",
        max_depth=3,
        max_pages=100,
        ignore_sitemap=False,
        respect_robots=True,
    )
    async with AsyncSessionLocal() as db:
        db.add(crawl)
        await db.commit()
        await db.refresh(crawl)

    response = await client.get(f"/crawls/{crawl.id}/pages", headers=auth_headers)
    assert response.status_code == 404

    async with AsyncSessionLocal() as db:
        c = await db.get(Crawl, crawl.id)
        if c:
            await db.delete(c)
            await db.commit()


async def test_list_crawl_pages_filter_accepts_running(client, auth_headers, bypass_ssrf):
    """?status=running matches — "running" is what the coordinator writes mid-scrape."""
    create_resp = await client.post(
        "/crawls",
        json={"seed_url": f"https://{uuid.uuid4().hex}.local/"},
        headers=auth_headers,
    )
    crawl_id = create_resp.json()["id"]

    async with AsyncSessionLocal() as db:
        db.add(
            CrawlPage(
                crawl_id=uuid.UUID(crawl_id),
                url="https://example.local/a",
                depth=0,
                status="running",
            )
        )
        await db.commit()

    response = await client.get(
        f"/crawls/{crawl_id}/pages", params={"status": "running"}, headers=auth_headers
    )
    assert response.status_code == 200
    assert [p["status"] for p in response.json()] == ["running"]


async def test_list_crawl_pages_filter_rejects_processing(client, auth_headers, bypass_ssrf):
    """?status=processing is rejected — nothing ever writes it, so it could only mislead."""
    create_resp = await client.post(
        "/crawls",
        json={"seed_url": f"https://{uuid.uuid4().hex}.local/"},
        headers=auth_headers,
    )
    crawl_id = create_resp.json()["id"]

    response = await client.get(
        f"/crawls/{crawl_id}/pages", params={"status": "processing"}, headers=auth_headers
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /crawls/{id}
# ---------------------------------------------------------------------------


async def test_delete_crawl_cancels_and_skips_queue(client, auth_headers, bypass_ssrf):
    """DELETE /crawls/{id} sets status=cancelled and marks pending queue items skipped."""
    create_resp = await client.post(
        "/crawls",
        json={"seed_url": f"https://{uuid.uuid4().hex}.local/"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    crawl_id = create_resp.json()["id"]

    response = await client.delete(f"/crawls/{crawl_id}", headers=auth_headers)
    assert response.status_code == 204

    async with AsyncSessionLocal() as db:
        crawl = await db.get(Crawl, uuid.UUID(crawl_id))
        assert crawl is not None
        assert crawl.status == "cancelled"

        queue_result = await db.execute(
            select(CrawlQueueItem).where(CrawlQueueItem.crawl_id == uuid.UUID(crawl_id))
        )
        items = queue_result.scalars().all()
    assert len(items) == 1
    assert all(item.status == "skipped" for item in items)


async def test_delete_crawl_already_cancelled_returns_409(client, auth_headers, bypass_ssrf):
    """DELETE /crawls/{id} on an already-cancelled crawl returns 409."""
    create_resp = await client.post(
        "/crawls",
        json={"seed_url": f"https://{uuid.uuid4().hex}.local/"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    crawl_id = create_resp.json()["id"]

    await client.delete(f"/crawls/{crawl_id}", headers=auth_headers)
    second = await client.delete(f"/crawls/{crawl_id}", headers=auth_headers)
    assert second.status_code == 409


async def test_delete_crawl_other_user_returns_404(client, auth_headers, db_user):
    """DELETE /crawls/{id} returns 404 for another user's crawl."""
    crawl = Crawl(
        user_id=db_user.id,
        seed_url="https://other-user-delete.local/",
        status="queued",
        output_format="markdown",
        engine="http",
        max_depth=3,
        max_pages=100,
        ignore_sitemap=False,
        respect_robots=True,
    )
    async with AsyncSessionLocal() as db:
        db.add(crawl)
        await db.commit()
        await db.refresh(crawl)

    response = await client.delete(f"/crawls/{crawl.id}", headers=auth_headers)
    assert response.status_code == 404

    async with AsyncSessionLocal() as db:
        c = await db.get(Crawl, crawl.id)
        if c:
            await db.delete(c)
            await db.commit()
