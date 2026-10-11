import asyncio
from collections.abc import Coroutine
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError, RetryState
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from app.pipelines.catalog import BLOCK_OVERHEAD_SECONDS, CATALOG, InputBinding, Timing
    from app.pipelines.definition import (
        CleanBlock,
        LLMExtractBlock,
        RunBlock,
        ScrapeBlock,
        ValidateBlock,
        WebhookBlock,
    )
    from app.schemas.jobs import Engine
    from app.workflows.activities.contracts import (
        CLEAN_ACTIVITY,
        LLM_EXTRACT_ACTIVITY,
        SCRAPE_ACTIVITY,
        VALIDATE_ACTIVITY,
        CleanInput,
        CleanOutput,
        Credentials,
        LLMInput,
        LLMOutput,
        ScrapeInput,
        ScrapeOptions,
        ScrapeOutput,
        StoredObject,
        ValidateInput,
    )
    from app.workflows.queues import (
        CONTENT_QUEUE,
        LLM_QUEUE,
        SCRAPE_HTTP_QUEUE,
        SCRAPE_PLAYWRIGHT_QUEUE,
        WORKFLOW_QUEUE,
    )

MIRROR_BLOCK_ACTIVITY = "mirror_block_status"
MIRROR_RUN_ACTIVITY = "mirror_run_status"
RECORD_STORAGE_ACTIVITY = "record_storage"
CHECK_CANCELLED_ACTIVITY = "check_cancelled"
WEBHOOK_ACTIVITY = "deliver_webhook"

# The webhook payload's event for a pipeline run's delivery (job lane: job.completed).
WEBHOOK_EVENT = "pipeline.result"

# Raised non-retryable by an activity that finds its run cancelled (ADR-009 15d's fallback).
RUN_CANCELLED_ERROR = "RunCancelled"

RunStatus = Literal["running", "completed", "failed", "cancelled"]


class LLMKey(BaseModel):
    provider: Literal["anthropic", "openai_compatible"]
    # Fernet ciphertext — workflow history stores inputs in plain JSON.
    encrypted_api_key: str
    base_url: str | None = None


class PipelineRunInput(BaseModel):
    run_id: str
    pipeline_id: str
    blocks: list[RunBlock]
    # The run ceiling, resolved at the trigger: declared, or what the blocks need.
    time_budget_seconds: int
    inputs: dict[str, str] = {}
    # pipeline_run_blocks.id per block id: each block's artifact key (ADR-011).
    artifact_ids: dict[str, str]
    llm_keys: dict[str, LLMKey] = {}
    # Scrape blocks' stored secrets, by block id.
    credentials: dict[str, Credentials] = {}
    # Webhook blocks' signing secrets as Fernet ciphertext (credentials key), by block id.
    webhook_secrets: dict[str, str] = {}


class MirrorBlockInput(BaseModel):
    run_id: str
    block_id: str
    status: Literal["running", "waiting", "completed", "failed"]
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None


class MirrorRunInput(BaseModel):
    run_id: str
    status: Literal["completed", "failed", "cancelled"]
    result_path: str | None = None
    error: str | None = None


class RecordStorageInput(BaseModel):
    run_id: str
    block_id: str
    objects: list[StoredObject]


class StorageUsage(BaseModel):
    used: int
    limit: int


class RunRef(BaseModel):
    run_id: str


class WebhookInput(BaseModel):
    run_id: str
    pipeline_id: str
    block_id: str
    url: str
    # The block's input reference: what is delivered.
    result_path: str
    encrypted_secret: str | None = None
    # Fixed by the workflow so every attempt signs the same bytes.
    timestamp: str
    # The retry policy's; the last attempt mirrors no `waiting`.
    maximum_attempts: int


# Three per block — running, record_storage, completed — must fit BLOCK_OVERHEAD_SECONDS.
DB_ACTIVITY_SECONDS = 20
assert 3 * DB_ACTIVITY_SECONDS <= BLOCK_OVERHEAD_SECONDS


async def _db(activity: str, arg: BaseModel, result_type: type) -> Any:
    return await workflow.execute_activity(
        activity,
        arg,
        task_queue=WORKFLOW_QUEUE,
        result_type=result_type,
        start_to_close_timeout=timedelta(seconds=10),
        schedule_to_close_timeout=timedelta(seconds=DB_ACTIVITY_SECONDS),
        retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=5)),
    )


def _block_options(timing: Timing, remaining_s: float) -> dict[str, Any]:
    options: dict[str, Any] = {
        "schedule_to_start_timeout": timedelta(seconds=timing.schedule_to_start),
        "start_to_close_timeout": timedelta(seconds=timing.start_to_close),
        # The block's budget, cut to what is left of the run's.
        "schedule_to_close_timeout": timedelta(seconds=min(timing.budget, remaining_s)),
        # Only reached for an interruptible block: its wait ends, a running attempt finishes.
        "cancellation_type": ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        "retry_policy": RetryPolicy(
            initial_interval=timedelta(seconds=timing.initial_interval),
            backoff_coefficient=timing.backoff_coefficient,
            maximum_interval=timedelta(seconds=timing.maximum_interval),
            maximum_attempts=timing.maximum_attempts,
        ),
    }
    if timing.heartbeat is not None:
        options["heartbeat_timeout"] = timedelta(seconds=timing.heartbeat)
    return options


def _is_cancellation(exc: BaseException) -> bool:
    """A block stopped by a cancel: Temporal's (a wait interrupted) or the row's (15d fallback)."""
    cause = exc.cause if isinstance(exc, ActivityError) else exc
    if isinstance(cause, CancelledError | asyncio.CancelledError):
        return True
    return isinstance(cause, ApplicationError) and cause.type == RUN_CANCELLED_ERROR


def _failure_message(exc: Exception, maximum_attempts: int) -> str:
    if isinstance(exc, ActivityError):
        message = str(exc.cause or exc)
        # Neither error carries an attempt count; the activity cannot know which is its last.
        if exc.retry_state == RetryState.MAXIMUM_ATTEMPTS_REACHED:
            message += f" (gave up after {maximum_attempts} attempts)"
        return message
    if isinstance(exc, ApplicationError):
        return str(exc)
    # Not a FailureError: left to propagate, it fails the workflow task, which Temporal
    # retries forever — the run stays running and holds its slot.
    return f"internal error: {type(exc).__name__}: {exc}"


@workflow.defn
class PipelineWorkflow:
    """Runs a pinned definition block by block; mirrors each status into the app DB.

    A cancel is honoured at the next block boundary, never mid-block (R3): from Temporal
    (``handle.cancel()``) or from the row — the running-mirror refuses to start a block on a
    run that is no longer running or whose cancel was requested.
    """

    def __init__(self) -> None:
        self._cancel_requested = False

    @workflow.run
    async def run(self, input: PipelineRunInput) -> str | None:
        deadline = workflow.now() + timedelta(seconds=input.time_budget_seconds)
        refs: dict[str, str] = {}
        result_path: str | None = None
        wall: str | None = None
        for block in input.blocks:
            block_type = CATALOG[block.type]
            input_ref = refs.get(block.input) if block.input else None
            if self._cancel_requested or (
                await self._mirror_block(input.run_id, block.id, "running", input_ref=input_ref)
                != "running"
            ):
                return await self._cancel(input.run_id)

            timing = block_type.timing(block.config)
            try:
                # ADR-009 15a/15d: an effect block may wait between attempts, and a cancel
                # interrupts that wait; a running attempt still finishes (R3).
                objects = await self._finish(
                    self._execute(block, input, input_ref, deadline),
                    interruptible=block_type.kind == "effect",
                )
                usage = None
                if objects:
                    usage = await self._finish(
                        _db(
                            RECORD_STORAGE_ACTIVITY,
                            RecordStorageInput(
                                run_id=input.run_id, block_id=block.id, objects=objects
                            ),
                            StorageUsage,
                        )
                    )
            except workflow.NondeterminismError:
                # Code no longer matches history: the task retries until a deploy fixes it.
                raise
            except (Exception, asyncio.CancelledError) as exc:
                if _is_cancellation(exc):
                    await self._mirror_block(
                        input.run_id, block.id, "failed", error="cancelled before it finished"
                    )
                    return await self._cancel(input.run_id)
                if not isinstance(exc, Exception):
                    raise
                message = _failure_message(exc, timing.maximum_attempts)
                await self._mirror_block(input.run_id, block.id, "failed", error=message)
                await self._mirror_run(input.run_id, "failed", error=f"{block.id}: {message}")
                raise ApplicationError(f"{block.id}: {message}", non_retryable=True) from exc

            # ADR-009 §5: content blocks write an object; effect blocks pass their input on.
            output_ref = objects[0].path if block_type.kind == "content" else input_ref
            assert output_ref is not None
            refs[block.id] = output_ref
            if block_type.kind == "content":
                result_path = output_ref
            if wall is None and usage is not None and usage.used > usage.limit:
                # §8d: the run finishes and is charged; the headroom buffer stops the next one.
                wall = (
                    f"storage_wall: block {block.id!r} took storage to {usage.used} of "
                    f"{usage.limit} bytes; the run completed and is charged. Delete results "
                    "to start new runs"
                )
            await self._mirror_block(input.run_id, block.id, "completed", output_ref=output_ref)

        await self._mirror_run(input.run_id, "completed", result_path=result_path, error=wall)
        return result_path

    async def _finish(self, coro: Coroutine[Any, Any, Any], interruptible: bool = False) -> Any:
        """Await to completion even if the workflow is cancelled meanwhile (R3: never mid-block).

        ``interruptible`` passes the cancel on to the activity: its retry wait ends at once, a
        running attempt still completes (``WAIT_CANCELLATION_COMPLETED``).
        """
        task = asyncio.ensure_future(coro)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    raise
                if interruptible and not self._cancel_requested:
                    task.cancel()
                self._cancel_requested = True
        return task.result()

    async def _mirror_block(
        self,
        run_id: str,
        block_id: str,
        status: Literal["running", "waiting", "completed", "failed"],
        **fields: str | None,
    ) -> RunStatus:
        return await self._finish(
            _db(
                MIRROR_BLOCK_ACTIVITY,
                MirrorBlockInput(run_id=run_id, block_id=block_id, status=status, **fields),
                str,
            )
        )

    async def _mirror_run(
        self,
        run_id: str,
        status: Literal["completed", "failed", "cancelled"],
        **fields: str | None,
    ) -> RunStatus:
        return await self._finish(
            _db(MIRROR_RUN_ACTIVITY, MirrorRunInput(run_id=run_id, status=status, **fields), str)
        )

    async def _cancel(self, run_id: str) -> None:
        # Completed blocks keep their outputs (R3); the pending tail is skipped by the mirror.
        await self._mirror_run(run_id, "cancelled")
        if self._cancel_requested:
            raise asyncio.CancelledError()
        return None

    async def _execute(
        self,
        block: RunBlock,
        input: PipelineRunInput,
        input_ref: str | None,
        deadline: datetime,
    ) -> list[StoredObject]:
        """Runs the block's activity on its queue; returns what it stored, result first."""
        remaining_s = (deadline - workflow.now()).total_seconds()
        if remaining_s <= 0:
            raise ApplicationError("the run's time budget is spent", non_retryable=True)
        options = _block_options(CATALOG[block.type].timing(block.config), remaining_s)
        artifact_id = input.artifact_ids[block.id]

        if isinstance(block, ScrapeBlock):
            config = block.config
            url = (
                input.inputs[config.url.input]
                if isinstance(config.url, InputBinding)
                else str(config.url)
            )
            playwright = config.engine == Engine.playwright
            scraped = await workflow.execute_activity(
                SCRAPE_ACTIVITY,
                ScrapeInput(
                    artifact_id=artifact_id,
                    url=url,
                    output_format=config.output_format.value,
                    credentials=input.credentials.get(block.id),
                    options=ScrapeOptions(
                        respect_robots=config.respect_robots, actions=config.actions
                    ),
                    playwright_options=(
                        config.playwright_options.model_dump(mode="json")
                        if playwright and config.playwright_options
                        else None
                    ),
                ),
                task_queue=SCRAPE_PLAYWRIGHT_QUEUE if playwright else SCRAPE_HTTP_QUEUE,
                result_type=ScrapeOutput,
                **options,
            )
            return [scraped.result, *scraped.screenshots]

        if isinstance(block, LLMExtractBlock):
            if input_ref is None:
                raise ApplicationError("llm_extract has no input", non_retryable=True)
            config = block.config
            key = input.llm_keys[str(config.llm_key_id)]
            extracted = await workflow.execute_activity(
                LLM_EXTRACT_ACTIVITY,
                LLMInput(
                    artifact_id=artifact_id,
                    raw_minio_path=input_ref,
                    provider=key.provider,
                    encrypted_api_key=key.encrypted_api_key,
                    base_url=key.base_url,
                    model=config.model,
                    output_schema=config.output_schema,
                ),
                task_queue=LLM_QUEUE,
                result_type=LLMOutput,
                **options,
            )
            return [extracted.result]

        if isinstance(block, CleanBlock):
            if input_ref is None:
                raise ApplicationError("clean has no input", non_retryable=True)
            cleaned = await workflow.execute_activity(
                CLEAN_ACTIVITY,
                CleanInput(artifact_id=artifact_id, input_path=input_ref),
                task_queue=CONTENT_QUEUE,
                result_type=CleanOutput,
                **options,
            )
            return [cleaned.result]

        if isinstance(block, ValidateBlock):
            if input_ref is None:
                raise ApplicationError("validate has no input", non_retryable=True)
            await workflow.execute_activity(
                VALIDATE_ACTIVITY,
                ValidateInput(
                    input_path=input_ref,
                    rules=[r.model_dump(mode="json", by_alias=True) for r in block.config.rules],
                ),
                task_queue=CONTENT_QUEUE,
                **options,
            )
            return []

        if isinstance(block, WebhookBlock):
            if input_ref is None:
                raise ApplicationError("webhook has no input", non_retryable=True)
            # The retry policy is the delivery loop (ADR-009 §15); the activity mirrors
            # running/waiting per attempt itself, since the workflow is blocked across retries.
            await workflow.execute_activity(
                WEBHOOK_ACTIVITY,
                WebhookInput(
                    run_id=input.run_id,
                    pipeline_id=input.pipeline_id,
                    block_id=block.id,
                    url=str(block.config.url),
                    result_path=input_ref,
                    encrypted_secret=input.webhook_secrets.get(block.id),
                    timestamp=workflow.now().isoformat(),
                    maximum_attempts=options["retry_policy"].maximum_attempts,
                ),
                task_queue=WORKFLOW_QUEUE,
                **options,
            )
            return []

        raise ApplicationError(f"block type {block.type!r} cannot run yet", non_retryable=True)
