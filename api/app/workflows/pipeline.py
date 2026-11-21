import asyncio
from collections.abc import Coroutine
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    RetryState,
    TimeoutType,
)
from temporalio.exceptions import TimeoutError as ActivityTimeoutError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from app.pipelines.catalog import (
        BLOCK_OVERHEAD_SECONDS,
        CAPTURE_TIMING,
        CATALOG,
        DB_ACTIVITY_SECONDS,
        DIFF_TIMING,
        EXTRACTION,
        HOST_SLOT_SECONDS,
        PAGE_HTML,
        PAGE_JSON,
        PAGE_MARKDOWN,
        RELEASE_OUTPUTS_TIMING,
        WEBHOOK_RESULT_EVENT,
        InputBinding,
        RunInputValue,
        Timing,
        block_outputs,
        first_change_gate,
        is_change_gate,
    )
    from app.pipelines.definition import (
        ApprovalBlock,
        BucketBlock,
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
        CAPTURE_ACTIVITY,
        CLEAN_ACTIVITY,
        DELIVER_BUCKET_ACTIVITY,
        DIFF_ACTIVITY,
        LLM_EXTRACT_ACTIVITY,
        NOT_WRITTEN,
        SCRAPE_ACTIVITY,
        UNDO_BUCKET_ACTIVITY,
        VALIDATE_ACTIVITY,
        BucketLocation,
        CaptureInput,
        CaptureOutput,
        CarriedValue,
        CleanInput,
        CleanOutput,
        Credentials,
        DeliverBucketInput,
        DeliverBucketOutput,
        DiffInput,
        DiffOutput,
        LLMInput,
        LLMOutput,
        ScrapeInput,
        ScrapeOptions,
        ScrapeOutput,
        StoredObject,
        UndoBucketInput,
        UndoBucketOutput,
        ValidateInput,
        ValidateVerdict,
        object_key,
    )
    from app.workflows.queues import (
        CONTENT_QUEUE,
        DELIVERY_QUEUE,
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
LOAD_DESTINATION_ACTIVITY = "load_destination"
MIRROR_ROLLBACK_STARTED_ACTIVITY = "mirror_rollback_started"
MIRROR_UNDO_ACTIVITY = "mirror_undo_status"
REQUEST_APPROVAL_ACTIVITY = "request_approval"
REMIND_APPROVAL_ACTIVITY = "remind_approval"
RESOLVE_APPROVAL_ACTIVITY = "resolve_approval"
SUPERSEDE_APPROVAL_ACTIVITY = "supersede_approval"
# Sent by whoever records an answer (or a supersede) after its commit: a wake-up, not the answer.
APPROVAL_SIGNAL = "approval_answered"
# Sent to a monitor check's tick once its approval is requested (ADR-016 §9e).
CHECK_SETTLED_SIGNAL = "check_settled"
# ADR-016 §9c: the reminder, only when the deadline is further away than this.
APPROVAL_REMINDER_BEFORE = timedelta(hours=2)

# ADR-012: a Validate's stop mode, its bound rule values and the stop path (ADR-014 §6, J.6).
VALIDATE_STOP_PATCH = "validate-stop"
# ADR-012: a Webhook block that only sends pipeline.failed is skipped on a succeeding run (J.7).
WEBHOOK_EVENTS_PATCH = "webhook-events"
# ADR-012: a monitor's check, or a hand-run through a change gate (ADR-016 §15, L.8).
MONITOR_CHECK_PATCH = "monitor-check"
# ADR-012: bucket blocks' workflow-driven tries; Webhook destinations, block_id and deliveries (M.7).
SINKS_PATCH = "sinks"
# ADR-012: undoing the deliveries on the failure path, or marking what stays (ADR-014 §7, M.8).
ROLLBACK_PATCH = "rollback"
# ADR-012: an Approval block's wait (ADR-016 §9b to §9d, N.2).
APPROVAL_PATCH = "approval"

# The webhook payload's event for a pipeline run's delivery (job lane: job.completed).
WEBHOOK_EVENT = WEBHOOK_RESULT_EVENT

# Raised non-retryable by an activity that finds its run cancelled (ADR-009 15d's fallback).
RUN_CANCELLED_ERROR = "RunCancelled"
# Raised retryable by release_run_outputs; its one detail is the object keys still held.
OUTPUTS_LEFT_ERROR = "OutputsLeft"
# Non-retryable: the destination a block names is gone, another user's, or not its kind (ADR-015 §2c).
DESTINATION_GONE_ERROR = "DestinationDeleted"

RunStatus = Literal["running", "completed", "failed", "cancelled"]
# ADR-014 §5a; each list matches its CHECK in migration 4.10 — a new value widens both.
SkipReason = Literal["stopped", "upstream_failed", "cancelled", "not_started", "alert_only"]
StopReason = Literal["rule_not_met", "approval_rejected", "approval_expired", "approval_superseded"]
# ADR-016 §9a; matches ck_pipeline_run_blocks_status_reason with SkipReason.
WaitReason = Literal["awaiting_approval"]
# ADR-016 §9a; matches ck_pipeline_run_blocks_approval_answer.
ApprovalAnswer = Literal["approved", "rejected", "expired", "superseded"]
# ADR-016 §2b; matches ck_pipeline_runs_change_status.
ChangeStatus = Literal["first_check", "no_baseline", "changed", "unchanged"]
# ADR-014 §5a; matches ck_pipeline_run_blocks_undo_status.
UndoStatus = Literal["undoing", "undone", "undo_failed", "kept", "not_undoable"]


class LLMKey(BaseModel):
    provider: Literal["anthropic", "openai_compatible"]
    # Fernet ciphertext — workflow history stores inputs in plain JSON.
    encrypted_api_key: str
    base_url: str | None = None


class GateFingerprints(BaseModel):
    """Earlier checks' fingerprints of one change gate's input (ADR-016 §4c)."""

    baseline: str | None = None
    # Slice 2: the check waiting for approval.
    waiting: str | None = None


class CaptureSpec(BaseModel):
    """A carried input: what Capture reads from ``from_block``'s output (§6b)."""

    input: str
    from_block: str
    path: str
    type: Literal["number", "text"]


class MonitorContext(BaseModel):
    """What admission found for a monitor's check; the check never looks anything up (§4)."""

    monitor_id: str
    baseline_run_id: str | None = None
    waiting_run_id: str | None = None
    # By change-gate block id.
    gates: dict[str, GateFingerprints] = {}
    # The baseline's stored object per content block, while still stored — for Diff (§6a).
    baseline_outputs: dict[str, str] = {}
    captures: list[CaptureSpec] = []


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
    # A monitor's check; None for a hand-run.
    monitor: MonitorContext | None = None


class MirrorBlockInput(BaseModel):
    run_id: str
    block_id: str
    status: Literal["running", "waiting", "completed", "failed", "skipped"]
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    # skipped; or awaiting_approval on an Approval's waiting, which starts the block.
    status_reason: SkipReason | WaitReason | None = None
    reason_block_id: str | None = None
    # completed only: a stop-mode Validate's verdict, less its outcome.
    verdict: dict[str, Any] | None = None
    # completed only: a change gate's input, SHA-256 hex (ADR-016 §5e).
    fingerprint: str | None = None
    # completed only: where a sink wrote, {destination_id, kind, location} (ADR-014 §5a).
    delivery: dict[str, Any] | None = None


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
    # A monitor check's, written with its outcome (ADR-016 §4a, §5e, §6).
    baseline_eligible: bool = False
    change_status: ChangeStatus | None = None
    change_summary: dict[str, Any] | None = None
    captured_values: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _stop_names_block_and_reason(self) -> "MirrorRunInput":
        # The CHECK would refuse it on every retry.
        if (self.stopped_at_block_id is None) != (self.stop_reason is None):
            raise ValueError("stopped_at_block_id and stop_reason go together")
        return self


class ApprovalRef(BaseModel):
    run_id: str
    block_id: str


class RequestApprovalInput(ApprovalRef):
    # Workflow time: the wait's timers count to it.
    deadline: datetime
    # A check's change so far, for the request (ADR-016 §9c).
    change_status: ChangeStatus | None = None
    change_summary: dict[str, Any] | None = None


class ResolveApprovalInput(ApprovalRef):
    # The workflow's deadline timer fired: no answer recorded -> expired.
    expire: bool = False


class ApprovalOutcome(BaseModel):
    # None: nothing recorded yet.
    answer: ApprovalAnswer | None = None


class SupersedeApprovalInput(BaseModel):
    # The check waiting for approval, and the later check whose content differs (ADR-016 §9e).
    run_id: str
    superseded_by_run_id: str


class RecordStorageInput(BaseModel):
    run_id: str
    block_id: str
    objects: list[StoredObject]


class StorageUsage(BaseModel):
    used: int
    limit: int


class RunRef(BaseModel):
    run_id: str


class LoadDestinationInput(BaseModel):
    run_id: str
    destination_id: str
    # The kind the block needs.
    kind: str


class LoadedDestination(BaseModel):
    """The destination as it is now (ADR-015 §2b); the secret stays ciphertext."""

    address: dict[str, Any]
    encrypted_secret: str


class RollbackStartedInput(BaseModel):
    run_id: str
    failed_block_id: str
    error: str
    # The latest the undos can finish: their own budgets, not the run's (ADR-014 §7).
    rollback_deadline: datetime


class MirrorUndoInput(BaseModel):
    run_id: str
    # By block id; several only for kept / not_undoable (ADR-014 §7's one call).
    undo: dict[str, UndoStatus]
    # undo_failed only.
    error: str | None = None


class WebhookInput(BaseModel):
    run_id: str
    pipeline_id: str
    block_id: str
    # Exactly one: an inline URL, or a webhook destination read at each attempt (ADR-015 §2b).
    url: str | None = None
    # The block's input reference: what is delivered.
    result_path: str
    encrypted_secret: str | None = None
    # Fixed by the workflow so every attempt signs the same bytes.
    timestamp: str
    # The retry policy's; the last attempt mirrors no `waiting`.
    maximum_attempts: int
    # A monitor's check only (ADR-016 §6a): which monitor, and {status, baseline_run_id, summary}.
    monitor_id: str | None = None
    change: dict[str, Any] | None = None
    destination_id: str | None = None
    # Set from patch "sinks" on: the payload then carries block_id and these (PRD-017 R4).
    deliveries: list[dict[str, Any]] | None = None


# A bucket delivery's object extension, by the reference type it is given (ADR-015 §5).
_BUCKET_EXT: dict[str, Literal["html", "md", "json"]] = {
    PAGE_HTML: "html",
    PAGE_MARKDOWN: "md",
    PAGE_JSON: "json",
    EXTRACTION: "json",
}


_APPROVAL_STOPS: dict[str, StopReason] = {
    "rejected": "approval_rejected",
    "expired": "approval_expired",
    "superseded": "approval_superseded",
}


# A reversible sink's undo by its delivery record's kind: activity, input, output (ADR-015 §5).
_UNDO_ACTIVITIES: dict[str, tuple[str, type[BaseModel], type[BaseModel]]] = {
    "bucket": (UNDO_BUCKET_ACTIVITY, UndoBucketInput, UndoBucketOutput),
}


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


def _sink_failure(cause: BaseException | None, what: str = "delivery") -> tuple[str, bool]:
    """(message, final) for one try on the delivery queue: the worker's classified error as it
    says; a timeout transient (nothing took it, or it overran); anything else final."""
    if isinstance(cause, ApplicationError):
        return str(cause), cause.non_retryable
    if isinstance(cause, ActivityTimeoutError):
        if cause.type == TimeoutType.SCHEDULE_TO_START:
            return f"no delivery worker took the {what}", False
        return f"the {what} did not finish in time", False
    return f"the {what} failed ({type(cause).__name__})", True


def _may_have_written(cause: BaseException | None) -> bool:
    """A try whose request may have stored the object: anything but a worker's ``NOT_WRITTEN``
    or a try no worker took."""
    if isinstance(cause, ActivityTimeoutError):
        return cause.type != TimeoutType.SCHEDULE_TO_START
    return not (isinstance(cause, ApplicationError) and NOT_WRITTEN in cause.details)


def _bucket_record(
    destination_id: str, address: dict[str, Any], input: PipelineRunInput, block_id: str, ext: str
) -> dict[str, Any]:
    """The record a try against ``address`` would return: the worker builds the key the same way."""
    location = BucketLocation(
        endpoint=address["endpoint"],
        bucket=address["bucket"],
        region=address.get("region"),
        key=object_key(address.get("prefix", ""), input.pipeline_id, input.run_id, block_id, ext),
    )
    return {
        "destination_id": destination_id,
        "kind": "bucket",
        "location": location.model_dump(mode="json"),
    }


def _lost_reply(record: dict[str, Any]) -> str:
    loc = record["location"]
    return (
        f"a try whose reply was lost may have written bucket {loc['bucket']}, "
        f"key {loc['key']} at {loc['endpoint']}"
    )


def _run_cancelled() -> ApplicationError:
    return ApplicationError("the run was cancelled", type=RUN_CANCELLED_ERROR, non_retryable=True)


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


class _Check:
    """What a monitor check, or a hand-run through a change gate, learns as it goes (§5e, §6)."""

    def __init__(self, first_gate: str | None, monitor: "MonitorContext | None") -> None:
        self.first_gate = first_gate
        self.monitor = monitor
        self.change_status: ChangeStatus | None = None
        self.passed_first_gate = False
        # Expired or superseded: nobody looked at this content (§4a).
        self.lapsed = False
        # The bounded diff summaries, by the block that triggered each.
        self.summaries: dict[str, dict[str, Any]] = {}
        self.captured: dict[str, dict[str, Any]] = {}

    def known(self, block_id: str) -> dict[str, str]:
        gate = self.monitor.gates.get(block_id) if self.monitor is not None else None
        if gate is None:
            return {}
        return {k: v for k, v in (("baseline", gate.baseline), ("waiting", gate.waiting)) if v}

    def run_fields(self, completed: bool = True) -> dict[str, Any]:
        """What the run mirror writes with the outcome (§4a: only a completed check that passed
        its first gate can be a baseline)."""
        return {
            "baseline_eligible": completed
            and self.monitor is not None
            and (self.first_gate is None or self.passed_first_gate)
            and not self.lapsed,
            "change_status": self.change_status,
            "change_summary": self.summaries or None,
            "captured_values": self.captured or None,
        }

    def change(self) -> dict[str, Any] | None:
        """The Webhook payload's ``change``, once the first gate has let the run through."""
        if self.monitor is None or self.change_status not in ("changed", "first_check"):
            return None
        return {
            "status": self.change_status,
            "baseline_run_id": self.monitor.baseline_run_id,
            "summary": self.summaries or None,
        }


@workflow.defn
class PipelineWorkflow:
    """Runs a pinned definition block by block; mirrors each status into the app DB.

    A cancel is honoured at the next block boundary, never mid-block (R3): from Temporal
    (``handle.cancel()``) or from the row — the running-mirror refuses to start a block on a
    run that is no longer running or whose cancel was requested.
    """

    def __init__(self) -> None:
        self._cancel_requested = False
        self._check: _Check | None = None
        self._sinks = False
        # (block id, delivery record) per sink that delivered, in order (ADR-014 §7's `delivered`).
        self._delivered: list[tuple[str, dict[str, Any]]] = []
        # Irreversible sinks that sent (§7's `announced`); an alert-only skip never gets here.
        self._announced: list[str] = []
        # A failing bucket block's newest try that may have written: rollback undoes its key.
        self._unsure: tuple[str, dict[str, Any]] | None = None
        # Set by APPROVAL_SIGNAL: an answer may be recorded on the row (ADR-016 §9b).
        self._approval_woken = False

    @workflow.signal(name=APPROVAL_SIGNAL)
    def approval_answered(self) -> None:
        self._approval_woken = True

    @workflow.run
    async def run(self, input: PipelineRunInput) -> str | None:
        deadline = workflow.now() + timedelta(seconds=input.time_budget_seconds)
        refs: dict[str, str] = {}
        result_path: str | None = None
        wall: str | None = None
        first_gate = first_change_gate(input.blocks)
        if (input.monitor is not None or first_gate is not None) and workflow.patched(
            MONITOR_CHECK_PATCH
        ):
            self._check = _Check(first_gate, input.monitor)
        check = self._check
        self._sinks = any(CATALOG[b.type].sink is not None for b in input.blocks) and (
            workflow.patched(SINKS_PATCH)
        )
        # The content block whose object the next block reads: a gate's page diff compares it.
        last_content: str | None = None
        for block in input.blocks:
            block_type = CATALOG[block.type]
            input_ref = refs.get(block.input) if block.input else None
            verdict: ValidateVerdict | None = None
            answer: ApprovalAnswer | None = None
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
            approval = isinstance(block, ApprovalBlock) and workflow.patched(APPROVAL_PATCH)
            start: dict[str, Any] = (
                {"status": "waiting", "status_reason": "awaiting_approval"}
                if approval
                else {"status": "running"}
            )
            if self._cancel_requested or (
                await self._mirror_block(input.run_id, block.id, input_ref=input_ref, **start)
                != "running"
            ):
                return await self._cancel(input.run_id)

            timing = block_type.timing(block.config)
            delivery: dict[str, Any] | None = None
            try:
                # ADR-009 15a/15d: an effect block may wait between attempts, and a cancel
                # interrupts that wait; a running attempt still finishes (R3).
                if approval:
                    assert isinstance(block, ApprovalBlock)
                    answer, waited = await self._await_approval(block, input)
                    # §9d: the run's budget does not run during the wait.
                    deadline += waited
                    objects = []
                elif isinstance(block, BucketBlock) and self._sinks:
                    delivery = await self._deliver_bucket(block, input, input_ref, deadline, timing)
                    objects = []
                elif isinstance(block, ScrapeBlock) and workflow.patched(steps.HOST_SLOT_PATCH):
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
                # ⚠️ Before the block is mirrored completed: the run holds its slot only while
                # a block is running (§6a).
                if check is not None and verdict is not None and block.id == check.first_gate:
                    await self._first_gate(input, block, verdict, input_ref, last_content, deadline)
                if check is not None and check.monitor is not None and objects:
                    await self._after_content(input, block, objects[0].path, deadline)
            except workflow.NondeterminismError:
                # Code no longer matches history: the task retries until a deploy fixes it.
                raise
            except (Exception, asyncio.CancelledError) as exc:
                # ADR-014 §5a: undo_status and delivery belong to completed blocks; a failed
                # block's maybe-written key is named in its error and the run's.
                unsure = self._unsure_of(block.id)
                lost = f"; {_lost_reply(unsure)}" if unsure is not None else ""
                if _is_cancellation(exc):
                    await self._mirror_block(
                        input.run_id,
                        block.id,
                        "failed",
                        error=(
                            "cancelled while waiting for approval"
                            if approval
                            else f"cancelled before it finished{lost}"
                        ),
                    )
                    return await self._cancel(input.run_id)
                if not isinstance(exc, Exception):
                    raise
                message = _failure_message(exc, timing.maximum_attempts) + lost
                await self._mirror_block(input.run_id, block.id, "failed", error=message)
                error = f"{block.id}: {message}"
                if (self._delivered or self._announced or unsure is not None) and workflow.patched(
                    ROLLBACK_PATCH
                ):
                    left = await self._roll_back(input, block.id, error)
                    if left is not None:
                        error += f"; {left}"
                await self._mirror_run(
                    input.run_id,
                    "failed",
                    error=error,
                    failed_block_id=block.id,
                    **(check.run_fields(completed=False) if check is not None else {}),
                )
                raise ApplicationError(error, non_retryable=True) from exc

            if verdict is not None and verdict.outcome == "not_met":
                assert isinstance(block, ValidateBlock)
                return await self._stop(input, block, verdict, input_ref, result_path, wall)
            if answer is not None and answer != "approved":
                assert isinstance(block, ApprovalBlock)
                return await self._approval_stop(input, block, answer, input_ref, result_path, wall)

            # ADR-009 §5: content blocks write an object; effect blocks pass their input on.
            output_ref = objects[0].path if block_type.kind == "content" else input_ref
            assert output_ref is not None
            refs[block.id] = output_ref
            if block_type.kind == "content":
                result_path = output_ref
                last_content = block.id
            if wall is None and usage is not None and usage.used > usage.limit:
                # §8d: the run finishes and is charged; the headroom buffer stops the next one.
                wall = (
                    f"storage_wall: block {block.id!r} took storage to {usage.used} of "
                    f"{usage.limit} bytes; the run completed and is charged. Delete results "
                    "to start new runs"
                )
            extra: dict[str, Any] = {}
            if check is not None and verdict is not None and verdict.fingerprint is not None:
                extra["fingerprint"] = verdict.fingerprint
            if delivery is not None:
                extra["delivery"] = delivery
                self._delivered.append((block.id, delivery))
            elif block_type.sink is not None and not block_type.sink.reversible:
                self._announced.append(block.id)
            await self._mirror_block(
                input.run_id, block.id, "completed", output_ref=output_ref, **extra
            )

        await self._mirror_run(
            input.run_id,
            "completed",
            result_path=result_path,
            error=wall,
            **(check.run_fields() if check is not None else {}),
        )
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
        check = self._check
        extra = {}
        if check is not None and verdict.fingerprint is not None:
            extra["fingerprint"] = verdict.fingerprint
        await self._mirror_block(
            input.run_id, block.id, "completed", output_ref=input_ref, verdict=recorded, **extra
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
            **(check.run_fields() if check is not None else {}),
        )
        return result_path

    async def _await_approval(
        self, block: ApprovalBlock, input: PipelineRunInput
    ) -> tuple[ApprovalAnswer, timedelta]:
        """ADR-016 §9b: request, then wait for a wake-up, the reminder or the deadline, and read
        the answer back. Returns it and how long the run waited (§9d). A cancel raises."""
        ref = {"run_id": input.run_id, "block_id": block.id}
        check = self._check
        deadline = workflow.now() + timedelta(hours=block.config.deadline_hours)
        await self._finish(
            _db(
                REQUEST_APPROVAL_ACTIVITY,
                RequestApprovalInput(
                    **ref,
                    deadline=deadline,
                    change_status=check.change_status if check is not None else None,
                    change_summary=(check.summaries or None) if check is not None else None,
                ),
                None,
            )
        )
        started = workflow.now()
        remind_at = (
            deadline - APPROVAL_REMINDER_BEFORE
            if timedelta(hours=block.config.deadline_hours) > APPROVAL_REMINDER_BEFORE
            else None
        )
        answer: ApprovalAnswer | None = None
        waited = timedelta(0)
        while answer is None:
            if self._cancel_requested:
                raise _run_cancelled()
            woken = await self._wait_for_answer(remind_at or deadline)
            waited = workflow.now() - started
            if woken:
                self._approval_woken = False
                name, arg = RESOLVE_APPROVAL_ACTIVITY, ResolveApprovalInput(**ref)
            elif remind_at is not None:
                remind_at = None
                name, arg = REMIND_APPROVAL_ACTIVITY, ApprovalRef(**ref)
            else:
                name, arg = RESOLVE_APPROVAL_ACTIVITY, ResolveApprovalInput(**ref, expire=True)
            outcome: ApprovalOutcome = await self._finish(_db(name, arg, ApprovalOutcome))
            answer = outcome.answer
        if self._cancel_requested:
            raise _run_cancelled()
        return answer, waited

    async def _wait_for_answer(self, until: datetime) -> bool:
        """True: woken by the signal; False: ``until`` came first."""
        if self._approval_woken:
            return True
        timeout = (until - workflow.now()).total_seconds()
        if timeout <= 0:
            return False
        try:
            await workflow.wait_condition(lambda: self._approval_woken, timeout=timeout)
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            self._cancel_requested = True
            raise
        return True

    async def _approval_stop(
        self,
        input: PipelineRunInput,
        block: ApprovalBlock,
        answer: ApprovalAnswer,
        input_ref: str | None,
        result_path: str | None,
        wall: str | None,
    ) -> str | None:
        """Rejected, expired or superseded: the run ends completed at the Approval block; nothing
        is undone and every copy is kept (ADR-016 §9b)."""
        await self._mirror_block(input.run_id, block.id, "completed", output_ref=input_ref)
        check = self._check
        if check is not None and answer in ("expired", "superseded"):
            check.lapsed = True
        await self._mirror_run(
            input.run_id,
            "completed",
            result_path=result_path,
            error=wall,
            stopped_at_block_id=block.id,
            stop_reason=_APPROVAL_STOPS[answer],
            **(check.run_fields() if check is not None else {}),
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
        **fields: Any,
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

    async def _deliver_bucket(
        self,
        block: BucketBlock,
        input: PipelineRunInput,
        input_ref: str | None,
        deadline: datetime,
        timing: Timing,
    ) -> dict[str, Any]:
        """The workflow-driven tries (ADR-015 §4): load the destination, one attempt on the delivery
        queue; a transient failure mirrors ``waiting``, sleeps (a cancel ends the sleep) and mirrors
        ``running`` again. Returns the delivery record; raises on a final failure."""
        assert input_ref is not None and block.input is not None
        destination_id = str(block.config.destination_id)
        ext = _BUCKET_EXT[block_outputs(input.blocks)[block.input]]
        error = ""
        for attempt in range(1, timing.maximum_attempts + 1):
            if attempt > 1:
                await self._mirror_block(
                    input.run_id, block.id, "waiting", error=f"attempt {attempt - 1}: {error}"
                )
                if self._cancel_requested:
                    raise _run_cancelled()
                wait = min(
                    timing.initial_interval * timing.backoff_coefficient ** (attempt - 2),
                    timing.maximum_interval,
                )
                if wait >= (deadline - workflow.now()).total_seconds():
                    raise ApplicationError(
                        f"{error} (the run's time budget ran out after {attempt - 1} attempts)",
                        non_retryable=True,
                    )
                try:
                    await workflow.sleep(wait)
                except asyncio.CancelledError:
                    self._cancel_requested = True
                    raise
                # 15d: the row's cancel, should the engine's have been lost.
                running = await self._mirror_block(
                    input.run_id, block.id, "running", input_ref=input_ref
                )
                if running != "running":
                    raise _run_cancelled()
            loaded: LoadedDestination = await self._finish(
                _db(
                    LOAD_DESTINATION_ACTIVITY,
                    LoadDestinationInput(
                        run_id=input.run_id, destination_id=destination_id, kind="bucket"
                    ),
                    LoadedDestination,
                )
            )
            if self._cancel_requested:
                raise _run_cancelled()
            remaining_s = (deadline - workflow.now()).total_seconds()
            if remaining_s <= 0:
                raise ApplicationError("the run's time budget is spent", non_retryable=True)
            try:
                out: DeliverBucketOutput = await self._finish(
                    workflow.execute_activity(
                        DELIVER_BUCKET_ACTIVITY,
                        DeliverBucketInput(
                            address=loaded.address,
                            encrypted_secret=loaded.encrypted_secret,
                            source_path=input_ref,
                            pipeline_id=input.pipeline_id,
                            run_id=input.run_id,
                            block_id=block.id,
                            ext=ext,
                        ),
                        task_queue=DELIVERY_QUEUE,
                        result_type=DeliverBucketOutput,
                        schedule_to_start_timeout=timedelta(seconds=timing.schedule_to_start),
                        start_to_close_timeout=timedelta(seconds=timing.start_to_close),
                        schedule_to_close_timeout=timedelta(
                            seconds=min(
                                timing.schedule_to_start + timing.start_to_close, remaining_s
                            )
                        ),
                        # One try per call: the loop above is the retry policy.
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                )
            except ActivityError as exc:
                if _may_have_written(exc.cause):
                    self._unsure = (
                        block.id,
                        _bucket_record(destination_id, loaded.address, input, block.id, ext),
                    )
                error, final = _sink_failure(exc.cause)
                if final:
                    raise ApplicationError(error, non_retryable=True) from None
                if self._cancel_requested:
                    raise _run_cancelled() from None
                continue
            # Names only (ADR-014 §5a): never an address or a credential.
            return {
                "destination_id": destination_id,
                "kind": "bucket",
                "location": out.location.model_dump(mode="json"),
            }
        raise ApplicationError(
            f"{error} (gave up after {timing.maximum_attempts} attempts)", non_retryable=True
        )

    async def _mirror_undo(
        self, run_id: str, undo: dict[str, UndoStatus], error: str | None = None
    ) -> RunStatus:
        return await self._finish(
            _db(MIRROR_UNDO_ACTIVITY, MirrorUndoInput(run_id=run_id, undo=undo, error=error), str)
        )

    def _unsure_of(self, block_id: str) -> dict[str, Any] | None:
        return self._unsure[1] if self._unsure is not None and self._unsure[0] == block_id else None

    async def _roll_back(
        self, input: PipelineRunInput, failed_block_id: str, error: str
    ) -> str | None:
        """ADR-014 §7, between the failed block and the failed run. Nothing announced: undo each
        delivery, newest first, on its own budget. Announced: mark what stays, undo nothing (D13).

        The failed block's maybe-written key is undone first, its outcome returned for the run's
        error (no undo status: the block is not completed).

        ⚠️ Every await goes through ``_finish(..., interruptible=False)``: a cancel is ignored."""
        run_id = input.run_id
        unsure = self._unsure_of(failed_block_id)
        if self._announced:
            undo: dict[str, UndoStatus] = {block_id: "kept" for block_id, _ in self._delivered}
            undo |= dict.fromkeys(self._announced, "not_undoable")
            await self._mirror_undo(run_id, undo)
            return (
                None if unsure is None else "left in place: a webhook or email had already gone out"
            )
        types = {b.id: CATALOG[b.type] for b in input.blocks}
        undos: list[tuple[str, dict[str, Any], Timing]] = []
        for block_id, record in reversed(self._delivered):
            sink = types[block_id].sink
            assert sink is not None and sink.undo is not None
            undos.append((block_id, record, sink.undo))
        unsure_sink = types[failed_block_id].sink
        unsure_timing = unsure_sink.undo if unsure_sink is not None else None
        assert unsure is None or unsure_timing is not None
        need = sum(timing.need for _, _, timing in undos)
        if unsure is not None and unsure_timing is not None:
            need += unsure_timing.need
        started = await self._finish(
            _db(
                MIRROR_ROLLBACK_STARTED_ACTIVITY,
                RollbackStartedInput(
                    run_id=run_id,
                    failed_block_id=failed_block_id,
                    error=error,
                    rollback_deadline=workflow.now() + timedelta(seconds=need),
                ),
                str,
            )
        )
        # Not running: the run was deleted, and with it whose destination to load.
        if started != "running":
            return None
        left = None
        if unsure is not None and unsure_timing is not None:
            outcome = await self._undo(run_id, unsure, unsure_timing)
            if outcome is None:
                return None
            undo_status, undo_error = outcome
            left = (
                "rollback removed it"
                if undo_status == "undone"
                else f"rollback could not remove it, left in place: {undo_error}"
            )
        for block_id, record, timing in undos:
            if await self._mirror_undo(run_id, {block_id: "undoing"}) != "running":
                return left
            outcome = await self._undo(run_id, record, timing)
            if outcome is None:
                return left
            undo_status, undo_error = outcome
            await self._mirror_undo(run_id, {block_id: undo_status}, undo_error)
        return left

    async def _undo(
        self, run_id: str, record: dict[str, Any], timing: Timing
    ) -> tuple[UndoStatus, str | None] | None:
        """One delivery's undo: ADR-015 §4's tries less the waiting mirror and the cancel check,
        at the recorded location with the destination's current secret. None: the run is gone."""
        name, input_type, result_type = _UNDO_ACTIVITIES[record["kind"]]
        error = ""
        for attempt in range(1, timing.maximum_attempts + 1):
            if attempt > 1:
                wait = min(
                    timing.initial_interval * timing.backoff_coefficient ** (attempt - 2),
                    timing.maximum_interval,
                )
                await self._finish(workflow.sleep(wait))
            try:
                loaded: LoadedDestination = await self._finish(
                    _db(
                        LOAD_DESTINATION_ACTIVITY,
                        LoadDestinationInput(
                            run_id=run_id,
                            destination_id=record["destination_id"],
                            kind=record["kind"],
                        ),
                        LoadedDestination,
                    )
                )
            except ActivityError as exc:
                cause = exc.cause
                if isinstance(cause, ApplicationError) and cause.type == RUN_CANCELLED_ERROR:
                    return None
                if isinstance(cause, ApplicationError) and cause.non_retryable:
                    return "undo_failed", str(cause)
                error = "the destination could not be read"
                continue
            try:
                await self._finish(
                    workflow.execute_activity(
                        name,
                        input_type(
                            location=record["location"], encrypted_secret=loaded.encrypted_secret
                        ),
                        task_queue=DELIVERY_QUEUE,
                        result_type=result_type,
                        schedule_to_start_timeout=timedelta(seconds=timing.schedule_to_start),
                        start_to_close_timeout=timedelta(seconds=timing.start_to_close),
                        schedule_to_close_timeout=timedelta(
                            seconds=timing.schedule_to_start + timing.start_to_close
                        ),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                )
            except ActivityError as exc:
                error, final = _sink_failure(exc.cause, "undo")
                if final:
                    return "undo_failed", error
                continue
            return "undone", None
        return "undo_failed", f"{error} (gave up after {timing.maximum_attempts} attempts)"

    def _deliveries(self) -> list[dict[str, Any]]:
        """The Webhook payload's ``deliveries``: what the sinks before it wrote (PRD-017 R4)."""
        return [
            {"block_id": block_id, "kind": record["kind"], "location": record["location"]}
            for block_id, record in self._delivered
        ]

    def _webhook_check_fields(self) -> dict[str, Any]:
        check = self._check
        if check is None or check.monitor is None:
            return {}
        return {"monitor_id": check.monitor.monitor_id, "change": check.change()}

    async def _first_gate(
        self,
        input: PipelineRunInput,
        block: ValidateBlock,
        verdict: ValidateVerdict,
        input_ref: str | None,
        last_content: str | None,
        deadline: datetime,
    ) -> None:
        """The first change gate's verdict is the run's change status (§5e); a change with a
        baseline is compared at once (§6a)."""
        check = self._check
        assert check is not None
        if verdict.outcome == "not_met":
            check.change_status = "unchanged"
            return
        check.passed_first_gate = True
        if not check.known(block.id):
            check.change_status = "first_check" if check.monitor is not None else "no_baseline"
            return
        check.change_status = "changed"
        assert check.monitor is not None and input_ref is not None
        baseline = check.monitor.baseline_outputs.get(last_content or "")
        if baseline is None:
            # Collected since, or never stored: changed, the previous copy is no longer kept.
            check.summaries[block.id] = {"status": "baseline_gone", "kind": "page"}
            return
        check.summaries[block.id] = await self._diff(
            input, block.id, "page", baseline, input_ref, deadline
        )

    async def _after_content(
        self, input: PipelineRunInput, block: RunBlock, path: str, deadline: datetime
    ) -> None:
        """A monitor check's extraction diff and carried-value capture (§6a, §6b)."""
        check = self._check
        assert check is not None and check.monitor is not None
        monitor = check.monitor
        baseline = monitor.baseline_outputs.get(block.id)
        if isinstance(block, LLMExtractBlock) and baseline is not None:
            check.summaries[block.id] = await self._diff(
                input, block.id, "extraction", baseline, path, deadline
            )
        specs = [c for c in monitor.captures if c.from_block == block.id]
        if not specs:
            return
        remaining_s = (deadline - workflow.now()).total_seconds()
        try:
            if remaining_s <= 0:
                raise ApplicationError("the run's time budget is spent", non_retryable=True)
            captured = await self._finish(
                workflow.execute_activity(
                    CAPTURE_ACTIVITY,
                    CaptureInput(
                        input_path=path,
                        values=[
                            CarriedValue(input=c.input, path=c.path, type=c.type) for c in specs
                        ],
                    ),
                    task_queue=CONTENT_QUEUE,
                    result_type=CaptureOutput,
                    **_block_options(CAPTURE_TIMING, remaining_s),
                )
            )
        except (ActivityError, ApplicationError) as exc:
            # A capture never fails the check: the next check starts from the start value.
            reason = (
                f"could not be read: {exc.cause or exc}"
                if isinstance(exc, ActivityError)
                else str(exc)
            )
            for c in specs:
                check.captured[c.input] = {"missing": reason}
            return
        for name, value in captured.values.items():
            check.captured[name] = value.model_dump(mode="json", exclude_none=True)

    async def _diff(
        self,
        input: PipelineRunInput,
        block_id: str,
        kind: Literal["page", "extraction"],
        baseline_path: str,
        current_path: str,
        deadline: datetime,
    ) -> dict[str, Any]:
        """The bounded summary; the full diff is charged to ``block_id`` (§6a). Never fails the
        check and never reads "no change": a final error is ``unavailable``."""
        unavailable = {
            "status": "unavailable",
            "kind": kind,
            "reason": "the comparison could not be computed",
        }
        remaining_s = (deadline - workflow.now()).total_seconds()
        if remaining_s <= 0:
            return unavailable
        try:
            out: DiffOutput = await self._finish(
                workflow.execute_activity(
                    DIFF_ACTIVITY,
                    DiffInput(
                        artifact_id=input.artifact_ids[block_id],
                        kind=kind,
                        baseline_path=baseline_path,
                        current_path=current_path,
                    ),
                    task_queue=CONTENT_QUEUE,
                    result_type=DiffOutput,
                    **_block_options(DIFF_TIMING, remaining_s),
                )
            )
        except ActivityError:
            return unavailable
        if out.diff is not None:
            await self._finish(
                _db(
                    RECORD_STORAGE_ACTIVITY,
                    RecordStorageInput(run_id=input.run_id, block_id=block_id, objects=[out.diff]),
                    StorageUsage,
                )
            )
        return out.summary.model_dump(mode="json")

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
                        known_fingerprints=(
                            self._check.known(block.id)
                            if self._check is not None and is_change_gate(block.config)
                            else {}
                        ),
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
                    url=None if block.config.url is None else str(block.config.url),
                    destination_id=(
                        None
                        if block.config.destination_id is None
                        else str(block.config.destination_id)
                    ),
                    deliveries=self._deliveries() if self._sinks else None,
                    result_path=input_ref,
                    encrypted_secret=input.webhook_secrets.get(block.id),
                    timestamp=workflow.now().isoformat(),
                    maximum_attempts=options["retry_policy"].maximum_attempts,
                    **self._webhook_check_fields(),
                ),
                task_queue=WORKFLOW_QUEUE,
                **options,
            )
            return []

        raise ApplicationError(f"block type {block.type!r} cannot run yet", non_retryable=True)
