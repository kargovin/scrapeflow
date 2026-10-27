"""E.1 — BatchWorkflow, the batch-aware job-run activities, finish_batch and the batch lane switch."""

import asyncio
import io
import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.core.db import AsyncSessionLocal
from app.core.dispatch import build_batch_scrape_input, build_batch_scrape_message
from app.main import app
from app.models.batch import Batch, BatchItem
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey
from app.models.storage_object import StorageObject
from app.models.user_quota import UserQuota
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities import host_slots, job_runs
from app.workflows.activities.batches import NO_OUTCOME, finish_batch
from app.workflows.activities.contracts import (
    LLM_EXTRACT_ACTIVITY,
    SCRAPE_ACTIVITY,
    LLMInput,
    LLMOutput,
    ScrapeInput,
    ScrapeOutput,
    StoredObject,
)
from app.workflows.activities.job_runs import (
    finish_job_run,
    job_run_processing,
    job_run_started,
    read_llm_settings,
    record_job_objects,
)
from app.workflows.batch import (
    FINISH_BATCH_ACTIVITY,
    BatchInput,
    BatchWorkflow,
    FinishBatchInput,
    workflow_id,
)
from app.workflows.job import (
    FINISH_JOB_RUN_ACTIVITY,
    JOB_RUN_PROCESSING_ACTIVITY,
    JOB_RUN_STARTED_ACTIVITY,
    READ_LLM_SETTINGS_ACTIVITY,
    RECORD_JOB_OBJECTS_ACTIVITY,
    FinishJobRunInput,
    JobLLM,
    JobRunInput,
    JobRunRef,
    JobWorkflow,
    RecordJobObjectsInput,
)
from app.workflows.queues import LLM_QUEUE, SCRAPE_HTTP_QUEUE, WORKFLOW_QUEUE
from app.workflows.steps import (
    ACQUIRE_HOST_SLOT_ACTIVITY,
    RELEASE_HOST_SLOT_ACTIVITY,
    HostSlot,
    HostSlotInput,
    HostSlotRelease,
)

BUCKET = settings.minio_bucket
HASH = "00c0ffee12345678"


def _runs(n: int) -> list[JobRunInput]:
    return [
        JobRunInput(
            run_id=f"r{i}",
            engine="http",
            scrape=ScrapeInput(
                artifact_id=f"r{i}", url=f"https://example.com/{i}", output_format="html"
            ),
        )
        for i in range(n)
    ]


def _batch_input(n: int, k: int = 3) -> BatchInput:
    return BatchInput(batch_id="b1", max_in_flight=k, runs=_runs(n))


def _scraped(artifact_id: str) -> ScrapeOutput:
    return ScrapeOutput.model_validate(
        {
            "result": {"path": f"{BUCKET}/history/{artifact_id}/scrape.html", "size": 10},
            "content_hash": HASH,
        }
    )


async def _scrape_ok(input: ScrapeInput) -> ScrapeOutput:
    await asyncio.sleep(0.05)
    return _scraped(input.artifact_id)


class Harness:
    """BatchWorkflow over real child JobWorkflows; every activity mocked, recorded per run."""

    def __init__(self, scrape=_scrape_ok):
        self.started: list[str] = []
        self.finished: list[FinishJobRunInput] = []
        self.batch_finishes: list[FinishBatchInput] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._scrape = scrape

    def activities(self):
        @activity.defn(name=JOB_RUN_STARTED_ACTIVITY)
        async def started(input: JobRunRef) -> str:
            self.started.append(input.run_id)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            return "running"

        @activity.defn(name=READ_LLM_SETTINGS_ACTIVITY)
        async def load(input: JobRunRef) -> JobLLM:
            return JobLLM()

        @activity.defn(name=RECORD_JOB_OBJECTS_ACTIVITY)
        async def record(input: RecordJobObjectsInput) -> str:
            return "running"

        @activity.defn(name=JOB_RUN_PROCESSING_ACTIVITY)
        async def processing(input: JobRunRef) -> str:
            return "processing"

        @activity.defn(name=FINISH_JOB_RUN_ACTIVITY)
        async def finish(input: FinishJobRunInput) -> str:
            self.finished.append(input)
            self.in_flight -= 1
            return input.status

        @activity.defn(name=FINISH_BATCH_ACTIVITY)
        async def close(input: FinishBatchInput) -> str:
            self.batch_finishes.append(input)
            return "cancelled" if input.cancelled else "partial_failure"

        @activity.defn(name=ACQUIRE_HOST_SLOT_ACTIVITY)
        async def acquire(input: HostSlotInput) -> HostSlot:
            return HostSlot(domain="example.com")

        @activity.defn(name=RELEASE_HOST_SLOT_ACTIVITY)
        async def release(input: HostSlotRelease) -> None:
            return None

        @activity.defn(name=SCRAPE_ACTIVITY)
        async def scrape(input: ScrapeInput) -> ScrapeOutput:
            return await self._scrape(input)

        return [started, load, record, processing, finish, close, acquire, release], scrape

    async def run(self, batch_input: BatchInput, during=None):
        db_activities, scrape = self.activities()
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            async with (
                Worker(
                    env.client,
                    task_queue=WORKFLOW_QUEUE,
                    workflows=[BatchWorkflow, JobWorkflow],
                    activities=db_activities,
                ),
                Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
            ):
                handle = await env.client.start_workflow(
                    BatchWorkflow.run,
                    batch_input,
                    id=workflow_id(uuid.uuid4()),
                    task_queue=WORKFLOW_QUEUE,
                    execution_timeout=timedelta(days=1),
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


# ---------------------------------------------------------------------------
# BatchWorkflow (time-skipping env, activities mocked)
# ---------------------------------------------------------------------------


async def test_every_item_runs_as_a_child_and_the_batch_closes_once():
    h = Harness()
    result = await h.run(_batch_input(5))

    assert result == "partial_failure"  # what the mocked finish_batch says
    assert sorted(h.started) == [f"r{i}" for i in range(5)]
    assert sorted(f.run_id for f in h.finished) == [f"r{i}" for i in range(5)]
    assert {f.status for f in h.finished} == {"completed"}
    assert h.batch_finishes == [FinishBatchInput(batch_id="b1", cancelled=False)]


async def test_never_more_than_k_children_are_in_flight():
    h = Harness()
    await h.run(_batch_input(7, k=2))

    assert len(h.finished) == 7
    assert h.max_in_flight == 2


async def test_a_failed_child_does_not_stop_the_others():
    async def scrape(input):
        if input.artifact_id == "r1":
            raise ApplicationError("blocked:akamai", type="ScrapeFailed", non_retryable=True)
        return await _scrape_ok(input)

    h = Harness(scrape=scrape)
    await h.run(_batch_input(4))

    outcomes = {f.run_id: (f.status, f.error) for f in h.finished}
    assert outcomes == {
        "r0": ("completed", None),
        "r1": ("failed", "blocked:akamai"),
        "r2": ("completed", None),
        "r3": ("completed", None),
    }
    assert h.batch_finishes == [FinishBatchInput(batch_id="b1", cancelled=False)]


async def test_a_cancel_reaches_the_children_in_flight_and_starts_no_more():
    scraping, release = asyncio.Event(), asyncio.Event()
    in_scrape: list[str] = []

    async def scrape(input):
        in_scrape.append(input.artifact_id)
        if len(in_scrape) == 2:
            scraping.set()
        await release.wait()
        return _scraped(input.artifact_id)

    async def cancel_mid_scrape(handle):
        await scraping.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(scrape=scrape)
    result = await h.run(_batch_input(5, k=2), during=cancel_mid_scrape)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, CancelledError)
    # The two in flight finished their scrape, then stopped cancelled and dropped it.
    assert sorted(h.started) == ["r0", "r1"]
    assert {(f.run_id, f.status) for f in h.finished} == {("r0", "cancelled"), ("r1", "cancelled")}
    assert all(f.discard for f in h.finished)
    assert h.batch_finishes == [FinishBatchInput(batch_id="b1", cancelled=True)]


# ---------------------------------------------------------------------------
# The activities on a batch item's run (real DB)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _test_minio(monkeypatch):
    monkeypatch.setattr(job_runs, "_minio", app.state.minio)


async def _batch(user_id, statuses, **fields) -> tuple[Batch, list[BatchItem], list[JobRun]]:
    """A v2 batch with one item and run per status (an item mirrors its run, as v2 keeps it)."""
    async with AsyncSessionLocal() as db:
        batch = Batch(user_id=user_id, status="running", total=len(statuses), **fields)
        db.add(batch)
        await db.flush()
        items = [
            BatchItem(
                batch_id=batch.id,
                url=f"https://example.com/{i}",
                status="running" if s == "processing" else s,
            )
            for i, s in enumerate(statuses)
        ]
        db.add_all(items)
        await db.flush()
        runs = [
            JobRun(batch_item_id=item.id, status=s, lane="v2")
            for item, s in zip(items, statuses, strict=True)
        ]
        db.add_all(runs)
        await db.commit()
    return batch, items, runs


async def _reload(batch_id) -> tuple[Batch, dict[str, BatchItem], dict[uuid.UUID, JobRun]]:
    async with AsyncSessionLocal() as db:
        batch = await db.get(Batch, batch_id)
        items = (await db.scalars(select(BatchItem).where(BatchItem.batch_id == batch_id))).all()
        runs = (
            await db.scalars(select(JobRun).where(JobRun.batch_item_id.in_([i.id for i in items])))
        ).all()
    return batch, {i.url: i for i in items}, {r.id: r for r in runs}


async def _batch_deliveries(batch_id) -> list[WebhookDelivery]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.scalars(
                    select(WebhookDelivery).where(WebhookDelivery.batch_id == batch_id)
                )
            ).all()
        )


async def _listen(channel: str):
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(channel, lambda *a: received.put_nowait(a[3]))
    return conn, received


async def test_started_on_a_batch_run_moves_its_item_and_notifies_batch_status(db_user):
    batch, items, [run, _] = await _batch(db_user.id, ["pending", "pending"])
    conn, received = await _listen("batch_status")
    try:
        assert await job_run_started(JobRunRef(run_id=str(run.id))) == "running"
        payload = json.loads(await asyncio.wait_for(received.get(), 5))
    finally:
        await conn.close()

    assert payload == {
        "batch_id": str(batch.id),
        "completed": 0,
        "failed": 0,
        "total": 2,
        "status": "running",
        "item_url": items[0].url,
        "item_status": "running",
    }
    _, by_url, _ = await _reload(batch.id)
    assert by_url[items[0].url].status == "running"


async def test_processing_leaves_the_item_running(db_user):
    batch, items, [run] = await _batch(db_user.id, ["running"])
    assert await job_run_processing(JobRunRef(run_id=str(run.id))) == "processing"
    _, by_url, _ = await _reload(batch.id)
    assert by_url[items[0].url].status == "running"


async def test_finish_copies_the_outcome_to_the_item_and_recounts_the_batch(db_user):
    batch, items, runs = await _batch(db_user.id, ["running", "running", "running", "running"])
    path = f"{BUCKET}/history/{runs[0].id}/scrape.html"

    await finish_job_run(
        FinishJobRunInput(run_id=str(runs[0].id), status="completed", result_path=path)
    )
    await finish_job_run(FinishJobRunInput(run_id=str(runs[1].id), status="failed", error="e"))
    await finish_job_run(FinishJobRunInput(run_id=str(runs[2].id), status="completed"))
    # A retried activity: absolute counts, not increments (ADR-009 11d).
    await finish_job_run(
        FinishJobRunInput(run_id=str(runs[0].id), status="completed", result_path=path)
    )

    reloaded, by_url, _ = await _reload(batch.id)
    assert (reloaded.completed, reloaded.failed, reloaded.status) == (2, 1, "running")
    done = by_url[items[0].url]
    assert (done.status, done.result_path, done.completed_at is not None) == (
        "completed",
        path,
        True,
    )
    assert (by_url[items[1].url].status, by_url[items[1].url].error) == ("failed", "e")
    # An item's run writes no webhook row of its own.
    assert await _batch_deliveries(batch.id) == []


async def test_read_llm_settings_reads_the_batch_and_its_key(db_user):
    async with AsyncSessionLocal() as db:
        key = UserLLMKey(user_id=db_user.id, name="k", provider="anthropic", encrypted_api_key="ct")
        db.add(key)
        await db.commit()
    config = {"llm_key_id": str(key.id), "model": "m", "output_schema": {"type": "object"}}
    _, _, [run] = await _batch(db_user.id, ["running"], llm_config=config)

    llm = await read_llm_settings(JobRunRef(run_id=str(run.id)))
    assert (llm.provider, llm.encrypted_api_key, llm.model) == ("anthropic", "ct", "m")

    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserLLMKey).where(UserLLMKey.id == key.id))
        await db.commit()
    llm = await read_llm_settings(JobRunRef(run_id=str(run.id)))
    assert llm.error == "LLM key not found or deleted"


async def _put(path: str, size: int = 10) -> str:
    bucket, _, key = path.partition("/")
    await app.state.minio.put_object(bucket, key, io.BytesIO(b"x" * size), size)
    return path


async def _exists(path: str) -> bool:
    bucket, _, key = path.partition("/")
    try:
        await app.state.minio.stat_object(bucket, key)
        return True
    except Exception:
        return False


async def _remove(*paths: str) -> None:
    for path in paths:
        bucket, _, key = path.partition("/")
        await app.state.minio.remove_object(bucket, key)


async def _ledger(paths) -> list[StorageObject]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.scalars(select(StorageObject).where(StorageObject.object_key.in_(paths)))
            ).all()
        )


async def test_record_charges_the_batch_owner(db_user):
    _, _, [run] = await _batch(db_user.id, ["running"])
    path = f"{BUCKET}/history/{run.id}/scrape.html"

    state = await record_job_objects(
        RecordJobObjectsInput(run_id=str(run.id), objects=[StoredObject(path=path, size=10)])
    )
    assert state == "running"
    [row] = await _ledger([path])
    assert (row.user_id, row.job_run_id, row.bytes) == (db_user.id, run.id, 10)


async def test_over_the_storage_limit_the_item_fails_and_is_counted(db_user):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=db_user.id, storage_bytes_limit=5))
        await db.commit()
    batch, items, [run] = await _batch(db_user.id, ["running"])
    path = await _put(f"{BUCKET}/history/{run.id}/scrape.html")

    state = await record_job_objects(
        RecordJobObjectsInput(run_id=str(run.id), objects=[StoredObject(path=path, size=10)])
    )

    assert state == "failed"
    assert not await _exists(path)
    assert await _ledger([path]) == []
    reloaded, by_url, runs = await _reload(batch.id)
    assert (runs[run.id].status, runs[run.id].error) == ("failed", "storage_quota_exceeded")
    assert by_url[items[0].url].status == "failed"
    assert (reloaded.failed, reloaded.status) == (1, "running")  # finish_batch closes it


# ---------------------------------------------------------------------------
# finish_batch (real DB)
# ---------------------------------------------------------------------------


async def test_finish_batch_closes_completed_with_its_webhook_row(db_user):
    batch, _, _ = await _batch(
        db_user.id, ["completed", "completed"], webhook_url="https://hooks.example.com/b"
    )
    conn, received = await _listen("batch_status")
    try:
        assert await finish_batch(FinishBatchInput(batch_id=str(batch.id))) == "completed"
        payload = json.loads(await asyncio.wait_for(received.get(), 5))
    finally:
        await conn.close()

    reloaded, _, _ = await _reload(batch.id)
    assert (reloaded.status, reloaded.completed, reloaded.failed) == ("completed", 2, 0)
    assert reloaded.completed_at is not None
    assert payload["status"] == "completed" and payload["completed"] == 2
    [delivery] = await _batch_deliveries(batch.id)
    assert (delivery.event, delivery.payload["status"]) == ("batch.completed", "completed")

    # A retried activity: left as it is, no second row.
    assert await finish_batch(FinishBatchInput(batch_id=str(batch.id))) == "completed"
    assert len(await _batch_deliveries(batch.id)) == 1


async def test_finish_batch_with_a_failed_item_is_a_partial_failure(db_user):
    batch, _, _ = await _batch(db_user.id, ["completed", "failed", "completed"])
    assert await finish_batch(FinishBatchInput(batch_id=str(batch.id))) == "partial_failure"
    reloaded, _, _ = await _reload(batch.id)
    assert (reloaded.completed, reloaded.failed) == (2, 1)


async def test_finish_batch_fails_a_run_no_child_finished(db_user):
    batch, items, runs = await _batch(db_user.id, ["completed", "pending"])
    assert await finish_batch(FinishBatchInput(batch_id=str(batch.id))) == "partial_failure"

    reloaded, by_url, by_id = await _reload(batch.id)
    assert (by_id[runs[1].id].status, by_id[runs[1].id].error) == ("failed", NO_OUTCOME)
    assert (by_url[items[1].url].status, by_url[items[1].url].error) == ("failed", NO_OUTCOME)
    assert (reloaded.completed, reloaded.failed) == (1, 1)


async def test_finish_batch_on_a_cancel_cancels_what_is_left_and_writes_no_webhook(db_user):
    batch, items, runs = await _batch(
        db_user.id, ["completed", "running", "pending"], webhook_url="https://hooks.example.com/b"
    )
    state = await finish_batch(FinishBatchInput(batch_id=str(batch.id), cancelled=True))

    assert state == "cancelled"
    reloaded, by_url, by_id = await _reload(batch.id)
    assert [by_id[r.id].status for r in runs] == ["completed", "cancelled", "cancelled"]
    assert [by_url[i.url].status for i in items] == ["completed", "cancelled", "cancelled"]
    assert (reloaded.status, reloaded.completed) == ("cancelled", 1)
    assert await _batch_deliveries(batch.id) == []


async def test_finish_batch_keeps_the_apis_cancel(db_user):
    batch, _, _ = await _batch(db_user.id, ["cancelled", "completed"])
    async with AsyncSessionLocal() as db:
        row = await db.get(Batch, batch.id)
        row.status = "cancelled"
        await db.commit()
    assert await finish_batch(FinishBatchInput(batch_id=str(batch.id))) == "cancelled"


async def test_finish_batch_on_a_deleted_batch_is_gone():
    assert await finish_batch(FinishBatchInput(batch_id=str(uuid.uuid4()))) == "gone"


# ---------------------------------------------------------------------------
# End to end on the real activities
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def worker_redis(monkeypatch):
    client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    monkeypatch.setattr(host_slots, "_redis", client)
    yield client
    await client.aclose()


async def test_a_batch_runs_end_to_end_on_the_real_activities(db_user, worker_redis):
    batch, items, runs = await _batch(
        db_user.id, ["pending"] * 3, webhook_url="https://hooks.example.com/b"
    )
    pages = [await _put(f"{BUCKET}/history/{run.id}/scrape.html") for run in runs]
    failing = str(runs[1].id)

    @activity.defn(name=SCRAPE_ACTIVITY)
    async def scrape(input: ScrapeInput) -> ScrapeOutput:
        if input.artifact_id == failing:
            raise ApplicationError("blocked:akamai", type="ScrapeFailed", non_retryable=True)
        return ScrapeOutput.model_validate(
            {
                "result": {"path": f"{BUCKET}/history/{input.artifact_id}/scrape.html", "size": 10},
                "content_hash": HASH,
            }
        )

    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm(input: LLMInput) -> LLMOutput:
        raise AssertionError("no LLM step on this batch")

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=WORKFLOW_QUEUE,
                workflows=[BatchWorkflow, JobWorkflow],
                activities=[
                    job_run_started,
                    read_llm_settings,
                    record_job_objects,
                    job_run_processing,
                    finish_job_run,
                    finish_batch,
                    host_slots.acquire_host_slot,
                    host_slots.release_host_slot,
                ],
            ),
            Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
            Worker(env.client, task_queue=LLM_QUEUE, activities=[llm]),
        ):
            result = await env.client.execute_workflow(
                BatchWorkflow.run,
                BatchInput(
                    batch_id=str(batch.id),
                    max_in_flight=2,
                    runs=[
                        JobRunInput(
                            run_id=str(run.id),
                            engine="http",
                            scrape=build_batch_scrape_input(batch, item, run),
                        )
                        for item, run in zip(items, runs, strict=True)
                    ],
                ),
                id=workflow_id(batch.id),
                task_queue=WORKFLOW_QUEUE,
            )

    assert result == "partial_failure"
    reloaded, by_url, by_id = await _reload(batch.id)
    assert (reloaded.status, reloaded.completed, reloaded.failed) == ("partial_failure", 2, 1)
    assert [by_id[r.id].status for r in runs] == ["completed", "failed", "completed"]
    assert by_url[items[1].url].error == "blocked:akamai"
    assert by_url[items[0].url].result_path == pages[0]
    assert {o.object_key for o in await _ledger(pages)} == {pages[0], pages[2]}
    [delivery] = await _batch_deliveries(batch.id)
    assert delivery.event == "batch.completed"
    await _remove(pages[0], pages[2])
    if await _exists(pages[1]):
        await _remove(pages[1])


# ---------------------------------------------------------------------------
# The batch lane switch (POST /batch, DELETE /batch/{id})
# ---------------------------------------------------------------------------

BATCH = {
    "urls": ["https://example.com/a", "https://example.com/b", "https://example.com/c"],
    "output_format": "markdown",
    "respect_robots": True,
}


@pytest.fixture
def auth_headers(mock_clerk_auth):
    return {"Authorization": "Bearer fake.jwt.token"}


@pytest.fixture
def temporal():
    client = MagicMock()
    client.start_workflow = AsyncMock()
    handle = MagicMock()
    handle.cancel = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    app.state.temporal = client
    yield client
    app.state.temporal = None


@pytest.fixture
def lane(monkeypatch):
    def set_lane(value):
        monkeypatch.setattr(settings, "batch_lane", value)

    return set_lane


@pytest_asyncio.fixture
async def created_batches():
    ids: list[str] = []
    yield ids
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Batch).where(Batch.id.in_([uuid.UUID(i) for i in ids])))
        await db.commit()


async def test_on_v1_a_batch_is_published_to_nats(
    client, auth_headers, mock_jetstream, temporal, lane, created_batches
):
    lane("v1")
    response = await client.post("/batch", json=BATCH, headers=auth_headers)
    assert response.status_code == 201
    created_batches.append(response.json()["id"])

    _, _, runs = await _reload(uuid.UUID(response.json()["id"]))
    assert {r.lane for r in runs.values()} == {"v1"}
    assert mock_jetstream.publish.call_count == 3
    temporal.start_workflow.assert_not_called()


async def test_on_v2_a_batch_starts_one_workflow_and_skips_nats(
    client, auth_headers, mock_jetstream, temporal, lane, created_batches, monkeypatch
):
    lane("v2")
    monkeypatch.setattr(settings, "batch_max_in_flight", 4)
    response = await client.post("/batch", json=BATCH, headers=auth_headers)
    assert response.status_code == 201
    assert response.json()["status"] == "running"
    batch_id = response.json()["id"]
    created_batches.append(batch_id)

    mock_jetstream.publish.assert_not_called()
    [call] = temporal.start_workflow.call_args_list
    workflow, batch_input = call.args
    assert workflow == BatchWorkflow.run
    async with AsyncSessionLocal() as db:
        batch = await db.get(Batch, uuid.UUID(batch_id))
        items = (
            await db.scalars(
                select(BatchItem)
                .where(BatchItem.batch_id == batch.id)
                .order_by(BatchItem.created_at, BatchItem.id)
            )
        ).all()
        runs = {
            r.batch_item_id: r
            for r in (
                await db.scalars(
                    select(JobRun).where(JobRun.batch_item_id.in_([i.id for i in items]))
                )
            ).all()
        }
    assert {r.lane for r in runs.values()} == {"v2"}
    assert (batch_input.batch_id, batch_input.max_in_flight) == (batch_id, 4)
    expected = {
        str(runs[item.id].id): JobRunInput(
            run_id=str(runs[item.id].id),
            engine="http",
            scrape=build_batch_scrape_input(batch, item, runs[item.id]),
        )
        for item in items
    }
    assert {r.run_id: r for r in batch_input.runs} == expected
    assert call.kwargs["id"] == f"batch-{batch_id}"
    assert call.kwargs["task_queue"] == WORKFLOW_QUEUE
    assert call.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE


async def test_on_v2_a_batch_that_cannot_start_fails_every_item_with_503(
    client, auth_headers, mock_jetstream, temporal, lane, created_batches
):
    lane("v2")
    temporal.start_workflow.side_effect = RuntimeError("engine down")
    body = BATCH | {"webhook_url": "https://example.com/hook"}
    response = await client.post("/batch", json=body, headers=auth_headers)

    assert response.status_code == 503
    batch_id = response.json()["detail"]["batch_id"]
    created_batches.append(batch_id)
    batch, items, runs = await _reload(uuid.UUID(batch_id))
    error = "could not start workflow: engine down"
    assert (batch.status, batch.completed, batch.failed) == ("partial_failure", 0, 3)
    assert {(r.status, r.error) for r in runs.values()} == {("failed", error)}
    assert {(i.status, i.error) for i in items.values()} == {("failed", error)}
    [delivery] = await _batch_deliveries(batch.id)
    assert delivery.event == "batch.completed"
    mock_jetstream.publish.assert_not_called()


async def _set_status(batch_id: str, status: str) -> None:
    async with AsyncSessionLocal() as db:
        items = (
            await db.scalars(select(BatchItem).where(BatchItem.batch_id == uuid.UUID(batch_id)))
        ).all()
        first = await db.scalar(select(JobRun).where(JobRun.batch_item_id == items[0].id))
        first.status = status
        items[0].status = "running"
        await db.commit()


async def test_on_v2_a_cancel_writes_the_rows_then_cancels_the_batch_workflow(
    client, auth_headers, mock_jetstream, temporal, lane, created_batches
):
    lane("v2")
    response = await client.post("/batch", json=BATCH, headers=auth_headers)
    batch_id = response.json()["id"]
    created_batches.append(batch_id)
    # One item is in its LLM step: a cancel must reach it too.
    await _set_status(batch_id, "processing")
    conn, received = await _listen("batch_status")
    try:
        response = await client.delete(f"/batch/{batch_id}", headers=auth_headers)
        payload = json.loads(await asyncio.wait_for(received.get(), 5))
    finally:
        await conn.close()

    assert response.status_code == 204
    batch, items, runs = await _reload(uuid.UUID(batch_id))
    assert batch.status == "cancelled"
    assert {r.status for r in runs.values()} == {"cancelled"}
    assert {i.status for i in items.values()} == {"cancelled"}
    assert payload["status"] == "cancelled"
    temporal.get_workflow_handle.assert_called_once_with(f"batch-{batch_id}")
    temporal.get_workflow_handle.return_value.cancel.assert_awaited_once()


async def test_on_v1_a_cancel_never_reaches_the_engine(
    client, auth_headers, mock_jetstream, temporal, lane, created_batches
):
    lane("v1")
    response = await client.post("/batch", json=BATCH, headers=auth_headers)
    batch_id = response.json()["id"]
    created_batches.append(batch_id)

    assert (await client.delete(f"/batch/{batch_id}", headers=auth_headers)).status_code == 204
    batch, _, runs = await _reload(uuid.UUID(batch_id))
    assert batch.status == "cancelled"
    assert {r.status for r in runs.values()} == {"cancelled"}
    temporal.get_workflow_handle.assert_not_called()


async def test_a_cancel_leaves_a_finished_run_finished(
    client, auth_headers, mock_jetstream, temporal, lane, created_batches
):
    lane("v2")
    response = await client.post("/batch", json=BATCH, headers=auth_headers)
    batch_id = response.json()["id"]
    created_batches.append(batch_id)
    await _set_status(batch_id, "completed")

    assert (await client.delete(f"/batch/{batch_id}", headers=auth_headers)).status_code == 204
    _, _, runs = await _reload(uuid.UUID(batch_id))
    assert sorted(r.status for r in runs.values()) == ["cancelled", "cancelled", "completed"]


async def test_the_batch_scrape_input_carries_what_the_nats_message_carries(db_user):
    batch, [item], [run] = await _batch(
        db_user.id, ["pending"], output_format="json", engine="playwright", respect_robots=True
    )
    message = build_batch_scrape_message(batch, item, run).model_dump(mode="json")
    scrape_input = build_batch_scrape_input(batch, item, run).model_dump(mode="json")
    for field in ("run_id", "engine", "crawl_context", "schema_version"):
        message.pop(field, None)
    assert scrape_input == message
