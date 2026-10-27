from datetime import timedelta
from typing import Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.queues import WORKFLOW_QUEUE

ATTEMPT_WEBHOOK_ACTIVITY = "attempt_webhook_delivery"
SWEEP_WEBHOOKS_ACTIVITY = "sweep_pending_webhooks"

# The system Schedule that starts a delivery for any pending row nothing is delivering (F.4).
SWEEP_SCHEDULE_ID = "webhook-sweep"
SWEEP_EVERY = timedelta(minutes=5)

# One attempt is a DB read, a POST of at most 10 s and a DB write. Retried only when the
# activity itself fails (the DB, a worker restart); a refused POST is an outcome, not a failure.
_ATTEMPT_TIMEOUT = timedelta(seconds=30)
_ATTEMPT_RETRY = RetryPolicy(maximum_interval=timedelta(minutes=1))
# Past this the workflow fails and the row stays pending: the sweep starts it again.
_ATTEMPT_WINDOW = timedelta(hours=1)


def workflow_id(delivery_id: object) -> str:
    return f"webhook-delivery-{delivery_id}"


class DeliveryRef(BaseModel):
    delivery_id: str


class AttemptOutcome(BaseModel):
    # retry: the row is pending again, its next attempt due in retry_in_seconds.
    # gone: the row was deleted, or is no longer pending (delivered or given up elsewhere).
    status: Literal["delivered", "exhausted", "retry", "gone"]
    retry_in_seconds: int = 0


@workflow.defn
class WebhookDeliveryWorkflow:
    """Delivers one ``webhook_deliveries`` row: what ``webhook_loop.py`` did for it (F.6 deleted
    it), on a timer.

    The row holds the payload, the attempt count and the backoff (``attempt_webhook_delivery``);
    the workflow only waits between attempts. Jobs, batches and crawls write rows; a pipeline
    delivers through its Webhook block instead.
    """

    @workflow.run
    async def run(self, input: DeliveryRef) -> str:
        while True:
            outcome: AttemptOutcome = await workflow.execute_activity(
                ATTEMPT_WEBHOOK_ACTIVITY,
                input,
                task_queue=WORKFLOW_QUEUE,
                result_type=AttemptOutcome,
                start_to_close_timeout=_ATTEMPT_TIMEOUT,
                schedule_to_close_timeout=_ATTEMPT_WINDOW,
                retry_policy=_ATTEMPT_RETRY,
            )
            if outcome.status != "retry":
                return outcome.status
            await workflow.sleep(timedelta(seconds=outcome.retry_in_seconds))


@workflow.defn
class WebhookSweepWorkflow:
    """One pass of the ``webhook-sweep`` Schedule: start a delivery for every pending row overdue."""

    @workflow.run
    async def run(self) -> int:
        return await workflow.execute_activity(
            SWEEP_WEBHOOKS_ACTIVITY,
            task_queue=WORKFLOW_QUEUE,
            result_type=int,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
