"""Destinations (ADR-015 §2a to §2c). Every route is owner-scoped: another user's destination is a
404. No read returns the secret; a webhook's URL is shown by its host only."""

import uuid
from typing import Any, NamedTuple

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from app.auth.dependencies import get_current_admin_user, get_current_user
from app.core.db import get_db
from app.core.destination_tests import refuse_failed, run_test
from app.core.destinations import (
    decrypt_secret,
    encrypt_secret,
    monitors_using,
    pipelines_using,
    runs_using,
)
from app.core.pipeline_trigger import ssrf_check
from app.core.quota import lock_user
from app.core.rate_limit import check_rate_limit
from app.models.destination import Destination
from app.models.user import User
from app.schemas.destinations import (
    KIND_MODELS,
    AdminDestinationResponse,
    DestinationCreate,
    DestinationEdit,
    DestinationForm,
    DestinationKind,
    DestinationResponse,
    DestinationSavedResponse,
    DestinationTestResult,
    DestinationUpdate,
    display_host,
    endpoint_url,
    stored,
)
from app.workflows.client import get_temporal, get_temporal_or_none

router = APIRouter(prefix="/destinations", tags=["destinations"])
admin_router = APIRouter(prefix="/admin/destinations", tags=["admin"])
logger = structlog.get_logger()


def _response(d: Destination) -> DestinationResponse:
    return DestinationResponse(
        id=d.id,
        name=d.name,
        kind=d.kind,
        address=d.address,
        display_host=d.display_host,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


async def _owned(db: AsyncSession, user_id: uuid.UUID, destination_id: uuid.UUID) -> Destination:
    destination = await db.get(Destination, destination_id)
    if destination is None or destination.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Destination not found")
    return destination


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_destinations_user_id_name" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Destination name in use"
            ) from None
        raise


class _Checked(NamedTuple):
    address: dict[str, Any]
    encrypted_secret: str
    display_host: str


async def _checked(kind: str, address: BaseModel, secret: BaseModel) -> _Checked:
    """Checks the address (a DNS lookup); the address and secret as they would be stored."""
    url = endpoint_url(kind, address, secret)
    await ssrf_check(url)
    return _Checked(stored(address), encrypt_secret(stored(secret)), display_host(url))


def _apply(destination: Destination, form: _Checked) -> None:
    destination.address = form.address
    destination.encrypted_secret = form.encrypted_secret
    destination.display_host = form.display_host


def _parse(model: type[BaseModel], data: dict, field: str) -> BaseModel:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # No `input`: the merged secret holds stored values the caller did not send.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {"loc": ["body", field, *e["loc"]], "msg": e["msg"], "type": e["type"]}
                for e in exc.errors(include_url=False)
            ],
        ) from None


@router.post("/test", response_model=DestinationTestResult)
async def test_destination_form(
    body: DestinationForm,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    temporal: Client = Depends(get_temporal),
    _: None = Depends(check_rate_limit),
) -> DestinationTestResult:
    """An unsaved form: each check reported, nothing stored (ADR-015 §2d)."""
    form = await _checked(body.kind, body.address, body.secret)
    return await run_test(temporal, db, body.kind, form.address, form.encrypted_secret)


@router.post("", response_model=DestinationSavedResponse, status_code=status.HTTP_201_CREATED)
async def create_destination(
    body: DestinationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    temporal: Client = Depends(get_temporal),
    _: None = Depends(check_rate_limit),
) -> DestinationSavedResponse:
    form = await _checked(body.kind, body.address, body.secret)
    result = await run_test(temporal, db, body.kind, form.address, form.encrypted_secret)
    refuse_failed(result)
    destination = Destination(user_id=user.id, name=body.name, kind=body.kind)
    _apply(destination, form)
    db.add(destination)
    await _commit(db)
    await db.refresh(destination)
    logger.info("destination_created", destination_id=str(destination.id), kind=destination.kind)
    return DestinationSavedResponse(**_response(destination).model_dump(), test=result)


@router.get("", response_model=list[DestinationResponse])
async def list_destinations(
    kind: DestinationKind | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DestinationResponse]:
    stmt = select(Destination).where(Destination.user_id == user.id)
    if kind is not None:
        stmt = stmt.where(Destination.kind == kind)
    rows = await db.scalars(stmt.order_by(Destination.name))
    return [_response(d) for d in rows]


@router.get("/{destination_id}", response_model=DestinationResponse)
async def get_destination(
    destination_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DestinationResponse:
    return _response(await _owned(db, user.id, destination_id))


async def _edited(destination: Destination, body: DestinationEdit) -> _Checked:
    """The stored address and secret with the edit's fields over them, checked whole."""
    address_model, secret_model = KIND_MODELS[destination.kind]
    address = _parse(address_model, {**destination.address, **(body.address or {})}, "address")
    secret = decrypt_secret(destination.encrypted_secret)
    secret = _parse(secret_model, {**secret, **(body.secret or {})}, "secret")
    return await _checked(destination.kind, address, secret)


@router.post("/{destination_id}/test", response_model=DestinationTestResult)
async def test_destination(
    destination_id: uuid.UUID,
    body: DestinationEdit | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    temporal: Client = Depends(get_temporal),
    _: None = Depends(check_rate_limit),
) -> DestinationTestResult:
    """A saved destination, as stored, or with an edit not yet saved over it (the PATCH's merge);
    nothing is stored (ADR-015 §2a)."""
    destination = await _owned(db, user.id, destination_id)
    if body is not None and (body.address is not None or body.secret is not None):
        form = await _edited(destination, body)
        return await run_test(temporal, db, destination.kind, form.address, form.encrypted_secret)
    return await run_test(
        temporal, db, destination.kind, destination.address, destination.encrypted_secret
    )


@router.patch("/{destination_id}", response_model=DestinationSavedResponse)
async def update_destination(
    destination_id: uuid.UUID,
    body: DestinationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    temporal: Client | None = Depends(get_temporal_or_none),
    _: None = Depends(check_rate_limit),
) -> DestinationSavedResponse:
    destination = await _owned(db, user.id, destination_id)
    form, result = None, None
    if body.address is not None or body.secret is not None:
        form = await _edited(destination, body)
        result = await run_test(temporal, db, destination.kind, form.address, form.encrypted_secret)
        refuse_failed(result)
    # Only after the test: run_test commits the session before its wait.
    if body.name is not None:
        destination.name = body.name
    if form is not None:
        _apply(destination, form)
    await _commit(db)
    await db.refresh(destination)
    logger.info(
        "destination_updated",
        destination_id=str(destination.id),
        address=body.address is not None,
        secret=body.secret is not None,
    )
    return DestinationSavedResponse(**_response(destination).model_dump(), test=result)


@router.delete("/{destination_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_destination(
    destination_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """ADR-015 §2c: refused while a non-deleted pipeline's version, a running run or a monitor's
    notice setting refers to it (ADR-016 §13). The trigger and the monitor edits take the same
    lock, so nothing starts using a destination being deleted."""
    await lock_user(user.id, db)
    destination = await _owned(db, user.id, destination_id)
    pipelines = await pipelines_using(db, destination.id)
    runs = await runs_using(db, destination.id)
    monitors = await monitors_using(db, destination.id)
    if pipelines or runs or monitors:
        if pipelines:
            names = ", ".join(p.name for p in pipelines)
            message = f"Pipelines use this destination: {names}. Change or delete them first."
        elif monitors:
            names = ", ".join(m.name for m in monitors)
            message = (
                f"Monitors send their notices to this destination: {names}. "
                "Choose another notice destination for them first."
            )
        else:
            message = "Runs using this destination are still running; wait for them to end."
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "destination_in_use",
                "message": message,
                "pipelines": [{"id": str(p.id), "name": p.name} for p in pipelines],
                "runs": [{"id": str(r.id), "pipeline_id": str(r.pipeline_id)} for r in runs],
                "monitors": [{"id": str(m.id), "name": m.name} for m in monitors],
            },
        )
    await db.execute(delete(Destination).where(Destination.id == destination.id))
    await db.commit()
    logger.info("destination_deleted", destination_id=str(destination_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Admin (PRD-017 R8: read-only, never a credential)
# ---------------------------------------------------------------------------


def _admin_response(d: Destination, email: str) -> AdminDestinationResponse:
    return AdminDestinationResponse(
        **_response(d).model_dump(), user_id=d.user_id, user_email=email
    )


@admin_router.get("", response_model=list[AdminDestinationResponse])
async def admin_list_destinations(
    user_id: uuid.UUID | None = Query(default=None),
    kind: DestinationKind | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> list[AdminDestinationResponse]:
    stmt = select(Destination, User.email).join(User, User.id == Destination.user_id)
    if user_id is not None:
        stmt = stmt.where(Destination.user_id == user_id)
    if kind is not None:
        stmt = stmt.where(Destination.kind == kind)
    stmt = stmt.order_by(Destination.created_at.desc()).limit(limit).offset(offset)
    return [_admin_response(d, email) for d, email in (await db.execute(stmt)).all()]


@admin_router.get("/{destination_id}", response_model=AdminDestinationResponse)
async def admin_get_destination(
    destination_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> AdminDestinationResponse:
    row = (
        await db.execute(
            select(Destination, User.email)
            .join(User, User.id == Destination.user_id)
            .where(Destination.id == destination_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Destination not found")
    return _admin_response(*row)
