import asyncio
from collections.abc import Coroutine
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, FailureError

with workflow.unsafe.imports_passed_through():
    from app.workflows import steps
    from app.workflows.activities.contracts import (
        LLMInput,
        LLMOutput,
        ScrapeInput,
        ScrapeOutput,
        StoredObject,
    )

JOB_RUN_STARTED_ACTIVITY = "job_run_started"
LOAD_JOB_LLM_ACTIVITY = "load_job_llm"
RECORD_JOB_OBJECTS_ACTIVITY = "record_job_objects"
JOB_RUN_PROCESSING_ACTIVITY = "job_run_processing"
FINISH_JOB_RUN_ACTIVITY = "finish_job_run"

# v1's error for the same failure (result_consumer.py).
ACCOUNTING_FAILED = "storage_accounting_failed"

# What the DB activities return: the run's status after the call, or "gone" if its row is.
RunState = Literal["pending", "running", "processing", "completed", "failed", "cancelled", "gone"]


def workflow_id(run_id: object) -> str:
    return f"job-run-{run_id}"


class JobRunInput(BaseModel):
    run_id: str
    engine: steps.Engine
    scrape: ScrapeInput


class JobRunRef(BaseModel):
    run_id: str


class JobLLM(BaseModel):
    """The job's LLM step as it stands after the scrape; ``error`` = it has one that cannot run."""

    provider: Literal["anthropic", "openai_compatible"] | None = None
    # Fernet ciphertext — workflow history stores activity results in plain JSON.
    encrypted_api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    output_schema: dict[str, Any] | None = None
    error: str | None = None


class RecordJobObjectsInput(BaseModel):
    run_id: str
    objects: list[StoredObject]
    # The scrape's; written to job_runs.content_hash.
    content_hash: str | None = None


class FinishJobRunInput(BaseModel):
    run_id: str
    status: Literal["completed", "failed", "cancelled"]
    result_path: str | None = None
    error: str | None = None
    warnings: list[str] | None = None
    # Deleted unless the ledger holds them: objects this run will never charge.
    discard: list[str] = []


def _activity_error(exc: ActivityError) -> str:
    # The worker's own message, as v1 stored it (e.g. "blocked:akamai").
    cause = exc.cause
    if isinstance(cause, FailureError):
        return cause.message
    return str(cause or exc)


@workflow.defn
class JobWorkflow:
    """One run of a job on the v2 lane: what ``result_consumer.py`` does for it on v1.

    Scrape → charge → (LLM → charge) → ``completed``, each status mirrored to ``job_runs`` on the
    ``job_status`` channel and the webhook row written for ``webhook_loop.py`` to deliver. The API
    cancels the row first; every mirror refuses to move a run out of a state it did not set, so
    the workflow stops at the next step and drops what it had not charged. A step in flight
    always finishes — v1 never aborted a worker either.
    """

    def __init__(self) -> None:
        self._cancel_requested = False

    @workflow.run
    async def run(self, input: JobRunInput) -> str | None:
        state = await self._db(JOB_RUN_STARTED_ACTIVITY, JobRunRef(run_id=input.run_id), str)
        if state != "running":
            return self._stopped(state)
        try:
            return await self._run(input)
        except workflow.NondeterminismError:
            # Code no longer matches history: the task retries until a deploy fixes it.
            raise
        except (ApplicationError, asyncio.CancelledError):
            raise
        except Exception as exc:
            # Left to propagate it would fail the workflow task, retried forever: the run would
            # stay running and hold its slot.
            error = f"internal error: {type(exc).__name__}: {exc}"
            return await self._fail(input.run_id, error)

    async def _run(self, input: JobRunInput) -> str | None:
        run_id = input.run_id
        try:
            scraped: ScrapeOutput = await self._finish(steps.scrape(input.scrape, input.engine))
        except ActivityError as exc:
            return await self._fail(run_id, _activity_error(exc))
        objects = [scraped.result, *scraped.screenshots]
        paths = [o.path for o in objects]
        if self._cancel_requested:
            return await self._cancel(run_id, discard=paths)

        # Read after the scrape, as v1 did: a key deleted meanwhile fails the run.
        llm: JobLLM = await self._db(LOAD_JOB_LLM_ACTIVITY, JobRunRef(run_id=run_id), JobLLM)
        if llm.error is not None:
            return await self._fail(run_id, llm.error, discard=paths)

        state = await self._record(run_id, objects, scraped.content_hash)
        if state != "running":
            return self._stopped(state)
        if llm.provider is None:
            return await self._complete(run_id, scraped.result.path, scraped.warnings)

        if self._cancel_requested:
            return await self._cancel(run_id)
        state = await self._db(JOB_RUN_PROCESSING_ACTIVITY, JobRunRef(run_id=run_id), str)
        if state != "processing":
            return self._stopped(state)
        try:
            extracted: LLMOutput = await self._finish(
                steps.llm_extract(
                    LLMInput(
                        # Stage-named objects under one artifact id (ADR-011): llm.json beside
                        # scrape.{fmt}.
                        artifact_id=input.scrape.artifact_id,
                        raw_minio_path=scraped.result.path,
                        provider=llm.provider,
                        encrypted_api_key=llm.encrypted_api_key,
                        base_url=llm.base_url,
                        model=llm.model,
                        output_schema=llm.output_schema,
                    )
                )
            )
        except ActivityError as exc:
            return await self._fail(run_id, _activity_error(exc))
        if self._cancel_requested:
            return await self._cancel(run_id, discard=[extracted.result.path])

        state = await self._record(run_id, [extracted.result])
        if state != "processing":
            return self._stopped(state)
        # v1 stores the scrape's warnings only on a run without an LLM step.
        return await self._complete(run_id, extracted.result.path)

    async def _record(
        self, run_id: str, objects: list[StoredObject], content_hash: str | None = None
    ) -> str:
        """Charges the objects; returns the run's state (``failed`` at the storage wall)."""
        try:
            return await self._db(
                RECORD_JOB_OBJECTS_ACTIVITY,
                RecordJobObjectsInput(run_id=run_id, objects=objects, content_hash=content_hash),
                str,
            )
        except ActivityError:
            # A run that fails accounting holds nothing (v1's rule).
            await self._fail(run_id, ACCOUNTING_FAILED, discard=[o.path for o in objects])
            return "gone"

    async def _finish_run(self, run_id: str, status: str, **fields: Any) -> str:
        return await self._db(
            FINISH_JOB_RUN_ACTIVITY, FinishJobRunInput(run_id=run_id, status=status, **fields), str
        )

    async def _complete(
        self, run_id: str, result_path: str, warnings: list[str] | None = None
    ) -> str | None:
        state = await self._finish_run(
            run_id, "completed", result_path=result_path, warnings=warnings
        )
        return result_path if state == "completed" else self._stopped(state)

    async def _fail(self, run_id: str, error: str, discard: list[str] | None = None) -> None:
        state = await self._finish_run(run_id, "failed", error=error, discard=discard or [])
        if state == "failed":
            raise ApplicationError(error, non_retryable=True)
        return self._stopped(state)

    async def _cancel(self, run_id: str, discard: list[str] | None = None) -> None:
        state = await self._finish_run(run_id, "cancelled", discard=discard or [])
        return self._stopped(state)

    def _stopped(self, state: str) -> None:
        """The run is out of the workflow's hands: cancelled, failed by a mirror, or deleted."""
        if state == "cancelled" and self._cancel_requested:
            raise asyncio.CancelledError()
        if state == "failed":
            # Failed by a mirror (the storage wall); job_runs.error says why.
            raise ApplicationError("the run was failed: see job_runs.error", non_retryable=True)
        return None

    async def _finish(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Await to completion even if the workflow is cancelled meanwhile; note the cancel."""
        task = asyncio.ensure_future(coro)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    raise
                self._cancel_requested = True
        return task.result()

    async def _db(self, activity: str, arg: BaseModel, result_type: type) -> Any:
        return await self._finish(steps.db(activity, arg, result_type))
