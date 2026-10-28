"""D.3 — JobWorkflow and the job-lane DB activities."""

import asyncio
import io
import uuid
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.core import host_limiter
from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey
from app.models.storage_object import StorageObject
from app.models.user_quota import UserQuota
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows import steps
from app.workflows.activities import host_slots, job_runs
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
from app.workflows.job import (
    ACCOUNTING_FAILED,
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
    workflow_id,
)
from app.workflows.queues import (
    LLM_QUEUE,
    SCRAPE_HTTP_QUEUE,
    SCRAPE_PLAYWRIGHT_QUEUE,
    WORKFLOW_QUEUE,
)
from app.workflows.steps import (
    ACQUIRE_HOST_SLOT_ACTIVITY,
    RELEASE_HOST_SLOT_ACTIVITY,
    HostSlot,
    HostSlotInput,
    HostSlotRelease,
)

BUCKET = settings.minio_bucket
SCRAPE_PATH = f"{BUCKET}/history/r1/scrape.html"
SHOT_PATH = f"{BUCKET}/screenshots/r1/0.png"
LLM_PATH = f"{BUCKET}/history/r1/llm.json"
HASH = "00c0ffee12345678"

RUN_INPUT = JobRunInput(
    run_id="r1",
    engine="http",
    scrape=ScrapeInput(artifact_id="r1", url="https://example.com", output_format="html"),
)
KEY = JobLLM(
    provider="anthropic",
    encrypted_api_key="ciphertext",
    model="m",
    output_schema={"type": "object"},
)


def _scrape_output(screenshots=(), warnings=()):
    return ScrapeOutput.model_validate(
        {
            "result": {"path": SCRAPE_PATH, "size": 10},
            "content_hash": HASH,
            "warnings": list(warnings),
            "screenshots": [{"path": p, "size": 5} for p in screenshots],
        }
    )


async def _scrape_ok(input: ScrapeInput) -> ScrapeOutput:
    return _scrape_output()


async def _llm_ok(input: LLMInput) -> LLMOutput:
    return LLMOutput.model_validate({"result": {"path": LLM_PATH, "size": 3}})


class Harness:
    """Mocked activities on their real queues; records every DB-activity call in order."""

    def __init__(
        self, scrape=_scrape_ok, llm=_llm_ok, job_llm=None, replies=None, record=None, slots=()
    ):
        self.calls: list = []
        # Host-slot and scrape activities in call order, beside the DB calls.
        self.order: list[str] = []
        self.acquired: list[HostSlotInput] = []
        self.released: list[HostSlotRelease] = []
        self.asked = asyncio.Event()
        # What each acquire returns, in turn; then a granted slot.
        self.slots = list(slots)
        self.scraped: list[ScrapeInput] = []
        self.extracted: list[LLMInput] = []
        self.job_llm = job_llm or JobLLM()
        # Activity name -> the state it returns (default: the state the call moves the run to).
        self.replies: dict[str, str] = replies or {}
        self._scrape = scrape
        self._llm = llm
        self._record = record
        self._state = "pending"

    def activities(self):
        def reply(name, default):
            self._state = self.replies.get(name, default)
            return self._state

        @activity.defn(name=JOB_RUN_STARTED_ACTIVITY)
        async def started(input: JobRunRef) -> str:
            self.calls.append(("started", input.run_id))
            return reply(JOB_RUN_STARTED_ACTIVITY, "running")

        @activity.defn(name=READ_LLM_SETTINGS_ACTIVITY)
        async def load(input: JobRunRef) -> JobLLM:
            self.calls.append(("read_llm_settings", input.run_id))
            return self.job_llm

        @activity.defn(name=RECORD_JOB_OBJECTS_ACTIVITY)
        async def record(input: RecordJobObjectsInput) -> str:
            self.calls.append(input)
            self.order.append("record")
            if self._record is not None:
                return await self._record(input)
            return reply(RECORD_JOB_OBJECTS_ACTIVITY, self._state)

        @activity.defn(name=JOB_RUN_PROCESSING_ACTIVITY)
        async def processing(input: JobRunRef) -> str:
            self.calls.append(("processing", input.run_id))
            return reply(JOB_RUN_PROCESSING_ACTIVITY, "processing")

        @activity.defn(name=FINISH_JOB_RUN_ACTIVITY)
        async def finish(input: FinishJobRunInput) -> str:
            self.calls.append(input)
            return reply(FINISH_JOB_RUN_ACTIVITY, input.status)

        @activity.defn(name=ACQUIRE_HOST_SLOT_ACTIVITY)
        async def acquire(input: HostSlotInput) -> HostSlot:
            self.order.append("acquire")
            self.acquired.append(input)
            self.asked.set()
            return self.slots.pop(0) if self.slots else HostSlot(domain="example.com")

        @activity.defn(name=RELEASE_HOST_SLOT_ACTIVITY)
        async def release(input: HostSlotRelease) -> None:
            self.order.append("release")
            self.released.append(input)

        @activity.defn(name=SCRAPE_ACTIVITY)
        async def scrape(input: ScrapeInput) -> ScrapeOutput:
            self.order.append("scrape")
            self.scraped.append(input)
            return await self._scrape(input)

        @activity.defn(name=LLM_EXTRACT_ACTIVITY)
        async def llm(input: LLMInput) -> LLMOutput:
            self.extracted.append(input)
            return await self._llm(input)

        return [started, load, record, processing, finish, acquire, release], scrape, llm

    async def run(self, run_input=RUN_INPUT, during=None, timeout=timedelta(days=1)):
        db_activities, scrape, llm = self.activities()
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            async with (
                Worker(
                    env.client,
                    task_queue=WORKFLOW_QUEUE,
                    workflows=[JobWorkflow],
                    activities=db_activities,
                ),
                Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
                Worker(env.client, task_queue=SCRAPE_PLAYWRIGHT_QUEUE, activities=[scrape]),
                Worker(env.client, task_queue=LLM_QUEUE, activities=[llm]),
            ):
                handle = await env.client.start_workflow(
                    JobWorkflow.run,
                    run_input,
                    id=workflow_id(uuid.uuid4()),
                    task_queue=WORKFLOW_QUEUE,
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


def _record(objects, content_hash=None):
    return RecordJobObjectsInput(run_id="r1", objects=objects, content_hash=content_hash)


SCRAPED = [StoredObject(path=SCRAPE_PATH, size=10)]


# ---------------------------------------------------------------------------
# JobWorkflow (time-skipping env, activities mocked)
# ---------------------------------------------------------------------------


async def test_a_job_without_llm_is_scraped_charged_and_completed():
    async def scrape(input):
        return _scrape_output([SHOT_PATH], warnings=["action 1 timed out"])

    h = Harness(scrape=scrape)
    result = await h.run()

    assert result == SCRAPE_PATH
    assert h.scraped == [RUN_INPUT.scrape]
    assert h.extracted == []
    assert h.calls == [
        ("started", "r1"),
        ("read_llm_settings", "r1"),
        _record(
            [StoredObject(path=SCRAPE_PATH, size=10), StoredObject(path=SHOT_PATH, size=5)], HASH
        ),
        FinishJobRunInput(
            run_id="r1",
            status="completed",
            result_path=SCRAPE_PATH,
            warnings=["action 1 timed out"],
        ),
    ]


async def test_the_engine_picks_the_scrape_queue():
    seen = []

    async def scrape(input):
        seen.append(activity.info().task_queue)
        return _scrape_output()

    await Harness(scrape=scrape).run(RUN_INPUT.model_copy(update={"engine": "playwright"}))
    await Harness(scrape=scrape).run()
    assert seen == [SCRAPE_PLAYWRIGHT_QUEUE, SCRAPE_HTTP_QUEUE]


async def test_a_job_with_llm_goes_processing_and_completes_on_the_extraction():
    h = Harness(job_llm=KEY)
    result = await h.run()

    assert result == LLM_PATH
    [llm] = h.extracted
    assert (llm.artifact_id, llm.raw_minio_path, llm.encrypted_api_key, llm.model) == (
        "r1",
        SCRAPE_PATH,
        "ciphertext",
        "m",
    )
    assert h.calls == [
        ("started", "r1"),
        ("read_llm_settings", "r1"),
        _record(SCRAPED, HASH),
        ("processing", "r1"),
        _record([StoredObject(path=LLM_PATH, size=3)]),
        FinishJobRunInput(run_id="r1", status="completed", result_path=LLM_PATH),
    ]


async def test_a_failed_scrape_fails_the_run_with_the_workers_message():
    async def scrape(input):
        raise ApplicationError("blocked:akamai", type="ScrapeFailed", non_retryable=True)

    h = Harness(scrape=scrape)
    result = await h.run()

    assert isinstance(result, WorkflowFailureError)
    assert h.calls == [
        ("started", "r1"),
        FinishJobRunInput(run_id="r1", status="failed", error="blocked:akamai"),
    ]


async def test_a_deleted_llm_key_fails_the_run_and_drops_the_uncharged_scrape():
    async def scrape(input):
        return _scrape_output([SHOT_PATH])

    h = Harness(scrape=scrape, job_llm=JobLLM(error="LLM key not found or deleted"))
    result = await h.run()

    assert isinstance(result, WorkflowFailureError)
    assert h.extracted == []
    assert h.calls[-1] == FinishJobRunInput(
        run_id="r1",
        status="failed",
        error="LLM key not found or deleted",
        discard=[SCRAPE_PATH, SHOT_PATH],
    )
    assert not any(isinstance(c, RecordJobObjectsInput) for c in h.calls)


async def test_a_failed_llm_fails_the_run_and_keeps_the_charged_scrape():
    async def llm(input):
        raise ApplicationError("401 invalid key", type="LLMFailed", non_retryable=True)

    h = Harness(llm=llm, job_llm=KEY)
    result = await h.run()

    assert isinstance(result, WorkflowFailureError)
    assert h.calls[-1] == FinishJobRunInput(run_id="r1", status="failed", error="401 invalid key")


async def test_a_failed_accounting_fails_the_run_and_drops_the_objects():
    async def record(input):
        raise ApplicationError("db down", non_retryable=True)

    h = Harness(record=record)
    result = await h.run()

    assert isinstance(result, WorkflowFailureError)
    assert h.calls[-1] == FinishJobRunInput(
        run_id="r1", status="failed", error=ACCOUNTING_FAILED, discard=[SCRAPE_PATH]
    )


async def test_the_storage_wall_fails_the_run_through_the_record_mirror():
    h = Harness(job_llm=KEY, replies={RECORD_JOB_OBJECTS_ACTIVITY: "failed"})
    result = await h.run()

    assert isinstance(result, WorkflowFailureError)
    # The mirror failed the run itself (v1's job-lane rule); nothing after it runs.
    assert h.extracted == []
    assert not any(isinstance(c, FinishJobRunInput) for c in h.calls)


async def test_a_run_cancelled_before_it_started_is_never_scraped():
    h = Harness(replies={JOB_RUN_STARTED_ACTIVITY: "cancelled"})
    assert await h.run() is None
    assert h.scraped == []
    assert h.calls == [("started", "r1")]


async def test_an_engine_cancel_during_the_start_mirror_never_scrapes():
    release = asyncio.Event()

    class Slow(Harness):
        def activities(self):
            db, scrape, llm = super().activities()

            @activity.defn(name=JOB_RUN_STARTED_ACTIVITY)
            async def started(input: JobRunRef) -> str:
                self.calls.append(("started", input.run_id))
                await release.wait()
                return "running"

            return [started, *db[1:]], scrape, llm

    async def cancel_during_start(handle):
        await asyncio.sleep(0.5)
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Slow()
    result = await h.run(during=cancel_during_start)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, CancelledError)
    assert h.scraped == []
    assert h.calls == [("started", "r1"), FinishJobRunInput(run_id="r1", status="cancelled")]


async def test_a_row_cancel_during_the_scrape_stops_at_the_record_mirror():
    h = Harness(job_llm=KEY, replies={RECORD_JOB_OBJECTS_ACTIVITY: "cancelled"})
    assert await h.run() is None
    assert h.extracted == []
    assert not any(isinstance(c, FinishJobRunInput) for c in h.calls)


async def test_a_workflow_cancel_lets_the_scrape_finish_then_drops_its_objects():
    started, release = asyncio.Event(), asyncio.Event()

    async def scrape(input):
        started.set()
        await release.wait()
        return _scrape_output([SHOT_PATH])

    async def cancel_mid_scrape(handle):
        await started.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(scrape=scrape, job_llm=KEY, replies={FINISH_JOB_RUN_ACTIVITY: "cancelled"})
    result = await h.run(during=cancel_mid_scrape)

    assert isinstance(result, WorkflowFailureError)
    assert isinstance(result.cause, CancelledError)
    assert h.extracted == []
    assert h.calls == [
        ("started", "r1"),
        FinishJobRunInput(run_id="r1", status="cancelled", discard=[SCRAPE_PATH, SHOT_PATH]),
    ]


# --- E.9: the per-host slot around the scrape ---------------------------------------------------

BUSY = HostSlot(domain="example.com", wait_ms=1000)


async def test_the_scrape_runs_inside_a_host_slot():
    h = Harness()
    assert await h.run() == SCRAPE_PATH
    assert h.order == ["acquire", "scrape", "release", "record"]
    (acquired,) = h.acquired
    assert acquired.url == "https://example.com"
    ttl = steps.host_lease_ttl(RUN_INPUT.scrape, "http")
    assert acquired.lease_ttl_ms == ttl.total_seconds() * 1000
    assert h.released == [HostSlotRelease(domain="example.com", lease_id=acquired.lease_id)]


async def test_only_the_scrape_gets_the_five_minute_queue_timer():
    h = Harness(job_llm=KEY)
    await h.run()
    timers = {
        e.activity_task_scheduled_event_attributes.activity_type.name: (
            e.activity_task_scheduled_event_attributes.schedule_to_start_timeout.ToTimedelta()
        )
        for e in h.history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    }
    assert timers[SCRAPE_ACTIVITY] == timedelta(minutes=5)
    assert timers[LLM_EXTRACT_ACTIVITY] == timedelta(seconds=60)


async def test_a_busy_host_delays_the_scrape():
    h = Harness(slots=[BUSY, BUSY])
    assert await h.run() == SCRAPE_PATH
    assert h.order == ["acquire", "acquire", "acquire", "scrape", "release", "record"]


async def test_a_failed_scrape_still_frees_the_host():
    async def scrape(input):
        raise ApplicationError("blocked:akamai", type="ScrapeFailed", non_retryable=True)

    h = Harness(scrape=scrape)
    assert isinstance(await h.run(), WorkflowFailureError)
    assert h.order == ["acquire", "scrape", "release"]


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
    assert h.calls == [("started", "r1"), FinishJobRunInput(run_id="r1", status="cancelled")]


async def test_a_cancel_during_the_scrape_still_frees_the_host():
    started, release = asyncio.Event(), asyncio.Event()

    async def scrape(input):
        started.set()
        await release.wait()
        return _scrape_output()

    async def cancel_mid_scrape(handle):
        await started.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    h = Harness(scrape=scrape, replies={FINISH_JOB_RUN_ACTIVITY: "cancelled"})
    result = await h.run(during=cancel_mid_scrape)

    assert isinstance(result, WorkflowFailureError)
    assert h.order == ["acquire", "scrape", "release"]
    assert h.calls[-1] == FinishJobRunInput(run_id="r1", status="cancelled", discard=[SCRAPE_PATH])


async def test_an_unexpected_exception_fails_the_run_instead_of_hanging():
    # A provider with no key: LLMInput refuses it inside the workflow.
    h = Harness(job_llm=JobLLM(provider="anthropic", model="m", output_schema={}))
    result = await h.run(timeout=timedelta(seconds=30))

    assert isinstance(result, WorkflowFailureError)
    assert result.cause.non_retryable
    last = h.calls[-1]
    assert (last.status, last.error.startswith("internal error: ValidationError")) == (
        "failed",
        True,
    )


# ---------------------------------------------------------------------------
# The DB activities, against the dev DB and MinIO
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _test_minio(monkeypatch):
    monkeypatch.setattr(job_runs, "_minio", app.state.minio)


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


def _key(stage: str = "scrape.html") -> str:
    return f"{BUCKET}/history/{uuid.uuid4()}/{stage}"


async def _job_run(user_id, status="pending", **job_fields) -> JobRun:
    async with AsyncSessionLocal() as db:
        job = Job(user_id=user_id, url="https://example.com", **job_fields)
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status=status, lane="v2")
        db.add(run)
        await db.commit()
        await db.refresh(run)
    return run


async def _row(run_id) -> JobRun:
    async with AsyncSessionLocal() as db:
        return await db.get(JobRun, run_id)


async def _deliveries(run_id) -> list[WebhookDelivery]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.scalars(select(WebhookDelivery).where(WebhookDelivery.run_id == run_id))
            ).all()
        )


async def _ledger(paths) -> list[str]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.scalars(
                    select(StorageObject.object_key).where(StorageObject.object_key.in_(paths))
                )
            ).all()
        )


async def test_started_moves_pending_to_running_once(db_user):
    run = await _job_run(db_user.id)
    ref = JobRunRef(run_id=str(run.id))

    assert await job_run_started(ref) == "running"
    first = (await _row(run.id)).started_at
    assert first is not None
    assert await job_run_started(ref) == "running"  # a retried activity
    assert (await _row(run.id)).started_at == first


async def test_no_mirror_moves_a_cancelled_run(db_user):
    run = await _job_run(db_user.id, status="cancelled")
    ref = JobRunRef(run_id=str(run.id))

    assert await job_run_started(ref) == "cancelled"
    assert await job_run_processing(ref) == "cancelled"
    assert (
        await finish_job_run(
            FinishJobRunInput(run_id=str(run.id), status="completed", result_path=SCRAPE_PATH)
        )
        == "cancelled"
    )
    row = await _row(run.id)
    assert (row.status, row.result_path, row.started_at) == ("cancelled", None, None)


async def test_a_deleted_run_is_gone_to_every_mirror():
    ref = JobRunRef(run_id=str(uuid.uuid4()))
    assert await job_run_started(ref) == "gone"
    assert await job_run_processing(ref) == "gone"
    assert await record_job_objects(RecordJobObjectsInput(run_id=ref.run_id, objects=[])) == "gone"
    assert await finish_job_run(FinishJobRunInput(run_id=ref.run_id, status="failed")) == "gone"


async def test_every_transition_notifies_the_job_status_channel(db_user):
    run = await _job_run(db_user.id, llm_config=None)
    ref = JobRunRef(run_id=str(run.id))
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener("job_status", lambda *a: received.put_nowait(a[3]))
    try:
        await job_run_started(ref)
        await job_run_processing(ref)
        await finish_job_run(FinishJobRunInput(run_id=ref.run_id, status="failed", error="e"))
        payloads = [await asyncio.wait_for(received.get(), 5) for _ in range(3)]
    finally:
        await conn.close()
    prefix = f"{run.job_id}:{run.id}"
    assert payloads == [f"{prefix}:running", f"{prefix}:processing", f"{prefix}:failed"]


async def test_record_charges_once_and_stores_the_content_hash(db_user):
    run = await _job_run(db_user.id, status="running")
    key = _key()
    arg = RecordJobObjectsInput(
        run_id=str(run.id), objects=[StoredObject(path=key, size=1234)], content_hash=HASH
    )

    assert await record_job_objects(arg) == "running"
    assert await record_job_objects(arg) == "running"  # a retried activity

    async with AsyncSessionLocal() as db:
        rows = (
            await db.scalars(select(StorageObject).where(StorageObject.object_key == key))
        ).all()
        quota = await db.get(UserQuota, db_user.id)
    assert [(r.job_run_id, r.bytes) for r in rows] == [(run.id, 1234)]
    assert quota.storage_bytes_used == 1234
    assert (await _row(run.id)).content_hash == HASH


async def test_record_charges_every_object_of_the_stage(db_user):
    run = await _job_run(db_user.id, status="running")
    page, shot = _key(), _key("0.png")

    await record_job_objects(
        RecordJobObjectsInput(
            run_id=str(run.id),
            objects=[StoredObject(path=page, size=300), StoredObject(path=shot, size=40)],
        )
    )

    assert sorted(await _ledger([page, shot])) == sorted([page, shot])
    async with AsyncSessionLocal() as db:
        assert (await db.get(UserQuota, db_user.id)).storage_bytes_used == 340


async def test_objects_that_fill_the_limit_exactly_are_charged(db_user):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=db_user.id, storage_bytes_limit=100))
        await db.commit()
    run = await _job_run(db_user.id, status="running")
    key = _key()

    state = await record_job_objects(
        RecordJobObjectsInput(run_id=str(run.id), objects=[StoredObject(path=key, size=100)])
    )

    assert state == "running"
    assert await _ledger([key]) == [key]


async def test_over_the_storage_limit_the_objects_go_and_the_run_fails(db_user):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=db_user.id, storage_bytes_limit=100))
        await db.commit()
    run = await _job_run(db_user.id, status="running")
    page, shot = await _put(_key(), 80), await _put(_key("0.png"), 80)

    state = await record_job_objects(
        RecordJobObjectsInput(
            run_id=str(run.id),
            objects=[StoredObject(path=page, size=80), StoredObject(path=shot, size=80)],
        )
    )

    assert state == "failed"
    row = await _row(run.id)
    assert (row.status, row.error) == ("failed", "storage_quota_exceeded")
    assert not await _exists(page)
    assert not await _exists(shot)
    assert await _ledger([page, shot]) == []


async def test_a_stopped_run_gets_its_objects_dropped_uncharged(db_user):
    run = await _job_run(db_user.id, status="cancelled")
    page = await _put(_key())

    state = await record_job_objects(
        RecordJobObjectsInput(run_id=str(run.id), objects=[StoredObject(path=page, size=10)])
    )

    assert state == "cancelled"
    assert not await _exists(page)
    assert await _ledger([page]) == []


async def test_finish_completed_writes_the_row_and_the_webhook_row(db_user):
    run = await _job_run(db_user.id, status="running", webhook_url="https://hooks.example.com/in")

    state = await finish_job_run(
        FinishJobRunInput(
            run_id=str(run.id), status="completed", result_path=SCRAPE_PATH, warnings=["w"]
        )
    )

    assert state == "completed"
    row = await _row(run.id)
    assert (row.status, row.result_path, row.warnings, row.completed_at is not None) == (
        "completed",
        SCRAPE_PATH,
        ["w"],
        True,
    )
    [delivery] = await _deliveries(run.id)
    assert (delivery.event, delivery.status, delivery.webhook_url) == (
        "job.completed",
        "pending",
        "https://hooks.example.com/in",
    )
    payload = delivery.payload
    assert (payload["job_id"], payload["run_id"], payload["result_path"]) == (
        str(run.job_id),
        str(run.id),
        SCRAPE_PATH,
    )
    # Change detection is not ported to v2 (owner, 2026-10-12): the fields stay, empty.
    assert (payload["diff_detected"], payload["diff_summary"]) == (None, None)


async def test_finish_failed_writes_the_error_and_a_job_failed_row(db_user):
    run = await _job_run(db_user.id, status="processing", webhook_url="https://h.example.com")

    state = await finish_job_run(
        FinishJobRunInput(run_id=str(run.id), status="failed", error="401 invalid key")
    )

    assert state == "failed"
    row = await _row(run.id)
    assert (row.status, row.error, row.result_path) == ("failed", "401 invalid key", None)
    [delivery] = await _deliveries(run.id)
    assert (delivery.event, delivery.payload["error"]) == ("job.failed", "401 invalid key")


async def test_the_webhook_event_filter_is_respected(db_user):
    run = await _job_run(
        db_user.id,
        status="running",
        webhook_url="https://h.example.com",
        webhook_events=["job.failed"],
    )
    await finish_job_run(
        FinishJobRunInput(run_id=str(run.id), status="completed", result_path=SCRAPE_PATH)
    )
    assert await _deliveries(run.id) == []


async def test_finish_cancelled_writes_no_webhook_row(db_user):
    run = await _job_run(db_user.id, status="running", webhook_url="https://h.example.com")
    assert await finish_job_run(FinishJobRunInput(run_id=str(run.id), status="cancelled")) == (
        "cancelled"
    )
    assert (await _row(run.id)).status == "cancelled"
    assert await _deliveries(run.id) == []


async def test_discard_spares_an_object_the_ledger_holds(db_user):
    run = await _job_run(db_user.id, status="running")
    charged, loose = await _put(_key()), await _put(_key("0.png"))
    await record_job_objects(
        RecordJobObjectsInput(run_id=str(run.id), objects=[StoredObject(path=charged, size=10)])
    )

    await finish_job_run(
        FinishJobRunInput(run_id=str(run.id), status="failed", error="e", discard=[charged, loose])
    )

    assert await _exists(charged)
    assert not await _exists(loose)
    await _remove(charged)


async def test_read_llm_settings_reads_the_job_and_its_key_now(db_user):
    async with AsyncSessionLocal() as db:
        key = UserLLMKey(
            user_id=db_user.id,
            name=f"k-{uuid.uuid4().hex[:6]}",
            provider="openai_compatible",
            encrypted_api_key="ciphertext",
            base_url="https://llm.example.com/v1",
        )
        db.add(key)
        await db.commit()
        await db.refresh(key)
    config = {"llm_key_id": str(key.id), "model": "m", "output_schema": {"type": "object"}}
    with_llm = await _job_run(db_user.id, llm_config=config)
    without = await _job_run(db_user.id)
    missing = await _job_run(db_user.id, llm_config={**config, "llm_key_id": str(uuid.uuid4())})

    assert await read_llm_settings(JobRunRef(run_id=str(with_llm.id))) == JobLLM(
        provider="openai_compatible",
        encrypted_api_key="ciphertext",
        base_url="https://llm.example.com/v1",
        model="m",
        output_schema={"type": "object"},
    )
    assert await read_llm_settings(JobRunRef(run_id=str(without.id))) == JobLLM()
    assert await read_llm_settings(JobRunRef(run_id=str(missing.id))) == JobLLM(
        error="LLM key not found or deleted"
    )


# ---------------------------------------------------------------------------
# End to end on the real DB activities (scrape and LLM mocked)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def worker_redis(monkeypatch):
    # A fresh client per test: the module-level one would outlive the test's event loop.
    client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    monkeypatch.setattr(host_slots, "_redis", client)
    yield client
    await client.aclose()


async def _run_real(run: JobRun, scrape, during=None):
    @activity.defn(name=SCRAPE_ACTIVITY)
    async def scrape_activity(input: ScrapeInput) -> ScrapeOutput:
        return await scrape(input)

    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm(input: LLMInput) -> LLMOutput:
        raise AssertionError("no LLM step on this job")

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=WORKFLOW_QUEUE,
                workflows=[JobWorkflow],
                activities=[
                    job_run_started,
                    read_llm_settings,
                    record_job_objects,
                    job_run_processing,
                    finish_job_run,
                    host_slots.acquire_host_slot,
                    host_slots.release_host_slot,
                ],
            ),
            Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape_activity]),
            Worker(env.client, task_queue=LLM_QUEUE, activities=[llm]),
        ):
            handle = await env.client.start_workflow(
                JobWorkflow.run,
                JobRunInput(
                    run_id=str(run.id),
                    engine="http",
                    scrape=ScrapeInput(
                        artifact_id=str(run.id), url="https://example.com", output_format="html"
                    ),
                ),
                id=workflow_id(run.id),
                task_queue=WORKFLOW_QUEUE,
            )
            if during:
                await during(handle)
            return await handle.result()


async def test_a_job_run_completes_end_to_end_on_the_real_mirrors(db_user, worker_redis):
    run = await _job_run(db_user.id)
    page = await _put(f"{BUCKET}/history/{run.id}/scrape.html")
    leases, _ = host_limiter._keys("example.com")
    held_during_scrape = []

    async def scrape(input):
        held_during_scrape.append(await worker_redis.zcard(leases))
        return ScrapeOutput.model_validate(
            {"result": {"path": page, "size": 10}, "content_hash": HASH}
        )

    assert await _run_real(run, scrape) == page
    row = await _row(run.id)
    assert (row.status, row.result_path, row.content_hash) == ("completed", page, HASH)
    assert await _ledger([page]) == [page]
    # The real limiter: a lease in Redis while the scrape ran, gone after.
    assert held_during_scrape == [1]
    assert await worker_redis.zcard(leases) == 0
    await _remove(page)


async def test_a_row_cancel_mid_scrape_drops_the_object_and_charges_nothing(db_user):
    """The API's cancel (D.4) writes the row; the engine need not be told."""
    run = await _job_run(db_user.id)
    page = await _put(f"{BUCKET}/history/{run.id}/scrape.html")
    started, release = asyncio.Event(), asyncio.Event()

    async def scrape(input):
        started.set()
        await release.wait()
        return ScrapeOutput.model_validate(
            {"result": {"path": page, "size": 10}, "content_hash": HASH}
        )

    async def cancel_row(handle):
        await started.wait()
        async with AsyncSessionLocal() as db:
            row = await db.get(JobRun, run.id)
            row.status = "cancelled"
            await db.commit()
        release.set()

    assert await _run_real(run, scrape, during=cancel_row) is None
    row = await _row(run.id)
    assert (row.status, row.result_path) == ("cancelled", None)
    assert not await _exists(page)
    assert await _ledger([page]) == []


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_a_mirror_never_overwrites_a_terminal_status_it_did_not_set(db_user, status):
    run = await _job_run(db_user.id, status=status)
    assert await job_run_started(JobRunRef(run_id=str(run.id))) == status
    assert (await _row(run.id)).status == status
