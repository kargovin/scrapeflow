from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.workflows import steps
    from app.workflows.activities.contracts import LLMInput, ScrapeInput, ScrapeOptions
    from app.workflows.queues import WORKFLOW_QUEUE

MIRROR_BLOCK_ACTIVITY = "mirror_block_status"
MIRROR_RUN_ACTIVITY = "mirror_run_status"

# MVP: the block types PipelineWorkflow can execute; the trigger refuses the rest.
SUPPORTED_BLOCK_TYPES = frozenset({"scrape", "llm_extract"})


class LLMKey(BaseModel):
    provider: Literal["anthropic", "openai_compatible"]
    # Fernet ciphertext — workflow history stores inputs in plain JSON.
    encrypted_api_key: str
    base_url: str | None = None


class PipelineRunInput(BaseModel):
    run_id: str
    definition: dict[str, Any]
    inputs: dict[str, str] = {}
    # pipeline_run_blocks.id per block id: each block's artifact key (ADR-011).
    artifact_ids: dict[str, str]
    llm_keys: dict[str, LLMKey] = {}


class MirrorBlockInput(BaseModel):
    run_id: str
    block_id: str
    status: Literal["running", "completed", "failed"]
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None


class MirrorRunInput(BaseModel):
    run_id: str
    status: Literal["completed", "failed"]
    result_path: str | None = None
    error: str | None = None


def resolve_url(config: dict[str, Any], inputs: dict[str, str]) -> str:
    url = config["url"]
    return inputs[url["$input"]] if isinstance(url, dict) else url


async def _mirror(activity: str, input: BaseModel) -> None:
    await workflow.execute_activity(
        activity,
        input,
        task_queue=WORKFLOW_QUEUE,
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=RetryPolicy(maximum_attempts=5),
    )


@workflow.defn
class PipelineWorkflow:
    """Runs a pinned definition block by block; mirrors each status into the app DB."""

    @workflow.run
    async def run(self, input: PipelineRunInput) -> str | None:
        refs: dict[str, str] = {}
        result_path: str | None = None
        for block in input.definition["blocks"]:
            block_id = block["id"]
            input_ref = refs.get(block["input"]) if block.get("input") else None
            await _mirror(
                MIRROR_BLOCK_ACTIVITY,
                MirrorBlockInput(
                    run_id=input.run_id, block_id=block_id, status="running", input_ref=input_ref
                ),
            )
            try:
                output_ref = await self._execute(block, input, input_ref)
            except (ActivityError, ApplicationError) as exc:
                message = str(exc.cause or exc) if isinstance(exc, ActivityError) else str(exc)
                await _mirror(
                    MIRROR_BLOCK_ACTIVITY,
                    MirrorBlockInput(
                        run_id=input.run_id, block_id=block_id, status="failed", error=message
                    ),
                )
                await _mirror(
                    MIRROR_RUN_ACTIVITY,
                    MirrorRunInput(
                        run_id=input.run_id, status="failed", error=f"{block_id}: {message}"
                    ),
                )
                raise ApplicationError(f"{block_id}: {message}", non_retryable=True) from exc

            refs[block_id] = output_ref
            result_path = output_ref
            await _mirror(
                MIRROR_BLOCK_ACTIVITY,
                MirrorBlockInput(
                    run_id=input.run_id,
                    block_id=block_id,
                    status="completed",
                    output_ref=output_ref,
                ),
            )

        await _mirror(
            MIRROR_RUN_ACTIVITY,
            MirrorRunInput(run_id=input.run_id, status="completed", result_path=result_path),
        )
        return result_path

    async def _execute(
        self, block: dict[str, Any], input: PipelineRunInput, input_ref: str | None
    ) -> str:
        config = block["config"]
        artifact_id = input.artifact_ids[block["id"]]
        if block["type"] == "scrape":
            scraped = await steps.scrape(
                ScrapeInput(
                    artifact_id=artifact_id,
                    url=resolve_url(config, input.inputs),
                    output_format=config["output_format"],
                    options=ScrapeOptions(
                        respect_robots=config["respect_robots"], actions=config["actions"]
                    ),
                    playwright_options=config["playwright_options"],
                ),
                config["engine"],
            )
            return scraped.result.path
        if block["type"] == "llm_extract":
            key = input.llm_keys[config["llm_key_id"]]
            extracted = await steps.llm_extract(
                LLMInput(
                    artifact_id=artifact_id,
                    raw_minio_path=input_ref,
                    provider=key.provider,
                    encrypted_api_key=key.encrypted_api_key,
                    base_url=key.base_url,
                    model=config["model"],
                    output_schema=config["output_schema"],
                )
            )
            return extracted.result.path
        raise ApplicationError(f"block type {block['type']!r} is not supported yet")
