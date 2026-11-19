import asyncio
from collections.abc import Coroutine
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError, RetryState
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from app.pipelines.catalog import (
        BLOCK_OVERHEAD_SECONDS,
        CATALOG,
        DB_ACTIVITY_SECONDS,
        HOST_SLOT_SECONDS,
        RELEASE_OUTPUTS_TIMING,
        WEBHOOK_RESULT_EVENT,
        InputBinding,
        RunInputValue,
        Timing,
    )
    from app.pipelines.definition import (
        CleanBlock,
        LLMExtractBlock,
        RunBlock,
        ScrapeBlock,
        ValidateBlock,
        WebhookBlock,
    )
    from app.schemas.jobs import Engine
    from app.workflows import steps
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
        ValidateVerdict,
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
RELEASE_RUN_OUTPUTS_ACTIVITY = "release_run_outputs"

# ADR-012: a Validate's stop mode, its bound rule values and the stop path (ADR-014 §6, J.6).
VALIDATE_STOP_PATCH = "validate-stop"
# ADR-012: a Webhook block that only sends pipeline.failed is skipped on a succeeding run (J.7).
WEBHOOK_EVENTS_PATCH = "webhook-events"

# The webhook payload's event for a pipeline run's delivery (job lane: job.completed).
WEBHOOK_EVENT = WEBHOOK_RESULT_EVENT

# Raised non-retryable by an activity that finds its run cancelled (ADR-009 15d's fallback).
RUN_CANCELLED_ERROR = "RunCancelled"
# Raised retryable by release_run_outputs; its one detail is the object keys still held.
OUTPUTS_LEFT_ERROR = "OutputsLeft"

RunStatus = Literal["running", "completed", "failed", "cancelled"]
# ADR-014 §5a; each list matches its CHECK in migration 4.10 — a new value widens both.
SkipReason = Literal["stopped", "upstream_failed", "cancelled", "not_started", "alert_only"]
StopReason = Literal["rule_not_met"]


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
    inputs: dict[str, RunInputValue] = {}
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
    status: Literal["running", "waiting", "completed", "failed", "skipped"]
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    # skipped only.
    status_reason: SkipReason | None = None
    reason_block_id: str | None = None
    # completed only: a stop-mode Validate's verdict, less its outcome.
    verdict: dict[str, Any] | None = None


class MirrorRunInput(BaseModel):
    run_id: str
    status: Literal["completed", "failed", "cancelled"]
    result_path: str | None = None
    error: str | None = None
    # completed only: the block that ended the run early.
    stopped_at_block_id: str | None = None
    stop_reason: StopReason | None = None
    # failed only.
    failed_block_id: str | None = None

    @model_validator(mode="after")
    def _stop_names_block_and_reason(self) -> "MirrorRunInput":
        # The CHECK would refuse it on every retry.
        if (self.stopped_at_block_id is None) != (self.stop_reason is None):
            raise ValueError("stopped_at_block_id and stop_reason go together")
        return self


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
assert 3 * DB_ACTIVITY_SECONDS <= BLOCK_OVERHEAD_SECONDS
# A Scrape block's host-slot wait: the cap, its last acquire, the release.
assert (
    steps.HOST_WAIT_CAP + 2 * steps.HOST_SLOT_ACTIVITY_TIMEOUT
).total_seconds() <= HOST_SLOT_SECONDS


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


def _bound_rules(block: ValidateBlock, inputs: dict[str, RunInputValue]) -> list[dict[str, Any]]:
    """The rules as the worker reads them, each bound value replaced by its run input's."""
    rules = []
    for rule in block.config.rules:
        dumped = rule.model_dump(mode="json", by_alias=True)
        for field in ("value", "text"):
            bound = getattr(rule, field, None)
            if isinstance(bound, InputBinding):
                dumped[field] = inputs[bound.input]
        rules.append(dumped)
    return rules


def _kept_objects(exc: ActivityError) -> list[str]:
    cause = exc.cause
    if isinstance(cause, ApplicationError) and cause.type == OUTPUTS_LEFT_ERROR and cause.details:
        return list(cause.details[0])
    # Gave up without a count (e.g. timed out): the result is assumed still there.
    return []


def _scrape_url(block: ScrapeBlock, input: PipelineRunInput) -> str:
    url = block.config.url
    return str(input.inputs[url.input]) if isinstance(url, InputBinding) else str(url)


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
            verdict: ValidateVerdict | None = None
            if (
                isinstance(block, WebhookBlock)
                and WEBHOOK_RESULT_EVENT not in block.config.events
                and workflow.patched(WEBHOOK_EVENTS_PATCH)
            ):
                if self._cancel_requested:
                    return await self._cancel(input.run_id)
                # E1: it sends only when the run breaks, through webhook_deliveries (§8).
                await self._mirror_block(
                    input.run_id, block.id, "skipped", status_reason="alert_only"
                )
                assert input_ref is not None
                refs[block.id] = input_ref
                continue
            if self._cancel_requested or (
                await self._mirror_block(input.run_id, block.id, "running", input_ref=input_ref)
                != "running"
            ):
                return await self._cancel(input.run_id)

            timing = block_type.timing(block.config)
            try:
                # ADR-009 15a/15d: an effect block may wait between attempts, and a cancel
                # interrupts that wait; a running attempt still finishes (R3).
                if isinstance(block, ScrapeBlock) and workflow.patched(steps.HOST_SLOT_PATCH):
                    objects = await self._scrape_in_host_slot(
                        block, input, input_ref, deadline, timing
                    )
                else:
                    outcome = await self._finish(
                        self._execute(block, input, input_ref, deadline),
                        interruptible=block_type.kind == "effect",
                    )
                    if isinstance(outcome, ValidateVerdict):
                        verdict, objects = outcome, []
                    else:
                        objects = outcome
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
                await self._mirror_run(
                    input.run_id, "failed", error=f"{block.id}: {message}", failed_block_id=block.id
                )
                raise ApplicationError(f"{block.id}: {message}", non_retryable=True) from exc

            if verdict is not None and verdict.outcome == "not_met":
                assert isinstance(block, ValidateBlock)
                return await self._stop(input, block, verdict, input_ref, result_path, wall)

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

    async def _stop(
        self,
        input: PipelineRunInput,
        block: ValidateBlock,
        verdict: ValidateVerdict,
        input_ref: str | None,
        result_path: str | None,
        wall: str | None,
    ) -> str | None:
        """A stop-mode Validate's rule was not met: the run ends completed, here (ADR-014 §6)."""
        assert verdict.rule_index is not None
        # The rule as saved: a bound value stays a binding; `expected` holds what was compared.
        saved = block.config.rules[verdict.rule_index].model_dump(mode="json", by_alias=True)
        # The change gate's two fields only when set: other verdicts keep their recorded shape.
        unset = {k for k in ("fingerprint", "matched") if getattr(verdict, k) is None}
        recorded = verdict.model_dump(mode="json", exclude={"outcome"} | unset) | {"rule": saved}
        await self._mirror_block(
            input.run_id, block.id, "completed", output_ref=input_ref, verdict=recorded
        )
        notes = [wall] if wall else []
        if block.config.on_stop_outputs == "delete":
            try:
                await self._finish(
                    workflow.execute_activity(
                        RELEASE_RUN_OUTPUTS_ACTIVITY,
                        RunRef(run_id=input.run_id),
                        task_queue=WORKFLOW_QUEUE,
                        # Its own full budget: the Validate's need reserves it (Timing.after).
                        **_block_options(RELEASE_OUTPUTS_TIMING, RELEASE_OUTPUTS_TIMING.budget),
                    )
                )
                result_path = None
            except ActivityError as exc:
                # E3: the run still ends stopped; what is left stays on disk and charged.
                kept = _kept_objects(exc)
                if kept and result_path not in kept:
                    result_path = None
                count = f"{len(kept)} stored objects" if kept else "stored objects"
                notes.append(f"stopped; {count} could not be deleted and are kept and charged")
        await self._mirror_run(
            input.run_id,
            "completed",
            result_path=result_path,
            error="; ".join(notes) or None,
            stopped_at_block_id=block.id,
            stop_reason="rule_not_met",
        )
        return result_path

    async def _mirror_block(
        self,
        run_id: str,
        block_id: str,
        status: Literal["running", "waiting", "completed", "failed", "skipped"],
        **fields: Any,
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

    async def _scrape_in_host_slot(
        self,
        block: ScrapeBlock,
        input: PipelineRunInput,
        input_ref: str | None,
        deadline: datetime,
        timing: Timing,
    ) -> list[StoredObject]:
        # The block's activity can hold the slot no longer than its budget (schedule_to_close).
        lease_ttl = timedelta(seconds=timing.budget) + steps.HOST_LEASE_MARGIN
        try:
            held = await steps.wait_for_host_slot(_scrape_url(block, input), lease_ttl)
        except asyncio.CancelledError:
            # Cancelled while waiting for the host: nothing was scraped.
            self._cancel_requested = True
            raise
        try:
            # After the wait, so the block's budget is cut to what the run has left.
            return await self._finish(self._execute(block, input, input_ref, deadline))
        finally:
            await self._finish(steps.release_host_slot(held))

    async def _execute(
        self,
        block: RunBlock,
        input: PipelineRunInput,
        input_ref: str | None,
        deadline: datetime,
    ) -> list[StoredObject] | ValidateVerdict:
        """Runs the block's activity on its queue; returns what it stored, result first.

        A stop-mode Validate returns its verdict instead (it stores nothing).
        """
        remaining_s = (deadline - workflow.now()).total_seconds()
        if remaining_s <= 0:
            raise ApplicationError("the run's time budget is spent", non_retryable=True)
        options = _block_options(CATALOG[block.type].timing(block.config), remaining_s)
        artifact_id = input.artifact_ids[block.id]

        if isinstance(block, ScrapeBlock):
            config = block.config
            url = _scrape_url(block, input)
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
            if workflow.patched(VALIDATE_STOP_PATCH):
                verdict = await workflow.execute_activity(
                    VALIDATE_ACTIVITY,
                    ValidateInput(
                        input_path=input_ref,
                        rules=_bound_rules(block, input.inputs),
                        when_unmet=block.config.when_unmet,
                    ),
                    task_queue=CONTENT_QUEUE,
                    result_type=ValidateVerdict | None,
                    **options,
                )
                return verdict if verdict is not None else []
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
