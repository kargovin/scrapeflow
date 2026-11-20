"""Destinations (ADR-015 §2): the secret's encryption and what refers to a destination."""

import json
import uuid
from collections.abc import Iterable
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.destination import Destination, PipelineVersionDestination
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
