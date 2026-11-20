"""The destination test (ADR-015 §2d): one test activity on the delivery queue, its checks returned."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeoutError

with workflow.unsafe.imports_passed_through():
    from app.workflows.activities.contracts import (
        TEST_BUCKET_ACTIVITY,
        TEST_POSTGRES_ACTIVITY,
        TEST_SMTP_ACTIVITY,
        TEST_WEBHOOK_ACTIVITY,
        CheckResult,
        DestinationTestInput,
        DestinationTestOutput,
    )
    from app.workflows.queues import DELIVERY_QUEUE

DESTINATION_TEST_WORKFLOW = "DestinationTestWorkflow"

TEST_ACTIVITIES = {
    "bucket": TEST_BUCKET_ACTIVITY,
    "postgres": TEST_POSTGRES_ACTIVITY,
    "smtp": TEST_SMTP_ACTIVITY,
    "webhook": TEST_WEBHOOK_ACTIVITY,
}

# The API waits 30 s (§2d): a queue wait plus the test (the worker's own deadline is 20 s) fit in it.
TEST_QUEUE_WAIT = timedelta(seconds=4)
TEST_TIMEOUT = timedelta(seconds=25)
API_WAIT_SECONDS = 30
# Bounds a test whose workflow worker is gone; the API has stopped waiting long before.
EXECUTION_TIMEOUT = timedelta(seconds=60)

# The workflow fails with this type when nothing could run the test: the API answers 503.
TEST_UNAVAILABLE_ERROR = "DestinationTestUnavailable"


def workflow_id(test_id: object) -> str:
    return f"destination-test-{test_id}"


@workflow.defn(name=DESTINATION_TEST_WORKFLOW)
class DestinationTestWorkflow:
    """Runs the kind's test activity once on the delivery worker (no database, the only worker that
    reaches destinations). The input holds the secret as ciphertext only."""

    @workflow.run
    async def run(self, input: DestinationTestInput) -> DestinationTestOutput:
        try:
            return await workflow.execute_activity(
                TEST_ACTIVITIES[input.kind],
                input,
                task_queue=DELIVERY_QUEUE,
                result_type=DestinationTestOutput,
                schedule_to_start_timeout=TEST_QUEUE_WAIT,
                start_to_close_timeout=TEST_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            return DestinationTestOutput(checks=[failed_test(input.kind, exc.cause)])


def failed_test(kind: str, cause: BaseException | None) -> CheckResult:
    """A test that raised instead of reporting checks. Raises when no delivery worker took it."""
    if isinstance(cause, ActivityTimeoutError):
        if cause.type == TimeoutType.SCHEDULE_TO_START:
            raise ApplicationError(
                "no delivery worker took the test; try again",
                type=TEST_UNAVAILABLE_ERROR,
                non_retryable=True,
            )
        return _failed("the test did not finish in time")
    if isinstance(cause, ApplicationError):
        # An activity no delivery worker registers (the SDK's own type); its message lists the rest.
        if cause.type == "NotFoundError":
            return _failed(f"the delivery worker cannot test a {kind} destination")
        return _failed(cause.message)
    return _failed(f"the test failed ({type(cause).__name__})")


def _failed(message: str) -> CheckResult:
    return CheckResult(check="test", outcome="failed", message=message)
