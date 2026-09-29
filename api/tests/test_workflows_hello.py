import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.hello import HelloWorkflow, HelloWorkflowInput, SayHelloInput, say_hello
from app.workflows.queues import WORKFLOW_QUEUE

# Each test starts its own in-memory Temporal test server, so sharing WORKFLOW_QUEUE across tests
# cannot collide — and hello.py routes its activity to WORKFLOW_QUEUE explicitly anyway.


async def test_hello_workflow_returns_greeting():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[HelloWorkflow],
            activities=[say_hello],
        ):
            result = await env.client.execute_workflow(
                HelloWorkflow.run,
                HelloWorkflowInput(name="Karthik"),
                id=f"hello-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )

    assert result == "Hello, Karthik"


async def test_hello_workflow_gives_up_after_three_attempts():
    attempts: list[int] = []

    @activity.defn(name="say_hello")
    async def always_fails(input: SayHelloInput) -> str:
        attempts.append(activity.info().attempt)
        raise RuntimeError("boom")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[HelloWorkflow],
            activities=[always_fails],
        ):
            with pytest.raises(WorkflowFailureError) as exc_info:
                await env.client.execute_workflow(
                    HelloWorkflow.run,
                    HelloWorkflowInput(name="Karthik"),
                    id=f"hello-{uuid.uuid4()}",
                    task_queue=WORKFLOW_QUEUE,
                )

    assert attempts == [1, 2, 3]
    activity_error = exc_info.value.cause
    assert isinstance(activity_error, ActivityError)
    assert isinstance(activity_error.cause, ApplicationError)
    assert activity_error.cause.message == "boom"
