"""Migration 4.13 (M.1): destinations, the versions that refer to them, and destination-backed
delivery rows, as the database enforces them (ADR-015 §2a, §2b, §8)."""

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal
from app.models.destination import DESTINATION_KINDS, Destination, PipelineVersionDestination
from app.models.pipeline import Pipeline, PipelineRun, PipelineVersion
from app.models.user import User
from app.models.webhook_delivery import WebhookDelivery


def _destination(user_id, name="d", kind="webhook", **fields) -> Destination:
    return Destination(
        user_id=user_id,
        name=name,
        kind=kind,
        encrypted_secret="ciphertext",
        display_host="hooks.example.net",
        **fields,
    )


async def _saved(user_id, name="d", kind="webhook") -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        destination = _destination(user_id, name, kind)
        db.add(destination)
        await db.commit()
        return destination.id


async def _version(user_id) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        pipeline = Pipeline(user_id=user_id, name=f"dest-{uuid.uuid4().hex[:8]}")
        db.add(pipeline)
        await db.flush()
        version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition={"blocks": []})
        db.add(version)
        await db.commit()
        return version.id


async def _run(user_id) -> uuid.UUID:
    version_id = await _version(user_id)
    async with AsyncSessionLocal() as db:
        version = await db.get(PipelineVersion, version_id)
        run_id = uuid.uuid4()
        db.add(
            PipelineRun(
                id=run_id,
                pipeline_id=version.pipeline_id,
                pipeline_version_id=version_id,
                user_id=user_id,
                status="failed",
                workflow_id=f"pipeline-run-{run_id}",
            )
        )
        await db.commit()
    return run_id


def _alert(pipeline_run_id, **fields) -> WebhookDelivery:
    fields.setdefault("webhook_url", "https://hooks.example.net/in")
    return WebhookDelivery(
        pipeline_run_id=pipeline_run_id,
        block_id="notify",
        event="pipeline.failed",
        payload={"event": "pipeline.failed"},
        **fields,
    )


async def _refused(constraint: str, *rows) -> None:
    async with AsyncSessionLocal() as db:
        db.add_all(rows)
        with pytest.raises(IntegrityError) as err:
            await db.commit()
    assert constraint in str(err.value)


# ---------------------------------------------------------------------------
# destinations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", DESTINATION_KINDS)
async def test_every_kind_is_admitted_with_an_empty_address_by_default(db_user, kind):
    destination_id = await _saved(db_user.id, kind=kind)
    async with AsyncSessionLocal() as db:
        destination = await db.get(Destination, destination_id)
        assert (destination.kind, destination.address) == (kind, {})
        assert destination.created_at and destination.updated_at


async def test_an_unknown_kind_is_refused(db_user):
    await _refused("ck_destinations_kind", _destination(db_user.id, kind="sheet"))


async def test_a_name_is_unique_per_user_not_across_users(db_user, other_user):
    await _saved(db_user.id, "alerts")
    await _saved(other_user.id, "alerts")
    await _refused("uq_destinations_user_id_name", _destination(db_user.id, "alerts", "bucket"))


async def test_destinations_go_with_their_user():
    user = User(clerk_id=f"user_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.com")
    async with AsyncSessionLocal() as db:
        db.add(user)
        await db.commit()
    destination_id = await _saved(user.id)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()
        assert await db.get(Destination, destination_id) is None


# ---------------------------------------------------------------------------
# pipeline_version_destinations
# ---------------------------------------------------------------------------


async def test_a_version_refers_to_a_destination_once(db_user):
    version_id, destination_id = await _version(db_user.id), await _saved(db_user.id)
    async with AsyncSessionLocal() as db:
        db.add(
            PipelineVersionDestination(
                pipeline_version_id=version_id, destination_id=destination_id
            )
        )
        await db.commit()
    await _refused(
        "pipeline_version_destinations_pkey",
        PipelineVersionDestination(pipeline_version_id=version_id, destination_id=destination_id),
    )


async def test_deleting_a_destination_or_a_version_removes_the_reference(db_user):
    kept = await _saved(db_user.id, "kept")
    gone = await _saved(db_user.id, "gone")
    version_id = await _version(db_user.id)
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                PipelineVersionDestination(pipeline_version_id=version_id, destination_id=kept),
                PipelineVersionDestination(pipeline_version_id=version_id, destination_id=gone),
            ]
        )
        await db.commit()
        await db.execute(delete(Destination).where(Destination.id == gone))
        await db.commit()
        refs = select(PipelineVersionDestination.destination_id).where(
            PipelineVersionDestination.pipeline_version_id == version_id
        )
        assert list(await db.scalars(refs)) == [kept]
        await db.execute(delete(PipelineVersion).where(PipelineVersion.id == version_id))
        await db.commit()
        assert list(await db.scalars(refs)) == []
        assert await db.get(Destination, kept) is not None


# ---------------------------------------------------------------------------
# webhook_deliveries: kind and destination
# ---------------------------------------------------------------------------


async def test_an_existing_kind_of_row_is_a_webhook_by_default(db_user):
    run_id = await _run(db_user.id)
    async with AsyncSessionLocal() as db:
        alert = _alert(run_id)
        db.add(alert)
        await db.commit()
        await db.refresh(alert)
        assert (alert.kind, alert.destination_id) == ("webhook", None)


@pytest.mark.parametrize("kind", ["webhook", "email"])
async def test_a_destination_row_has_no_url(db_user, kind):
    run_id = await _run(db_user.id)
    destination_id = await _saved(db_user.id, kind="smtp" if kind == "email" else "webhook")
    async with AsyncSessionLocal() as db:
        db.add(_alert(run_id, webhook_url=None, destination_id=destination_id, kind=kind))
        await db.commit()


async def test_a_row_never_has_both_a_url_and_a_destination(db_user):
    run_id = await _run(db_user.id)
    destination_id = await _saved(db_user.id)
    await _refused(
        "ck_webhook_deliveries_one_target", _alert(run_id, destination_id=destination_id)
    )


async def test_an_email_row_has_no_url(db_user):
    await _refused(
        "ck_webhook_deliveries_email_no_url", _alert(await _run(db_user.id), kind="email")
    )


async def test_an_unknown_delivery_kind_is_refused(db_user):
    await _refused(
        "ck_webhook_deliveries_kind",
        _alert(await _run(db_user.id), webhook_url=None, kind="sms"),
    )


async def test_deleting_a_destination_keeps_its_delivery_rows(db_user):
    run_id = await _run(db_user.id)
    destination_id = await _saved(db_user.id)
    async with AsyncSessionLocal() as db:
        alert = _alert(run_id, webhook_url=None, destination_id=destination_id)
        db.add(alert)
        await db.commit()
        await db.execute(delete(Destination).where(Destination.id == destination_id))
        await db.commit()
        await db.refresh(alert)
        assert (alert.destination_id, alert.webhook_url) == (None, None)
