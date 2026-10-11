import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from miniopy_async import Minio
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.core.db import get_db
from app.core.minio import get_minio
from app.models.pipeline import Pipeline, PipelineRun
from app.models.user import User
from app.routers.pipelines import (
    latest_version_select,
    load_run_result,
    pipeline_response,
    run_blocks,
    run_response,
    run_summary,
    runs_select,
)
from app.schemas.pipelines import (
    AdminPipelineResponse,
    AdminPipelineRunResponse,
    AdminPipelineRunSummary,
    PipelineRunResultResponse,
)

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


async def _admin_run(db: AsyncSession, pipeline_id: uuid.UUID, run_id: uuid.UUID):
    row = (
        await db.execute(
            runs_select().where(PipelineRun.id == run_id, PipelineRun.pipeline_id == pipeline_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return row


@router.get("/{pipeline_id}/runs", response_model=list[AdminPipelineRunSummary])
async def admin_list_runs(
    pipeline_id: uuid.UUID,
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> list[AdminPipelineRunSummary]:
    stmt = runs_select().where(PipelineRun.pipeline_id == pipeline_id)
    if run_status is not None:
        stmt = stmt.where(PipelineRun.status == run_status)
    rows = (await db.execute(stmt.limit(limit).offset(offset))).all()
    return [
        AdminPipelineRunSummary(**run_summary(run, version).model_dump(), user_id=run.user_id)
        for run, version in rows
    ]


@router.get("/{pipeline_id}/runs/{run_id}", response_model=AdminPipelineRunResponse)
async def admin_get_run(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> AdminPipelineRunResponse:
    run, version = await _admin_run(db, pipeline_id, run_id)
    response = run_response(run, version, await run_blocks(db, run.id))
    return AdminPipelineRunResponse(**response.model_dump(), user_id=run.user_id)


@router.get("/{pipeline_id}/runs/{run_id}/result", response_model=PipelineRunResultResponse)
async def admin_get_run_result(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    block: str | None = Query(default=None),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    minio: Minio = Depends(get_minio),
) -> PipelineRunResultResponse:
    run, _ = await _admin_run(db, pipeline_id, run_id)
    return await load_run_result(db, minio, run, block)
