import asyncio
import copy
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket, status
from miniopy_async import Minio
from miniopy_async.error import S3Error
from sqlalchemy import Select, delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.auth.dependencies import auth_from_token, get_current_user
from app.core.db import get_db
from app.core.job_notifier import WebSocketConnectionLimitExceeded
from app.core.ledger import ReleaseOutcome, release_pipeline_run_objects
from app.core.minio import get_minio
from app.core.pipeline_status import notify_run
from app.core.pipeline_trigger import (
    build_run,
    encrypt_secret,
    mark_not_started,
    owned_llm_keys,
    ssrf_check,
)
from app.core.quota import admit, lock_user
from app.core.rate_limit import check_rate_limit
from app.models.monitor import Monitor
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.user import User
from app.models.webhook_delivery import WebhookDelivery
from app.pipelines.catalog import (
    CATALOG,
    InputBinding,
    LLMExtractConfigV1,
    ScrapeConfigV1,
    WebhookConfigV1,
)
from app.pipelines.definition import (
    DefinitionUnreadable,
    LLMExtractBlock,
    ScrapeBlock,
    WebhookBlock,
    input_value_problems,
    parse_definition,
)
from app.pipelines.validator import (
    PipelineValidationError,
    Problem,
    ValidatedPipeline,
    validate,
)
from app.schemas.monitors import MonitorRef
from app.schemas.pipelines import (
    OutputState,
    PipelineCreate,
    PipelineResponse,
    PipelineRunAlert,
    PipelineRunBlockResponse,
    PipelineRunCancelResponse,
    PipelineRunCreate,
    PipelineRunResponse,
    PipelineRunResultResponse,
    PipelineRunSummary,
    PipelineUpdate,
    PipelineVersionResponse,
)
from app.settings import settings
from app.workflows.client import get_temporal, get_temporal_or_none
from app.workflows.pipeline import PipelineWorkflow
from app.workflows.queues import WORKFLOW_QUEUE

router = APIRouter(prefix="/pipelines", tags=["pipelines"])
logger = structlog.get_logger()


def require_pipelines_enabled() -> None:
    if not settings.pipelines_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Pipelines are switched off"
        )


# ---------------------------------------------------------------------------
# Definitions: what is stored, what is returned
# ---------------------------------------------------------------------------


def unprocessable(problems: list[Problem]) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[{"block_id": p.block_id, "message": p.message} for p in problems],
    )


def _raw_blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = raw.get("blocks")
    return [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []


def _assign_block_ids(raw: dict[str, Any]) -> None:
    taken = {b["id"] for b in _raw_blocks(raw) if isinstance(b.get("id"), str)}
    for block in _raw_blocks(raw):
        if "id" not in block:
            while (new_id := uuid.uuid4().hex[:8]) in taken:
                pass
            taken.add(new_id)
            block["id"] = new_id


def _given_secrets(raw: dict[str, Any]) -> dict[str, set[str]]:
    """Secret fields present in each block's submitted config — present = set or clear."""
    given: dict[str, set[str]] = {}
    for block in _raw_blocks(raw):
        block_type = CATALOG.get(t) if isinstance(t := block.get("type"), str) else None
        config = block.get("config")
        if block_type and isinstance(config, dict) and isinstance(block.get("id"), str):
            given[block["id"]] = {n for n in block_type.secret_fields if n in config}
    return given


def _store_secrets(
    validated: ValidatedPipeline,
    given: dict[str, set[str]],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """Moves secret config values out of ``config`` into ``secrets``, encrypted.

    A kept block (same id as in ``previous``) that does not send a secret field keeps its
    stored ciphertext; sending the field sets it, or clears it when empty.
    """
    kept = {b["id"]: b.get("secrets", {}) for b in (previous or {}).get("blocks", [])}
    stored = copy.deepcopy(validated.definition)
    for block, parsed in zip(stored["blocks"], validated.blocks, strict=True):
        secrets: dict[str, str] = {}
        for name in sorted(parsed.type.secret_fields):
            if name in given.get(block["id"], set()):
                value = getattr(parsed.config, name)
                if value:
                    secrets[name] = encrypt_secret(value)
            elif name in kept.get(block["id"], {}):
                secrets[name] = kept[block["id"]][name]
            block["config"][name] = None
        block["secrets"] = secrets
    return stored


def _public_definition(stored: dict[str, Any]) -> dict[str, Any]:
    """The stored definition as the API returns it: secrets named, never their values."""
    public = copy.deepcopy(stored)
    for block in public.get("blocks", []):
        block_type = CATALOG.get(block.get("type"))
        for name in block_type.secret_fields if block_type else ():
            block.get("config", {}).pop(name, None)
        block["secrets"] = sorted(block.get("secrets", {}))
    return public


async def _prepare_definition(
    raw: dict[str, Any], user: User, db: AsyncSession, previous: dict[str, Any] | None
) -> dict[str, Any]:
    """Validates a submitted definition and returns what to store as a new version."""
    for block in _raw_blocks(raw):
        block.pop("secrets", None)  # GET's read-only list; a round trip sends it back
    _assign_block_ids(raw)
    given = _given_secrets(raw)
    try:
        validated = validate(raw)
    except PipelineValidationError as exc:
        raise unprocessable(exc.problems) from None

    key_ids = {
        b.config.llm_key_id for b in validated.blocks if isinstance(b.config, LLMExtractConfigV1)
    }
    keys = await owned_llm_keys(db, user.id, key_ids)
    if len(keys) != len(key_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="LLM Key not found")

    # Fail at save on what is known now; the trigger checks again (DNS can change).
    for block in validated.blocks:
        config = block.config
        if isinstance(config, ScrapeConfigV1 | WebhookConfigV1) and not isinstance(
            config.url, InputBinding
        ):
            await ssrf_check(str(config.url))
    for key in keys.values():
        if key.base_url:
            await ssrf_check(key.base_url)

    return _store_secrets(validated, given, previous)


# ---------------------------------------------------------------------------
# Pipelines CRUD (C.4)
# ---------------------------------------------------------------------------


def latest_version_select(*columns: Any) -> Select:
    """Each pipeline joined to its latest version, plus ``columns``. Callers add the tenant filter."""
    other = aliased(PipelineVersion)
    latest = (
        select(func.max(other.version))
        .where(other.pipeline_id == Pipeline.id)
        .correlate(Pipeline)
        .scalar_subquery()
    )
    return (
        select(Pipeline, PipelineVersion, *columns)
        .select_from(Pipeline)
        .join(
            PipelineVersion,
            (PipelineVersion.pipeline_id == Pipeline.id) & (PipelineVersion.version == latest),
        )
    )


def _owned_latest(user_id: uuid.UUID) -> Select:
    return latest_version_select().where(Pipeline.user_id == user_id, Pipeline.deleted_at.is_(None))


async def _owned_pipeline(db: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID):
    row = (await db.execute(_owned_latest(user_id).where(Pipeline.id == pipeline_id))).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    return row


def pipeline_response(pipeline: Pipeline, version: PipelineVersion) -> PipelineResponse:
    return PipelineResponse(
        id=pipeline.id,
        name=pipeline.name,
        version=version.version,
        definition=_public_definition(version.definition),
        created_at=pipeline.created_at,
        updated_at=pipeline.updated_at,
    )


def _name_conflict(exc: IntegrityError) -> HTTPException | None:
    if "uq_pipelines_user_id_name" in str(exc.orig):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Pipeline name already in use"
        )
    if "uq_pipeline_versions_pipeline_id_version" in str(exc.orig):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The pipeline was updated at the same time; fetch it and retry",
        )
    return None


@router.post("", response_model=PipelineResponse, status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    body: PipelineCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    __: None = Depends(require_pipelines_enabled),
) -> PipelineResponse:
    held = await db.scalar(
        select(func.count())
        .select_from(Pipeline)
        .where(Pipeline.user_id == user.id, Pipeline.deleted_at.is_(None))
    )
    limit = settings.max_pipelines_per_user
    if (held or 0) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "quota_exceeded",
                "quota_type": "pipelines",
                "message": f"Pipeline limit reached ({held}/{limit}). Delete one to continue.",
                "resets_at": None,
            },
        )

    definition = await _prepare_definition(body.definition, user, db, previous=None)

    # IDs generated here so both INSERTs run inside commit(), where the IntegrityError is caught.
    pipeline = Pipeline(id=uuid.uuid4(), user_id=user.id, name=body.name)
    version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition=definition)
    db.add_all([pipeline, version])
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if conflict := _name_conflict(exc):
            raise conflict from None
        raise

    logger.info("pipeline_created", pipeline_id=str(pipeline.id), blocks=len(definition["blocks"]))
    response = pipeline_response(pipeline, version)
    response.monitors = []
    return response


@router.get("", response_model=list[PipelineResponse])
async def list_pipelines(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[PipelineResponse]:
    stmt = _owned_latest(user.id).order_by(Pipeline.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).all()
    return [pipeline_response(pipeline, version) for pipeline, version in rows]


@router.get("/{pipeline_id}", response_model=PipelineResponse)
async def get_pipeline(
    pipeline_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineResponse:
    response = pipeline_response(*await _owned_pipeline(db, user.id, pipeline_id))
    response.monitors = await monitors_using(db, pipeline_id)
    return response


async def monitors_using(db: AsyncSession, pipeline_id: uuid.UUID) -> list[MonitorRef]:
    rows = await db.scalars(
        select(Monitor).where(Monitor.pipeline_id == pipeline_id).order_by(Monitor.name)
    )
    return [MonitorRef(id=m.id, name=m.name, state=m.state) for m in rows]


@router.patch("/{pipeline_id}", response_model=PipelineResponse)
async def update_pipeline(
    pipeline_id: uuid.UUID,
    body: PipelineUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    __: None = Depends(require_pipelines_enabled),
) -> PipelineResponse:
    pipeline, version = await _owned_pipeline(db, user.id, pipeline_id)
    changed = False
    if body.name is not None and body.name != pipeline.name:
        pipeline.name = body.name
        changed = True
    if body.definition is not None:
        definition = await _prepare_definition(body.definition, user, db, version.definition)
        # An unchanged definition is not a new version.
        if definition != version.definition:
            version = PipelineVersion(
                pipeline_id=pipeline.id, version=version.version + 1, definition=definition
            )
            db.add(version)
            changed = True
    if not changed:
        response = pipeline_response(pipeline, version)
        response.monitors = await monitors_using(db, pipeline.id)
        return response

    pipeline.updated_at = datetime.now(UTC)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if conflict := _name_conflict(exc):
            raise conflict from None
        raise
    logger.info("pipeline_updated", pipeline_id=str(pipeline.id), version=version.version)
    response = pipeline_response(pipeline, version)
    response.monitors = await monitors_using(db, pipeline.id)
    return response


@router.get("/{pipeline_id}/versions", response_model=list[PipelineVersionResponse])
async def list_versions(
    pipeline_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PipelineVersionResponse]:
    await _owned_pipeline(db, user.id, pipeline_id)
    versions = await db.scalars(
        select(PipelineVersion)
        .where(PipelineVersion.pipeline_id == pipeline_id)
        .order_by(PipelineVersion.version.desc())
    )
    return [
        PipelineVersionResponse(
            version=v.version,
            definition=_public_definition(v.definition),
            created_at=v.created_at,
        )
        for v in versions
    ]


@router.delete("/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(
    pipeline_id: uuid.UUID,
    permanent: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio: Minio = Depends(get_minio),
    temporal: Client | None = Depends(get_temporal_or_none),
) -> Response:
    """No runs: hard delete. Runs: soft delete — runs and their artifacts stay, the name is held.

    ``permanent``: every run, its stored objects and the pipeline go (ADR-016 §11) — a
    soft-deleted pipeline included.
    """
    user_id = user.id  # the rollback below expires every instance in the session
    if permanent:
        return await _delete_permanently(db, minio, temporal, user_id, pipeline_id)
    # A monitor's create takes the lock too, so it cannot land between the check and the delete.
    await lock_user(user_id, db)
    pipeline, _ = await _owned_pipeline(db, user_id, pipeline_id)
    await refuse_if_monitored(db, pipeline.id)
    has_runs = await db.scalar(select(exists().where(PipelineRun.pipeline_id == pipeline.id)))
    if not has_runs:
        try:
            await db.execute(delete(Pipeline).where(Pipeline.id == pipeline.id))
            await db.commit()
            logger.info("pipeline_deleted", pipeline_id=str(pipeline_id), mode="hard")
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except IntegrityError:
            # A run was inserted since the check; its FK refuses the delete.
            await db.rollback()
            await lock_user(user_id, db)
            pipeline, _ = await _owned_pipeline(db, user_id, pipeline_id)
            await refuse_if_monitored(db, pipeline.id)

    now = datetime.now(UTC)
    pipeline.deleted_at = now
    pipeline.updated_at = now
    await db.commit()
    logger.info("pipeline_deleted", pipeline_id=str(pipeline_id), mode="soft")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def refuse_if_monitored(db: AsyncSession, pipeline_id: uuid.UUID) -> None:
    """ADR-016 §2a: 409 naming every monitor using the pipeline, whatever its state. The caller
    holds ``lock_user``."""
    monitors = await monitors_using(db, pipeline_id)
    if monitors:
        names = ", ".join(m.name for m in monitors)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "pipeline_in_use",
                "message": f"Monitors use this pipeline: {names}. Delete them first.",
                "monitors": [m.model_dump(mode="json") for m in monitors],
            },
        )


async def request_cancel(db: AsyncSession, temporal: Client | None, runs: list[PipelineRun]):
    """Row first, then the engine, best effort: a run left without a row reads as cancelled."""
    running = [r for r in runs if r.status == "running"]
    now = datetime.now(UTC)
    for run in running:
        if run.cancel_requested_at is None:
            run.cancel_requested_at = now
            await notify_run(db, run)
    if temporal is None:
        return
    for run in running:
        try:
            await temporal.get_workflow_handle(run.workflow_id).cancel()
        except Exception as exc:
            logger.warning("pipeline_run_cancel_not_sent", run_id=str(run.id), error=str(exc))


async def release_runs(
    db: AsyncSession, minio: Minio, temporal: Client | None, runs: list[PipelineRun]
) -> ReleaseOutcome:
    """The caller holds each run row locked. Objects first; on any left, commits what was freed
    and refuses — the rows' cascade would drop ledger rows for objects still on disk."""
    await request_cancel(db, temporal, runs)
    outcome = ReleaseOutcome()
    for run in runs:
        outcome.merge(
            await release_pipeline_run_objects(db, minio, run.id, "pipeline run on delete")
        )
    if outcome.failed:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{outcome.failed} stored objects could not be removed; nothing was deleted. Retry."
            ),
        )
    return outcome


async def _delete_permanently(
    db: AsyncSession,
    minio: Minio,
    temporal: Client | None,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> Response:
    # ⚠️ Held through the object deletes: a run admitted meanwhile would outlive its pipeline.
    await lock_user(user_id, db)
    pipeline = await db.scalar(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == user_id)
    )
    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    await refuse_if_monitored(db, pipeline_id)
    runs = list(
        await db.scalars(
            select(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id).with_for_update()
        )
    )
    outcome = await release_runs(db, minio, temporal, runs)
    await db.execute(delete(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id))
    await db.execute(delete(Pipeline).where(Pipeline.id == pipeline_id))
    await db.commit()
    logger.info(
        "pipeline_deleted",
        pipeline_id=str(pipeline_id),
        mode="permanent",
        runs=len(runs),
        objects_released=outcome.released,
        bytes_freed=outcome.freed_bytes,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Runs (C.5)
# ---------------------------------------------------------------------------


def output_state(block: PipelineRunBlock) -> OutputState:
    if block.collected_at is not None:
        return "collected"
    if block.output_ref is None:
        return "none"
    block_type = CATALOG.get(block.type)
    return "passed_through" if block_type and block_type.kind == "effect" else "stored"


def block_status_reason(block: PipelineRunBlock) -> str | None:
    if block.status == "skipped" and block.status_reason is None:
        return "not_recorded"
    return block.status_reason


def run_summary(run: PipelineRun, version: int) -> PipelineRunSummary:
    return PipelineRunSummary(
        id=run.id,
        pipeline_id=run.pipeline_id,
        pipeline_version_id=run.pipeline_version_id,
        version=version,
        status=run.status,
        cancel_requested_at=run.cancel_requested_at,
        inputs=run.inputs,
        result_path=run.result_path,
        error=run.error,
        created_at=run.created_at,
        finished_at=run.finished_at,
        stopped_at_block_id=run.stopped_at_block_id,
        stop_reason=run.stop_reason,
        failed_block_id=run.failed_block_id,
        rollback_deadline=run.rollback_deadline,
    )


def run_response(
    run: PipelineRun,
    version: int,
    blocks: list[PipelineRunBlock],
    alerts: list[WebhookDelivery] = (),
) -> PipelineRunResponse:
    return PipelineRunResponse(
        **run_summary(run, version).model_dump(),
        alerts=[
            PipelineRunAlert(
                block_id=a.block_id,
                status=a.status,
                attempts=a.attempts,
                last_error=a.last_error,
                next_attempt_at=a.next_attempt_at,
                delivered_at=a.delivered_at,
                created_at=a.created_at,
            )
            for a in alerts
        ],
        blocks=[
            PipelineRunBlockResponse(
                block_id=b.block_id,
                position=b.position,
                type=b.type,
                status=b.status,
                input_ref=b.input_ref,
                output_ref=b.output_ref,
                output_state=output_state(b),
                error=b.error,
                started_at=b.started_at,
                finished_at=b.finished_at,
                collected_at=b.collected_at,
                status_reason=block_status_reason(b),
                reason_block_id=b.reason_block_id,
                verdict=b.verdict,
                delivery=b.delivery,
                undo_status=b.undo_status,
                undo_error=b.undo_error,
            )
            for b in blocks
        ],
    )


async def run_alerts(db: AsyncSession, run_id: uuid.UUID) -> list[WebhookDelivery]:
    return list(
        (
            await db.scalars(
                select(WebhookDelivery)
                .where(WebhookDelivery.pipeline_run_id == run_id)
                .order_by(WebhookDelivery.created_at, WebhookDelivery.block_id)
            )
        ).all()
    )


async def version_to_run(
    db: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID, version: int | None
) -> tuple[Pipeline, PipelineVersion]:
    if version is None:
        pipeline, latest = await _owned_pipeline(db, user_id, pipeline_id)
        return pipeline, latest
    row = (
        await db.execute(
            select(Pipeline, PipelineVersion)
            .join(PipelineVersion, PipelineVersion.pipeline_id == Pipeline.id)
            .where(
                Pipeline.id == pipeline_id,
                Pipeline.user_id == user_id,
                Pipeline.deleted_at.is_(None),
                PipelineVersion.version == version,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline version not found"
        )
    return row[0], row[1]


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
    __: None = Depends(require_pipelines_enabled),
    temporal: Client = Depends(get_temporal),
) -> PipelineRunResponse:
    # The ownership check is the only tenant boundary (ADR-009 §12).
    pipeline, version = await version_to_run(db, user.id, pipeline_id, body.version)

    # Parse, do not re-validate: a pinned version keeps the rules of the day it was saved.
    try:
        parsed, stored_secrets = parse_definition(version.definition)
    except DefinitionUnreadable as exc:
        logger.error(
            "pipeline_version_unreadable", pipeline_version_id=str(version.id), error=str(exc)
        )
        raise unprocessable(
            [Problem(b, f"this version cannot be run: {m}") for b, m in exc.problems]
        ) from None

    declared, given = set(parsed.inputs), set(body.inputs)
    problems = [Problem(None, f"missing run input {n!r}") for n in sorted(declared - given)]
    problems += [Problem(None, f"unexpected run input {n!r}") for n in sorted(given - declared)]
    problems += [Problem(b, m) for b, m in input_value_problems(parsed, body.inputs)]
    if problems:
        raise unprocessable(problems)

    # The scrape workers do not check SSRF; this is the only check before the fetch.
    # The Webhook activity checks again on every attempt; this one fails the run up front.
    for block in parsed.blocks:
        if isinstance(block, ScrapeBlock):
            url = block.config.url
            await ssrf_check(
                str(body.inputs[url.input]) if isinstance(url, InputBinding) else str(url)
            )
        elif isinstance(block, WebhookBlock):
            await ssrf_check(str(block.config.url))

    llm_blocks = [b for b in parsed.blocks if isinstance(b, LLMExtractBlock)]
    keys = await owned_llm_keys(db, user.id, {b.config.llm_key_id for b in llm_blocks})
    if missing := [b for b in llm_blocks if str(b.config.llm_key_id) not in keys]:
        raise unprocessable([Problem(b.id, "LLM key no longer exists") for b in missing])
    for key in keys.values():
        if key.base_url:
            await ssrf_check(key.base_url)

    # §8d: output size is unknowable up front, so refuse to start near the wall.
    await admit(user.id, db, headroom_bytes=settings.storage_headroom_bytes)
    # Read again under the lock: a permanent delete holds it until the pipeline is gone.
    if (
        await db.scalar(
            select(Pipeline.id).where(Pipeline.id == pipeline.id, Pipeline.deleted_at.is_(None))
        )
        is None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")

    built = build_run(
        run_id=uuid.uuid4(),
        user_id=user.id,
        version=version,
        parsed=parsed,
        stored_secrets=stored_secrets,
        keys=keys,
        inputs=body.inputs,
    )
    run, run_blocks, run_id = built.run, built.blocks, built.run.id
    db.add(run)
    db.add_all(run_blocks)
    # Rows before the start: a run that failed to start is visible; a workflow without rows is not.
    await db.commit()

    try:
        await temporal.start_workflow(
            PipelineWorkflow.run,
            built.input,
            id=run.workflow_id,
            task_queue=WORKFLOW_QUEUE,
            # The default is ALLOW_DUPLICATE.
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except Exception as exc:
        logger.exception("pipeline_run_start_failed", run_id=str(run_id))
        await mark_not_started(db, run, run_blocks, exc, lambda: temporal)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"run_id": str(run_id), "error": run.error},
        ) from None

    logger.info(
        "pipeline_run_started",
        run_id=str(run_id),
        pipeline_id=str(pipeline.id),
        version=version.version,
    )
    return run_response(run, version.version, run_blocks)


def _owned_run(pipeline_id: uuid.UUID, run_id: uuid.UUID, user_id: uuid.UUID) -> Select:
    return select(PipelineRun).where(
        PipelineRun.id == run_id,
        PipelineRun.pipeline_id == pipeline_id,
        PipelineRun.user_id == user_id,
    )


async def version_number(db: AsyncSession, run: PipelineRun) -> int:
    version = await db.scalar(
        select(PipelineVersion.version).where(PipelineVersion.id == run.pipeline_version_id)
    )
    assert version is not None  # FK
    return version


async def run_blocks(db: AsyncSession, run_id: uuid.UUID) -> list[PipelineRunBlock]:
    blocks = await db.scalars(
        select(PipelineRunBlock)
        .where(PipelineRunBlock.pipeline_run_id == run_id)
        .order_by(PipelineRunBlock.position)
    )
    return list(blocks)


@router.get("/{pipeline_id}/runs/{run_id}", response_model=PipelineRunResponse)
async def get_run(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineRunResponse:
    run = await db.scalar(_owned_run(pipeline_id, run_id, user.id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run_response(
        run,
        await version_number(db, run),
        await run_blocks(db, run.id),
        await run_alerts(db, run.id),
    )


async def _budget_left(
    db: AsyncSession,
    run: PipelineRun,
    active: list[PipelineRunBlock],
    now: datetime,
    workflow_notified: bool,
) -> int:
    version = await db.get(PipelineVersion, run.pipeline_version_id)
    try:
        parsed, _ = parse_definition(version.definition if version else None)
    except DefinitionUnreadable:
        return 0
    by_id = {b.id: b for b in parsed.blocks}
    left = 0
    for row in active:
        block = by_id.get(row.block_id)
        if block is None:
            continue
        block_type = CATALOG[block.type]
        timing = block_type.timing(block.config)
        if workflow_notified and block_type.kind == "effect":
            # The engine ends its retry wait; at most the attempt in flight finishes.
            left = max(left, timing.start_to_close)
            continue
        elapsed = (now - row.started_at).total_seconds() if row.started_at else 0
        left = max(left, int(timing.budget - elapsed))
    return max(left, 0)


@router.post(
    "/{pipeline_id}/runs/{run_id}/cancel",
    response_model=PipelineRunCancelResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_run(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    temporal: Client | None = Depends(get_temporal_or_none),
) -> PipelineRunCancelResponse:
    """Requests the cancel; the row turns ``cancelled`` only once the run has stopped (R3).

    Row first (instant UI, works with the engine down), then a best-effort workflow cancel that
    interrupts a retry wait (ADR-009 15d); the mirror's boundary check is the fallback.
    """
    run = await db.scalar(_owned_run(pipeline_id, run_id, user.id).with_for_update())
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"The run is already {run.status}"
        )
    now = datetime.now(UTC)
    if run.cancel_requested_at is None:
        run.cancel_requested_at = now
        await notify_run(db, run)
    await db.commit()

    workflow_notified = False
    if temporal is not None:
        try:
            await temporal.get_workflow_handle(run.workflow_id).cancel()
            workflow_notified = True
        except Exception as exc:
            logger.warning("pipeline_run_cancel_not_sent", run_id=str(run.id), error=str(exc))

    blocks = await run_blocks(db, run.id)
    active = [b for b in blocks if b.status in ("running", "waiting")]
    logger.info("pipeline_run_cancel_requested", run_id=str(run.id), notified=workflow_notified)
    return PipelineRunCancelResponse(
        run=run_response(run, await version_number(db, run), blocks),
        still_running=[b.block_id for b in active],
        upper_bound_seconds=await _budget_left(db, run, active, now, workflow_notified),
        llm_billed=any(b.type == "llm_extract" and b.started_at for b in blocks),
        workflow_notified=workflow_notified,
    )


@router.delete("/{pipeline_id}/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio: Minio = Depends(get_minio),
    temporal: Client | None = Depends(get_temporal_or_none),
) -> Response:
    """The run, its stored objects, block rows and alerts (ADR-016 §11). A run still going is
    cancelled and deleted now; what its workflow writes after this is dropped."""
    run = await db.scalar(_owned_run(pipeline_id, run_id, user.id).with_for_update())
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    outcome = await release_runs(db, minio, temporal, [run])
    await db.execute(delete(PipelineRun).where(PipelineRun.id == run_id))
    await db.commit()
    logger.info(
        "pipeline_run_deleted",
        run_id=str(run_id),
        objects_released=outcome.released,
        bytes_freed=outcome.freed_bytes,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Run reads: list, result, live status (C.11)
# ---------------------------------------------------------------------------


def runs_select() -> Select:
    """Runs with their version number, newest first. Callers add the tenant filter."""
    return (
        select(PipelineRun, PipelineVersion.version)
        .join(PipelineVersion, PipelineVersion.id == PipelineRun.pipeline_version_id)
        .order_by(PipelineRun.created_at.desc())
    )


@router.get("/{pipeline_id}/runs", response_model=list[PipelineRunSummary])
async def list_runs(
    pipeline_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[PipelineRunSummary]:
    stmt = runs_select().where(
        PipelineRun.pipeline_id == pipeline_id, PipelineRun.user_id == user.id
    )
    if run_status is not None:
        stmt = stmt.where(PipelineRun.status == run_status)
    rows = (await db.execute(stmt.limit(limit).offset(offset))).all()
    return [run_summary(run, version) for run, version in rows]


async def load_run_result(
    db: AsyncSession, minio: Minio, run: PipelineRun, block_id: str | None
) -> PipelineRunResultResponse:
    """The run's result, or one block's stored output. The caller has authorized ``run``:
    a result path is bare, so nothing may resolve one before the ownership check (§12)."""
    blocks = await run_blocks(db, run.id)
    if block_id is None:
        if run.result_path is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="The run has no result"
            )
        path = run.result_path
        # Effect blocks after it pass the same reference on; the first holder produced it.
        block_id = next(b.block_id for b in blocks if b.output_ref == path)
    else:
        block = next((b for b in blocks if b.block_id == block_id), None)
        if block is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")
        state = output_state(block)
        if state == "collected":
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"This block's output was collected at {block.collected_at.isoformat()}",
            )
        if state == "passed_through":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This block stores nothing; it passed its input on",
            )
        if state == "none":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="This block produced no output"
            )
        path = block.output_ref

    bucket, _, key = path.partition("/")
    try:
        response = await minio.get_object(bucket, key)
        content = await response.read()
        response.close()
        await response.release()
    except S3Error as exc:
        if exc.code != "NoSuchKey":
            raise
        logger.error("pipeline_result_missing", run_id=str(run.id), path=path)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="The stored output is missing"
        ) from None
    return PipelineRunResultResponse(
        content=content.decode("utf-8", errors="replace"), result_path=path, block_id=block_id
    )


@router.get("/{pipeline_id}/runs/{run_id}/result", response_model=PipelineRunResultResponse)
async def get_run_result(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    block: str | None = Query(default=None, description="A block id: that step's stored output"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio: Minio = Depends(get_minio),
) -> PipelineRunResultResponse:
    run = await db.scalar(_owned_run(pipeline_id, run_id, user.id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return await load_run_result(db, minio, run, block)


_TERMINAL = frozenset({"completed", "failed", "cancelled"})
WATCH_TIMEOUT_SECONDS = 300


def _watch_message(pipeline_id: str, update: dict) -> dict:
    run_status = update["status"]
    message = {
        "type": run_status if run_status in _TERMINAL else "status_update",
        "run_id": update["run_id"],
        "status": run_status,
        "cancel_requested": update.get("cancel_requested", False),
        "stopped_at_block_id": update.get("stopped_at_block_id"),
        "rolling_back": update.get("rolling_back", False),
        "change_status": update.get("change_status"),
        "block_id": update.get("block_id"),
        "block_status": update.get("block_status"),
        "block_status_reason": update.get("block_status_reason"),
        "block_undo_status": update.get("block_undo_status"),
    }
    if run_status == "completed":
        message["result_url"] = f"/pipelines/{pipeline_id}/runs/{update['run_id']}/result"
    return message


@router.websocket("/{pipeline_id}/runs/{run_id}/watch")
async def run_status_stream(
    websocket: WebSocket,
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Live status for one run until it is terminal, as ``/jobs/{id}/watch`` does for a job.

    Auth: ?token= (API key or Clerk JWT). Close codes: 4001 unauthorized, 4004 not found,
    4029 connection limit. A ``timeout`` message after 300 s idle; the client reconnects (11b).
    """
    await websocket.accept()
    user = await auth_from_token(token, db)
    if user is None:
        await websocket.close(code=4001, reason="unauthorized")
        return
    run = await db.scalar(_owned_run(pipeline_id, run_id, user.id))
    if run is None:
        await websocket.close(code=4004, reason="not found")
        return

    notifier = websocket.app.state.job_notifier
    pid = str(pipeline_id)
    try:
        async with notifier.subscribe_pipeline_run(str(run.id), str(user.id)) as queue:
            # Re-read after subscribing: an update in between is queued, not lost.
            await db.refresh(run)
            snapshot = {
                "run_id": str(run.id),
                "status": run.status,
                "cancel_requested": run.cancel_requested_at is not None,
                "stopped_at_block_id": run.stopped_at_block_id,
                "rolling_back": run.status == "running" and run.rollback_deadline is not None,
                "change_status": run.change_status,
            }
            # Hours-long watches must not hold a pooled connection.
            await db.close()
            await websocket.send_json(_watch_message(pid, snapshot))
            if run.status in _TERMINAL:
                await websocket.close()
                return
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=WATCH_TIMEOUT_SECONDS)
                except TimeoutError:
                    await websocket.send_json({"type": "timeout"})
                    break
                message = _watch_message(pid, update)
                await websocket.send_json(message)
                if message["status"] in _TERMINAL:
                    break
    except WebSocketConnectionLimitExceeded:
        await websocket.close(code=4029, reason="connection limit exceeded")
        return
    await websocket.close()
