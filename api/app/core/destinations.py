"""Destinations (ADR-015 §2): the secret's encryption and what refers to a destination."""

import json
import uuid
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.destination import PipelineVersionDestination
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
