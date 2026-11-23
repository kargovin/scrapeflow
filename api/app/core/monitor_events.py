"""A monitor's own history (ADR-016 §7b): one ``monitor_events`` row per situation, and its notice (§8).

Every write happens under ``quota.lock_user`` — "find the open row, then bump it or insert one"
is a read followed by a write. A new situation, or a point event (``record_point``), of a
``NOTICE_KINDS`` kind queues a ``monitor.notice`` row in the same transaction when the monitor
names a notice destination; the writer starts it after its commit
(``webhook_delivery.start_pending(..., monitor_event_id=...)``), the sweep otherwise.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.destination import Destination
from app.models.monitor import NOTICE_KINDS, Monitor, MonitorEvent
from app.models.webhook_delivery import WebhookDelivery

NOTICE_EVENT = "monitor.notice"


async def close_open(
    db: AsyncSession, monitor_id: uuid.UUID, now: datetime, except_kind: str | None = None
) -> None:
    """Ends whatever situation is going on: a firing did something else, or the monitor paused
    or resumed."""
    stmt = update(MonitorEvent).where(
        MonitorEvent.monitor_id == monitor_id, MonitorEvent.ended_at.is_(None)
    )
    if except_kind is not None:
        stmt = stmt.where(MonitorEvent.kind != except_kind)
    await db.execute(stmt.values(ended_at=now))


def notice_payload(event: MonitorEvent, now: datetime) -> dict[str, Any]:
    return {
        "event": NOTICE_EVENT,
        "monitor_id": str(event.monitor_id),
        "notice": event.kind,
        "detail": event.detail,
        "first_at": event.first_at.isoformat(),
        "timestamp": now.isoformat(),
    }


async def queue_notice(db: AsyncSession, monitor: Monitor, event: MonitorEvent) -> bool:
    """The notice row for a new situation, if its kind sends one and the monitor names a
    destination. ON CONFLICT DO NOTHING: one row per situation."""
    if event.kind not in NOTICE_KINDS or monitor.notice_destination_id is None:
        return False
    destination = await db.get(Destination, monitor.notice_destination_id)
    if destination is None:
        return False
    now = datetime.now(UTC)
    await db.execute(
        pg_insert(WebhookDelivery)
        .values(
            monitor_id=monitor.id,
            monitor_event_id=event.id,
            event=NOTICE_EVENT,
            kind="email" if destination.kind == "smtp" else "webhook",
            # Its URL or mail settings are read at each attempt (ADR-015 §2b).
            destination_id=destination.id,
            payload=notice_payload(event, now),
            status="pending",
            next_attempt_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["monitor_event_id"],
            index_where=WebhookDelivery.monitor_event_id.isnot(None),
        )
    )
    return True


async def record(
    db: AsyncSession,
    monitor: Monitor,
    kind: str,
    now: datetime,
    detail: dict[str, Any] | None = None,
) -> MonitorEvent | None:
    """A firing in situation ``kind``: bumps its open row, or ends any other and starts one.

    Returns the row only when it was inserted — a new situation, the one a notice is sent for.
    """
    monitor_id = monitor.id
    await close_open(db, monitor_id, now, except_kind=kind)
    open_row = await db.scalar(
        select(MonitorEvent).where(
            MonitorEvent.monitor_id == monitor_id,
            MonitorEvent.kind == kind,
            MonitorEvent.ended_at.is_(None),
        )
    )
    if open_row is not None:
        open_row.count += 1
        open_row.last_at = now
        if detail is not None:
            open_row.detail = detail
        return None
    event = MonitorEvent(
        monitor_id=monitor_id, kind=kind, first_at=now, last_at=now, count=1, detail=detail
    )
    db.add(event)
    await db.flush()
    await queue_notice(db, monitor, event)
    return event


async def record_point(
    db: AsyncSession, monitor: Monitor, kind: str, now: datetime, detail: dict[str, Any]
) -> MonitorEvent:
    """Something that happened once (ADR-017): written closed, and closes no open situation."""
    event = MonitorEvent(
        monitor_id=monitor.id,
        kind=kind,
        first_at=now,
        last_at=now,
        count=1,
        ended_at=now,
        detail=detail,
    )
    db.add(event)
    await db.flush()
    await queue_notice(db, monitor, event)
    return event
