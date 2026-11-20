"""Runs the destination test from the API (ADR-015 §2d): a DestinationTestWorkflow, awaited ≤ 30 s."""

import asyncio
import uuid
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError

from app.schemas.destinations import DestinationCheck, DestinationTestResult
from app.workflows.activities.contracts import DestinationTestInput
from app.workflows.destination_test import (
    API_WAIT_SECONDS,
    EXECUTION_TIMEOUT,
    TEST_UNAVAILABLE_ERROR,
    DestinationTestWorkflow,
    workflow_id,
)
from app.workflows.queues import WORKFLOW_QUEUE

logger = structlog.get_logger()


async def run_test(
    temporal: Client | None,
    db: AsyncSession,
    kind: str,
    address: dict[str, Any],
    encrypted_secret: str,
) -> DestinationTestResult:
    """The checks as the delivery worker ran them. 503 when no test could run."""
    if temporal is None:
        raise _unavailable("Workflow engine unavailable")
    # Ends the request's read transaction: no pooled connection is held through the wait.
    await db.commit()
    test_id = workflow_id(uuid.uuid4())
    try:
        async with asyncio.timeout(API_WAIT_SECONDS):
            out = await temporal.execute_workflow(
                DestinationTestWorkflow.run,
                DestinationTestInput(kind=kind, address=address, encrypted_secret=encrypted_secret),
                id=test_id,
                task_queue=WORKFLOW_QUEUE,
                execution_timeout=EXECUTION_TIMEOUT,
            )
    except TimeoutError:
        logger.warning("destination_test_timed_out", workflow_id=test_id, kind=kind)
        raise _unavailable(f"The destination test did not finish in {API_WAIT_SECONDS} s") from None
    except WorkflowFailureError as exc:
        cause = exc.cause
        if isinstance(cause, ApplicationError) and cause.type == TEST_UNAVAILABLE_ERROR:
            message = cause.message
        else:
            message = "The destination test could not run"
        logger.warning(
            "destination_test_unavailable", workflow_id=test_id, kind=kind, error=message
        )
        raise _unavailable(message) from None
    except Exception:
        logger.exception("destination_test_start_failed", workflow_id=test_id, kind=kind)
        raise _unavailable("Workflow engine unavailable") from None

    checks = [DestinationCheck(**c.model_dump()) for c in out.checks]
    result = DestinationTestResult(
        passed=bool(checks) and all(c.outcome != "failed" for c in checks), checks=checks
    )
    logger.info(
        "destination_tested",
        workflow_id=test_id,
        kind=kind,
        passed=result.passed,
        **{c.check: c.outcome for c in checks},
    )
    return result


def refuse_failed(result: DestinationTestResult) -> None:
    """Save and edit: any failed check refuses; a warning saves (§2d)."""
    if result.passed:
        return
    failed = next((c for c in result.checks if c.outcome == "failed"), None)
    message = f"{failed.check}: {failed.message}" if failed else "the test ran no check"
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "error": "destination_test_failed",
            "message": f"The destination test failed — {message}",
            "checks": [c.model_dump() for c in result.checks],
        },
    )


def _unavailable(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message)
