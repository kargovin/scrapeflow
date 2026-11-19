"""A pipeline run's rows and its workflow input, built in one place (ADR-016 §3b).

The API's trigger, a monitor's Check now and a monitor firing's admission all start runs; each
builds them here, so a check and a hand-run cannot drift (P9's lesson, applied to pipelines).
Pure: the caller adds the rows to its session and commits before it starts the workflow.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet

from app.models.llm_keys import UserLLMKey
from app.models.pipeline import PipelineRun, PipelineRunBlock, PipelineVersion
from app.pipelines.definition import ParsedDefinition, ScrapeBlock, WebhookBlock
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
