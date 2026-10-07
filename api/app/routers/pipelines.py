import uuid
from asyncio import get_running_loop
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.auth.dependencies import get_current_user
from app.core.db import get_db
from app.core.quota import check_user_quota
from app.core.rate_limit import check_rate_limit
from app.core.security import validate_no_ssrf
from app.models.llm_keys import UserLLMKey
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.user import User
from app.pipelines.catalog import LLMExtractConfigV1
from app.pipelines.validator import (
    PipelineValidationError,
    Problem,
    ValidatedPipeline,
    validate,
)
from app.schemas.pipelines import (
    PipelineCreate,
    PipelineResponse,
    PipelineRunBlockResponse,
    PipelineRunCreate,
    PipelineRunResponse,
)
from app.workflows.client import get_temporal
from app.workflows.pipeline import (
    SUPPORTED_BLOCK_TYPES,
    LLMKey,
    PipelineRunInput,
    PipelineWorkflow,
    resolve_url,
)
from app.workflows.queues import WORKFLOW_QUEUE

router = APIRouter(prefix="/pipelines", tags=["pipelines"])
logger = structlog.get_logger()


def _assign_block_ids(definition: dict[str, Any]) -> None:
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if isinstance(block, dict) and "id" not in block:
            block["id"] = uuid.uuid4().hex[:8]


def _unprocessable(problems: list[Problem]) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[{"block_id": p.block_id, "message": p.message} for p in problems],
    )


def _secret_problems(validated: ValidatedPipeline) -> list[Problem]:
    # MVP: secrets are refused until the definition can hold them encrypted.
    return [
        Problem(block.id, f"{name} is not supported yet")
        for block in validated.blocks
        for name in sorted(block.type.secret_fields)
        if getattr(block.config, name) is not None
    ]


def _with_latest_version(user_id: uuid.UUID) -> Select:
    # return the pipeline joined with pipelineversion with the latest version
    other = aliased(PipelineVersion)
    latest = (
        select(func.max(other.version))
        .where(other.pipeline_id == Pipeline.id)
        .correlate(Pipeline)
        .scalar_subquery()
    )
    return (
        select(Pipeline, PipelineVersion)
        .join(
            PipelineVersion,
            (PipelineVersion.pipeline_id == Pipeline.id) & (PipelineVersion.version == latest),
        )
        .where(Pipeline.user_id == user_id, Pipeline.deleted_at.is_(None))
    )


def _response(pipeline: Pipeline, version: PipelineVersion) -> PipelineResponse:
    return PipelineResponse(
        id=pipeline.id,
        name=pipeline.name,
        version=version.version,
        definition=version.definition,
        created_at=pipeline.created_at,
        updated_at=pipeline.updated_at,
    )


@router.post("", response_model=PipelineResponse, status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    body: PipelineCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
) -> PipelineResponse:
    _assign_block_ids(body.definition)
    try:
        validated = validate(body.definition)
    except PipelineValidationError as exc:
        raise _unprocessable(exc.problems) from None

    if problems := _secret_problems(validated):
        raise _unprocessable(problems)

    key_ids = {
        b.config.llm_key_id for b in validated.blocks if isinstance(b.config, LLMExtractConfigV1)
    }
    if key_ids:
        owned = await db.scalars(
            select(UserLLMKey.id).where(UserLLMKey.id.in_(key_ids), UserLLMKey.user_id == user.id)
        )
        if key_ids - set(owned):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="LLM Key not found")

    pipeline = Pipeline(id=uuid.uuid4(), user_id=user.id, name=body.name)
    version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition=validated.definition)
    db.add_all([pipeline, version])
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_pipelines_user_id_name" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Pipeline name already in use"
            ) from None
        raise

    logger.info("pipeline_created", pipeline_id=str(pipeline.id), blocks=len(validated.blocks))
    return _response(pipeline, version)


@router.get("", response_model=list[PipelineResponse])
async def list_pipelines(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[PipelineResponse]:
    stmt = (
        _with_latest_version(user.id)
        .order_by(Pipeline.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).all()
    return [_response(pipeline, version) for pipeline, version in rows]


@router.get("/{pipeline_id}", response_model=PipelineResponse)
async def get_pipeline(
    pipeline_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineResponse:
    stmt = _with_latest_version(user.id).where(Pipeline.id == pipeline_id)
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    return _response(*row)


def _run_response(run: PipelineRun, blocks: list[PipelineRunBlock]) -> PipelineRunResponse:
    return PipelineRunResponse(
        id=run.id,
        pipeline_id=run.pipeline_id,
        pipeline_version_id=run.pipeline_version_id,
        status=run.status,
        inputs=run.inputs,
        result_path=run.result_path,
        error=run.error,
        created_at=run.created_at,
        finished_at=run.finished_at,
        blocks=[
            PipelineRunBlockResponse(
                block_id=b.block_id,
                position=b.position,
                type=b.type,
                status=b.status,
                input_ref=b.input_ref,
                output_ref=b.output_ref,
                error=b.error,
                started_at=b.started_at,
                finished_at=b.finished_at,
            )
            for b in blocks
        ],
    )


async def _ssrf_check(url: str) -> None:
    await get_running_loop().run_in_executor(None, validate_no_ssrf, url)


@router.post(
    "/{pipeline_id}/runs",
    response_model=PipelineRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(
    pipeline_id: uuid.UUID,
    body: PipelineRunCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    temporal: Client = Depends(get_temporal),
) -> PipelineRunResponse:
    stmt = _with_latest_version(user.id).where(Pipeline.id == pipeline_id)
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    pipeline, version = row
    definition = version.definition  # JSONB
    blocks = definition["blocks"]

    problems = [
        Problem(b["id"], f"block type {b['type']!r} cannot run yet")
        for b in blocks
        if b["type"] not in SUPPORTED_BLOCK_TYPES
    ]
    declared, given = set(definition["inputs"]), set(body.inputs)
    problems += [Problem(None, f"missing run input {n!r}") for n in sorted(declared - given)]
    problems += [Problem(None, f"unexpected run input {n!r}") for n in sorted(given - declared)]
    if problems:
        raise _unprocessable(problems)

    # The scrape workers do not check SSRF; this is the only check before the fetch.
    for b in blocks:
        if b["type"] == "scrape":
            await _ssrf_check(resolve_url(b["config"], body.inputs))

    llm_blocks = [b for b in blocks if b["type"] == "llm_extract"]
    keys: dict[str, UserLLMKey] = {}
    if llm_blocks:
        key_ids = {uuid.UUID(b["config"]["llm_key_id"]) for b in llm_blocks}
        owned = await db.scalars(
            select(UserLLMKey).where(UserLLMKey.id.in_(key_ids), UserLLMKey.user_id == user.id)
        )
        keys = {str(k.id): k for k in owned}
        if missing := [b for b in llm_blocks if b["config"]["llm_key_id"] not in keys]:
            raise _unprocessable([Problem(b["id"], "LLM key no longer exists") for b in missing])
        for key in keys.values():
            if key.base_url:
                await _ssrf_check(key.base_url)

    await check_user_quota(user.id, db, "monthly_runs")
    await check_user_quota(user.id, db, "concurrent_jobs")

    run_id = uuid.uuid4()
    run = PipelineRun(
        id=run_id,
        pipeline_id=pipeline.id,
        pipeline_version_id=version.id,
        user_id=user.id,
        status="running",
        inputs=body.inputs,
        workflow_id=f"pipeline-run-{run_id}",
    )
    run_blocks = [
        PipelineRunBlock(
            id=uuid.uuid4(),
            pipeline_run_id=run_id,
            block_id=b["id"],
            position=i,
            type=b["type"],
            status="pending",
        )
        for i, b in enumerate(blocks)
    ]
    db.add(run)
    db.add_all(run_blocks)
    # Rows before the start: a run that failed to start is visible; a workflow without rows is not.
    await db.commit()

    try:
        await temporal.start_workflow(
            PipelineWorkflow.run,
            PipelineRunInput(
                run_id=str(run_id),
                definition=definition,
                inputs=body.inputs,
                artifact_ids={b.block_id: str(b.id) for b in run_blocks},
                llm_keys={
                    key_id: LLMKey(
                        provider=k.provider,
                        encrypted_api_key=k.encrypted_api_key,
                        base_url=k.base_url,
                    )
                    for key_id, k in keys.items()
                },
            ),
            id=run.workflow_id,
            task_queue=WORKFLOW_QUEUE,
            # The default is ALLOW_DUPLICATE.
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except Exception as exc:
        logger.exception("pipeline_run_start_failed", run_id=str(run_id))
        run.status = "failed"
        run.error = f"could not start workflow: {exc}"
        run.finished_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"run_id": str(run_id), "error": run.error},
        ) from None

    logger.info("pipeline_run_started", run_id=str(run_id), pipeline_id=str(pipeline.id))
    return _run_response(run, run_blocks)


@router.get("/{pipeline_id}/runs/{run_id}", response_model=PipelineRunResponse)
async def get_run(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineRunResponse:
    run = await db.scalar(
        select(PipelineRun).where(
            PipelineRun.id == run_id,
            PipelineRun.pipeline_id == pipeline_id,
            PipelineRun.user_id == user.id,
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    blocks = (
        await db.scalars(
            select(PipelineRunBlock)
            .where(PipelineRunBlock.pipeline_run_id == run.id)
            .order_by(PipelineRunBlock.position)
        )
    ).all()
    return _run_response(run, list(blocks))
