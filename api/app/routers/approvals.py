"""Approvals waiting for the user (ADR-016 §9b, PRD-018 R9). Answering is
``POST /pipelines/{pid}/runs/{rid}/approval``."""

import uuid

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user, get_current_user
from app.core import approvals
from app.core.db import get_db
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock
from app.models.user import User
from app.pipelines.catalog import APPROVAL
from app.schemas.approvals import AdminApprovalResponse, ApprovalResponse

router = APIRouter(prefix="/approvals", tags=["approvals"])
admin_router = APIRouter(prefix="/admin/approvals", tags=["admin"])
logger = structlog.get_logger()


async def approval_response(
    db: AsyncSession, run: PipelineRun, block: PipelineRunBlock, pipeline_name: str
) -> ApprovalResponse:
    return ApprovalResponse(
        pipeline_id=run.pipeline_id,
        pipeline_name=pipeline_name,
        run_id=run.id,
        block_id=block.block_id,
        monitor_id=run.monitor_id,
        run_status=run.status,
        state=approvals.approval_state(block),
        requested_at=block.started_at,
        deadline=block.approval_deadline,
        answered_at=block.answered_at,
        answered_by=block.answered_by,
        superseded_by_run_id=block.superseded_by_run_id,
        change_status=run.change_status,
        change_summary=run.change_summary,
        deliveries=await approvals.deliveries(db, run.id),
    )


def _waiting_select(*columns) -> Select:
    """Approvals waiting for an answer, soonest deadline first."""
    return (
        select(PipelineRunBlock, PipelineRun, Pipeline.name, *columns)
        .join(PipelineRun, PipelineRun.id == PipelineRunBlock.pipeline_run_id)
        .join(Pipeline, Pipeline.id == PipelineRun.pipeline_id)
        .where(
            PipelineRun.status == "running",
            PipelineRun.cancel_requested_at.is_(None),
            PipelineRunBlock.type == APPROVAL.name,
            PipelineRunBlock.status == "waiting",
            PipelineRunBlock.approval_deadline.is_not(None),
            PipelineRunBlock.approval_answer.is_(None),
        )
        .order_by(PipelineRunBlock.approval_deadline, PipelineRun.id)
    )


@router.get("", response_model=list[ApprovalResponse])
async def list_approvals(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ApprovalResponse]:
    """The user's approvals waiting for an answer, soonest deadline first."""
    rows = await db.execute(_waiting_select().where(PipelineRun.user_id == user.id))
    return [await approval_response(db, run, block, name) for block, run, name in rows.all()]


# ---------------------------------------------------------------------------
# Admin (PRD-018 R9: read-only — only the owner answers)
# ---------------------------------------------------------------------------


@admin_router.get("", response_model=list[AdminApprovalResponse])
async def admin_list_approvals(
    user_id: uuid.UUID | None = Query(default=None),
    monitor_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> list[AdminApprovalResponse]:
    """Every user's approvals waiting for an answer, soonest deadline first."""
    stmt = _waiting_select(User.email).join(User, User.id == PipelineRun.user_id)
    if user_id is not None:
        stmt = stmt.where(PipelineRun.user_id == user_id)
    if monitor_id is not None:
        stmt = stmt.where(PipelineRun.monitor_id == monitor_id)
    rows = await db.execute(stmt.limit(limit).offset(offset))
    return [
        AdminApprovalResponse(
            **(await approval_response(db, run, block, name)).model_dump(),
            user_id=run.user_id,
            user_email=email,
        )
        for block, run, name, email in rows.all()
    ]
