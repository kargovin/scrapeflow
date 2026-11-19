"""A pipeline run's rows and its workflow input, built in one place (ADR-016 §3b).

The API's trigger, a monitor's Check now and a monitor firing's admission all start runs; each
builds them here, so a check and a hand-run cannot drift (P9's lesson, applied to pipelines).
``build_run`` is pure: the caller adds the rows to its session and commits before it starts the
workflow. ``mark_not_started`` is what every caller does when that start fails.
"""

import json
import uuid
from asyncio import get_running_loop
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import webhook_delivery
from app.core.monitor_checks import after_commit, monitor_check_ended
from app.core.pipeline_alerts import queue_failure_alerts
from app.core.quota import lock_user
from app.core.security import validate_no_ssrf
from app.models.llm_keys import UserLLMKey
from app.models.pipeline import PipelineRun, PipelineRunBlock, PipelineVersion
from app.pipelines.catalog import InputBinding
from app.pipelines.definition import LLMExtractBlock, ParsedDefinition, ScrapeBlock, WebhookBlock
from app.settings import settings
from app.workflows.activities.contracts import Credentials
from app.workflows.pipeline import LLMKey, PipelineRunInput


def workflow_id(run_id: object) -> str:
    return f"pipeline-run-{run_id}"


def encrypt_secret(value: Any) -> str:
    """Fernet ciphertext under the credentials key — how block secrets are stored and sent."""
    plain = value if isinstance(value, str) else json.dumps(value)
    return Fernet(settings.credentials_encryption_key).encrypt(plain.encode()).decode()


def scrape_credentials(secrets: Mapping[str, str]) -> Credentials | None:
    # As jobs resolve them: the block's proxy, else the platform default; cookies as stored.
    proxy = secrets.get("proxy_url")
    if proxy is None and settings.default_proxy_url:
        proxy = encrypt_secret(settings.default_proxy_url)
    cookies = secrets.get("cookies")
    if proxy is None and cookies is None:
        return None
    return Credentials(encrypted_proxy_url=proxy, encrypted_cookies=cookies)


async def ssrf_check(url: str) -> None:
    """Raises the API's 400 on a private address. A DNS lookup: never under ``lock_user``."""
    await get_running_loop().run_in_executor(None, validate_no_ssrf, url)


def llm_key_ids(parsed: ParsedDefinition) -> set[uuid.UUID]:
    return {b.config.llm_key_id for b in parsed.blocks if isinstance(b, LLMExtractBlock)}


async def owned_llm_keys(
    db: AsyncSession, user_id: uuid.UUID, key_ids: set[uuid.UUID]
) -> dict[str, UserLLMKey]:
    """The user's keys among ``key_ids``, by id as a string; another user's key is left out."""
    if not key_ids:
        return {}
    owned = await db.scalars(
        select(UserLLMKey).where(UserLLMKey.id.in_(key_ids), UserLLMKey.user_id == user_id)
    )
    return {str(k.id): k for k in owned}


def run_urls(
    parsed: ParsedDefinition, inputs: Mapping[str, Any], keys: Mapping[str, UserLLMKey]
) -> list[str]:
    """Every address a run reaches that the trigger checks for SSRF before it starts."""
    urls = []
    for block in parsed.blocks:
        if isinstance(block, ScrapeBlock):
            url = block.config.url
            urls.append(str(inputs[url.input]) if isinstance(url, InputBinding) else str(url))
        elif isinstance(block, WebhookBlock):
            urls.append(str(block.config.url))
    urls += [k.base_url for k in keys.values() if k.base_url]
    return urls


async def mark_not_started(
    db: AsyncSession,
    run: PipelineRun,
    blocks: list[PipelineRunBlock],
    exc: Exception,
    get_client: Callable[[], Any],
) -> None:
    """The workflow could not start: the run fails, its blocks never ran, and — in the same
    transaction — its failure alerts are queued and a monitor's check is counted (§7a)."""
    if run.monitor_id is not None:
        # User then run, as every writer of a monitor's state (ADR-016 §8).
        await lock_user(run.user_id, db)
    run.status = "failed"
    run.error = f"could not start workflow: {exc}"
    run.finished_at = datetime.now(UTC)
    for block in blocks:
        block.status = "skipped"
        block.status_reason = "not_started"
    alerts = await queue_failure_alerts(db, run)
    ended = await monitor_check_ended(db, run)
    await db.commit()
    if alerts:
        # The engine is likely down: the sweep sends what this cannot start.
        await webhook_delivery.start_pending(get_client, db, pipeline_run_id=run.id)
    await after_commit(_awaitable(get_client), ended)


def _awaitable(get_client: Callable[[], Any]) -> Callable[[], Any]:
    async def client():
        return get_client()

    return client


@dataclass(frozen=True)
class BuiltRun:
    run: PipelineRun
    blocks: list[PipelineRunBlock]
    input: PipelineRunInput


def build_run(
    *,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    version: PipelineVersion,
    parsed: ParsedDefinition,
    stored_secrets: Mapping[str, Mapping[str, str]],
    keys: Mapping[str, UserLLMKey],
    inputs: dict[str, Any],
) -> BuiltRun:
    """The run row (``running``), one ``pending`` row per block, and the workflow's input.

    ``keys``: the LLM keys the blocks name, by id as a string, already checked as the user's.
    """
    run = PipelineRun(
        id=run_id,
        pipeline_id=version.pipeline_id,
        pipeline_version_id=version.id,
        user_id=user_id,
        status="running",
        inputs=inputs,
        workflow_id=workflow_id(run_id),
    )
    blocks = [
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
    run_input = PipelineRunInput(
        run_id=str(run_id),
        pipeline_id=str(version.pipeline_id),
        blocks=parsed.blocks,
        time_budget_seconds=parsed.run_budget_seconds,
        inputs=inputs,
        artifact_ids={b.block_id: str(b.id) for b in blocks},
        llm_keys={
            key_id: LLMKey(
                provider=k.provider, encrypted_api_key=k.encrypted_api_key, base_url=k.base_url
            )
            for key_id, k in keys.items()
        },
        credentials={
            b.id: c
            for b in parsed.blocks
            if isinstance(b, ScrapeBlock)
            and (c := scrape_credentials(stored_secrets.get(b.id, {}))) is not None
        },
        webhook_secrets={
            b.id: secret
            for b in parsed.blocks
            if isinstance(b, WebhookBlock)
            and (secret := stored_secrets.get(b.id, {}).get("secret")) is not None
        },
    )
    return BuiltRun(run, blocks, run_input)
