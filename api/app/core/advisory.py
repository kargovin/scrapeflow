import asyncio
import json
from datetime import UTC, datetime

import structlog
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.constants import NATS_ADVISORY_MAX_DELIVER_SUBJECT
from app.models.job_runs import JobRun

logger = structlog.get_logger()


async def _handle_advisory(msg: Msg, db_factory: async_sessionmaker[AsyncSession]) -> None:
    """Process a single MaxDeliver advisory: find the stalled run and mark it failed."""
    try:
        data = json.loads(msg.data.decode())
        stream_seq = data["stream_seq"]
    except (KeyError, json.JSONDecodeError) as e:
        logger.error("Malformed MaxDeliver advisory, discarding", error=str(e))
        return

    async with db_factory() as db:
        result = await db.execute(
            select(JobRun).where(
                JobRun.nats_stream_seq == stream_seq,
                JobRun.status.in_(["pending", "running", "processing"]),
            )
        )
        run = result.scalar_one_or_none()
        if run is None:
            logger.warning(
                "Advisory received but no matching active run found", stream_seq=stream_seq
            )
            return

        run.status = "failed"
        run.error = "Max NATS redeliveries exceeded"
        run.completed_at = datetime.now(UTC)
        await db.commit()
        logger.info(
            "Marked run failed via MaxDeliver advisory",
            run_id=str(run.id),
            stream_seq=stream_seq,
        )


async def maxdeliver_advisory_subscriber(
    nats_client: NATSClient,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Subscribe to NATS MaxDeliver advisories and fail stalled job runs."""

    async def _on_msg(msg: Msg) -> None:
        try:
            await _handle_advisory(msg, db_factory)
        except Exception:
            logger.exception("Unhandled error processing MaxDeliver advisory")

    sub = await nats_client.subscribe(NATS_ADVISORY_MAX_DELIVER_SUBJECT, cb=_on_msg)

    try:
        await asyncio.Future()  # run until cancelled
    except asyncio.CancelledError:
        await sub.unsubscribe()
        raise
