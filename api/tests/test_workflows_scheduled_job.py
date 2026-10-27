"""F.1 — ScheduledJobWorkflow: one tick of a job's Schedule (time-skipping env, activities mocked)."""

import asyncio
import uuid
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.activities.contracts import ScrapeInput
from app.workflows.job import JobRunInput
from app.workflows.queues import WORKFLOW_QUEUE
from app.workflows.scheduled_job import (
    ADMIT_SCHEDULED_RUN_ACTIVITY,
    WAIT_CAP,
    WAIT_FIRST,
    WAIT_MAX,
    Admission,
    AdmitInput,
    ScheduledJobInput,
    ScheduledJobWorkflow,
    workflow_id,
)

JOB_ID = "11111111-1111-1111-1111-111111111111"


def _run(run_id: str) -> JobRunInput:
    return JobRunInput(
        run_id=run_id,
        engine="http",
        scrape=ScrapeInput(artifact_id=run_id, url="https://example.com", output_format="html"),
    )


# What the child JobWorkflow did and is to do, per test. Written by an activity: the fake child
# runs in the workflow sandbox, which has its own copy of this module.
CHILD: dict = {}


@activity.defn(name="fake_child_started")
async def fake_child_started(args: list) -> str:
    workflow_id, input = args
    CHILD.setdefault("runs", []).append((workflow_id, JobRunInput.model_validate(input)))
    return CHILD["outcome"]


@workflow.defn(name="JobWorkflow")
class FakeJobWorkflow:
    @workflow.run
    async def run(self, input: JobRunInput) -> str | None:
        outcome = await workflow.execute_activity(
            "fake_child_started",
            [workflow.info().workflow_id, input.model_dump()],
            start_to_close_timeout=timedelta(seconds=5),
            result_type=str,
        )
        if outcome == "failed":
            raise ApplicationError("blocked:akamai", non_retryable=True)
        if outcome == "wait":
            await workflow.wait_condition(lambda: False)
        return None


class Harness:
    def __init__(self, verdicts, child="completed"):
        # Each ask takes the next verdict; the last one repeats.
        self.verdicts = list(verdicts)
        self.child = child
        self.asks: list[tuple[AdmitInput, object]] = []

    async def run(self, during=None):
        CHILD.clear()
        CHILD["outcome"] = self.child
        env_box = {}

        @activity.defn(name=ADMIT_SCHEDULED_RUN_ACTIVITY)
        async def admit(input: AdmitInput) -> Admission:
            self.asks.append((input, await env_box["env"].get_current_time()))
            verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            if verdict == "waiting_unless_last" and not input.last_chance:
                return Admission(verdict="waiting")
            if verdict == "waiting_unless_last":
                return Admission(verdict="skipped")
            run = _run(input.run_id) if verdict == "admitted" else None
            return Admission(verdict=verdict, run=run)

        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            env_box["env"] = env
            async with Worker(
                env.client,
                task_queue=WORKFLOW_QUEUE,
                workflows=[ScheduledJobWorkflow, FakeJobWorkflow],
                activities=[admit, fake_child_started],
            ):
                handle = await env.client.start_workflow(
                    ScheduledJobWorkflow.run,
                    ScheduledJobInput(job_id=JOB_ID),
                    id=f"{workflow_id(JOB_ID)}-{uuid.uuid4()}",
                    task_queue=WORKFLOW_QUEUE,
                    execution_timeout=timedelta(days=3),
                )
                if during:
                    await during(handle)
                result = await handle.result()
                self.history = await handle.fetch_history()
                return result


async def test_an_admitted_tick_runs_its_job_as_a_child_named_after_the_run():
    h = Harness(["admitted"])
    assert await h.run() == "completed"

    [(ask, _)] = h.asks
    assert (ask.job_id, ask.last_chance) == (JOB_ID, False)
    [(child_id, child_input)] = CHILD["runs"]
    assert child_id == f"job-run-{ask.run_id}"
    assert child_input == _run(ask.run_id)


async def test_a_full_concurrency_meter_waits_on_a_doubling_timer_with_one_run_id():
    h = Harness(["waiting"] * 5 + ["admitted"])
    assert await h.run() == "completed"

    assert len(h.asks) == 6
    assert len({ask.run_id for ask, _ in h.asks}) == 1
    gaps = [b - a for (_, a), (_, b) in zip(h.asks, h.asks[1:], strict=False)]
    expected = [WAIT_FIRST * 2**i for i in range(5)]
    expected = [min(e, WAIT_MAX) for e in expected]
    for gap, want in zip(gaps, expected, strict=True):
        assert want <= gap < want + timedelta(seconds=5)
    assert len(CHILD["runs"]) == 1


async def test_a_skipped_failed_or_stopped_tick_ends_without_a_run():
    for verdict in ("skipped", "failed", "stopped"):
        h = Harness([verdict])
        assert await h.run() == verdict
        assert len(h.asks) == 1
        assert "runs" not in CHILD


async def test_a_tick_that_waits_past_the_cap_asks_once_more_as_its_last_chance():
    h = Harness(["waiting_unless_last"])
    assert await h.run() == "skipped"

    *waits, (last, at) = h.asks
    assert not any(ask.last_chance for ask, _ in waits)
    assert last.last_chance
    assert at - h.asks[0][1] >= WAIT_CAP
    # Doubling to 5 min, not every 15 s: a day of waiting stays a few hundred asks.
    assert len(h.asks) < 300
    assert "runs" not in CHILD


async def test_a_failed_run_ends_the_tick_without_failing_it():
    # The run's failure is on its row; a failed tick would only add noise to the Schedule.
    assert await Harness(["admitted"], child="failed").run() == "failed"


async def test_a_cancelled_run_ends_the_tick_as_cancelled():
    h = Harness(["admitted"], child="wait")

    async def during(handle):
        while "runs" not in CHILD:
            await asyncio.sleep(0.05)
        [(child_id, _)] = CHILD["runs"]
        # As the API's cancel does: the run's workflow, by its id.
        await handle._client.get_workflow_handle(child_id).cancel()

    assert await h.run(during) == "cancelled"
