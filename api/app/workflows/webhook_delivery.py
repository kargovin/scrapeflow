from datetime import timedelta
from typing import Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeoutError

with workflow.unsafe.imports_passed_through():
    from app.workflows.activities.contracts import (
        EMAIL_SEND_ATTEMPT_SECONDS,
        SEND_EMAIL_ACTIVITY,
        SendEmailInput,
        SendEmailOutput,
    )
    from app.workflows.queues import DELIVERY_QUEUE, WORKFLOW_QUEUE

ATTEMPT_WEBHOOK_ACTIVITY = "attempt_webhook_delivery"
RECORD_EMAIL_ATTEMPT_ACTIVITY = "record_email_attempt"
SWEEP_WEBHOOKS_ACTIVITY = "sweep_pending_webhooks"

# ADR-012: a kind = 'email' row is sent by SendEmail on the delivery queue (M.14).
EMAIL_PATCH = "email-delivery"

# The system Schedule that starts a delivery for any pending row nothing is delivering (F.4).
SWEEP_SCHEDULE_ID = "webhook-sweep"
SWEEP_EVERY = timedelta(minutes=5)

# One attempt is a DB read, a POST of at most 10 s and a DB write. Retried only when the
# activity itself fails (the DB, a worker restart); a refused POST is an outcome, not a failure.
_ATTEMPT_TIMEOUT = timedelta(seconds=30)
_ATTEMPT_RETRY = RetryPolicy(maximum_interval=timedelta(minutes=1))
# Past this the workflow fails and the row stays pending: the sweep starts it again.
_ATTEMPT_WINDOW = timedelta(hours=1)
# A send waits this long for a delivery worker to take it; past it the try is transient.
_SEND_QUEUE_WAIT = timedelta(seconds=60)


def workflow_id(delivery_id: object) -> str:
    return f"webhook-delivery-{delivery_id}"


class DeliveryRef(BaseModel):
    delivery_id: str


class AttemptOutcome(BaseModel):
    # retry: the row is pending again, its next attempt due in retry_in_seconds.
    # gone: the row was deleted, or is no longer pending (delivered or given up elsewhere).
    # email: an email row; nothing was sent yet — `email` is what to send.
    status: Literal["delivered", "exhausted", "retry", "gone", "email"]
    retry_in_seconds: int = 0
    email: SendEmailInput | None = None


class EmailAttempt(BaseModel):
    """How one SendEmail went; recorded on the row by ``record_email_attempt``."""

    delivery_id: str
    error: str | None = None
    error_type: str | None = None
    final: bool = False
    # Recipients the server refused while it accepted others.
    refused: list[str] = []


@workflow.defn
class WebhookDeliveryWorkflow:
    """Delivers one ``webhook_deliveries`` row: what ``webhook_loop.py`` did for it (F.6 deleted
    it), on a timer.

    The row holds the payload, the attempt count and the backoff (``attempt_webhook_delivery``);
    the workflow only waits between attempts. Jobs, batches and crawls write rows; a pipeline
    delivers through its Webhook block instead. An email row's attempt is three steps: the row read
    here, ``SendEmail`` on the delivery queue (no database there), the outcome written back here.
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
            if outcome.status == "email" and workflow.patched(EMAIL_PATCH):
                assert outcome.email is not None
                outcome = await self._send_email(input, outcome.email)
            if outcome.status != "retry":
                return outcome.status
            await workflow.sleep(timedelta(seconds=outcome.retry_in_seconds))

    async def _send_email(self, ref: DeliveryRef, email: SendEmailInput) -> AttemptOutcome:
        attempt = EmailAttempt(delivery_id=ref.delivery_id)
        try:
            sent: SendEmailOutput = await workflow.execute_activity(
                SEND_EMAIL_ACTIVITY,
                email,
                task_queue=DELIVERY_QUEUE,
                result_type=SendEmailOutput,
                start_to_close_timeout=timedelta(seconds=EMAIL_SEND_ATTEMPT_SECONDS),
                schedule_to_start_timeout=_SEND_QUEUE_WAIT,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            attempt.refused = sent.refused
        except ActivityError as exc:
            attempt = _failed_send(ref, exc.cause)
        return await workflow.execute_activity(
            RECORD_EMAIL_ATTEMPT_ACTIVITY,
            attempt,
            task_queue=WORKFLOW_QUEUE,
            result_type=AttemptOutcome,
            start_to_close_timeout=_ATTEMPT_TIMEOUT,
            schedule_to_close_timeout=_ATTEMPT_WINDOW,
            retry_policy=_ATTEMPT_RETRY,
        )


def _failed_send(ref: DeliveryRef, cause: BaseException | None) -> EmailAttempt:
    """The worker's classified error as it says; a timeout transient; anything else final."""
    if isinstance(cause, ApplicationError):
        return EmailAttempt(
            delivery_id=ref.delivery_id,
            error=cause.message,
            error_type=cause.type,
            final=cause.non_retryable,
        )
    if isinstance(cause, ActivityTimeoutError):
        waited = cause.type == TimeoutType.SCHEDULE_TO_START
        return EmailAttempt(
            delivery_id=ref.delivery_id,
            error="no delivery worker took the send"
            if waited
            else "the send did not finish in time",
            error_type="DeliveryTimeout",
        )
    return EmailAttempt(
        delivery_id=ref.delivery_id,
        error=f"the send failed ({type(cause).__name__})",
        error_type="DeliveryFailed",
        final=True,
    )


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
