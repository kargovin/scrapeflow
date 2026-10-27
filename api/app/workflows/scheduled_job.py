from datetime import timedelta
from typing import Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import CancelledError, ChildWorkflowError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows import steps
    from app.workflows.job import JobRunInput, JobWorkflow
    from app.workflows.job import workflow_id as job_workflow_id
    from app.workflows.queues import WORKFLOW_QUEUE

ADMIT_SCHEDULED_RUN_ACTIVITY = "admit_scheduled_run"

# A tick waiting for a concurrency slot re-asks on a doubling timer (ADR-010 §1)...
WAIT_FIRST = timedelta(seconds=15)
WAIT_MAX = timedelta(minutes=5)
# ...and is skipped past this. Under SKIP the Schedule drops every tick while one waits.
WAIT_CAP = timedelta(hours=24)


def workflow_id(job_id: object) -> str:
    # The Schedule appends each tick's time to this.
    return f"scheduled-job-{job_id}"


class ScheduledJobInput(BaseModel):
    job_id: str


class AdmitInput(BaseModel):
    job_id: str
    # Minted by the workflow, so a retried admission finds the run it already inserted.
    run_id: str
    # Past WAIT_CAP: a full concurrency meter skips the tick instead of waiting again.
    last_chance: bool = False


# admitted: run inserted · waiting: concurrency full · skipped: recorded on the job, no run ·
# failed: storage full, a failed run inserted · stopped: the job is gone, paused or not v2's.
Verdict = Literal["admitted", "waiting", "skipped", "failed", "stopped"]


class Admission(BaseModel):
    verdict: Verdict
    run: JobRunInput | None = None


@workflow.defn
class ScheduledJobWorkflow:
    """One tick of a job's Temporal Schedule: admit a run against the meters, then run it.

    The Schedule fires on every tick; the meters decide here (ADR-010 §1): monthly skips the tick,
    storage fails it, concurrency waits on a timer and asks again. The run itself is a child
    ``JobWorkflow``, and this workflow stays open until it ends, so the Schedule's ``SKIP``
    (ADR-013) drops a tick that fires while the previous one is waiting or running.
    """

    @workflow.run
    async def run(self, input: ScheduledJobInput) -> str:
        run_id = str(workflow.uuid4())
        started = workflow.now()
        delay = WAIT_FIRST
        while True:
            admission: Admission = await steps.db(
                ADMIT_SCHEDULED_RUN_ACTIVITY,
                AdmitInput(
                    job_id=input.job_id,
                    run_id=run_id,
                    last_chance=workflow.now() - started >= WAIT_CAP,
                ),
                Admission,
            )
            if admission.verdict != "waiting":
                break
            await workflow.sleep(delay)
            delay = min(delay * 2, WAIT_MAX)
        if admission.verdict != "admitted":
            return admission.verdict

        try:
            await workflow.execute_child_workflow(
                JobWorkflow.run,
                admission.run,
                id=job_workflow_id(run_id),
                task_queue=WORKFLOW_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                # The run outlives this workflow if it is terminated: its row is the run's record.
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
        except ChildWorkflowError as exc:
            # The run's outcome is on its row; the Schedule never pauses on a failure (ADR-013).
            return "cancelled" if isinstance(exc.cause, CancelledError) else "failed"
        return "completed"
