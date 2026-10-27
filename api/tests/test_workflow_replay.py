"""C.13 — every recorded history must replay on today's workflow code (ADR-012).

A failure means a change to a workflow body would strand runs started before it: wrap the change
in ``workflow.patched("<id>")``. Never regenerate a history to make this pass — add new ones with
``tests/replay/export_history.py`` (compose) or ``tests/replay/record_harness.py`` (mocked).
Runs before the api image is built (``.github/workflows/build-push.yml``); needs no services.
"""

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from app.workflows.batch import BatchWorkflow
from app.workflows.crawl import CrawlWorkflow
from app.workflows.job import JobWorkflow
from app.workflows.pipeline import PipelineWorkflow
from app.workflows.scheduled_job import ScheduledJobWorkflow
from app.workflows.webhook_delivery import WebhookDeliveryWorkflow, WebhookSweepWorkflow

HISTORIES = sorted((Path(__file__).parent / "replay" / "histories").glob("*.json"))


def test_the_corpus_is_not_empty():
    assert len(HISTORIES) >= 31


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: p.stem)
async def test_history_replays_on_current_code(path: Path):
    history = WorkflowHistory.from_json(path.stem, path.read_text())
    replayer = Replayer(
        workflows=[
            PipelineWorkflow,
            JobWorkflow,
            BatchWorkflow,
            CrawlWorkflow,
            ScheduledJobWorkflow,
            WebhookDeliveryWorkflow,
            WebhookSweepWorkflow,
        ],
        data_converter=pydantic_data_converter,
    )
    # Raises on a nondeterminism error or a payload the current models cannot decode.
    await replayer.replay_workflow(history)
