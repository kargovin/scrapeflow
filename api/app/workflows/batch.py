import asyncio
from collections.abc import Coroutine
from typing import Any

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows import steps
    from app.workflows.job import JobRunInput, JobWorkflow
    from app.workflows.job import workflow_id as job_workflow_id
    from app.workflows.queues import WORKFLOW_QUEUE

FINISH_BATCH_ACTIVITY = "finish_batch"


def workflow_id(batch_id: object) -> str:
    return f"batch-{batch_id}"


class BatchInput(BaseModel):
    batch_id: str
    # E.0: children in flight at once; the rest wait here, where no queue timer runs.
    max_in_flight: int
    runs: list[JobRunInput]


class FinishBatchInput(BaseModel):
    batch_id: str
    cancelled: bool = False


@workflow.defn
class BatchWorkflow:
    """A batch on the v2 lane: one child ``JobWorkflow`` per item, at most ``max_in_flight`` at once.

    Each child mirrors its own run and item (and the batch's counters); this workflow only bounds
    the fan-out and closes the batch once every child is done. A cancel reaches the children in
    flight, waits for them to stop, and the rest are never started — the API has cancelled their
    rows already, or ``finish_batch`` does.
    """

    def __init__(self) -> None:
        self._in_flight = 0
        self._cancel_requested = False

    @workflow.run
    async def run(self, input: BatchInput) -> str:
        try:
            await self._fan_out(input)
        except asyncio.CancelledError:
            self._cancel_requested = True
        state = await self._finish(input.batch_id)
        if state == "cancelled" and self._cancel_requested:
            raise asyncio.CancelledError()
        return state

    async def _fan_out(self, input: BatchInput) -> None:
        children: list[asyncio.Task] = []
        try:
            for run in input.runs:
                await workflow.wait_condition(lambda: self._in_flight < input.max_in_flight)
                self._in_flight += 1
                children.append(asyncio.create_task(self._child(run)))
            await asyncio.gather(*children)
        except asyncio.CancelledError:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            raise

    async def _child(self, run: JobRunInput) -> None:
        try:
            await workflow.execute_child_workflow(
                JobWorkflow.run,
                run,
                id=job_workflow_id(run.run_id),
                task_queue=WORKFLOW_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed child has written its run's outcome; one that never ran is failed by
            # finish_batch.
            workflow.logger.info("Batch child %s ended: %s", run.run_id, exc)
        finally:
            self._in_flight -= 1

    async def _finish(self, batch_id: str) -> str:
        arg = FinishBatchInput(batch_id=batch_id, cancelled=self._cancel_requested)
        return await self._through_cancel(steps.db(FINISH_BATCH_ACTIVITY, arg, str))

    async def _through_cancel(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Awaits ``coro`` to completion even if the workflow is cancelled meanwhile."""
        task = asyncio.ensure_future(coro)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    raise
                self._cancel_requested = True
        return task.result()
