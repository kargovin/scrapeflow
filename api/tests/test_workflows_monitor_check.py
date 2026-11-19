"""L.7 — MonitorCheckWorkflow: one firing of a monitor's Schedule (time-skipping env, admission
mocked, the child PipelineWorkflow faked)."""

import uuid
from datetime import timedelta
from itertools import pairwise

from temporalio import activity, workflow
from temporalio.api.enums.v1 import ParentClosePolicy as ApiParentClosePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.monitor_check import (
    ADMIT_MONITOR_CHECK_ACTIVITY,
    WAIT_CAP,
    WAIT_FIRST,
    WAIT_MAX,
    AdmitMonitorCheckInput,
    MonitorAdmission,
    MonitorCheckInput,
    MonitorCheckWorkflow,
    workflow_id,
)
from app.workflows.pipeline import MonitorContext, PipelineRunInput
from app.workflows.queues import WORKFLOW_QUEUE

# The sandbox re-imports this module to check the fake workflow; that one pulls in the DB.
with workflow.unsafe.imports_passed_through():
    from tests.test_workflows_pipeline import PARSED

MONITOR_ID = "22222222-2222-2222-2222-222222222222"
PARENT_CLOSE_POLICY_ABANDON = ApiParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON


def _run(run_id: str) -> PipelineRunInput:
    return PipelineRunInput(
        run_id=run_id,
        pipeline_id=str(uuid.UUID(int=1)),
        blocks=PARSED.blocks,
        time_budget_seconds=PARSED.run_budget_seconds,
        artifact_ids={b.id: str(uuid.UUID(int=i + 2)) for i, b in enumerate(PARSED.blocks)},
        monitor=MonitorContext(monitor_id=MONITOR_ID),
    )


CHILD: dict = {}


@activity.defn(name="fake_child_started")
async def fake_child_started(args: list) -> str:
    workflow_id, input = args
    CHILD.setdefault("runs", []).append((workflow_id, PipelineRunInput.model_validate(input)))
    return CHILD["outcome"]


@workflow.defn(name="PipelineWorkflow")
class FakePipelineWorkflow:
    @workflow.run
    async def run(self, input: PipelineRunInput) -> str | None:
        outcome = await workflow.execute_activity(
            "fake_child_started",
            [workflow.info().workflow_id, input.model_dump(mode="json")],
            start_to_close_timeout=timedelta(seconds=5),
            result_type=str,
        )
        if outcome == "failed":
            raise ApplicationError("fetch: blocked:akamai", non_retryable=True)
        return None


class Harness:
    def __init__(self, verdicts, child="completed"):
        # Each ask takes the next verdict; the last one repeats.
        self.verdicts = list(verdicts)
        self.child = child
        self.asks: list[tuple[AdmitMonitorCheckInput, object]] = []

    async def run(self):
        CHILD.clear()
        CHILD["outcome"] = self.child
        env_box = {}

        @activity.defn(name=ADMIT_MONITOR_CHECK_ACTIVITY)
        async def admit(input: AdmitMonitorCheckInput) -> MonitorAdmission:
            self.asks.append((input, await env_box["env"].get_current_time()))
            verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            if verdict == "waiting_unless_last":
                return MonitorAdmission(verdict="skipped" if input.last_chance else "waiting")
            run = _run(input.run_id) if verdict == "admitted" else None
            return MonitorAdmission(verdict=verdict, run=run)

        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            env_box["env"] = env
            async with Worker(
                env.client,
                task_queue=WORKFLOW_QUEUE,
                workflows=[MonitorCheckWorkflow, FakePipelineWorkflow],
                activities=[admit, fake_child_started],
            ):
                handle = await env.client.start_workflow(
                    MonitorCheckWorkflow.run,
                    MonitorCheckInput(monitor_id=MONITOR_ID),
                    id=f"{workflow_id(MONITOR_ID)}-{uuid.uuid4()}",
                    task_queue=WORKFLOW_QUEUE,
                    execution_timeout=timedelta(days=3),
                )
                result = await handle.result()
                self.history = await handle.fetch_history()
                return result


async def test_an_admitted_firing_runs_its_check_as_a_child_named_after_the_run():
    h = Harness(["admitted"])
    assert await h.run() == "completed"

    [(ask, _)] = h.asks
    assert (ask.monitor_id, ask.last_chance) == (MONITOR_ID, False)
    [(child_id, child_input)] = CHILD["runs"]
    assert child_id == f"pipeline-run-{ask.run_id}"
    assert child_input == _run(ask.run_id)
    [started] = [
        e.start_child_workflow_execution_initiated_event_attributes
        for e in h.history.events
        if e.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert started.parent_close_policy == PARENT_CLOSE_POLICY_ABANDON


async def test_a_failed_check_does_not_fail_the_firing():
    h = Harness(["admitted"], child="failed")
    assert await h.run() == "failed"


async def test_skipped_failed_and_stopped_firings_run_nothing():
    for verdict in ("skipped", "failed", "stopped"):
        h = Harness([verdict])
        assert await h.run() == verdict
        assert len(h.asks) == 1 and "runs" not in CHILD


async def test_a_full_concurrency_meter_waits_on_a_doubling_timer_with_one_run_id():
    h = Harness(["waiting"] * 6 + ["admitted"])
    assert await h.run() == "completed"

    assert len({ask.run_id for ask, _ in h.asks}) == 1
    gaps = [later - earlier for (_, earlier), (_, later) in pairwise(h.asks)]
    expected = [min(WAIT_FIRST * 2**n, WAIT_MAX) for n in range(6)]
    for gap, want in zip(gaps, expected, strict=True):
        assert want <= gap < want + timedelta(seconds=5)
    assert len(CHILD["runs"]) == 1


async def test_a_firing_gives_up_after_a_day_of_waiting():
    h = Harness(["waiting_unless_last"])
    assert await h.run() == "skipped"

    last, _ = h.asks[-1]
    assert last.last_chance and not any(ask.last_chance for ask, _ in h.asks[:-1])
    assert h.asks[-1][1] - h.asks[0][1] >= WAIT_CAP
