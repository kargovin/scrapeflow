import asyncio
import json
import secrets
import uuid
from asyncio import get_running_loop
from datetime import UTC, datetime, timedelta

import structlog
from croniter import croniter
from cryptography.fernet import Fernet
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    status,
)
from miniopy_async import Minio
from nats.js import JetStreamContext
from sqlalchemy import delete, func, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth.dependencies import auth_from_token, get_current_user
from app.core import schedules
from app.core.credentials import resolve_credentials
from app.core.db import get_db
from app.core.dispatch import (
    cancel_active_runs,
    cancel_workflows,
    dispatch_run,
    fail_unstarted_run,
)
from app.core.job_notifier import WebSocketConnectionLimitExceeded
from app.core.ledger import release_run_objects
from app.core.minio import get_minio
from app.core.nats import get_jetstream
from app.core.quota import Admit, admission, check_scheduled_jobs_quota, lock_user
from app.core.rate_limit import check_rate_limit
from app.core.security import validate_no_ssrf
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.job_secrets import JobSecrets, JobSecretType
from app.models.llm_keys import UserLLMKey
from app.models.user import User
from app.schemas.jobs import (
    CancelJobResponse,
    Engine,
    JobCreate,
    JobPatch,
    JobResponse,
    JobResultResponse,
    RotateWebhookSecretResponse,
)
from app.settings import settings
from app.workflows.client import lazy_client

router = APIRouter(prefix="/jobs", tags=["jobs"])

logger = structlog.get_logger()


async def check_job_quota(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Admit:
    """FastAPI dependency — the admission create_job awaits right before its insert.

    Structured as a dependency so tests can override it via dependency_overrides,
    matching the same pattern used for check_rate_limit.
    """
    return admission(user.id, db)


def validate_cron_min_interval(cron_expr: str, min_minutes: int) -> None:
    base = datetime.now(UTC)
    c = croniter(cron_expr, base)
    prev = c.get_next(datetime)
    cutoff = base + timedelta(minutes=min_minutes * 2)
    while prev < cutoff:
        curr = c.get_next(datetime)
        if (curr - prev).total_seconds() < min_minutes * 60:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Schedule interval must be at least {min_minutes} minutes",
            )
        prev = curr


def _validate_cron(cron: str) -> None:
    if not croniter.is_valid(cron):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid cron expression"
        )
    validate_cron_min_interval(cron, settings.schedule_min_interval_minutes)


_SCHEDULE_FIELDS = ("schedule_cron", "schedule_status", "schedule_lane", "next_run_at")


async def _apply_schedule_changes(job: Job, changes: dict, db: AsyncSession) -> bool:
    """Writes a PATCH's schedule fields onto the job; returns whether it arms a schedule.

    ``schedule_cron: null`` removes the schedule. Every armed schedule is a Temporal Schedule: v1's
    loop is gone (F.6), so a v1 schedule left paused from before moves to v2 when it is resumed.
    Arming checks the cap (ADR-013) under the user's lock; the caller commits.
    """
    was_active = job.schedule_status == "active"
    if "schedule_cron" in changes:
        cron = changes["schedule_cron"]
        if cron is None:
            if changes.get("schedule_status") is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="schedule_status needs a schedule_cron",
                )
            job.schedule_cron = job.schedule_status = job.schedule_lane = job.next_run_at = None
            return False
        _validate_cron(cron)
        job.schedule_cron = cron
        if job.schedule_status is None:
            job.schedule_status = "active"
    if "schedule_status" in changes:
        if changes["schedule_status"] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="To remove a schedule, set schedule_cron to null",
            )
        if job.schedule_cron is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The job has no schedule",
            )
        job.schedule_status = changes["schedule_status"]
    if job.schedule_status is None:
        return False
    if job.schedule_lane is None or job.schedule_status == "active":
        job.schedule_lane = "v2"
        job.next_run_at = None
    arms = job.schedule_status == "active" and not was_active
    if arms:
        await lock_user(job.user_id, db)
        await check_scheduled_jobs_quota(job.user_id, db, exclude_job_id=job.id)
    return arms


def _jobs_with_latest_run_stmt(
    user_id: uuid.UUID,
    job_id: uuid.UUID | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    latest_run_sq = (
        select(JobRun)
        .where(JobRun.job_id == Job.id)
        .order_by(JobRun.created_at.desc())
        .limit(1)
        .correlate(Job)
        .lateral()
    )
    latest_run = aliased(JobRun, latest_run_sq)
    has_proxy_sq = (
        select(JobSecrets.id)
        .where(JobSecrets.job_id == Job.id, JobSecrets.secret_type == JobSecretType.proxy)
        .correlate(Job)
        .exists()
        .label("has_proxy")
    )
    has_cookies_sq = (
        select(JobSecrets.id)
        .where(JobSecrets.job_id == Job.id, JobSecrets.secret_type == JobSecretType.cookies)
        .correlate(Job)
        .exists()
        .label("has_cookies")
    )
    stmt = (
        select(Job, latest_run, has_proxy_sq, has_cookies_sq)
        .join(latest_run, true())
        .where(Job.user_id == user_id)
        .order_by(Job.created_at.desc())
    )
    if job_id is not None:
        stmt = stmt.where(Job.id == job_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return stmt


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    admit: Admit = Depends(check_job_quota),
    js: JetStreamContext = Depends(get_jetstream),
) -> JobResponse:
    # Validate for SSRF for
    # 1) url to scrape
    # 2) Webhook Url
    # 3) LLM base url in case of Openai compatible url
    await get_running_loop().run_in_executor(None, validate_no_ssrf, str(body.url))

    if body.webhook_url:
        await get_running_loop().run_in_executor(None, validate_no_ssrf, str(body.webhook_url))

    if body.llm_config:
        key_id = body.llm_config.llm_key_id
        user_llm_key = await db.get(UserLLMKey, key_id)
        if user_llm_key is None or user_llm_key.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="LLM Key not found")
        if user_llm_key.base_url:
            await get_running_loop().run_in_executor(
                None, validate_no_ssrf, str(user_llm_key.base_url)
            )

    # validate cron exp and also validate min intervals constraint is not broken
    if body.schedule_cron:
        _validate_cron(body.schedule_cron)

    # Generate a token and encrypt it using Fernet
    webhook_secret_plain: str | None = None
    webhook_secret_encrypted: str | None = None
    if body.webhook_url:
        f = Fernet(settings.llm_key_encryption_key)
        webhook_secret_plain = secrets.token_hex(32)
        webhook_secret_encrypted = f.encrypt(webhook_secret_plain.encode()).decode()

    if body.engine == Engine.playwright and not body.playwright_options:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Playwright options missing"
        )

    if body.actions and body.engine != Engine.playwright:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="actions require engine: playwright",
        )

    await admit()
    if body.schedule_cron:
        # ADR-013: under the lock admit() took, committed with the job.
        await check_scheduled_jobs_quota(user.id, db)
    job = Job(
        user_id=user.id,
        url=body.url,
        output_format=body.output_format,
        engine=body.engine,
        schedule_cron=body.schedule_cron,
        schedule_status="active" if body.schedule_cron else None,
        schedule_lane="v2" if body.schedule_cron else None,
        webhook_url=body.webhook_url,
        webhook_secret=webhook_secret_encrypted if body.webhook_url else None,
        llm_config=body.llm_config.model_dump(mode="json") if body.llm_config else None,
        playwright_options=body.playwright_options.model_dump()
        if body.playwright_options
        else None,
        playwright_actions=body.actions,
        respect_robots=body.respect_robots,
        proxy_provider=body.proxy_provider,
        webhook_events=body.webhook_events,
    )
    db.add(job)
    await db.flush()
    job_run = JobRun(job_id=job.id, status="pending", lane=settings.job_lane)
    db.add(job_run)
    await db.commit()
    await db.refresh(job)
    await db.refresh(job_run)

    # Persist proxy secret if provided (write-only; never returned)
    if body.proxy_url:
        f = Fernet(settings.credentials_encryption_key)
        encrypted = f.encrypt(body.proxy_url.encode()).decode()
        upsert = (
            pg_insert(JobSecrets)
            .values(
                job_id=job.id,
                secret_type=JobSecretType.proxy,
                encrypted_value=encrypted,
            )
            .on_conflict_do_update(
                constraint="uq_job_secrets_job_type",
                set_={"encrypted_value": encrypted, "updated_at": func.now()},
            )
        )
        await db.execute(upsert)
        await db.commit()

    # Persist cookies secret if provided (write-only; never returned)
    if body.cookies:
        f = Fernet(settings.credentials_encryption_key)
        encrypted = f.encrypt(json.dumps(body.cookies).encode()).decode()
        upsert = (
            pg_insert(JobSecrets)
            .values(
                job_id=job.id,
                secret_type=JobSecretType.cookies,
                encrypted_value=encrypted,
            )
            .on_conflict_do_update(
                constraint="uq_job_secrets_job_type",
                set_={"encrypted_value": encrypted, "updated_at": func.now()},
            )
        )
        await db.execute(upsert)
        await db.commit()

    if schedules.is_v2(job):
        # Before the first run: a job whose schedule cannot be armed is not run either.
        try:
            await schedules.put(await lazy_client(request.app.state), job)
        except Exception as exc:
            logger.exception("job_schedule_put_failed", job_id=str(job.id))
            job.schedule_status = "paused"
            await fail_unstarted_run(db, job, job_run, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "job_id": str(job.id),
                    "run_id": str(job_run.id),
                    "error": "the schedule could not be armed: the job was created with its "
                    "schedule paused and was not run",
                },
            ) from None

    # Resolve credentials: per-job secret > platform default > None (PRD-005)
    credentials = await resolve_credentials(job, db)

    # Dispatch after the DB insert (ADR-001). v1: if NATS is down the run stays pending for
    # stale-pending recovery. v2 has no recovery: a run whose workflow did not start is failed.
    try:
        await dispatch_run(job, job_run, credentials, js, lambda: lazy_client(request.app.state))
    except Exception as exc:
        if job_run.lane == "v1":
            raise
        logger.exception("job_run_start_failed", job_id=str(job.id), run_id=str(job_run.id))
        await fail_unstarted_run(db, job, job_run, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"job_id": str(job.id), "run_id": str(job_run.id), "error": job_run.error},
        ) from None
    logger.info("job_created", job_id=str(job.id), user_id=str(user.id), url=job.url)

    return JobResponse(
        id=job.id,
        user_id=job.user_id,
        url=job.url,
        status=job_run.status,
        output_format=job.output_format,
        result_path=job_run.result_path,
        error=job_run.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        run_id=job_run.id,
        webhook_secret=webhook_secret_plain,
        has_proxy=body.proxy_url is not None,
        has_cookies=bool(body.cookies),
        actions=job.playwright_actions,
        webhook_events=job.webhook_events,
    )


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[JobResponse]:
    stmt = _jobs_with_latest_run_stmt(user.id, limit=limit, offset=offset)
    result = await db.execute(stmt)
    rows = result.all()
    return [
        JobResponse(
            id=job.id,
            user_id=job.user_id,
            url=job.url,
            output_format=job.output_format,
            created_at=job.created_at,
            updated_at=job.updated_at,
            run_id=run.id,
            status=run.status,
            result_path=run.result_path,
            diff_detected=run.diff_detected,
            error=run.error,
            completed_at=run.completed_at,
            has_proxy=has_proxy,
            has_cookies=has_cookies,
            actions=job.playwright_actions,
            webhook_events=job.webhook_events,
        )
        for job, run, has_proxy, has_cookies in rows
    ]


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobResponse:
    stmt = _jobs_with_latest_run_stmt(user.id, job_id=job_id)
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job, run, has_proxy, has_cookies = row
    return JobResponse(
        id=job.id,
        user_id=job.user_id,
        url=job.url,
        output_format=job.output_format,
        created_at=job.created_at,
        updated_at=job.updated_at,
        run_id=run.id,
        status=run.status,
        result_path=run.result_path,
        diff_detected=run.diff_detected,
        error=run.error,
        completed_at=run.completed_at,
        has_proxy=has_proxy,
        has_cookies=has_cookies,
        actions=job.playwright_actions,
        webhook_events=job.webhook_events,
    )


@router.delete("/{job_id}", response_model=CancelJobResponse)
async def cancel_job(
    job_id: uuid.UUID,
    request: Request,
    permanent: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio_client: Minio = Depends(get_minio),
) -> CancelJobResponse | Response:
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if permanent:
        # Objects before the DB row (external-before-internal), enumerated from the
        # ledger rather than derived from result_path — an LLM run holds more than one.
        runs = (await db.execute(select(JobRun).where(JobRun.job_id == job_id))).scalars().all()
        # A v2 run still in flight finds its row gone and drops what it writes after this.
        await cancel_workflows(
            [r for r in runs if r.status in ("pending", "running", "processing")],
            lambda: lazy_client(request.app.state),
        )
        outcome = await release_run_objects(
            db, minio_client, runs, user.id, "job object on permanent delete"
        )
        if outcome.failed:
            # Keep the job: its cascade would drop ledger rows for objects still on
            # disk. What was freed stays freed — the ledger and the counter moved
            # together per object.
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"{outcome.failed} stored object(s) could not be removed; "
                    "the job was not deleted. Retry."
                ),
            )

        if job.schedule_lane == "v2":
            await schedules.forget(lambda: lazy_client(request.app.state), [job.id])
        await db.delete(job)
        await db.commit()
        logger.info(
            "job_permanently_deleted",
            job_id=str(job_id),
            user_id=str(user.id),
            objects_released=outcome.released,
            bytes_freed=outcome.freed_bytes,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Soft cancel (default behaviour — existing semantics unchanged)
    active_runs = await cancel_active_runs(db, job_id)
    if not active_runs:
        return CancelJobResponse(message="Job has no active run to cancel")
    await cancel_workflows(active_runs, lambda: lazy_client(request.app.state))
    logger.info("job_cancelled", job_id=str(job_id))
    return CancelJobResponse(message="Job run cancelled")


@router.get("/{job_id}/runs", response_model=list[JobResponse])
async def list_job_runs(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    job = await db.get(Job, job_id)
    if job is None or user.id != job.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    result = await db.execute(
        select(JobRun)
        .where(JobRun.job_id == job_id)
        .order_by(JobRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    runs = result.scalars().all()
    return [
        JobResponse(
            id=job.id,
            user_id=job.user_id,
            url=job.url,
            output_format=job.output_format,
            created_at=job.created_at,
            updated_at=job.updated_at,
            run_id=run.id,
            status=run.status,
            result_path=run.result_path,
            diff_detected=run.diff_detected,
            error=run.error,
            completed_at=run.completed_at,
            actions=job.playwright_actions,
            webhook_events=job.webhook_events,
        )
        for run in runs
    ]


@router.patch("/{job_id}", response_model=JobResponse)
async def patch_jobs(
    body: JobPatch,
    job_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    updates = body.model_dump(exclude_unset=True)
    webhook_secret_plain = None

    _UNSET = object()
    # Pop secrets now so they don't reach the setattr loop, but defer DB writes until after all validations.
    proxy_url_pending = updates.pop("proxy_url", _UNSET)
    cookies_pending = updates.pop("cookies", _UNSET)
    schedule_changes = {
        f: updates.pop(f) for f in ("schedule_cron", "schedule_status") if f in updates
    }

    # actions maps to job.playwright_actions — validate engine constraint before writing
    if "actions" in updates:
        actions_val = updates.pop("actions")
        if actions_val and job.engine != "playwright":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="actions require engine: playwright",
            )
        job.playwright_actions = actions_val

    for field, value in updates.items():
        if field == "webhook_url":
            if value is None:
                job.webhook_secret = None
            else:
                await get_running_loop().run_in_executor(None, validate_no_ssrf, str(value))

                f = Fernet(settings.llm_key_encryption_key)
                webhook_secret_plain = secrets.token_hex(32)
                webhook_secret_encrypted = f.encrypt(webhook_secret_plain.encode()).decode()
                job.webhook_secret = webhook_secret_encrypted

        elif field == "llm_config":
            result = await db.execute(
                select(JobRun).where(JobRun.job_id == job_id).where(JobRun.status == "processing")
            )
            job_run = result.scalar_one_or_none()
            if job_run:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail="LLM config cant be updated as job run in process",
                )
            if value is not None:
                key_id = value.get("llm_key_id")
                if key_id is not None:
                    user_llm_key = await db.get(UserLLMKey, key_id)
                    if user_llm_key is None or user_llm_key.user_id != user.id:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND, detail="LLM Key not found"
                        )

        setattr(job, field, value)

    if schedule_changes:
        before = {f: getattr(job, f) for f in _SCHEDULE_FIELDS}
        arms = await _apply_schedule_changes(job, schedule_changes, db)
        engine_involved = before["schedule_lane"] == "v2" or job.schedule_lane == "v2"

    # All validations passed — write secrets now.
    if proxy_url_pending is not _UNSET:
        if proxy_url_pending:
            f = Fernet(settings.credentials_encryption_key)
            encrypted = f.encrypt(proxy_url_pending.encode()).decode()
            upsert = (
                pg_insert(JobSecrets)
                .values(
                    job_id=job_id,
                    secret_type=JobSecretType.proxy,
                    encrypted_value=encrypted,
                )
                .on_conflict_do_update(
                    constraint="uq_job_secrets_job_type",
                    set_={"encrypted_value": encrypted, "updated_at": func.now()},
                )
            )
            await db.execute(upsert)
        else:
            await db.execute(
                delete(JobSecrets).where(
                    JobSecrets.job_id == job_id,
                    JobSecrets.secret_type == JobSecretType.proxy,
                )
            )

    if cookies_pending is not _UNSET:
        if cookies_pending:
            f = Fernet(settings.credentials_encryption_key)
            encrypted = f.encrypt(json.dumps(cookies_pending).encode()).decode()
            upsert = (
                pg_insert(JobSecrets)
                .values(
                    job_id=job_id,
                    secret_type=JobSecretType.cookies,
                    encrypted_value=encrypted,
                )
                .on_conflict_do_update(
                    constraint="uq_job_secrets_job_type",
                    set_={"encrypted_value": encrypted, "updated_at": func.now()},
                )
            )
            await db.execute(upsert)
        else:
            await db.execute(
                delete(JobSecrets).where(
                    JobSecrets.job_id == job_id,
                    JobSecrets.secret_type == JobSecretType.cookies,
                )
            )

    await db.commit()

    if schedule_changes and engine_involved:
        try:
            await schedules.sync(await lazy_client(request.app.state), job)
        except Exception as exc:
            logger.warning("job_schedule_sync_failed", job_id=str(job_id), error=str(exc))
            if job.schedule_status == "active" and (
                arms or job.schedule_cron != before["schedule_cron"]
            ):
                # The engine would not fire what the row says: put the schedule back as it was.
                for field, value in before.items():
                    setattr(job, field, value)
                await db.commit()
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Workflow engine unavailable: the schedule was not changed",
                ) from None
            # Disarming: the row already stops every tick at admission.

    stmt = _jobs_with_latest_run_stmt(user.id, job_id=job_id)
    result = await db.execute(stmt)
    row = result.one()
    job, run, has_proxy, has_cookies = row
    return JobResponse(
        id=job.id,
        user_id=job.user_id,
        url=job.url,
        output_format=job.output_format,
        created_at=job.created_at,
        updated_at=job.updated_at,
        run_id=run.id,
        status=run.status,
        result_path=run.result_path,
        diff_detected=run.diff_detected,
        error=run.error,
        completed_at=run.completed_at,
        webhook_secret=webhook_secret_plain,
        has_proxy=has_proxy,
        has_cookies=has_cookies,
        actions=job.playwright_actions,
        webhook_events=job.webhook_events,
    )


@router.post("/{job_id}/webhook-secret/rotate", response_model=RotateWebhookSecretResponse)
async def rotate_webhook_secrets(
    job_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.webhook_url is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Job has no webhook URL"
        )
    f = Fernet(settings.llm_key_encryption_key)
    webhook_secret_plain = secrets.token_hex(32)
    webhook_secret_encrypted = f.encrypt(webhook_secret_plain.encode()).decode()
    job.webhook_secret = webhook_secret_encrypted

    await db.commit()
    return RotateWebhookSecretResponse(webhook_secret=webhook_secret_plain)


async def load_completed_result(
    job: Job,
    db: AsyncSession,
    minio_client: Minio,
) -> JobResultResponse:
    """Load the latest completed run's stored content for a job from MinIO.

    Shared by the user-scoped GET /jobs/{id}/result and the admin
    GET /admin/jobs/{id}/result. Caller is responsible for authorization
    (owner check for the user route; admin dependency for the admin route).
    Raises 404 if there is no completed run with a stored result.
    """
    result = await db.execute(
        select(JobRun)
        .where(JobRun.job_id == job.id, JobRun.status == "completed")
        .order_by(JobRun.completed_at.desc())
        .limit(1)
    )
    run = result.scalar_one_or_none()
    if run is None or run.result_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No completed result available"
        )

    return await read_stored_result(
        minio_client, run.result_path, job.output_format.value, run.warnings
    )


_FORMAT_BY_EXTENSION = {"md": "markdown", "json": "json", "html": "html"}


async def read_stored_result(
    minio_client: Minio,
    result_path: str,
    output_format: str | None = None,
    warnings: list[str] | None = None,
) -> JobResultResponse:
    """One stored object's content. ``output_format`` defaults to the object's extension — a
    batch item or crawl page with an LLM step stored ``llm.json``, not its scrape format."""
    bucket, _, key = result_path.partition("/")
    response = await minio_client.get_object(bucket, key)
    content_bytes = await response.read()
    response.close()

    if output_format is None:
        output_format = _FORMAT_BY_EXTENSION.get(result_path.rsplit(".", 1)[-1], "html")
    return JobResultResponse(
        content=content_bytes.decode("utf-8", errors="replace"),
        output_format=output_format,
        result_path=result_path,
        warnings=warnings,
    )


@router.get("/{job_id}/result", response_model=JobResultResponse)
async def get_job_result(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio_client: Minio = Depends(get_minio),
) -> JobResultResponse:
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return await load_completed_result(job, db, minio_client)


_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _status_msg(job_id: str, run: JobRun) -> dict:
    return {"type": "status_update", "job_id": job_id, "run_id": str(run.id), "status": run.status}


def _terminal_msg(job_id: str, run: JobRun) -> dict:
    msg: dict = {"type": run.status, "job_id": job_id, "run_id": str(run.id), "status": run.status}
    if run.status == "completed":
        msg["result_url"] = f"/jobs/{job_id}/result"
    return msg


@router.websocket("/{job_id}/watch")
async def job_status_stream(
    websocket: WebSocket,
    job_id: uuid.UUID,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Stream live status updates for a single job run until it reaches a terminal state.

    Auth: ?token= query param (API key or Clerk JWT). Browsers cannot set Authorization
    headers on WebSocket upgrade requests.
    Close codes: 4001 unauthorized, 4004 not found / wrong user.
    """
    await websocket.accept()

    user = await auth_from_token(token, db)
    if user is None:
        await websocket.close(code=4001, reason="unauthorized")
        return

    result = await db.execute(
        select(Job, JobRun)
        .join(JobRun, JobRun.job_id == Job.id)
        .where(Job.id == job_id, Job.user_id == user.id)
        .order_by(JobRun.created_at.desc())
        .limit(1)
    )
    row = result.one_or_none()
    if row is None:
        await websocket.close(code=4004, reason="not found")
        return

    job, run = row
    job_id_str = str(job_id)

    if run.status in _TERMINAL_STATUSES:
        await websocket.send_json(_terminal_msg(job_id_str, run))
        await websocket.close()
        return

    await websocket.send_json(_status_msg(job_id_str, run))

    notifier = websocket.app.state.job_notifier
    try:
        async with notifier.subscribe_job(job_id_str, str(user.id)) as queue:
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=300)
                except TimeoutError:
                    await websocket.send_json({"type": "timeout"})
                    break
                new_status = update["status"]
                if new_status in _TERMINAL_STATUSES:
                    msg: dict = {
                        "type": new_status,
                        "job_id": job_id_str,
                        "run_id": update["run_id"],
                        "status": new_status,
                    }
                    if new_status == "completed":
                        msg["result_url"] = f"/jobs/{job_id_str}/result"
                    await websocket.send_json(msg)
                    break
                await websocket.send_json(
                    {
                        "type": "status_update",
                        "job_id": job_id_str,
                        "run_id": update["run_id"],
                        "status": new_status,
                    }
                )
    except WebSocketConnectionLimitExceeded:
        await websocket.close(code=4029, reason="connection limit exceeded")
        return

    await websocket.close()
