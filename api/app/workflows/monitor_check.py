"""One firing of a monitor's Temporal Schedule (ADR-016 §3)."""

import asyncio
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import CancelledError, ChildWorkflowError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows import steps
    from app.workflows.pipeline import CHECK_SETTLED_SIGNAL, PipelineRunInput, PipelineWorkflow
    from app.workflows.queues import WORKFLOW_QUEUE

# ADR-012: the tick returns once its check waits for an approval (ADR-016 §9e, N.3).
APPROVAL_SETTLED_PATCH = "approval-settled"

# The Schedule starts the workflow by this type name.
MONITOR_CHECK_WORKFLOW = "MonitorCheckWorkflow"
ADMIT_MONITOR_CHECK_ACTIVITY = "admit_monitor_check"

# A firing waiting for a concurrency slot re-asks on a doubling timer (§3b), as a job's tick does...
WAIT_FIRST = timedelta(seconds=15)
WAIT_MAX = timedelta(minutes=5)
# ...and gives up past this. Under SKIP the Schedule drops every firing while one waits.
WAIT_CAP = timedelta(hours=24)


def workflow_id(monitor_id: object) -> str:
    # The Schedule appends each firing's time to this.
    return f"monitor-check-{monitor_id}"


class MonitorCheckInput(BaseModel):
    monitor_id: str


class AdmitMonitorCheckInput(BaseModel):
    monitor_id: str
    # Minted by the workflow, so a retried admission finds the check it already inserted.
    run_id: str
    # Past WAIT_CAP: a full concurrency meter gives up instead of waiting again.
    last_chance: bool = False


# admitted: a check inserted · waiting: concurrency full · skipped: a monitor event, no run ·
# failed: a failed check inserted (storage full, or it cannot run) · stopped: not active or gone.
Verdict = Literal["admitted", "waiting", "skipped", "failed", "stopped"]


class MonitorAdmission(BaseModel):
    verdict: Verdict
    run: PipelineRunInput | None = None


@workflow.defn(name=MONITOR_CHECK_WORKFLOW)
class MonitorCheckWorkflow:
    """Admit a check against the monitor and the meters, then run it as a child.

    Open until the check ends or waits for an approval, so the Schedule's ``SKIP`` drops a
    firing that comes while one waits for a slot or runs (§3c, §9e). It never fails because its
    check did: the Schedule must not pause.
    """

    def __init__(self) -> None:
        self._settled = False

    @workflow.signal(name=CHECK_SETTLED_SIGNAL)
    def check_settled(self) -> None:
        self._settled = True

    @workflow.run
    async def run(self, input: MonitorCheckInput) -> str:
        run_id = str(workflow.uuid4())
        started = workflow.now()
        delay = WAIT_FIRST
        while True:
            admission: MonitorAdmission = await steps.db(
                ADMIT_MONITOR_CHECK_ACTIVITY,
                AdmitMonitorCheckInput(
                    monitor_id=input.monitor_id,
                    run_id=run_id,
                    last_chance=workflow.now() - started >= WAIT_CAP,
                ),
                MonitorAdmission,
            )
            if admission.verdict != "waiting":
                break
            await workflow.sleep(delay)
            delay = min(delay * 2, WAIT_MAX)
        if admission.verdict != "admitted":
            return admission.verdict

        settles = workflow.patched(APPROVAL_SETTLED_PATCH)
        child = workflow.execute_child_workflow(
            PipelineWorkflow.run,
            admission.run,
            id=f"pipeline-run-{run_id}",
            task_queue=WORKFLOW_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            # The check outlives this workflow (terminated, or settled): its row is its record.
            parent_close_policy=ParentClosePolicy.ABANDON,
        )
        try:
            if not settles:
                await child
            elif not await self._check_ends(asyncio.ensure_future(child)):
                # §9e: it waits for an approval and runs on alone.
                return "awaiting_approval"
        except ChildWorkflowError as exc:
            return "cancelled" if isinstance(exc.cause, CancelledError) else "failed"
        return "completed"

    async def _check_ends(self, child: "asyncio.Future[object]") -> bool:
        """True once the check has ended (raising as it did); False when it settles first."""
        try:
            await workflow.wait_condition(lambda: self._settled or child.done())
        except asyncio.CancelledError:
            # As the unpatched branch: the cancel goes on to the check.
            child.cancel()
        if not child.done() and self._settled:
            return False
        await child
        return True
