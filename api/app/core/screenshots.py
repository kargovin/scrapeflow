"""Screenshots a page action stored (BUG-004): read from the storage ledger, never the bucket —
an object without a ledger row is not the user's."""

import re

import structlog
from fastapi import HTTPException, Response, status
from miniopy_async import Minio
from miniopy_async.error import S3Error
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage_object import StorageObject
from app.schemas.jobs import Screenshot

logger = structlog.get_logger()

# {bucket}/screenshots/{artifact_id}/{index}.png — playwright-worker/worker/storage.py
_KEY = re.compile(r"^[^/]+/screenshots/[^/]+/(\d+)\.png$")


async def _rows(db: AsyncSession, producer: ColumnElement[bool]) -> list[tuple[int, str, int]]:
    result = await db.execute(
        select(StorageObject.object_key, StorageObject.bytes).where(
            producer, StorageObject.object_key.like("%/screenshots/%")
        )
    )
    rows = []
    for key, size in result.all():
        match = _KEY.match(key)
        if match:
            rows.append((int(match.group(1)), key, size))
    return sorted(rows)


async def list_screenshots(db: AsyncSession, producer: ColumnElement[bool]) -> list[Screenshot]:
    """``producer`` selects the ledger rows of one producer, e.g.
    ``StorageObject.job_run_id == run.id``."""
    return [Screenshot(index=index, bytes=size) for index, _, size in await _rows(db, producer)]


async def screenshot_png(
    db: AsyncSession, minio: Minio, producer: ColumnElement[bool], index: int
) -> Response:
    """The PNG at ``index``; the caller has authorized the producer."""
    path = next((key for i, key, _ in await _rows(db, producer) if i == index), None)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Screenshot not found")
    bucket, _, key = path.partition("/")
    try:
        response = await minio.get_object(bucket, key)
        content = await response.read()
        response.close()
        await response.release()
    except S3Error as exc:
        if exc.code != "NoSuchKey":
            raise
        logger.error("screenshot_missing", path=path)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="The stored screenshot is missing"
        ) from None
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )
