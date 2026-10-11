"""C.6 + C.7 — PipelineWorkflow and the workflow-worker DB activities."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import select
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from temporalio.worker.workflow_sandbox import RestrictedWorkflowAccessError

from app.core.db import AsyncSessionLocal
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.storage_object import StorageObject
from app.models.user_quota import UserQuota
from app.pipelines.catalog import BLOCK_OVERHEAD_SECONDS, CLEAN, LLM_EXTRACT, SCRAPE, VALIDATE
from app.pipelines.definition import parse_definition
from app.settings import settings
from app.workflows.activities.contracts import (
    CLEAN_ACTIVITY,
    LLM_EXTRACT_ACTIVITY,
    SCRAPE_ACTIVITY,
    VALIDATE_ACTIVITY,
    CleanInput,
    CleanOutput,
    Credentials,
    LLMInput,
    LLMOutput,
    ScrapeInput,
    ScrapeOutput,
    StoredObject,
    ValidateInput,
)
from app.workflows.activities.db import (
    PIPELINE_STATUS_CHANNEL,
    check_cancelled,
    mirror_block_status,
    mirror_run_status,
    record_storage,
)
from app.workflows.pipeline import (
    DB_ACTIVITY_SECONDS,
    MIRROR_BLOCK_ACTIVITY,
    MIRROR_RUN_ACTIVITY,
    RECORD_STORAGE_ACTIVITY,
    LLMKey,
    MirrorBlockInput,
    MirrorRunInput,
    PipelineRunInput,
    PipelineWorkflow,
    RecordStorageInput,
    RunRef,
    StorageUsage,
)
from app.workflows.queues import CONTENT_QUEUE, LLM_QUEUE, SCRAPE_HTTP_QUEUE, WORKFLOW_QUEUE

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


class Harness:
    """Mocked activities on their real queues; records every DB-activity call in order."""

    def __init__(
        self, scrape=_scrape_ok, llm=_llm_ok, clean=_clean_ok, validate=_validate_ok, usage=None
    ):
        self.calls: list = []
        self.scraped: list[ScrapeInput] = []
        self.scrape_info: list = []
        self.extracted: list[LLMInput] = []
        self.cleaned: list[CleanInput] = []
        self.validated: list[ValidateInput] = []
        self.content_info: list = []
        self.usage = usage or StorageUsage(used=0, limit=100)
        self.running_reply: dict[str, str] = {}
        self._scrape = scrape
        self._llm = llm
        self._clean = clean
        self._validate = validate

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
        async def validate(input: ValidateInput) -> None:
            self.validated.append(input)
            self.content_info.append(activity.info())
            return await self._validate(input)

        return [mirror_block, mirror_run, record], scrape, llm, [clean, validate]

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
        run_id="r1", status="failed", error="fetch: the run's time budget is spent"
    )


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
        MirrorRunInput(run_id="r1", status="failed", error="extract: LLMFailed: 401 invalid key"),
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
        MirrorRunInput(run_id="r1", status="failed", error=f"fetch: {error}"),
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
    assert h.validated == [ValidateInput(input_path=CLEAN_PATH, rules=RULES)]
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
    assert validated.rules == [schema_rule]


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
        MirrorRunInput(run_id="r1", status="failed", error=f"guard: {message}"),
    ]


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
    base = {"run_id": str(run.id), "pipeline_id": str(run.pipeline_id)}
    assert payloads == [
        {**base, "status": "running", "block_id": "fetch", "block_status": "failed"},
        {**base, "status": "failed", "block_id": None, "block_status": None},
    ]


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


async def test_db_activities_on_a_missing_row_fail_without_retry(db_user):
    run = await _run_rows(db_user.id)
    with pytest.raises(ApplicationError) as exc_info:
        await mirror_block_status(
            MirrorBlockInput(run_id=str(run.id), block_id="nope", status="completed")
        )
    assert exc_info.value.non_retryable
    with pytest.raises(ApplicationError):
        await mirror_run_status(MirrorRunInput(run_id=str(uuid.uuid4()), status="completed"))
    with pytest.raises(ApplicationError):
        await record_storage(RecordStorageInput(run_id=str(run.id), block_id="nope", objects=[]))
    with pytest.raises(ApplicationError):
        await check_cancelled(RunRef(run_id=str(uuid.uuid4())))
