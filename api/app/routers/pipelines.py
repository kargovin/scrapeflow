import copy
import json
import uuid
from asyncio import get_running_loop
from datetime import UTC, datetime
from typing import Any

import structlog
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import Select, delete, exists, func, select
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
    parse_definition,
)
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
    PipelineUpdate,
    PipelineVersionResponse,
)
from app.settings import settings
from app.workflows.activities.contracts import Credentials
from app.workflows.client import get_temporal
from app.workflows.pipeline import (
    LLMKey,
    PipelineRunInput,
    PipelineWorkflow,
)
from app.workflows.queues import WORKFLOW_QUEUE

router = APIRouter(prefix="/pipelines", tags=["pipelines"])
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Definitions: what is stored, what is returned
# ---------------------------------------------------------------------------


def _unprocessable(problems: list[Problem]) -> HTTPException:
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


def _encrypt_secret(value: Any) -> str:
    plain = value if isinstance(value, str) else json.dumps(value)
    return Fernet(settings.credentials_encryption_key).encrypt(plain.encode()).decode()


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
                    secrets[name] = _encrypt_secret(value)
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


async def _ssrf_check(url: str) -> None:
    await get_running_loop().run_in_executor(None, validate_no_ssrf, url)


async def _owned_keys(
    db: AsyncSession, user_id: uuid.UUID, key_ids: set[uuid.UUID]
) -> dict[str, UserLLMKey]:
    if not key_ids:
        return {}
    owned = await db.scalars(
        select(UserLLMKey).where(UserLLMKey.id.in_(key_ids), UserLLMKey.user_id == user_id)
    )
    return {str(k.id): k for k in owned}


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
        raise _unprocessable(exc.problems) from None

    key_ids = {
        b.config.llm_key_id for b in validated.blocks if isinstance(b.config, LLMExtractConfigV1)
    }
    keys = await _owned_keys(db, user.id, key_ids)
    if len(keys) != len(key_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="LLM Key not found")

    # Fail at save on what is known now; the trigger checks again (DNS can change).
    for block in validated.blocks:
        config = block.config
        if isinstance(config, ScrapeConfigV1 | WebhookConfigV1) and not isinstance(
            config.url, InputBinding
        ):
            await _ssrf_check(str(config.url))
    for key in keys.values():
        if key.base_url:
            await _ssrf_check(key.base_url)

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
    return pipeline_response(pipeline, version)


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
    return pipeline_response(*await _owned_pipeline(db, user.id, pipeline_id))


@router.patch("/{pipeline_id}", response_model=PipelineResponse)
async def update_pipeline(
    pipeline_id: uuid.UUID,
    body: PipelineUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
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
        return pipeline_response(pipeline, version)

    pipeline.updated_at = datetime.now(UTC)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if conflict := _name_conflict(exc):
            raise conflict from None
        raise
    logger.info("pipeline_updated", pipeline_id=str(pipeline.id), version=version.version)
    return pipeline_response(pipeline, version)


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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """No runs: hard delete. Runs: soft delete — runs and their artifacts stay, the name is held."""
    user_id = user.id  # the rollback below expires every instance in the session
    pipeline, _ = await _owned_pipeline(db, user_id, pipeline_id)
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
            pipeline, _ = await _owned_pipeline(db, user_id, pipeline_id)

    now = datetime.now(UTC)
    pipeline.deleted_at = now
    pipeline.updated_at = now
    await db.commit()
    logger.info("pipeline_deleted", pipeline_id=str(pipeline_id), mode="soft")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Runs (C.5)
# ---------------------------------------------------------------------------


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


async def _version_to_run(
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


def _scrape_credentials(secrets: dict[str, str]) -> Credentials | None:
    # As jobs resolve them: the block's proxy, else the platform default; cookies as stored.
    proxy = secrets.get("proxy_url")
    if proxy is None and settings.default_proxy_url:
        proxy = _encrypt_secret(settings.default_proxy_url)
    cookies = secrets.get("cookies")
    if proxy is None and cookies is None:
        return None
    return Credentials(encrypted_proxy_url=proxy, encrypted_cookies=cookies)


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
    # The ownership check is the only tenant boundary (ADR-009 §12).
    pipeline, version = await _version_to_run(db, user.id, pipeline_id, body.version)

    # Parse, do not re-validate: a pinned version keeps the rules of the day it was saved.
    try:
        parsed, stored_secrets = parse_definition(version.definition)
    except DefinitionUnreadable as exc:
        logger.error(
            "pipeline_version_unreadable", pipeline_version_id=str(version.id), error=str(exc)
        )
        raise _unprocessable(
            [Problem(b, f"this version cannot be run: {m}") for b, m in exc.problems]
        ) from None

    declared, given = set(parsed.inputs), set(body.inputs)
    problems = [Problem(None, f"missing run input {n!r}") for n in sorted(declared - given)]
    problems += [Problem(None, f"unexpected run input {n!r}") for n in sorted(given - declared)]
    if problems:
        raise _unprocessable(problems)

    # The scrape workers do not check SSRF; this is the only check before the fetch.
    # The Webhook activity checks again on every attempt; this one fails the run up front.
    for block in parsed.blocks:
        if isinstance(block, ScrapeBlock):
            url = block.config.url
            await _ssrf_check(body.inputs[url.input] if isinstance(url, InputBinding) else str(url))
        elif isinstance(block, WebhookBlock):
            await _ssrf_check(str(block.config.url))

    llm_blocks = [b for b in parsed.blocks if isinstance(b, LLMExtractBlock)]
    keys = await _owned_keys(db, user.id, {b.config.llm_key_id for b in llm_blocks})
    if missing := [b for b in llm_blocks if str(b.config.llm_key_id) not in keys]:
        raise _unprocessable([Problem(b.id, "LLM key no longer exists") for b in missing])
    for key in keys.values():
        if key.base_url:
            await _ssrf_check(key.base_url)

    await check_user_quota(user.id, db, "monthly_runs")
    await check_user_quota(user.id, db, "concurrent_jobs")
    # §8d: output size is unknowable up front, so refuse to start near the wall.
    await check_user_quota(
        user.id, db, "storage_bytes", headroom_bytes=settings.storage_headroom_bytes
    )

    credentials = {
        b.id: c
        for b in parsed.blocks
        if isinstance(b, ScrapeBlock)
        and (c := _scrape_credentials(stored_secrets.get(b.id, {}))) is not None
    }

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
            block_id=b.id,
            position=i,
            type=b.type,
            status="pending",
        )
        for i, b in enumerate(parsed.blocks)
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
                pipeline_id=str(pipeline.id),
                blocks=parsed.blocks,
                time_budget_seconds=parsed.run_budget_seconds,
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
                credentials=credentials,
                webhook_secrets={
                    b.id: secret
                    for b in parsed.blocks
                    if isinstance(b, WebhookBlock)
                    and (secret := stored_secrets.get(b.id, {}).get("secret")) is not None
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
        for block in run_blocks:
            block.status = "skipped"
        await db.commit()
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
