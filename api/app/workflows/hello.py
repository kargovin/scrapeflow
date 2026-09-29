from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.queues import WORKFLOW_QUEUE


@dataclass
class SayHelloInput:
    name: str


@dataclass
class HelloWorkflowInput:
    name: str


@activity.defn
async def say_hello(input: SayHelloInput) -> str:
    return f"Hello, {input.name}"


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, input: HelloWorkflowInput) -> str:
        return await workflow.execute_activity(
            say_hello,
            SayHelloInput(name=input.name),
            task_queue=WORKFLOW_QUEUE,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
