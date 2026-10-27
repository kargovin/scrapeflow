import asyncio
import uuid
from asyncio import get_running_loop
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi import status as http_status
from miniopy_async import Minio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import auth_from_token, get_current_user
from app.core import crawl_progress
from app.core.db import get_db
from app.core.dispatch import cancel_crawl as cancel_crawl_rows
from app.core.dispatch import (
    cancel_crawl_workflow,
    fail_unstarted_crawl,
    start_crawl,
)
from app.core.job_notifier import WebSocketConnectionLimitExceeded
from app.core.ledger import release_crawl_objects
from app.core.minio import get_minio
from app.core.quota import Admit, admission
from app.core.rate_limit import check_rate_limit
from app.core.security import validate_no_ssrf
from app.models.crawl import Crawl, CrawlPage
from app.models.user import User
from app.routers.jobs import read_stored_result
from app.schemas.crawls import CrawlCreate, CrawlPageResponse, CrawlResponse
from app.schemas.jobs import JobResultResponse
from app.workflows.client import lazy_client

router = APIRouter(prefix="/crawls", tags=["crawls"])
logger = structlog.get_logger()

_TERMINAL_STATUSES = crawl_progress.TERMINAL

# The values activities/crawls.py writes to crawl_pages.status. Must track the writer.
PAGE_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})


async def check_crawl_quota(
    body: CrawlCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Admit:
    """FastAPI dependency — the admission create_crawl awaits right before its insert.

    A crawl is pre-checked against its declared ceiling exactly as a batch is against
    len(urls): monthly_runs with batch_count=max_pages, since every page is one attempted
    fetch. Concurrency is one slot for the whole crawl (ADR-009 §8). The meter charges
    actuals — crawl_pages rows as the workflow claims them.
    """
    return admission(user.id, db, runs=body.max_pages)


async def owned_crawl(db: AsyncSession, crawl_id: uuid.UUID, user: User) -> Crawl:
    crawl = await db.get(Crawl, crawl_id)
    if crawl is None or crawl.user_id != user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Crawl not found")
    return crawl


async def list_pages(
    db: AsyncSession, crawl_id: uuid.UUID, limit: int, offset: int, page_status: str | None
) -> list[CrawlPageResponse]:
    stmt = (
        select(CrawlPage)
        .where(CrawlPage.crawl_id == crawl_id)
        .order_by(CrawlPage.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    if page_status is not None:
        if page_status not in PAGE_STATUSES:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Page Status not valid",
            )
        stmt = stmt.where(CrawlPage.status == page_status)
    result = await db.execute(stmt)
    return [CrawlPageResponse.model_validate(page) for page in result.scalars()]


async def page_result(
    db: AsyncSession, minio_client: Minio, crawl_id: uuid.UUID, page_id: uuid.UUID
) -> JobResultResponse:
    page = await db.get(CrawlPage, page_id)
    if page is None or page.crawl_id != crawl_id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Page not found")
    if page.status != "completed" or page.result_path is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="No completed result available"
        )
    return await read_stored_result(minio_client, page.result_path)


@router.post("", response_model=CrawlResponse, status_code=http_status.HTTP_201_CREATED)
async def create_crawl(
    body: CrawlCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    admit: Admit = Depends(check_crawl_quota),
) -> CrawlResponse:
    seed_url = str(body.seed_url)
    loop = get_running_loop()
    await loop.run_in_executor(None, validate_no_ssrf, seed_url)
    if body.webhook_url:
        await loop.run_in_executor(None, validate_no_ssrf, str(body.webhook_url))

    await admit()
    # One-active-crawl-per-domain: reject if the same user already has a non-terminal
    # crawl whose seed_url shares the same origin.
    parsed = urlparse(seed_url)
    origin_prefix = f"{parsed.scheme}://{parsed.netloc}%"
    existing_id = await db.scalar(
        select(Crawl.id)
        .where(
            Crawl.user_id == user.id,
            Crawl.seed_url.like(origin_prefix),
            ~Crawl.status.in_(list(_TERMINAL_STATUSES)),
        )
        .limit(1)
    )
    if existing_id is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"error": "active_crawl_exists", "crawl_id": str(existing_id)},
        )

    crawl = Crawl(
        user_id=user.id,
        seed_url=seed_url,
        status="queued",
        max_depth=body.max_depth,
        max_pages=body.max_pages,
        include_paths=body.include_paths,
        exclude_paths=body.exclude_paths,
        ignore_sitemap=body.ignore_sitemap,
        output_format=body.output_format.value,
        engine=body.engine.value,
        webhook_url=body.webhook_url,
        respect_robots=body.respect_robots,
        schedule_cron=body.schedule_cron,
    )
    db.add(crawl)
    await db.commit()
    await db.refresh(crawl)

    # The seed joins the frontier through admission, in the workflow (ADR-009 13d).
    try:
        await start_crawl(crawl, lambda: lazy_client(request.app.state))
    except Exception as exc:
        # No recovery on v2: a crawl whose workflow did not start is failed.
        logger.exception("crawl_start_failed", crawl_id=str(crawl.id))
        await fail_unstarted_crawl(db, crawl, exc)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"crawl_id": str(crawl.id), "error": crawl.error},
        ) from None

    logger.info("crawl_created", crawl_id=str(crawl.id), user_id=str(user.id))
    return CrawlResponse.model_validate(crawl)


@router.get("", response_model=list[CrawlResponse])
async def list_crawls(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CrawlResponse]:
    result = await db.execute(
        select(Crawl)
        .where(Crawl.user_id == user.id)
        .order_by(Crawl.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [CrawlResponse.model_validate(c) for c in result.scalars()]


@router.get("/{crawl_id}", response_model=CrawlResponse)
async def get_crawl(
    crawl_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CrawlResponse:
    return CrawlResponse.model_validate(await owned_crawl(db, crawl_id, user))


@router.get("/{crawl_id}/pages", response_model=list[CrawlPageResponse])
async def list_crawl_pages(
    crawl_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    page_status: str | None = Query(default=None, alias="status"),
) -> list[CrawlPageResponse]:
    await owned_crawl(db, crawl_id, user)
    return await list_pages(db, crawl_id, limit, offset, page_status)


@router.get("/{crawl_id}/pages/{page_id}/result", response_model=JobResultResponse)
async def get_crawl_page_result(
    crawl_id: uuid.UUID,
    page_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio_client: Minio = Depends(get_minio),
) -> JobResultResponse:
    await owned_crawl(db, crawl_id, user)
    return await page_result(db, minio_client, crawl_id, page_id)


@router.delete("/{crawl_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def cancel_crawl(
    crawl_id: uuid.UUID,
    request: Request,
    permanent: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio_client: Minio = Depends(get_minio),
) -> None:
    crawl = await owned_crawl(db, crawl_id, user)

    if permanent:
        if crawl.status not in _TERMINAL_STATUSES:
            # Its activities find the row gone and drop what they fetch.
            await cancel_crawl_workflow(crawl_id, lambda: lazy_client(request.app.state))
        # Objects before the DB row (external-before-internal), enumerated from the
        # ledger. This is the only way a user frees crawl storage — the cancel below
        # keeps every page on disk and charged.
        outcome = await release_crawl_objects(
            db, minio_client, crawl_id, "crawl object on permanent delete"
        )
        if outcome.failed:
            # Keep the crawl: its cascade would drop ledger rows for objects still on
            # disk. What was freed stays freed.
            await db.commit()
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"{outcome.failed} stored object(s) could not be removed; "
                    "the crawl was not deleted. Retry."
                ),
            )

        # Core delete so Postgres cascades (crawl_pages, crawl_queue, webhook_deliveries);
        # the ORM would first try to NULL the children's NOT NULL crawl_id.
        await db.execute(delete(Crawl).where(Crawl.id == crawl_id))
        await db.commit()
        logger.info(
            "crawl_permanently_deleted",
            crawl_id=str(crawl_id),
            user_id=str(user.id),
            objects_released=outcome.released,
            bytes_freed=outcome.freed_bytes,
        )
        return

    if crawl.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Crawl is already {crawl.status}",
        )

    # Row first, engine second (best effort), as on the job and batch lanes.
    if await cancel_crawl_rows(db, crawl_id) is None:
        await db.refresh(crawl)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Crawl is already {crawl.status}",
        )
    await cancel_crawl_workflow(crawl_id, lambda: lazy_client(request.app.state))
    logger.info("crawl_cancelled", crawl_id=str(crawl_id), user_id=str(user.id))


def _crawl_msg(crawl: Crawl) -> dict:
    return {
        "type": crawl.status if crawl.status in _TERMINAL_STATUSES else "crawl_progress",
        "crawl_id": str(crawl.id),
        "status": crawl.status,
        "total_queued": crawl.total_queued,
        "total_completed": crawl.total_completed,
        "total_failed": crawl.total_failed,
    }


@router.websocket("/{crawl_id}/watch")
async def crawl_status_stream(
    websocket: WebSocket,
    crawl_id: uuid.UUID,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Stream live crawl progress until the crawl reaches a terminal state.

    Each message carries absolute totals and, for a page outcome, the page. Auth and close-code
    semantics mirror /batch/{id}/watch.
    """
    await websocket.accept()

    user = await auth_from_token(token, db)
    if user is None:
        await websocket.close(code=4001, reason="unauthorized")
        return

    crawl = await db.get(Crawl, crawl_id)
    if crawl is None or crawl.user_id != user.id:
        await websocket.close(code=4004, reason="not found")
        return

    await websocket.send_json(_crawl_msg(crawl))
    if crawl.status in _TERMINAL_STATUSES:
        await websocket.close()
        return

    notifier = websocket.app.state.job_notifier
    try:
        async with notifier.subscribe_crawl(str(crawl_id), str(user.id)) as queue:
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=300)
                except TimeoutError:
                    await websocket.send_json({"type": "timeout"})
                    break
                status = update.get("status", "")
                msg = {
                    "type": status if status in _TERMINAL_STATUSES else "crawl_progress",
                    "crawl_id": str(crawl_id),
                    "status": status,
                    "total_queued": update.get("total_queued", 0),
                    "total_completed": update.get("total_completed", 0),
                    "total_failed": update.get("total_failed", 0),
                }
                if update.get("page_url"):
                    msg["latest_page"] = {
                        "url": update["page_url"],
                        "status": update.get("page_status", ""),
                    }
                await websocket.send_json(msg)
                if status in _TERMINAL_STATUSES:
                    break
    except WebSocketConnectionLimitExceeded:
        await websocket.close(code=4029, reason="connection limit exceeded")
        return

    await websocket.close()
