import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.core.db import get_db
from app.models.pipeline import Pipeline
from app.models.user import User
from app.routers.pipelines import latest_version_select, pipeline_response
from app.schemas.pipelines import AdminPipelineResponse

router = APIRouter(prefix="/admin/pipelines", tags=["admin"])


def _admin_select():
    return latest_version_select(User.email).join(User, User.id == Pipeline.user_id)


def _admin_response(pipeline, version, email) -> AdminPipelineResponse:
    return AdminPipelineResponse(
        **pipeline_response(pipeline, version).model_dump(),
        user_id=pipeline.user_id,
        user_email=email,
        deleted_at=pipeline.deleted_at,
    )


@router.get("", response_model=list[AdminPipelineResponse])
async def admin_list_pipelines(
    user_id: uuid.UUID | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> list[AdminPipelineResponse]:
    stmt = _admin_select()
    if user_id is not None:
        stmt = stmt.where(Pipeline.user_id == user_id)
    if not include_deleted:
        stmt = stmt.where(Pipeline.deleted_at.is_(None))
    stmt = stmt.order_by(Pipeline.created_at.desc()).limit(limit).offset(offset)
    return [_admin_response(*row) for row in (await db.execute(stmt)).all()]


@router.get("/{pipeline_id}", response_model=AdminPipelineResponse)
async def admin_get_pipeline(
    pipeline_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> AdminPipelineResponse:
    row = (await db.execute(_admin_select().where(Pipeline.id == pipeline_id))).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    return _admin_response(*row)
