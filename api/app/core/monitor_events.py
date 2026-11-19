"""A monitor's own history (ADR-016 §7b): one ``monitor_events`` row per situation.

Every write happens under ``quota.lock_user`` — "find the open row, then bump it or insert one"
is a read followed by a write.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monitor import MonitorEvent


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


async def record(
    db: AsyncSession,
    monitor_id: uuid.UUID,
    kind: str,
    now: datetime,
    detail: dict[str, Any] | None = None,
) -> MonitorEvent | None:
    """A firing in situation ``kind``: bumps its open row, or ends any other and starts one.

    Returns the row only when it was inserted — a new situation, the one a notice is sent for.
    """
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
    return event
