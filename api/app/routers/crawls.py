import uuid
from asyncio import get_running_loop
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from miniopy_async import Minio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.db import get_db
from app.core.ledger import release_crawl_objects
from app.core.minio import get_minio
from app.core.quota import check_user_quota
from app.core.rate_limit import check_rate_limit
from app.core.security import validate_no_ssrf
from app.models.crawl import Crawl, CrawlPage, CrawlQueueItem
from app.models.user import User
from app.schemas.crawls import CrawlCreate, CrawlPageResponse, CrawlResponse

router = APIRouter(prefix="/crawls", tags=["crawls"])
logger = structlog.get_logger()

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# The values the coordinator actually writes to crawl_pages.status
# (dispatcher.py sets "pending"; result_handler.py sets the other three).
# Must track the writer — "processing" was accepted here but never written.
_PAGE_STATUSES = frozenset({"pending", "running", "completed", "failed"})


async def check_crawl_quota(
    body: CrawlCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """FastAPI dependency — raises 429 if any quota dimension is exceeded.

    A crawl is pre-checked against its declared ceiling exactly as a batch is against
    len(urls): monthly_runs with batch_count=max_pages, since every page is one attempted
    fetch. Concurrency is one slot for the whole crawl (ADR-009 §8). The meter charges
    actuals — crawl_pages rows as the coordinator creates them.
    """
    await check_user_quota(user.id, db, "monthly_runs", batch_count=body.max_pages)
    await check_user_quota(user.id, db, "concurrent_jobs")
    await check_user_quota(user.id, db, "storage_bytes")


@router.post("", response_model=CrawlResponse, status_code=http_status.HTTP_201_CREATED)
async def create_crawl(
    body: CrawlCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    _quota: None = Depends(check_crawl_quota),
) -> CrawlResponse:
    seed_url = str(body.seed_url)
    loop = get_running_loop()
    await loop.run_in_executor(None, validate_no_ssrf, seed_url)
    if body.webhook_url:
        await loop.run_in_executor(None, validate_no_ssrf, str(body.webhook_url))

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
    await db.flush()

    db.add(CrawlQueueItem(crawl_id=crawl.id, url=seed_url, depth=0))
    await db.commit()
    await db.refresh(crawl)

    logger.info("crawl_created", crawl_id=str(crawl.id), user_id=str(user.id))
    return CrawlResponse.model_validate(crawl)


@router.get("/{crawl_id}", response_model=CrawlResponse)
async def get_crawl(
    crawl_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CrawlResponse:
    crawl = await db.get(Crawl, crawl_id)
    if crawl is None or crawl.user_id != user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Crawl not found")
    return CrawlResponse.model_validate(crawl)


@router.get("/{crawl_id}/pages", response_model=list[CrawlPageResponse])
async def list_crawl_pages(
    crawl_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    page_status: str | None = Query(default=None, alias="status"),
) -> list[CrawlPageResponse]:
    crawl = await db.get(Crawl, crawl_id)
    if crawl is None or crawl.user_id != user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Crawl not found")

    stmt = (
        select(CrawlPage)
        .where(CrawlPage.crawl_id == crawl_id)
        .order_by(CrawlPage.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    if page_status is not None:
        if page_status not in _PAGE_STATUSES:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Page Status not valid",
            )
        stmt = stmt.where(CrawlPage.status == page_status)

    result = await db.execute(stmt)
    return [CrawlPageResponse.model_validate(page) for page in result.scalars()]


@router.delete("/{crawl_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def cancel_crawl(
    crawl_id: uuid.UUID,
    permanent: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio_client: Minio = Depends(get_minio),
) -> None:
    crawl = await db.get(Crawl, crawl_id)
    if crawl is None or crawl.user_id != user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Crawl not found")

    if permanent:
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

    crawl.status = "cancelled"
    await db.execute(
        update(CrawlQueueItem)
        .where(
            CrawlQueueItem.crawl_id == crawl_id,
            CrawlQueueItem.status == "pending",
        )
        .values(status="skipped")
    )
    await db.commit()
    logger.info("crawl_cancelled", crawl_id=str(crawl_id), user_id=str(user.id))
