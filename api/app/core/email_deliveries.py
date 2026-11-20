"""A ``kind = 'email'`` row of ``webhook_deliveries`` as a message for ``send_email`` (ADR-015 §4, §5).

The row stays the record; its destination is read at each attempt. One arm per owner: an owner with
no arm raises, as the webhook secret lookup does.
"""

import json

from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.exceptions import ApplicationError

from app.models.destination import Destination
from app.models.pipeline import Pipeline, PipelineRun
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities.contracts import SendEmailInput


class Undeliverable(Exception):
    """The row can never be sent as it stands (its destination is gone or not an email one)."""


def _one_line(text: str) -> str:
    return " ".join(text.split())


async def email_for(db: AsyncSession, delivery: WebhookDelivery) -> SendEmailInput:
    """Raises Undeliverable, or ApplicationError (non-retryable) for an owner with no arm."""
    if delivery.pipeline_run_id is not None:
        run = await db.get(PipelineRun, delivery.pipeline_run_id)
        if run is None:
            raise Undeliverable("the run was deleted")
        pipeline = await db.get(Pipeline, run.pipeline_id)
        owner_id = run.user_id
        # <{run_id}.{block_id}.failed@host> for the failure alert (ADR-015 §5).
        local = f"{run.id}.{delivery.block_id}.{delivery.event.removeprefix('pipeline.')}"
        name = _one_line(pipeline.name) if pipeline else str(run.pipeline_id)
        subject = f"[ScrapeFlow] {name}: {delivery.event}"
        heading = f"{delivery.event} — pipeline {name}, run {run.id}"
        run_id = str(run.id)
    else:
        raise ApplicationError(
            f"email delivery {delivery.id} has no owner it can send for", non_retryable=True
        )

    destination = (
        await db.get(Destination, delivery.destination_id) if delivery.destination_id else None
    )
    if destination is None or destination.user_id != owner_id:
        raise Undeliverable("destination deleted")
    if destination.kind != "smtp":
        raise Undeliverable(f"destination is a {destination.kind} destination, not smtp")

    return SendEmailInput(
        address=destination.address,
        encrypted_secret=destination.encrypted_secret,
        message_id=f"<{local}@{settings.public_host}>",
        subject=subject,
        body=f"{heading}\n\n{json.dumps(delivery.payload, indent=2)}\n",
        run_id=run_id,
    )
