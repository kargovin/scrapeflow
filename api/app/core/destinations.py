"""Destinations (ADR-015 §2): the secret's encryption and what refers to a destination."""

import json
import uuid
from collections.abc import Iterable
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.destination import Destination, PipelineVersionDestination
from app.models.monitor import Monitor
from app.models.pipeline import Pipeline, PipelineRun, PipelineVersion
from app.settings import settings


def encrypt_secret(secret: dict[str, Any]) -> str:
    return Fernet(settings.destination_encryption_key).encrypt(json.dumps(secret).encode()).decode()


def decrypt_secret(token: str) -> dict[str, Any]:
    return json.loads(Fernet(settings.destination_encryption_key).decrypt(token.encode()))


async def pipelines_using(db: AsyncSession, destination_id: uuid.UUID) -> list[Pipeline]:
    """Non-deleted pipelines with any version referring to it (a run can name any version)."""
    rows = await db.scalars(
        select(Pipeline)
        .join(PipelineVersion, PipelineVersion.pipeline_id == Pipeline.id)
        .join(
            PipelineVersionDestination,
            PipelineVersionDestination.pipeline_version_id == PipelineVersion.id,
        )
        .where(
            PipelineVersionDestination.destination_id == destination_id,
            Pipeline.deleted_at.is_(None),
        )
        .distinct()
        .order_by(Pipeline.name)
    )
    return list(rows)


async def runs_using(db: AsyncSession, destination_id: uuid.UUID) -> list[PipelineRun]:
    """Runs still ``running`` (rolling back included) whose version refers to it, any pipeline."""
    rows = await db.scalars(
        select(PipelineRun)
        .join(
            PipelineVersionDestination,
            PipelineVersionDestination.pipeline_version_id == PipelineRun.pipeline_version_id,
        )
        .where(
            PipelineVersionDestination.destination_id == destination_id,
            PipelineRun.status == "running",
        )
        .order_by(PipelineRun.created_at)
    )
    return list(rows)


async def monitors_using(db: AsyncSession, destination_id: uuid.UUID) -> list[Monitor]:
    """Monitors naming it as their notice destination, whatever their state (ADR-016 §13)."""
    rows = await db.scalars(
        select(Monitor)
        .where(Monitor.notice_destination_id == destination_id)
        .order_by(Monitor.name)
    )
    return list(rows)


# ADR-016 §8: a notice is a webhook POST or an email.
NOTICE_DESTINATION_KINDS = ("webhook", "smtp")


async def notice_destination_problem(
    db: AsyncSession, user_id: uuid.UUID, destination_id: uuid.UUID
) -> str | None:
    """Why it cannot be a notice destination, or None. Raises LookupError when it is gone or
    another user's (a 404: existence is not revealed). The caller holds ``lock_user``."""
    destination = await db.get(Destination, destination_id)
    if destination is None or destination.user_id != user_id:
        raise LookupError("Destination not found")
    if destination.kind not in NOTICE_DESTINATION_KINDS:
        return (
            f"destination {destination.name!r} is a {destination.kind} destination; "
            "a notice destination must be a webhook or smtp destination"
        )
    return None


async def reference_problems(
    db: AsyncSession, user_id: uuid.UUID, refs: Iterable[tuple[str, uuid.UUID, str]]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Each (block id, destination id, kind) checked against the user's destinations.

    Returns the block ids naming one that is gone or another user's (a 404: existence is not
    revealed), and (block id, message) for one of the wrong kind.
    """
    refs = list(refs)
    if not refs:
        return [], []
    owned = {
        d.id: d
        for d in await db.scalars(
            select(Destination).where(
                Destination.id.in_({r[1] for r in refs}), Destination.user_id == user_id
            )
        )
    }
    missing, wrong_kind = [], []
    for block_id, destination_id, kind in refs:
        destination = owned.get(destination_id)
        if destination is None:
            missing.append(block_id)
        elif destination.kind != kind:
            wrong_kind.append(
                (
                    block_id,
                    f"destination {destination.name!r} is a {destination.kind} destination; "
                    f"this block needs a {kind} destination",
                )
            )
    return missing, wrong_kind


class DestinationGone(Exception):
    """A destination a block names is deleted, another user's, or not the kind it needs."""


async def current_destination(
    db: AsyncSession, destination_id: uuid.UUID | None, user_id: uuid.UUID, kind: str
) -> Destination:
    """The destination as it is now, read at each attempt (ADR-015 §2b). Raises DestinationGone."""
    destination = await db.get(Destination, destination_id) if destination_id else None
    # Another user's reads as deleted: a run never delivers with someone else's credentials.
    if destination is None or destination.user_id != user_id:
        raise DestinationGone("destination deleted")
    if destination.kind != kind:
        raise DestinationGone(f"destination is a {destination.kind} destination, not {kind}")
    return destination


def webhook_target(destination: Destination) -> tuple[str, bytes]:
    """A webhook destination's URL and signing key (b"" when it has none). Raises InvalidToken."""
    secret = decrypt_secret(destination.encrypted_secret)
    return secret["url"], (secret.get("secret") or "").encode()
