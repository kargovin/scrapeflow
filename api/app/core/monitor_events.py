"""A monitor's own history (ADR-016 §7b): one ``monitor_events`` row per situation.

Every write happens under ``quota.lock_user`` — "find the open row, then bump it or insert one"
is a read followed by a write.
"""

import uuid
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monitor import MonitorEvent


async def close_open(db: AsyncSession, monitor_id: uuid.UUID, now: datetime) -> None:
    """Ends whatever situation is going on: a firing did something else, or the monitor paused."""
    await db.execute(
        update(MonitorEvent)
        .where(MonitorEvent.monitor_id == monitor_id, MonitorEvent.ended_at.is_(None))
        .values(ended_at=now)
    )
