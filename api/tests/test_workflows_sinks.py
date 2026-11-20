"""M.7 — sinks in PipelineWorkflow: the destination load before each try, the delivery record."""

import uuid

import pytest
from sqlalchemy import update
from temporalio.exceptions import ApplicationError

from app.core.db import AsyncSessionLocal
from app.core.destinations import encrypt_secret
from app.models.destination import Destination
from app.models.pipeline import PipelineRunBlock
from app.workflows.activities.db import load_destination, mirror_block_status
from app.workflows.pipeline import (
    DESTINATION_GONE_ERROR,
    RUN_CANCELLED_ERROR,
    LoadDestinationInput,
    MirrorBlockInput,
)
from tests.test_workflows_pipeline import _block, _run_rows

BUCKET_ADDRESS = {
    "endpoint": "https://s3.example.com/",
    "bucket": "exports",
    "prefix": "scrapeflow",
    "region": None,
}
LOCATION = {
    "endpoint": "https://s3.example.com/",
    "bucket": "exports",
    "region": None,
    "key": "scrapeflow/p1/r1/store.json",
}


async def _bucket_destination(user_id, kind="bucket") -> Destination:
    async with AsyncSessionLocal() as db:
        d = Destination(
            user_id=user_id,
            name=f"b-{uuid.uuid4().hex[:6]}",
            kind=kind,
            address=BUCKET_ADDRESS if kind == "bucket" else {},
            encrypted_secret=encrypt_secret({"access_key": "a", "secret_key": "s"}),
            display_host="s3.example.com",
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
    return d


def _load(run_id, destination_id, kind="bucket"):
    return load_destination(
        LoadDestinationInput(run_id=str(run_id), destination_id=str(destination_id), kind=kind)
    )


# --- load_destination (workflow queue) ---------------------------------------------------------


async def test_load_destination_returns_the_current_address_and_ciphertext(db_user):
    run = await _run_rows(db_user.id)
    destination = await _bucket_destination(db_user.id)

    loaded = await _load(run.id, destination.id)
    assert (loaded.address, loaded.encrypted_secret) == (
        BUCKET_ADDRESS,
        destination.encrypted_secret,
    )

    # Each try reads it again: an edit takes effect on the next one (ADR-015 §2b).
    moved = {**BUCKET_ADDRESS, "bucket": "elsewhere"}
    async with AsyncSessionLocal() as db:
        row = await db.get(Destination, destination.id)
        row.address = moved
        await db.commit()
    assert (await _load(run.id, destination.id)).address == moved


async def test_a_deleted_destination_is_final(db_user):
    run = await _run_rows(db_user.id)
    destination = await _bucket_destination(db_user.id)
    async with AsyncSessionLocal() as db:
        await db.delete(await db.get(Destination, destination.id))
        await db.commit()

    with pytest.raises(ApplicationError) as exc:
        await _load(run.id, destination.id)
    assert (exc.value.type, exc.value.non_retryable, exc.value.message) == (
        DESTINATION_GONE_ERROR,
        True,
        "destination deleted",
    )


async def test_another_users_destination_reads_as_deleted(db_user, other_user):
    run = await _run_rows(db_user.id)
    destination = await _bucket_destination(other_user.id)
    with pytest.raises(ApplicationError) as exc:
        await _load(run.id, destination.id)
    assert (exc.value.type, exc.value.message) == (DESTINATION_GONE_ERROR, "destination deleted")


async def test_a_destination_of_another_kind_is_final(db_user):
    run = await _run_rows(db_user.id)
    destination = await _bucket_destination(db_user.id, kind="smtp")
    with pytest.raises(ApplicationError) as exc:
        await _load(run.id, destination.id)
    assert (exc.value.type, exc.value.non_retryable, exc.value.message) == (
        DESTINATION_GONE_ERROR,
        True,
        "destination is a smtp destination, not bucket",
    )


async def test_a_deleted_run_reads_as_cancelled(db_user):
    destination = await _bucket_destination(db_user.id)
    with pytest.raises(ApplicationError) as exc:
        await _load(uuid.uuid4(), destination.id)
    assert (exc.value.type, exc.value.non_retryable) == (RUN_CANCELLED_ERROR, True)


# --- the delivery record (mirror_block_status, completed) --------------------------------------


async def test_a_completed_sink_records_where_it_delivered(db_user):
    run = await _run_rows(db_user.id, (("store", "bucket", "running"),))
    record = {"destination_id": str(uuid.uuid4()), "kind": "bucket", "location": LOCATION}

    await mirror_block_status(
        MirrorBlockInput(
            run_id=str(run.id),
            block_id="store",
            status="completed",
            output_ref="scrapeflow-results/history/a/llm.json",
            delivery=record,
        )
    )
    assert (await _block(run.id, "store")).delivery == record


async def test_a_block_that_delivered_nothing_records_no_delivery(db_user):
    run = await _run_rows(db_user.id, (("store", "bucket", "running"),))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(PipelineRunBlock.pipeline_run_id == run.id)
            .values(delivery={"stale": True})
        )
        await db.commit()
    await mirror_block_status(
        MirrorBlockInput(run_id=str(run.id), block_id="store", status="completed")
    )
    assert (await _block(run.id, "store")).delivery is None
