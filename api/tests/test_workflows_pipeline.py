"""C.6 + C.7 (MVP) — PipelineWorkflow and the two mirror activities."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from temporalio.worker.workflow_sandbox import RestrictedWorkflowAccessError

from app.core.db import AsyncSessionLocal
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.workflows.activities.contracts import (
    LLM_EXTRACT_ACTIVITY,
    SCRAPE_ACTIVITY,
    LLMInput,
    LLMOutput,
    ScrapeInput,
    ScrapeOutput,
)
from app.workflows.activities.db import mirror_block_status, mirror_run_status
from app.workflows.pipeline import (
    MIRROR_BLOCK_ACTIVITY,
    MIRROR_RUN_ACTIVITY,
    LLMKey,
    MirrorBlockInput,
    MirrorRunInput,
    PipelineRunInput,
    PipelineWorkflow,
)
from app.workflows.queues import LLM_QUEUE, SCRAPE_HTTP_QUEUE, WORKFLOW_QUEUE

SCRAPE_PATH = "scrapeflow-results/history/a-fetch/scrape.html"
LLM_PATH = "scrapeflow-results/history/a-extract/llm.json"

DEFINITION = {
    "inputs": {"url": {"type": "url"}},
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
        },
        {
            "id": "extract",
            "type": "llm_extract",
            "input": "fetch",
            "config_schema_version": 1,
            "config": {"llm_key_id": "k1", "model": "m", "output_schema": {"type": "object"}},
        },
    ],
}

RUN_INPUT = PipelineRunInput(
    run_id="r1",
    definition=DEFINITION,
    inputs={"url": "https://example.com"},
    artifact_ids={"fetch": "a-fetch", "extract": "a-extract"},
    llm_keys={"k1": LLMKey(provider="anthropic", encrypted_api_key="ciphertext")},
)


async def _run(llm_extract, run_input=RUN_INPUT, timeout=timedelta(seconds=30), **worker_kwargs):
    mirrored: list = []
    scraped: list[ScrapeInput] = []

    @activity.defn(name=MIRROR_BLOCK_ACTIVITY)
    async def mirror_block(input: MirrorBlockInput) -> None:
        mirrored.append(input)

    @activity.defn(name=MIRROR_RUN_ACTIVITY)
    async def mirror_run(input: MirrorRunInput) -> None:
        mirrored.append(input)

    @activity.defn(name=SCRAPE_ACTIVITY)
    async def scrape(input: ScrapeInput) -> ScrapeOutput:
        scraped.append(input)
        return ScrapeOutput.model_validate(
            {"result": {"path": SCRAPE_PATH, "size": 1}, "content_hash": "00c0ffee12345678"}
        )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=WORKFLOW_QUEUE,
                workflows=[PipelineWorkflow],
                activities=[mirror_block, mirror_run],
                **worker_kwargs,
            ),
            Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
            Worker(env.client, task_queue=LLM_QUEUE, activities=[llm_extract]),
        ):
            try:
                result = await env.client.execute_workflow(
                    PipelineWorkflow.run,
                    run_input,
                    id=f"pipeline-run-{uuid.uuid4()}",
                    task_queue=WORKFLOW_QUEUE,
                    # A failing workflow task retries forever, in real time: no time skipping.
                    execution_timeout=timeout,
                )
            except WorkflowFailureError as exc:
                result = exc
    return result, mirrored, scraped


async def test_blocks_run_in_order_and_every_step_is_mirrored():
    received: list[LLMInput] = []

    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm_extract(input: LLMInput) -> LLMOutput:
        received.append(input)
        return LLMOutput.model_validate({"result": {"path": LLM_PATH, "size": 1}})

    result, mirrored, scraped = await _run(llm_extract)

    assert result == LLM_PATH
    assert [(s.artifact_id, s.url, s.output_format) for s in scraped] == [
        ("a-fetch", "https://example.com", "html")
    ]
    [llm] = received
    assert (llm.artifact_id, llm.raw_minio_path, llm.encrypted_api_key) == (
        "a-extract",
        SCRAPE_PATH,
        "ciphertext",
    )
    assert mirrored == [
        MirrorBlockInput(run_id="r1", block_id="fetch", status="running"),
        MirrorBlockInput(run_id="r1", block_id="fetch", status="completed", output_ref=SCRAPE_PATH),
        MirrorBlockInput(run_id="r1", block_id="extract", status="running", input_ref=SCRAPE_PATH),
        MirrorBlockInput(run_id="r1", block_id="extract", status="completed", output_ref=LLM_PATH),
        MirrorRunInput(run_id="r1", status="completed", result_path=LLM_PATH),
    ]


async def test_a_failed_block_fails_the_block_the_run_and_the_workflow():
    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm_extract(input: LLMInput) -> LLMOutput:
        raise ApplicationError("401 invalid key", type="LLMFailed", non_retryable=True)

    result, mirrored, _ = await _run(llm_extract)

    assert isinstance(result, WorkflowFailureError)
    assert mirrored[-2:] == [
        MirrorBlockInput(
            run_id="r1", block_id="extract", status="failed", error="LLMFailed: 401 invalid key"
        ),
        MirrorRunInput(run_id="r1", status="failed", error="extract: LLMFailed: 401 invalid key"),
    ]


@activity.defn(name=LLM_EXTRACT_ACTIVITY)
async def _unreached_llm_extract(input: LLMInput) -> LLMOutput:
    raise AssertionError("LLMExtract must not run")


async def test_an_unexpected_exception_fails_the_run_instead_of_hanging():
    blocks = [dict(DEFINITION["blocks"][0]), DEFINITION["blocks"][1]]
    blocks[0]["config"] = {k: v for k, v in blocks[0]["config"].items() if k != "output_format"}
    run_input = RUN_INPUT.model_copy(update={"definition": {**DEFINITION, "blocks": blocks}})

    result, mirrored, scraped = await _run(_unreached_llm_extract, run_input)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, ApplicationError)
    assert result.cause.non_retryable
    assert scraped == []
    error = "internal error: KeyError: 'output_format'"
    assert mirrored == [
        MirrorBlockInput(run_id="r1", block_id="fetch", status="running"),
        MirrorBlockInput(run_id="r1", block_id="fetch", status="failed", error=error),
        MirrorRunInput(run_id="r1", status="failed", error=f"fetch: {error}"),
    ]


async def test_a_nondeterminism_error_is_left_to_the_workflow_task(monkeypatch):
    async def restricted(*args):
        raise RestrictedWorkflowAccessError("os.environ")

    # Unsandboxed, so the patched method is the one the worker runs.
    monkeypatch.setattr(PipelineWorkflow, "_execute", restricted)

    result, mirrored, _ = await _run(
        _unreached_llm_extract,
        timeout=timedelta(seconds=3),
        workflow_runner=UnsandboxedWorkflowRunner(),
    )

    assert isinstance(result, WorkflowFailureError)
    assert not isinstance(result.cause, ApplicationError)
    assert mirrored == [MirrorBlockInput(run_id="r1", block_id="fetch", status="running")]


# ---------------------------------------------------------------------------
# The mirror activities, against the dev DB
# ---------------------------------------------------------------------------


async def _run_rows(user_id, blocks=(("fetch", "scrape", "pending"),)) -> PipelineRun:
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
            status="running",
            workflow_id=f"pipeline-run-{run_id}",
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


async def _block(run_id) -> PipelineRunBlock:
    async with AsyncSessionLocal() as db:
        return await db.get(
            PipelineRunBlock,
            (
                await db.scalar(
                    PipelineRunBlock.__table__.select()
                    .with_only_columns(PipelineRunBlock.id)
                    .where(PipelineRunBlock.pipeline_run_id == run_id)
                )
            ),
        )


async def test_mirror_block_status_writes_the_row(db_user):
    run = await _run_rows(db_user.id)
    rid = str(run.id)

    await mirror_block_status(MirrorBlockInput(run_id=rid, block_id="fetch", status="running"))
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
    await mirror_run_status(
        MirrorRunInput(run_id=str(run.id), status="completed", result_path=LLM_PATH)
    )
    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.status, row.result_path, row.finished_at is not None) == (
        "completed",
        LLM_PATH,
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
    async with AsyncSessionLocal() as db:
        statuses = dict(
            (
                await db.execute(
                    select(PipelineRunBlock.block_id, PipelineRunBlock.status).where(
                        PipelineRunBlock.pipeline_run_id == run.id
                    )
                )
            ).all()
        )
    assert statuses == {"fetch": "completed", "extract": "failed", "notify": "skipped"}


async def test_mirror_of_a_missing_row_fails_without_retry(db_user):
    run = await _run_rows(db_user.id)
    with pytest.raises(ApplicationError) as exc_info:
        await mirror_block_status(
            MirrorBlockInput(run_id=str(run.id), block_id="nope", status="running")
        )
    assert exc_info.value.non_retryable
    with pytest.raises(ApplicationError):
        await mirror_run_status(MirrorRunInput(run_id=str(uuid.uuid4()), status="completed"))
