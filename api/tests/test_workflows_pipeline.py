"""C.6 + C.7 — PipelineWorkflow and the workflow-worker DB activities."""

import asyncio
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import delete, select, update
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from temporalio.worker.workflow_sandbox import RestrictedWorkflowAccessError

from app.core.db import AsyncSessionLocal
from app.core.pipeline_status import PIPELINE_STATUS_CHANNEL
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.storage_object import StorageObject
from app.models.user_quota import UserQuota
from app.pipelines.catalog import (
    BLOCK_OVERHEAD_SECONDS,
    CLEAN,
    LLM_EXTRACT,
    RELEASE_OUTPUTS_TIMING,
    SCRAPE,
    VALIDATE,
    WEBHOOK,
)
from app.pipelines.definition import parse_definition
from app.settings import settings
from app.workflows.activities.contracts import (
    CAPTURE_ACTIVITY,
    CLEAN_ACTIVITY,
    DELIVER_BUCKET_ACTIVITY,
    DIFF_ACTIVITY,
    LLM_EXTRACT_ACTIVITY,
    SCRAPE_ACTIVITY,
    UNDO_BUCKET_ACTIVITY,
    VALIDATE_ACTIVITY,
    CapturedValue,
    CaptureInput,
    CaptureOutput,
    CleanInput,
    CleanOutput,
    Credentials,
    DeliverBucketInput,
    DeliverBucketOutput,
    DiffInput,
    DiffOutput,
    DiffSummary,
    LLMInput,
    LLMOutput,
    ScrapeInput,
    ScrapeOutput,
    StoredObject,
    UndoBucketInput,
    UndoBucketOutput,
    ValidateInput,
    ValidateVerdict,
)
from app.workflows.activities.db import (
    check_cancelled,
    mirror_block_status,
    mirror_run_status,
    record_storage,
    release_run_outputs,
)
from app.workflows.pipeline import (
    DB_ACTIVITY_SECONDS,
    LOAD_DESTINATION_ACTIVITY,
    MIRROR_BLOCK_ACTIVITY,
    MIRROR_ROLLBACK_STARTED_ACTIVITY,
    MIRROR_RUN_ACTIVITY,
    MIRROR_UNDO_ACTIVITY,
    OUTPUTS_LEFT_ERROR,
    RECORD_STORAGE_ACTIVITY,
    RELEASE_RUN_OUTPUTS_ACTIVITY,
    RUN_CANCELLED_ERROR,
    WEBHOOK_ACTIVITY,
    LLMKey,
    LoadDestinationInput,
    LoadedDestination,
    MirrorBlockInput,
    MirrorRunInput,
    MirrorUndoInput,
    PipelineRunInput,
    PipelineWorkflow,
    RecordStorageInput,
    RollbackStartedInput,
    RunRef,
    StorageUsage,
    WebhookInput,
)
from app.workflows.queues import (
    CONTENT_QUEUE,
    DELIVERY_QUEUE,
    LLM_QUEUE,
    SCRAPE_HTTP_QUEUE,
    WORKFLOW_QUEUE,
)
from app.workflows.steps import (
    ACQUIRE_HOST_SLOT_ACTIVITY,
    HOST_LEASE_MARGIN,
    RELEASE_HOST_SLOT_ACTIVITY,
    HostSlot,
    HostSlotInput,
    HostSlotRelease,
)

SCRAPE_PATH = "scrapeflow-results/history/a-fetch/scrape.html"
SHOT_PATH = "scrapeflow-results/screenshots/a-fetch/0.png"
LLM_PATH = "scrapeflow-results/history/a-extract/llm.json"
CLEAN_PATH = "scrapeflow-results/history/a-clean/clean.md"
KEY_ID = "6b1f2c1e-0000-4000-8000-000000000001"

DEFINITION = {
    "inputs": {"url": {"type": "url"}},
    "time_budget_seconds": None,
    "blocks": [
        {
            "id": "fetch",
            "type": "scrape",
            "input": None,
            "config_schema_version": 1,
            "config": {
                "url": {"$input": "url"},
                "output_format": "html",
                "engine": "http",
                "playwright_options": None,
                "respect_robots": False,
                "proxy_url": None,
                "cookies": None,
                "actions": None,
            },
            "secrets": {},
        },
        {
            "id": "extract",
            "type": "llm_extract",
            "input": "fetch",
            "config_schema_version": 1,
            "config": {"llm_key_id": KEY_ID, "model": "m", "output_schema": {"type": "object"}},
            "secrets": {},
        },
    ],
}
PARSED, _ = parse_definition(DEFINITION)

RUN_INPUT = PipelineRunInput(
    run_id="r1",
    pipeline_id="p1",
    blocks=PARSED.blocks,
    time_budget_seconds=PARSED.run_budget_seconds,
    inputs={"url": "https://example.com"},
    artifact_ids={"fetch": "a-fetch", "extract": "a-extract"},
    llm_keys={KEY_ID: LLMKey(provider="anthropic", encrypted_api_key="ciphertext")},
    credentials={"fetch": Credentials(encrypted_proxy_url="proxy-ciphertext")},
)


def _scrape_output(screenshots=()):
    return ScrapeOutput.model_validate(
        {
            "result": {"path": SCRAPE_PATH, "size": 10},
            "content_hash": "00c0ffee12345678",
            "screenshots": [{"path": p, "size": 5} for p in screenshots],
        }
    )


async def _scrape_ok(input: ScrapeInput) -> ScrapeOutput:
    return _scrape_output()


async def _llm_ok(input: LLMInput) -> LLMOutput:
    return LLMOutput.model_validate({"result": {"path": LLM_PATH, "size": 3}})


async def _clean_ok(input: CleanInput) -> CleanOutput:
    return CleanOutput.model_validate({"result": {"path": CLEAN_PATH, "size": 7}})


async def _validate_ok(input: ValidateInput) -> None:
    return None


async def _release_ok(input: RunRef) -> None:
    return None


DIFF_PATH = "scrapeflow-results/history/a-gate/diff.json"


async def _diff_ok(input: DiffInput) -> DiffOutput:
    return DiffOutput(
        summary=DiffSummary(status="ok", kind=input.kind, lines=["-old", "+new"], lines_added=1),
        diff=StoredObject(path=f"scrapeflow-results/history/{input.artifact_id}/diff.json", size=9),
    )


async def _capture_ok(input: CaptureInput) -> CaptureOutput:
    return CaptureOutput(values={v.input: CapturedValue(value=8) for v in input.values})


async def _webhook_ok(input: WebhookInput) -> None:
    return None


BUCKET_ADDRESS = {"endpoint": "https://s3.example.com/", "bucket": "exports", "prefix": "out"}


async def _load_ok(input: LoadDestinationInput) -> LoadedDestination:
    return LoadedDestination(address=BUCKET_ADDRESS, encrypted_secret="bucket-ciphertext")


async def _bucket_ok(input: DeliverBucketInput) -> DeliverBucketOutput:
    key = f"out/{input.pipeline_id}/{input.run_id}/{input.block_id}.{input.ext}"
    return DeliverBucketOutput.model_validate(
        {
            "location": {"endpoint": "https://s3.example.com/", "bucket": "exports", "key": key},
            "size": 3,
        }
    )


async def _undo_ok(input: UndoBucketInput) -> UndoBucketOutput:
    return UndoBucketOutput()


class Harness:
    """Mocked activities on their real queues; records every DB-activity call in order."""

    def __init__(
        self,
        scrape=_scrape_ok,
        llm=_llm_ok,
        clean=_clean_ok,
        validate=_validate_ok,
        webhook=_webhook_ok,
        usage=None,
        slots=(),
        release_outputs=_release_ok,
        diff=_diff_ok,
        capture=_capture_ok,
        load=_load_ok,
        bucket=_bucket_ok,
        undo=_undo_ok,
    ):
        self.calls: list = []
        self.acquired: list[HostSlotInput] = []
        self.released: list[HostSlotRelease] = []
        self.asked = asyncio.Event()
        # What each acquire returns, in turn; then a granted slot.
        self.slots = list(slots)
        self.scraped: list[ScrapeInput] = []
        self.scrape_info: list = []
        self.extracted: list[LLMInput] = []
        self.cleaned: list[CleanInput] = []
        self.validated: list[ValidateInput] = []
        self.content_info: list = []
        self.delivered: list[WebhookInput] = []
        self.webhook_info: list = []
        self.usage = usage or StorageUsage(used=0, limit=100)
        self.running_reply: dict[str, str] = {}
        self._scrape = scrape
        self._llm = llm
        self._clean = clean
        self._validate = validate
        self._webhook = webhook
        self._release_outputs = release_outputs
        self._diff = diff
        self._capture = capture
        self.release_info: list = []
        self._load = load
        self._bucket = bucket
        self.bucket_inputs: list[DeliverBucketInput] = []
        self.bucket_info: list = []
        self._undo = undo
        self.undo_inputs: list[UndoBucketInput] = []
        self.undo_info: list = []
        # What the rollback mirrors answer: the run's status.
        self.rollback_reply = "running"

    def activities(self):
        @activity.defn(name=MIRROR_BLOCK_ACTIVITY)
        async def mirror_block(input: MirrorBlockInput) -> str:
            self.calls.append(input)
            if input.status == "running":
                return self.running_reply.get(input.block_id, "running")
            return "running"

        @activity.defn(name=MIRROR_RUN_ACTIVITY)
        async def mirror_run(input: MirrorRunInput) -> str:
            self.calls.append(input)
            return input.status

        @activity.defn(name=RECORD_STORAGE_ACTIVITY)
        async def record(input: RecordStorageInput) -> StorageUsage:
            self.calls.append(input)
            return self.usage

        @activity.defn(name=SCRAPE_ACTIVITY)
        async def scrape(input: ScrapeInput) -> ScrapeOutput:
            self.scraped.append(input)
            self.scrape_info.append(activity.info())
            return await self._scrape(input)

        @activity.defn(name=LLM_EXTRACT_ACTIVITY)
        async def llm(input: LLMInput) -> LLMOutput:
            self.extracted.append(input)
            return await self._llm(input)

        @activity.defn(name=CLEAN_ACTIVITY)
        async def clean(input: CleanInput) -> CleanOutput:
            self.cleaned.append(input)
            self.content_info.append(activity.info())
            return await self._clean(input)

        @activity.defn(name=VALIDATE_ACTIVITY)
        async def validate(input: ValidateInput) -> ValidateVerdict | None:
            self.validated.append(input)
            self.content_info.append(activity.info())
            return await self._validate(input)

        @activity.defn(name=WEBHOOK_ACTIVITY)
        async def webhook(input: WebhookInput) -> None:
            self.delivered.append(input)
            self.webhook_info.append(activity.info())
            return await self._webhook(input)

        @activity.defn(name=ACQUIRE_HOST_SLOT_ACTIVITY)
        async def acquire(input: HostSlotInput) -> HostSlot:
            self.acquired.append(input)
            self.asked.set()
            return self.slots.pop(0) if self.slots else HostSlot(domain="example.com")

        @activity.defn(name=RELEASE_HOST_SLOT_ACTIVITY)
        async def release(input: HostSlotRelease) -> None:
            self.released.append(input)

        @activity.defn(name=RELEASE_RUN_OUTPUTS_ACTIVITY)
        async def release_outputs(input: RunRef) -> None:
            self.calls.append(input)
            self.release_info.append(activity.info())
            return await self._release_outputs(input)

        @activity.defn(name=DIFF_ACTIVITY)
        async def diff(input: DiffInput) -> DiffOutput:
            self.calls.append(input)
            return await self._diff(input)

        @activity.defn(name=CAPTURE_ACTIVITY)
        async def capture(input: CaptureInput) -> CaptureOutput:
            self.calls.append(input)
            return await self._capture(input)

        @activity.defn(name=LOAD_DESTINATION_ACTIVITY)
        async def load(input: LoadDestinationInput) -> LoadedDestination:
            self.calls.append(input)
            return await self._load(input)

        @activity.defn(name=DELIVER_BUCKET_ACTIVITY)
        async def deliver_bucket(input: DeliverBucketInput) -> DeliverBucketOutput:
            self.bucket_inputs.append(input)
            self.bucket_info.append(activity.info())
            return await self._bucket(input)

        @activity.defn(name=MIRROR_ROLLBACK_STARTED_ACTIVITY)
        async def rollback_started(input: RollbackStartedInput) -> str:
            self.calls.append(input)
            return self.rollback_reply

        @activity.defn(name=MIRROR_UNDO_ACTIVITY)
        async def mirror_undo(input: MirrorUndoInput) -> str:
            self.calls.append(input)
            return self.rollback_reply

        @activity.defn(name=UNDO_BUCKET_ACTIVITY)
        async def undo_bucket(input: UndoBucketInput) -> UndoBucketOutput:
            self.undo_inputs.append(input)
            self.undo_info.append(activity.info())
            return await self._undo(input)

        workflow_queue = [mirror_block, mirror_run, record, webhook, acquire, release]
        workflow_queue += [release_outputs, load, rollback_started, mirror_undo]
        self.delivery_queue = [deliver_bucket, undo_bucket]
        return workflow_queue, scrape, llm, [clean, validate, diff, capture]

    async def run(self, run_input=RUN_INPUT, timeout=timedelta(days=1), during=None, **kw):
        db_activities, scrape, llm, content = self.activities()
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            async with (
                Worker(
                    env.client,
                    task_queue=WORKFLOW_QUEUE,
                    workflows=[PipelineWorkflow],
                    activities=db_activities,
                    **kw,
                ),
                Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
                Worker(env.client, task_queue=LLM_QUEUE, activities=[llm]),
                Worker(env.client, task_queue=CONTENT_QUEUE, activities=content),
                Worker(env.client, task_queue=DELIVERY_QUEUE, activities=self.delivery_queue),
            ):
                handle = await env.client.start_workflow(
                    PipelineWorkflow.run,
                    run_input,
                    id=f"pipeline-run-{uuid.uuid4()}",
                    task_queue=WORKFLOW_QUEUE,
                    # Caps every activity timeout too. A failing workflow task retries forever
                    # in real time (no time skipping), so stuck-task tests pass a short one.
                    execution_timeout=timeout,
                )
                if during:
                    await during(handle)
                try:
                    return await handle.result()
                except WorkflowFailureError as exc:
                    return exc
                finally:
                    # The replay corpus records from here (tests/replay/record_harness.py).
                    self.history = await handle.fetch_history()


def _block_calls(calls):
    return [(c.block_id, c.status) for c in calls if isinstance(c, MirrorBlockInput)]


# ---------------------------------------------------------------------------
# PipelineWorkflow (time-skipping env, activities mocked)
# ---------------------------------------------------------------------------


async def test_blocks_run_in_order_and_every_step_is_mirrored_and_charged():
    async def scrape(input):
        return _scrape_output([SHOT_PATH])

    h = Harness(scrape=scrape)
    result = await h.run()

    assert result == LLM_PATH
    [scraped] = h.scraped
    assert (scraped.artifact_id, scraped.url, scraped.output_format) == (
        "a-fetch",
        "https://example.com",
        "html",
    )
    assert scraped.credentials == Credentials(encrypted_proxy_url="proxy-ciphertext")
    [llm] = h.extracted
    assert (llm.artifact_id, llm.raw_minio_path, llm.encrypted_api_key) == (
        "a-extract",
        SCRAPE_PATH,
        "ciphertext",
    )
    assert h.calls == [
        MirrorBlockInput(run_id="r1", block_id="fetch", status="running"),
        RecordStorageInput(
            run_id="r1",
            block_id="fetch",
            objects=[
                StoredObject(path=SCRAPE_PATH, size=10),
                StoredObject(path=SHOT_PATH, size=5),
            ],
        ),
        MirrorBlockInput(run_id="r1", block_id="fetch", status="completed", output_ref=SCRAPE_PATH),
        MirrorBlockInput(run_id="r1", block_id="extract", status="running", input_ref=SCRAPE_PATH),
        RecordStorageInput(
            run_id="r1", block_id="extract", objects=[StoredObject(path=LLM_PATH, size=3)]
        ),
        MirrorBlockInput(run_id="r1", block_id="extract", status="completed", output_ref=LLM_PATH),
        MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH),
    ]


async def test_block_activity_timeouts_come_from_the_catalog():
    h = Harness()
    await h.run()
    [info] = h.scrape_info
    timing = SCRAPE.timing(PARSED.blocks[0].config)
    assert info.start_to_close_timeout == timedelta(seconds=timing.start_to_close)
    assert info.schedule_to_close_timeout == timedelta(seconds=timing.budget)
    # The three DB activities around a block fit the allowance the validator budgets for it.
    assert 3 * DB_ACTIVITY_SECONDS <= BLOCK_OVERHEAD_SECONDS


async def test_a_short_run_budget_cuts_the_block_budget():
    h = Harness()
    await h.run(RUN_INPUT.model_copy(update={"time_budget_seconds": 50}))
    [info] = h.scrape_info
    assert info.schedule_to_close_timeout <= timedelta(seconds=50)


async def test_a_spent_run_budget_fails_the_block_before_it_starts():
    h = Harness()
    result = await h.run(RUN_INPUT.model_copy(update={"time_budget_seconds": 0}))
    assert isinstance(result, WorkflowFailureError)
    assert h.scraped == []
    assert h.calls[-1] == MirrorRunInput(
        run_id="r1",
        status="failed",
        error="fetch: the run's time budget is spent",
        failed_block_id="fetch",
    )


# --- E.9: the per-host slot around a Scrape block ----------------------------------------------

BUSY = HostSlot(domain="example.com", wait_ms=1000)


async def test_the_scrape_block_runs_inside_a_host_slot():
    h = Harness()
    assert await h.run() == LLM_PATH
    (acquired,) = h.acquired
    assert acquired.url == "https://example.com"
    # The activity holds the slot at most its budget (schedule_to_close).
    timing = SCRAPE.timing(PARSED.blocks[0].config)
    assert acquired.lease_ttl_ms == (timing.budget + HOST_LEASE_MARGIN.total_seconds()) * 1000
    assert h.released == [HostSlotRelease(domain="example.com", lease_id=acquired.lease_id)]


async def test_a_host_wait_comes_out_of_the_run_budget_not_past_it():
    # Budget for the scrape alone, less 30 s; the host makes it wait 60 s first.
    timing = SCRAPE.timing(PARSED.blocks[0].config)
    h = Harness(slots=[HostSlot(domain="example.com", wait_ms=60_000)])
    await h.run(RUN_INPUT.model_copy(update={"time_budget_seconds": timing.budget + 30}))
    [info] = h.scrape_info
    assert info.schedule_to_close_timeout <= timedelta(seconds=timing.budget - 25)


async def test_a_failed_scrape_block_still_frees_the_host():
    async def scrape(input):
        raise ApplicationError("blocked:akamai", type="ScrapeFailed", non_retryable=True)

    h = Harness(scrape=scrape)
    assert isinstance(await h.run(), WorkflowFailureError)
    assert len(h.released) == 1


async def test_a_cancel_while_waiting_for_the_host_cancels_without_scraping():
    async def cancel_while_waiting(handle):
        await h.asked.wait()
        await handle.cancel()

    h = Harness(slots=[BUSY] * 1000)
    result = await h.run(during=cancel_while_waiting)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, CancelledError)
    assert h.scraped == []
    assert h.released == []
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="fetch", status="failed", error="cancelled before it finished"
        ),
        MirrorRunInput(run_id="r1", status="cancelled"),
    ]


async def test_a_failed_block_fails_the_block_the_run_and_the_workflow():
    async def llm(input):
        raise ApplicationError("401 invalid key", type="LLMFailed", non_retryable=True)

    h = Harness(llm=llm)
    result = await h.run()

    assert isinstance(result, WorkflowFailureError)
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="extract", status="failed", error="LLMFailed: 401 invalid key"
        ),
        MirrorRunInput(
            run_id="r1",
            status="failed",
            error="extract: LLMFailed: 401 invalid key",
            failed_block_id="extract",
        ),
    ]


async def test_exhausted_retries_say_how_many_attempts_were_made():
    async def llm(input):
        raise ApplicationError("503 overloaded", type="LLMTransient")

    h = Harness(llm=llm)
    result = await h.run()

    assert isinstance(result, WorkflowFailureError)
    attempts = LLM_EXTRACT.timing(PARSED.blocks[1].config).maximum_attempts
    assert len(h.extracted) == attempts
    assert h.calls[-1].error.endswith(f"503 overloaded (gave up after {attempts} attempts)")


async def test_an_unexpected_exception_fails_the_run_instead_of_hanging():
    # artifact_ids is missing a block: a KeyError inside _execute.
    h = Harness()
    result = await h.run(
        RUN_INPUT.model_copy(update={"artifact_ids": {"extract": "a-extract"}}),
        timeout=timedelta(seconds=30),
    )

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, ApplicationError)
    assert result.cause.non_retryable
    assert h.scraped == []
    error = "internal error: KeyError: 'fetch'"
    assert h.calls == [
        MirrorBlockInput(run_id="r1", block_id="fetch", status="running"),
        MirrorBlockInput(run_id="r1", block_id="fetch", status="failed", error=error),
        MirrorRunInput(
            run_id="r1", status="failed", error=f"fetch: {error}", failed_block_id="fetch"
        ),
    ]


async def test_a_nondeterminism_error_is_left_to_the_workflow_task(monkeypatch):
    async def restricted(*args):
        raise RestrictedWorkflowAccessError("os.environ")

    # Unsandboxed, so the patched method is the one the worker runs.
    monkeypatch.setattr(PipelineWorkflow, "_execute", restricted)

    h = Harness()
    result = await h.run(timeout=timedelta(seconds=3), workflow_runner=UnsandboxedWorkflowRunner())

    assert isinstance(result, WorkflowFailureError)
    assert not isinstance(result.cause, ApplicationError)
    assert h.calls == [MirrorBlockInput(run_id="r1", block_id="fetch", status="running")]


async def test_a_cancel_seen_by_the_mirror_stops_at_the_next_block():
    h = Harness()
    h.running_reply["extract"] = "cancelled"
    result = await h.run()

    assert result is None
    assert h.extracted == []
    assert _block_calls(h.calls) == [
        ("fetch", "running"),
        ("fetch", "completed"),
        ("extract", "running"),
    ]
    assert h.calls[-1] == MirrorRunInput(run_id="r1", status="cancelled")


async def test_a_workflow_cancel_lets_the_running_block_finish_then_stops():
    started, release = asyncio.Event(), asyncio.Event()

    async def scrape(input):
        started.set()
        await release.wait()
        return _scrape_output()

    async def cancel_mid_block(handle):
        await started.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(scrape=scrape)
    result = await h.run(during=cancel_mid_block)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, CancelledError)
    # R3: the block was never aborted; its output stays and is charged.
    assert _block_calls(h.calls) == [("fetch", "running"), ("fetch", "completed")]
    assert any(isinstance(c, RecordStorageInput) for c in h.calls)
    assert h.extracted == []
    assert h.calls[-1] == MirrorRunInput(run_id="r1", status="cancelled")


async def test_crossing_the_storage_wall_completes_the_run_with_a_named_error():
    h = Harness(usage=StorageUsage(used=150, limit=100))
    result = await h.run()

    assert result == LLM_PATH
    assert len(h.extracted) == 1  # the run finishes: later blocks still run
    final = h.calls[-1]
    assert (final.status, final.result_path) == ("completed", LLM_PATH)
    assert final.error.startswith("storage_wall: block 'fetch' took storage to 150 of 100 bytes")


# --- Clean + Validate (C.8) --------------------------------------------------------------

RULES = [
    {"kind": "min_length", "chars": 100},
    {"kind": "contains", "text": "Add to cart"},
]


def _with_content_blocks(*blocks):
    """fetch -> <blocks> -> extract, each block consuming the one before it."""
    stored = json.loads(json.dumps(DEFINITION))
    fetch, extract = stored["blocks"]
    previous, middle = "fetch", []
    for block_id, block_type, config in blocks:
        middle.append(
            {
                "id": block_id,
                "type": block_type,
                "input": previous,
                "config_schema_version": 1,
                "config": config,
                "secrets": {},
            }
        )
        previous = block_id
    extract["input"] = previous
    stored["blocks"] = [fetch, *middle, extract]
    parsed, _ = parse_definition(stored)
    return RUN_INPUT.model_copy(
        update={
            "blocks": parsed.blocks,
            "time_budget_seconds": parsed.run_budget_seconds,
            "artifact_ids": {b.id: f"a-{b.id}" for b in parsed.blocks},
        }
    )


CLEAN_GUARD = _with_content_blocks(("clean", "clean", {}), ("guard", "validate", {"rules": RULES}))


async def test_clean_and_validate_run_on_the_content_queue_between_scrape_and_llm():
    h = Harness()
    result = await h.run(CLEAN_GUARD)

    assert result == LLM_PATH
    assert h.cleaned == [CleanInput(artifact_id="a-clean", input_path=SCRAPE_PATH)]
    assert h.validated == [
        ValidateInput(input_path=CLEAN_PATH, rules=[{**r, "negate": False} for r in RULES])
    ]
    # Validate is an effect block: it stores nothing and passes Clean's object on to the LLM.
    [llm] = h.extracted
    assert llm.raw_minio_path == CLEAN_PATH
    assert h.calls[3:] == [
        MirrorBlockInput(run_id="r1", block_id="clean", status="running", input_ref=SCRAPE_PATH),
        RecordStorageInput(
            run_id="r1", block_id="clean", objects=[StoredObject(path=CLEAN_PATH, size=7)]
        ),
        MirrorBlockInput(run_id="r1", block_id="clean", status="completed", output_ref=CLEAN_PATH),
        MirrorBlockInput(run_id="r1", block_id="guard", status="running", input_ref=CLEAN_PATH),
        MirrorBlockInput(run_id="r1", block_id="guard", status="completed", output_ref=CLEAN_PATH),
        MirrorBlockInput(run_id="r1", block_id="extract", status="running", input_ref=CLEAN_PATH),
        RecordStorageInput(
            run_id="r1", block_id="extract", objects=[StoredObject(path=LLM_PATH, size=3)]
        ),
        MirrorBlockInput(run_id="r1", block_id="extract", status="completed", output_ref=LLM_PATH),
        MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH),
    ]


async def test_validate_block_sends_the_rules_by_alias():
    # The worker's twin parses the catalog's dump; json_schema's field is "schema" on the wire.
    schema_rule = {"kind": "json_schema", "schema": {"type": "object"}}
    h = Harness()
    await h.run(_with_content_blocks(("guard", "validate", {"rules": [schema_rule]})))

    [validated] = h.validated
    assert validated.rules == [{**schema_rule, "negate": False}]


async def test_a_trailing_validate_leaves_the_result_on_the_last_content_block():
    stored = json.loads(json.dumps(DEFINITION))
    stored["blocks"][1] = {
        "id": "guard",
        "type": "validate",
        "input": "fetch",
        "config_schema_version": 1,
        "config": {"rules": RULES},
        "secrets": {},
    }
    parsed, _ = parse_definition(stored)
    h = Harness()
    result = await h.run(
        RUN_INPUT.model_copy(
            update={
                "blocks": parsed.blocks,
                "time_budget_seconds": parsed.run_budget_seconds,
                "artifact_ids": {"fetch": "a-fetch", "guard": "a-guard"},
            }
        )
    )

    assert result == SCRAPE_PATH
    assert h.calls[-1] == MirrorRunInput(run_id="r1", status="completed", result_path=SCRAPE_PATH)


async def test_a_failed_rule_stops_the_run_before_the_llm_is_billed():
    async def validate(input):
        raise ApplicationError(
            "rules[1] (contains): 'Add to cart' not found",
            type="ValidationFailed",
            non_retryable=True,
        )

    h = Harness(validate=validate)
    result = await h.run(CLEAN_GUARD)

    assert isinstance(result, WorkflowFailureError)
    assert h.extracted == []
    assert len(h.validated) == 1  # non-retryable: one attempt
    message = "ValidationFailed: rules[1] (contains): 'Add to cart' not found"
    assert h.calls[-2:] == [
        MirrorBlockInput(run_id="r1", block_id="guard", status="failed", error=message),
        MirrorRunInput(
            run_id="r1", status="failed", error=f"guard: {message}", failed_block_id="guard"
        ),
    ]


# --- J.6: a stop-mode Validate (patch "validate-stop") ------------------------------------------

PRICE_RULE = {"kind": "compare", "path": "/price", "op": "lt", "value": {"$input": "max_price"}}


def _stop_guard(on_stop_outputs="keep", rules=None, max_price=100):
    """fetch -> clean -> guard (stop mode) -> extract; guard reads max_price."""
    run_input = _with_content_blocks(
        ("clean", "clean", {}),
        (
            "guard",
            "validate",
            {
                "rules": rules or [PRICE_RULE],
                "when_unmet": "stop",
                "on_stop_outputs": on_stop_outputs,
            },
        ),
    )
    return run_input.model_copy(update={"inputs": {**run_input.inputs, "max_price": max_price}})


NOT_MET = ValidateVerdict(
    outcome="not_met",
    rule_index=0,
    rule={"kind": "compare", "path": "/price", "op": "lt", "value": 100, "negate": False},
    found=120,
    expected=100,
)
RECORDED_VERDICT = {
    "rule_index": 0,
    "rule": {**PRICE_RULE, "negate": False},
    "found": 120,
    "expected": 100,
}


async def _not_met(input):
    return NOT_MET


async def _met(input):
    return ValidateVerdict(outcome="met")


async def test_a_stop_with_keep_ends_the_run_completed_at_the_validate():
    h = Harness(validate=_not_met)
    result = await h.run(_stop_guard("keep"))

    assert result == CLEAN_PATH
    assert h.extracted == []  # stopped before the LLM is billed
    assert len(h.validated) == 1
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1",
            block_id="guard",
            status="completed",
            output_ref=CLEAN_PATH,
            verdict=RECORDED_VERDICT,
        ),
        MirrorRunInput(
            run_id="r1",
            status="completed",
            result_path=CLEAN_PATH,
            stopped_at_block_id="guard",
            stop_reason="rule_not_met",
        ),
    ]
    assert not any(isinstance(c, RunRef) for c in h.calls)


async def test_a_stop_records_the_fingerprint_and_match_when_the_worker_sends_them():
    async def unchanged(input):
        return NOT_MET.model_copy(update={"fingerprint": "f" * 64, "matched": "baseline"})

    h = Harness(validate=unchanged)
    await h.run(_stop_guard("keep"))

    [guard] = [c for c in h.calls if getattr(c, "verdict", None)]
    assert guard.verdict == RECORDED_VERDICT | {"fingerprint": "f" * 64, "matched": "baseline"}


async def test_a_stop_with_delete_releases_the_runs_outputs_then_ends_without_a_result():
    h = Harness(validate=_not_met)
    result = await h.run(_stop_guard("delete"))

    assert result is None
    assert h.calls[-3:] == [
        MirrorBlockInput(
            run_id="r1",
            block_id="guard",
            status="completed",
            output_ref=CLEAN_PATH,
            verdict=RECORDED_VERDICT,
        ),
        RunRef(run_id="r1"),
        MirrorRunInput(
            run_id="r1",
            status="completed",
            result_path=None,
            stopped_at_block_id="guard",
            stop_reason="rule_not_met",
        ),
    ]
    # Its own full budget, on the workflow queue, whatever the run has left.
    [info] = h.release_info
    assert info.task_queue == WORKFLOW_QUEUE
    assert info.schedule_to_close_timeout == timedelta(seconds=RELEASE_OUTPUTS_TIMING.budget)


async def test_a_delete_that_gives_up_still_stops_and_says_what_it_kept():
    async def left(input):
        raise ApplicationError(
            "2 stored objects could not be deleted",
            [CLEAN_PATH, SCRAPE_PATH],
            type=OUTPUTS_LEFT_ERROR,
        )

    h = Harness(validate=_not_met, release_outputs=left)
    result = await h.run(_stop_guard("delete"))

    assert result == CLEAN_PATH  # still on disk, still retrievable
    assert len(h.release_info) == RELEASE_OUTPUTS_TIMING.maximum_attempts
    assert h.calls[-1] == MirrorRunInput(
        run_id="r1",
        status="completed",
        result_path=CLEAN_PATH,
        error="stopped; 2 stored objects could not be deleted and are kept and charged",
        stopped_at_block_id="guard",
        stop_reason="rule_not_met",
    )


async def test_a_delete_that_removed_the_result_but_not_the_rest_points_at_nothing():
    async def left(input):
        raise ApplicationError(
            "1 stored objects could not be deleted", [SCRAPE_PATH], type=OUTPUTS_LEFT_ERROR
        )

    h = Harness(validate=_not_met, release_outputs=left)
    assert await h.run(_stop_guard("delete")) is None
    assert h.calls[-1].result_path is None
    assert h.calls[-1].error.startswith("stopped; 1 stored objects could not be deleted")


async def test_a_met_stop_mode_validate_passes_through_to_the_next_block():
    h = Harness(validate=_met)
    result = await h.run(_stop_guard("keep"))

    assert result == LLM_PATH
    assert len(h.extracted) == 1
    [validated] = h.validated
    assert validated.when_unmet == "stop"
    guard = [c for c in h.calls if isinstance(c, MirrorBlockInput) and c.block_id == "guard"]
    assert guard[-1] == MirrorBlockInput(
        run_id="r1", block_id="guard", status="completed", output_ref=CLEAN_PATH
    )
    assert h.calls[-1] == MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH)


async def test_bound_rule_values_are_the_run_inputs_by_the_time_the_worker_sees_them():
    contains = {"kind": "contains", "text": {"$input": "phrase"}, "negate": True}
    run_input = _stop_guard("keep", rules=[PRICE_RULE, contains], max_price=99.5)
    run_input = run_input.model_copy(
        update={"inputs": {**run_input.inputs, "phrase": "Out of stock"}}
    )
    h = Harness(validate=_met)
    await h.run(run_input)

    [validated] = h.validated
    assert validated.rules == [
        {**PRICE_RULE, "value": 99.5, "negate": False},
        {"kind": "contains", "text": "Out of stock", "negate": True},
    ]


async def test_a_fail_mode_validate_says_so_and_ends_the_run_as_before():
    h = Harness()
    await h.run(CLEAN_GUARD)
    [validated] = h.validated
    assert validated.when_unmet == "fail"
    assert not any(isinstance(c, RunRef) for c in h.calls)


async def test_content_block_timeouts_come_from_the_catalog():
    h = Harness()
    await h.run(CLEAN_GUARD)

    clean_info, validate_info = h.content_info
    for info, block_type, block in (
        (clean_info, CLEAN, CLEAN_GUARD.blocks[1]),
        (validate_info, VALIDATE, CLEAN_GUARD.blocks[2]),
    ):
        timing = block_type.timing(block.config)
        assert info.task_queue == CONTENT_QUEUE
        assert info.start_to_close_timeout == timedelta(seconds=timing.start_to_close)
        assert info.schedule_to_close_timeout == timedelta(seconds=timing.budget)


# ---------------------------------------------------------------------------
# The DB activities, against the dev DB
# ---------------------------------------------------------------------------


async def _run_rows(user_id, blocks=(("fetch", "scrape", "pending"),), **run_fields) -> PipelineRun:
    async with AsyncSessionLocal() as db:
        pipeline = Pipeline(user_id=user_id, name=f"p-{uuid.uuid4().hex[:6]}")
        db.add(pipeline)
        await db.flush()
        version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition=DEFINITION)
        db.add(version)
        await db.flush()
        run_id = uuid.uuid4()
        run = PipelineRun(
            id=run_id,
            pipeline_id=pipeline.id,
            pipeline_version_id=version.id,
            user_id=user_id,
            status=run_fields.pop("status", "running"),
            workflow_id=f"pipeline-run-{run_id}",
            **run_fields,
        )
        db.add(run)
        db.add_all(
            PipelineRunBlock(
                pipeline_run_id=run_id, block_id=block_id, position=i, type=type, status=status
            )
            for i, (block_id, type, status) in enumerate(blocks)
        )
        await db.commit()
    return run


async def _statuses(run_id) -> tuple[str, dict[str, str]]:
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, run_id)
        blocks = dict(
            (
                await db.execute(
                    select(PipelineRunBlock.block_id, PipelineRunBlock.status).where(
                        PipelineRunBlock.pipeline_run_id == run_id
                    )
                )
            ).all()
        )
    return run.status, blocks


async def _block(run_id, block_id="fetch") -> PipelineRunBlock:
    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(PipelineRunBlock).where(
                PipelineRunBlock.pipeline_run_id == run_id, PipelineRunBlock.block_id == block_id
            )
        )


async def test_mirror_block_status_writes_the_row(db_user):
    run = await _run_rows(db_user.id)
    rid = str(run.id)

    status = await mirror_block_status(
        MirrorBlockInput(run_id=rid, block_id="fetch", status="running")
    )
    assert status == "running"
    block = await _block(run.id)
    assert (block.status, block.started_at is not None, block.finished_at) == (
        "running",
        True,
        None,
    )

    await mirror_block_status(
        MirrorBlockInput(run_id=rid, block_id="fetch", status="completed", output_ref=SCRAPE_PATH)
    )
    block = await _block(run.id)
    assert (block.status, block.output_ref, block.finished_at is not None) == (
        "completed",
        SCRAPE_PATH,
        True,
    )


async def test_mirror_run_status_writes_the_row(db_user):
    run = await _run_rows(db_user.id)
    status = await mirror_run_status(
        MirrorRunInput(run_id=str(run.id), status="completed", result_path=LLM_PATH)
    )
    assert status == "completed"
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.status, row.result_path, row.error, row.finished_at is not None) == (
        "completed",
        LLM_PATH,
        None,
        True,
    )


async def test_a_failed_run_skips_the_blocks_that_never_started(db_user):
    run = await _run_rows(
        db_user.id,
        (
            ("fetch", "scrape", "completed"),
            ("extract", "llm_extract", "failed"),
            ("notify", "webhook", "pending"),
        ),
    )
    await mirror_run_status(MirrorRunInput(run_id=str(run.id), status="failed", error="x"))
    assert await _statuses(run.id) == (
        "failed",
        {"fetch": "completed", "extract": "failed", "notify": "skipped"},
    )


async def test_a_mirror_never_moves_a_cancelled_run(db_user):
    run = await _run_rows(
        db_user.id,
        (("fetch", "scrape", "completed"), ("extract", "llm_extract", "pending")),
        status="cancelled",
    )
    status = await mirror_run_status(
        MirrorRunInput(run_id=str(run.id), status="completed", result_path=LLM_PATH)
    )
    assert status == "cancelled"
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.status, row.result_path) == ("cancelled", None)
    assert await _statuses(run.id) == ("cancelled", {"fetch": "completed", "extract": "skipped"})


async def test_a_cancel_request_stops_the_next_block_from_starting(db_user):
    run = await _run_rows(
        db_user.id,
        (("fetch", "scrape", "completed"), ("extract", "llm_extract", "pending")),
        cancel_requested_at=datetime.now(UTC),
    )
    status = await mirror_block_status(
        MirrorBlockInput(run_id=str(run.id), block_id="extract", status="running")
    )
    assert status == "cancelled"
    assert (await _block(run.id, "extract")).status == "pending"
    assert await check_cancelled(RunRef(run_id=str(run.id))) is True

    # The workflow's cancel lands next; the row the API only flagged becomes cancelled.
    status = await mirror_run_status(MirrorRunInput(run_id=str(run.id), status="cancelled"))
    assert status == "cancelled"
    assert await _statuses(run.id) == ("cancelled", {"fetch": "completed", "extract": "skipped"})


async def test_a_block_outcome_is_still_recorded_on_a_cancelled_run(db_user):
    run = await _run_rows(db_user.id, (("fetch", "scrape", "running"),), status="cancelled")
    await mirror_block_status(
        MirrorBlockInput(run_id=str(run.id), block_id="fetch", status="completed", output_ref="o")
    )
    assert (await _block(run.id)).status == "completed"


async def test_check_cancelled_is_false_for_a_running_run(db_user):
    run = await _run_rows(db_user.id)
    assert await check_cancelled(RunRef(run_id=str(run.id))) is False


async def test_every_mirror_write_notifies_identifiers_and_status_only(db_user):
    run = await _run_rows(db_user.id)
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(PIPELINE_STATUS_CHANNEL, lambda *a: received.put_nowait(a[3]))
    try:
        # An error far past pg_notify's 8000-byte cap: it must not reach the payload.
        await mirror_block_status(
            MirrorBlockInput(
                run_id=str(run.id), block_id="fetch", status="failed", error="x" * 9000
            )
        )
        await mirror_run_status(MirrorRunInput(run_id=str(run.id), status="failed", error="y"))
        payloads = [json.loads(await asyncio.wait_for(received.get(), 5)) for _ in range(2)]
    finally:
        await conn.close()
    base = {
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "cancel_requested": False,
        "stopped_at_block_id": None,
        "rolling_back": False,
        "change_status": None,
        "block_status_reason": None,
        "block_undo_status": None,
    }
    assert payloads == [
        {**base, "status": "running", "block_id": "fetch", "block_status": "failed"},
        {**base, "status": "failed", "block_id": None, "block_status": None},
    ]


async def _reasons(run_id) -> dict[str, tuple[str, str | None, str | None]]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    PipelineRunBlock.block_id,
                    PipelineRunBlock.status,
                    PipelineRunBlock.status_reason,
                    PipelineRunBlock.reason_block_id,
                ).where(PipelineRunBlock.pipeline_run_id == run_id)
            )
        ).all()
    return {b: (s, r, rb) for b, s, r, rb in rows}


STOP_BLOCKS = (
    ("fetch", "scrape", "completed"),
    ("check", "validate", "completed"),
    ("extract", "llm_extract", "pending"),
    ("notify", "webhook", "pending"),
)


async def test_a_stopped_run_records_where_and_why_and_skips_the_rest_as_stopped(db_user):
    run = await _run_rows(db_user.id, STOP_BLOCKS)
    arg = MirrorRunInput(
        run_id=str(run.id),
        status="completed",
        result_path=SCRAPE_PATH,
        stopped_at_block_id="check",
        stop_reason="rule_not_met",
    )
    assert await mirror_run_status(arg) == "completed"
    assert await mirror_run_status(arg) == "completed"  # a retried activity

    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.status, row.result_path, row.stopped_at_block_id, row.stop_reason) == (
        "completed",
        SCRAPE_PATH,
        "check",
        "rule_not_met",
    )
    assert row.failed_block_id is None
    assert await _reasons(run.id) == {
        "fetch": ("completed", None, None),
        "check": ("completed", None, None),
        "extract": ("skipped", "stopped", "check"),
        "notify": ("skipped", "stopped", "check"),
    }


async def test_a_failed_run_records_its_block_and_skips_the_rest_as_upstream_failed(db_user):
    run = await _run_rows(
        db_user.id,
        (
            ("fetch", "scrape", "completed"),
            ("extract", "llm_extract", "failed"),
            ("notify", "webhook", "pending"),
        ),
    )
    await mirror_run_status(
        MirrorRunInput(
            run_id=str(run.id), status="failed", error="extract: x", failed_block_id="extract"
        )
    )
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.status, row.failed_block_id, row.stopped_at_block_id) == (
        "failed",
        "extract",
        None,
    )
    assert (await _reasons(run.id))["notify"] == ("skipped", "upstream_failed", "extract")


async def test_a_cancelled_run_skips_the_rest_as_cancelled_whatever_the_workflow_reports(db_user):
    # The workflow's mirror lands after the run is cancelled: the row's ending decides the reason.
    run = await _run_rows(
        db_user.id, (("fetch", "scrape", "completed"), ("extract", "llm_extract", "pending"))
    )
    await mirror_run_status(MirrorRunInput(run_id=str(run.id), status="cancelled"))
    await mirror_run_status(
        MirrorRunInput(
            run_id=str(run.id), status="failed", error="extract: x", failed_block_id="extract"
        )
    )
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.status, row.failed_block_id) == ("cancelled", None)
    assert (await _reasons(run.id))["extract"] == ("skipped", "cancelled", None)


async def test_a_stop_reported_on_a_cancelled_run_is_not_recorded(db_user):
    run = await _run_rows(db_user.id, STOP_BLOCKS, status="cancelled")
    status = await mirror_run_status(
        MirrorRunInput(
            run_id=str(run.id),
            status="completed",
            stopped_at_block_id="check",
            stop_reason="rule_not_met",
        )
    )
    assert status == "cancelled"
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.stopped_at_block_id, row.stop_reason) == (None, None)
    assert (await _reasons(run.id))["extract"] == ("skipped", "cancelled", None)


async def test_a_run_that_completes_normally_skips_nothing(db_user):
    run = await _run_rows(
        db_user.id, (("fetch", "scrape", "completed"), ("notify", "webhook", "pending"))
    )
    await mirror_run_status(MirrorRunInput(run_id=str(run.id), status="completed"))
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.stopped_at_block_id, row.stop_reason, row.failed_block_id) == (None, None, None)
    assert (await _reasons(run.id))["notify"] == ("pending", None, None)


def test_a_stop_names_both_its_block_and_its_reason():
    with pytest.raises(ValueError, match="go together"):
        MirrorRunInput(run_id="r", status="completed", stopped_at_block_id="check")
    with pytest.raises(ValueError, match="go together"):
        MirrorRunInput(run_id="r", status="completed", stop_reason="rule_not_met")


async def test_a_completed_block_records_its_verdict(db_user):
    run = await _run_rows(db_user.id, (("check", "validate", "running"),))
    verdict = {
        "rule_index": 0,
        "rule": {"kind": "compare", "path": "/price", "op": "lt", "value": 100, "negate": False},
        "found": 120,
        "expected": 100,
    }
    await mirror_block_status(
        MirrorBlockInput(run_id=str(run.id), block_id="check", status="completed", verdict=verdict)
    )
    block = await _block(run.id, "check")
    assert (block.status, block.verdict) == ("completed", verdict)


async def test_a_block_mirrored_skipped_records_its_reason_and_notifies_it(db_user):
    run = await _run_rows(
        db_user.id, (("fetch", "scrape", "completed"), ("notify", "webhook", "pending"))
    )
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(PIPELINE_STATUS_CHANNEL, lambda *a: received.put_nowait(a[3]))
    try:
        await mirror_block_status(
            MirrorBlockInput(
                run_id=str(run.id), block_id="notify", status="skipped", status_reason="alert_only"
            )
        )
        payload = json.loads(await asyncio.wait_for(received.get(), 5))
    finally:
        await conn.close()
    block = await _block(run.id, "notify")
    assert (block.status, block.status_reason, block.started_at, block.finished_at) == (
        "skipped",
        "alert_only",
        None,
        None,
    )
    assert (payload["block_id"], payload["block_status"], payload["block_status_reason"]) == (
        "notify",
        "skipped",
        "alert_only",
    )


async def test_a_stopped_run_notifies_where_it_stopped(db_user):
    run = await _run_rows(db_user.id, STOP_BLOCKS)
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(PIPELINE_STATUS_CHANNEL, lambda *a: received.put_nowait(a[3]))
    try:
        await mirror_run_status(
            MirrorRunInput(
                run_id=str(run.id),
                status="completed",
                stopped_at_block_id="check",
                stop_reason="rule_not_met",
            )
        )
        payload = json.loads(await asyncio.wait_for(received.get(), 5))
    finally:
        await conn.close()
    assert (payload["status"], payload["stopped_at_block_id"], payload["rolling_back"]) == (
        "completed",
        "check",
        False,
    )


def test_mirror_inputs_recorded_before_j5_still_decode():
    # Every recorded history holds these shapes; the new fields must default.
    converter = pydantic_data_converter.payload_converter
    old_run = converter.to_payloads([{"run_id": "r", "status": "failed", "error": "x: y"}])
    old_block = converter.to_payloads(
        [{"run_id": "r", "block_id": "x", "status": "completed", "output_ref": "o"}]
    )
    [run] = converter.from_payloads(old_run, [MirrorRunInput])
    [block] = converter.from_payloads(old_block, [MirrorBlockInput])
    assert (run.failed_block_id, run.stopped_at_block_id, run.stop_reason) == (None, None, None)
    assert (block.verdict, block.status_reason, block.reason_block_id) == (None, None, None)


class _FakeMinio:
    """remove_object succeeds unless the key is in ``fail``."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.removed: list[str] = []

    async def remove_object(self, bucket, key):
        if f"{bucket}/{key}" in self.fail:
            raise RuntimeError("minio down")
        self.removed.append(f"{bucket}/{key}")


async def _stored_run(user_id):
    """A stopped-at-guard run: fetch and clean each hold an object; guard passed clean's on."""
    run = await _run_rows(
        user_id,
        (
            ("fetch", "scrape", "completed"),
            ("clean", "clean", "completed"),
            ("guard", "validate", "completed"),
            ("extract", "llm_extract", "pending"),
        ),
    )
    tag = uuid.uuid4().hex[:8]
    keys = {
        "fetch": f"scrapeflow-results/history/{tag}-fetch/scrape.html",
        "clean": f"scrapeflow-results/history/{tag}-clean/clean.md",
    }
    sizes = {"fetch": 4000, "clean": 300}
    for block_id, key in keys.items():
        await record_storage(
            RecordStorageInput(
                run_id=str(run.id),
                block_id=block_id,
                objects=[StoredObject(path=key, size=sizes[block_id])],
            )
        )
    async with AsyncSessionLocal() as db:
        for block_id, ref in (("fetch", keys["fetch"]), ("clean", keys["clean"])):
            await db.execute(
                update(PipelineRunBlock)
                .where(
                    PipelineRunBlock.pipeline_run_id == run.id,
                    PipelineRunBlock.block_id == block_id,
                )
                .values(output_ref=ref)
            )
        await db.execute(
            update(PipelineRunBlock)
            .where(PipelineRunBlock.pipeline_run_id == run.id, PipelineRunBlock.block_id == "guard")
            .values(output_ref=keys["clean"])
        )
        await db.commit()
    return run, keys


async def _used(user_id) -> int:
    async with AsyncSessionLocal() as db:
        quota = await db.get(UserQuota, user_id)
        return 0 if quota is None else quota.storage_bytes_used


async def _held(run_id) -> list[str]:
    async with AsyncSessionLocal() as db:
        return sorted(
            (
                await db.scalars(
                    select(StorageObject.object_key)
                    .join(
                        PipelineRunBlock,
                        PipelineRunBlock.id == StorageObject.pipeline_run_block_id,
                    )
                    .where(PipelineRunBlock.pipeline_run_id == run_id)
                )
            ).all()
        )


async def test_release_run_outputs_frees_exactly_the_runs_bytes_and_marks_blocks_collected(
    db_user, monkeypatch
):
    run, keys = await _stored_run(db_user.id)
    before = await _used(db_user.id)
    fake = _FakeMinio()

    async def get_fake():
        return fake

    monkeypatch.setattr("app.workflows.activities.db.get_minio", get_fake)

    await release_run_outputs(RunRef(run_id=str(run.id)))
    await release_run_outputs(RunRef(run_id=str(run.id)))  # a retry after a lost reply

    assert await _used(db_user.id) == before - 4300
    assert sorted(fake.removed) == sorted(keys.values())
    assert await _held(run.id) == []
    collected = {b: (await _block(run.id, b)).collected_at is not None for b in keys | {"guard": 0}}
    assert collected == {"fetch": True, "clean": True, "guard": True}
    # A block that never ran has nothing to collect.
    assert (await _block(run.id, "extract")).collected_at is None


async def test_release_run_outputs_commits_what_it_freed_and_raises_naming_the_rest(
    db_user, monkeypatch
):
    run, keys = await _stored_run(db_user.id)
    before = await _used(db_user.id)
    fake = _FakeMinio(fail={keys["fetch"]})

    async def get_fake():
        return fake

    monkeypatch.setattr("app.workflows.activities.db.get_minio", get_fake)

    with pytest.raises(ApplicationError) as exc_info:
        await release_run_outputs(RunRef(run_id=str(run.id)))
    assert exc_info.value.type == OUTPUTS_LEFT_ERROR
    assert not exc_info.value.non_retryable
    assert list(exc_info.value.details) == [[keys["fetch"]]]
    assert await _used(db_user.id) == before - 300
    assert await _held(run.id) == [keys["fetch"]]
    assert (await _block(run.id, "fetch")).collected_at is None
    assert (await _block(run.id, "clean")).collected_at is not None

    # The next attempt touches only what is left.
    fake.fail.clear()
    fake.removed.clear()
    await release_run_outputs(RunRef(run_id=str(run.id)))
    assert fake.removed == [keys["fetch"]]
    assert await _used(db_user.id) == before - 4300
    assert (await _block(run.id, "fetch")).collected_at is not None


async def test_record_storage_charges_once_and_reports_usage(db_user):
    run = await _run_rows(db_user.id)
    key = f"scrapeflow-results/history/{uuid.uuid4()}/scrape.html"
    arg = RecordStorageInput(
        run_id=str(run.id), block_id="fetch", objects=[StoredObject(path=key, size=1234)]
    )

    first = await record_storage(arg)
    again = await record_storage(arg)  # a retried activity

    assert first == again == StorageUsage(used=1234, limit=settings.default_quota_storage_bytes)
    block = await _block(run.id)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.scalars(select(StorageObject).where(StorageObject.object_key == key))
        ).all()
        quota = await db.get(UserQuota, db_user.id)
    assert [(r.pipeline_run_block_id, r.bytes, r.user_id) for r in rows] == [
        (block.id, 1234, db_user.id)
    ]
    assert quota.storage_bytes_used == 1234


async def test_db_activities_on_a_missing_block_fail_without_retry(db_user):
    run = await _run_rows(db_user.id)
    with pytest.raises(ApplicationError) as exc_info:
        await mirror_block_status(
            MirrorBlockInput(run_id=str(run.id), block_id="nope", status="completed")
        )
    assert exc_info.value.non_retryable
    with pytest.raises(ApplicationError) as exc_info:
        await record_storage(RecordStorageInput(run_id=str(run.id), block_id="nope", objects=[]))
    assert exc_info.value.non_retryable


# ---------------------------------------------------------------------------
# K.1 — a run deleted while its workflow is still going (ADR-016 §11)
# ---------------------------------------------------------------------------


async def test_every_mirror_reads_a_deleted_run_as_cancelled():
    gone = str(uuid.uuid4())
    for status in ("running", "waiting", "completed", "failed", "skipped"):
        reply = await mirror_block_status(
            MirrorBlockInput(run_id=gone, block_id="fetch", status=status)
        )
        assert reply == "cancelled", status
    for status in ("completed", "failed", "cancelled"):
        assert await mirror_run_status(MirrorRunInput(run_id=gone, status=status)) == "cancelled"
    assert await check_cancelled(RunRef(run_id=gone)) is True


@pytest.fixture
def fake_minio(monkeypatch):
    fake = _FakeMinio()

    async def get_fake():
        return fake

    monkeypatch.setattr("app.workflows.activities.db.get_minio", get_fake)
    return fake


async def test_record_storage_on_a_deleted_run_removes_the_objects_and_charges_nothing(
    db_user, fake_minio
):
    key = f"scrapeflow-results/history/{uuid.uuid4()}/scrape.html"
    before = await _used(db_user.id)

    with pytest.raises(ApplicationError) as exc_info:
        await record_storage(
            RecordStorageInput(
                run_id=str(uuid.uuid4()),
                block_id="fetch",
                objects=[StoredObject(path=key, size=500)],
            )
        )

    assert (exc_info.value.type, exc_info.value.non_retryable) == (RUN_CANCELLED_ERROR, True)
    assert fake_minio.removed == [key]
    assert await _used(db_user.id) == before
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(StorageObject).where(StorageObject.object_key == key)) is None


async def test_an_object_of_a_deleted_run_that_cannot_be_removed_is_retried(db_user, fake_minio):
    key = f"scrapeflow-results/history/{uuid.uuid4()}/scrape.html"
    fake_minio.fail.add(key)
    arg = RecordStorageInput(
        run_id=str(uuid.uuid4()), block_id="fetch", objects=[StoredObject(path=key, size=500)]
    )

    with pytest.raises(ApplicationError) as exc_info:
        await record_storage(arg)
    assert not exc_info.value.non_retryable
    assert exc_info.value.type != RUN_CANCELLED_ERROR

    fake_minio.fail.clear()
    with pytest.raises(ApplicationError) as exc_info:
        await record_storage(arg)
    assert exc_info.value.type == RUN_CANCELLED_ERROR
    assert fake_minio.removed == [key]


async def test_a_record_waits_for_a_delete_holding_the_run_and_then_drops_its_object(
    db_user, fake_minio
):
    """The interleave: the delete enumerated the ledger; a record arriving now must not leave
    an uncharged object behind once the run's rows cascade away."""
    run = await _run_rows(db_user.id)
    key = f"scrapeflow-results/history/{uuid.uuid4()}/scrape.html"
    before = await _used(db_user.id)

    async with AsyncSessionLocal() as deleter:
        await deleter.execute(
            select(PipelineRun.id).where(PipelineRun.id == run.id).with_for_update()
        )
        record = asyncio.create_task(
            record_storage(
                RecordStorageInput(
                    run_id=str(run.id),
                    block_id="fetch",
                    objects=[StoredObject(path=key, size=700)],
                )
            )
        )
        await asyncio.sleep(0.5)
        assert not record.done()
        await deleter.execute(delete(PipelineRun).where(PipelineRun.id == run.id))
        await deleter.commit()

    with pytest.raises(ApplicationError) as exc_info:
        await record
    assert exc_info.value.type == RUN_CANCELLED_ERROR
    assert fake_minio.removed == [key]
    assert await _used(db_user.id) == before
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(StorageObject).where(StorageObject.object_key == key)) is None


async def test_a_run_deleted_mid_block_ends_the_workflow_cancelled_not_failed(db_user, fake_minio):
    run = await _run_rows(
        db_user.id, (("fetch", "scrape", "pending"), ("extract", "llm_extract", "pending"))
    )
    key = f"scrapeflow-results/history/{uuid.uuid4()}/scrape.html"
    started, release = asyncio.Event(), asyncio.Event()

    @activity.defn(name=SCRAPE_ACTIVITY)
    async def scrape(input: ScrapeInput) -> ScrapeOutput:
        started.set()
        await release.wait()
        return ScrapeOutput.model_validate(
            {"result": {"path": key, "size": 10}, "content_hash": "00c0ffee12345678"}
        )

    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm(input: LLMInput) -> LLMOutput:
        raise AssertionError("a deleted run must not start its next block")

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=WORKFLOW_QUEUE,
                workflows=[PipelineWorkflow],
                activities=[
                    mirror_block_status,
                    mirror_run_status,
                    record_storage,
                    check_cancelled,
                ],
            ),
            Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
            Worker(env.client, task_queue=LLM_QUEUE, activities=[llm]),
        ):
            handle = await env.client.start_workflow(
                PipelineWorkflow.run,
                RUN_INPUT.model_copy(update={"run_id": str(run.id)}),
                id=run.workflow_id,
                task_queue=WORKFLOW_QUEUE,
            )
            await started.wait()
            async with AsyncSessionLocal() as db:
                await db.execute(delete(PipelineRun).where(PipelineRun.id == run.id))
                await db.commit()
            release.set()
            assert await handle.result() is None

    assert fake_minio.removed == [key]
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(StorageObject).where(StorageObject.object_key == key)) is None


# ---------------------------------------------------------------------------
# The Webhook block (C.9) — the R6 recipe, scrape -> llm -> webhook
# ---------------------------------------------------------------------------

HOOK_URL = "https://hooks.example.com/in"


def _with_webhook(
    secret: str | None = "secret-ciphertext", events: list[str] | None = None
) -> PipelineRunInput:
    stored = json.loads(json.dumps(DEFINITION))
    config = {"url": HOOK_URL, "secret": None}
    if events is not None:
        config["events"] = events
    stored["blocks"].append(
        {
            "id": "notify",
            "type": "webhook",
            "input": "extract",
            "config_schema_version": 1,
            "config": config,
            "secrets": {},
        }
    )
    parsed, _ = parse_definition(stored)
    return RUN_INPUT.model_copy(
        update={
            "blocks": parsed.blocks,
            "time_budget_seconds": parsed.run_budget_seconds,
            "artifact_ids": {b.id: f"a-{b.id}" for b in parsed.blocks},
            "webhook_secrets": {"notify": secret} if secret else {},
        }
    )


R6 = _with_webhook()


ALERT_ONLY = _with_webhook(events=["pipeline.failed"])


async def test_an_alert_only_webhook_is_skipped_on_a_succeeding_run_without_an_activity():
    h = Harness()
    result = await h.run(ALERT_ONLY)

    assert result == LLM_PATH
    assert h.delivered == []
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="notify", status="skipped", status_reason="alert_only"
        ),
        MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH),
    ]


async def test_a_webhook_sending_both_events_still_delivers_the_result():
    h = Harness()
    await h.run(_with_webhook(events=["pipeline.failed", "pipeline.result"]))
    assert len(h.delivered) == 1


async def test_a_failure_before_an_alert_only_webhook_fails_the_run_as_any_failure_does():
    async def llm(input):
        raise ApplicationError("401 invalid key", type="LLMFailed", non_retryable=True)

    h = Harness(llm=llm)
    result = await h.run(ALERT_ONLY)

    assert isinstance(result, WorkflowFailureError)
    assert h.delivered == []
    assert h.calls[-1] == MirrorRunInput(
        run_id="r1",
        status="failed",
        error="extract: LLMFailed: 401 invalid key",
        failed_block_id="extract",
    )


async def test_webhook_delivers_the_last_content_blocks_object_and_passes_it_on():
    h = Harness()
    result = await h.run(R6)

    # An effect block: the run's result stays on the extraction.
    assert result == LLM_PATH
    [delivered] = h.delivered
    assert delivered.model_dump(exclude={"timestamp"}) == {
        "run_id": "r1",
        "pipeline_id": "p1",
        "block_id": "notify",
        "url": HOOK_URL,
        "result_path": LLM_PATH,
        "encrypted_secret": "secret-ciphertext",
        "maximum_attempts": 5,
        # A hand-run: no monitor's fields.
        "monitor_id": None,
        "change": None,
        "destination_id": None,
        # Patch "sinks": no sink before it.
        "deliveries": [],
    }
    assert h.calls[-3:] == [
        MirrorBlockInput(run_id="r1", block_id="notify", status="running", input_ref=LLM_PATH),
        MirrorBlockInput(run_id="r1", block_id="notify", status="completed", output_ref=LLM_PATH),
        MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH),
    ]


async def test_webhook_timeouts_are_the_four_step_ladder():
    h = Harness()
    await h.run(R6)

    [info] = h.webhook_info
    timing = WEBHOOK.timing(R6.blocks[2].config)
    assert info.task_queue == WORKFLOW_QUEUE
    assert info.start_to_close_timeout == timedelta(seconds=20)
    # schedule_to_close = the horizon: >= 2.6 h, and inside the run budget.
    assert info.schedule_to_close_timeout == timedelta(seconds=timing.budget)
    assert timing.budget >= 2.6 * 3600
    assert R6.time_budget_seconds > timing.budget


async def test_webhook_retries_five_times_on_the_15b_schedule():
    async def down(input):
        raise ApplicationError("HTTP 503", type="WebhookFailed")

    h = Harness(webhook=down)
    result = await h.run(R6)

    assert isinstance(result, WorkflowFailureError)
    assert [d.maximum_attempts for d in h.delivered] == [5] * 5
    started = [i.started_time for i in h.webhook_info]
    waits = [round((b - a).total_seconds()) for a, b in itertools.pairwise(started)]
    # 30 s x10, capped at 7200 s (ADR-009 15b: attempts 4-5 drift from today's list).
    assert waits == [30, 300, 3000, 7200]
    assert h.calls[-1].error == "notify: WebhookFailed: HTTP 503 (gave up after 5 attempts)"


async def test_every_webhook_attempt_signs_the_same_payload():
    async def flaky(input):
        if activity.info().attempt < 3:
            raise ApplicationError("HTTP 503", type="WebhookFailed")

    h = Harness(webhook=flaky)
    result = await h.run(R6)

    assert result == LLM_PATH
    assert len(h.delivered) == 3
    assert len({d.timestamp for d in h.delivered}) == 1


async def test_an_ssrf_refusal_fails_the_run_on_the_first_attempt():
    async def blocked(input):
        raise ApplicationError(
            "ssrf_blocked: URL resolves to a private address: 10.0.0.5",
            type="WebhookBlocked",
            non_retryable=True,
        )

    h = Harness(webhook=blocked)
    result = await h.run(R6)

    assert isinstance(result, WorkflowFailureError)
    assert len(h.delivered) == 1
    assert h.calls[-1] == MirrorRunInput(
        run_id="r1",
        status="failed",
        error="notify: WebhookBlocked: ssrf_blocked: URL resolves to a private address: 10.0.0.5",
        failed_block_id="notify",
    )


async def test_a_cancelled_run_found_by_a_retry_ends_cancelled_not_failed():
    async def cancelled(input):
        raise ApplicationError(
            "the run was cancelled", type=RUN_CANCELLED_ERROR, non_retryable=True
        )

    h = Harness(webhook=cancelled)
    result = await h.run(R6)

    assert result is None
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="notify", status="failed", error="cancelled before it finished"
        ),
        MirrorRunInput(run_id="r1", status="cancelled"),
    ]


async def test_a_workflow_cancel_interrupts_the_wait_between_webhook_attempts():
    failed_once = asyncio.Event()

    async def down(input):
        failed_once.set()
        raise ApplicationError("HTTP 503", type="WebhookFailed")

    async def cancel_during_backoff(handle):
        await failed_once.wait()
        await asyncio.sleep(0.5)  # inside the 30 s backoff; nothing skips time here
        await handle.cancel()

    h = Harness(webhook=down)
    result = await h.run(R6, during=cancel_during_backoff)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, CancelledError)
    assert len(h.delivered) == 1
    assert h.calls[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="notify", status="failed", error="cancelled before it finished"
        ),
        MirrorRunInput(run_id="r1", status="cancelled"),
    ]


async def test_a_workflow_cancel_lets_a_running_webhook_attempt_finish():
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(input):
        started.set()
        await release.wait()

    async def cancel_mid_post(handle):
        await started.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(webhook=slow)
    result = await h.run(R6, during=cancel_mid_post)

    # R3: the POST was not abandoned; it was the last block, so the run completed.
    assert result == LLM_PATH
    assert h.calls[-2:] == [
        MirrorBlockInput(run_id="r1", block_id="notify", status="completed", output_ref=LLM_PATH),
        MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH),
    ]


async def test_a_row_cancel_mid_block_ends_the_run_at_the_next_boundary(db_user):
    """C.10 end to end on the real mirrors: the engine is never told, the row is enough."""
    run = await _run_rows(
        db_user.id, (("fetch", "scrape", "pending"), ("extract", "llm_extract", "pending"))
    )
    key = f"scrapeflow-results/history/{uuid.uuid4()}/scrape.html"
    started, release = asyncio.Event(), asyncio.Event()

    @activity.defn(name=SCRAPE_ACTIVITY)
    async def scrape(input: ScrapeInput) -> ScrapeOutput:
        started.set()
        await release.wait()
        return ScrapeOutput.model_validate(
            {"result": {"path": key, "size": 10}, "content_hash": "00c0ffee12345678"}
        )

    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm(input: LLMInput) -> LLMOutput:
        raise AssertionError("a cancelled run must not start its next block")

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=WORKFLOW_QUEUE,
                workflows=[PipelineWorkflow],
                activities=[
                    mirror_block_status,
                    mirror_run_status,
                    record_storage,
                    check_cancelled,
                ],
            ),
            Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
            Worker(env.client, task_queue=LLM_QUEUE, activities=[llm]),
        ):
            handle = await env.client.start_workflow(
                PipelineWorkflow.run,
                RUN_INPUT.model_copy(update={"run_id": str(run.id)}),
                id=run.workflow_id,
                task_queue=WORKFLOW_QUEUE,
            )
            await started.wait()
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(PipelineRun)
                    .where(PipelineRun.id == run.id)
                    .values(cancel_requested_at=datetime.now(UTC))
                )
                await db.commit()
            # Mid-block the row says running: never cancelled before it stopped (R3).
            assert (await _statuses(run.id))[0] == "running"
            release.set()
            assert await handle.result() is None

    assert await _statuses(run.id) == ("cancelled", {"fetch": "completed", "extract": "skipped"})
