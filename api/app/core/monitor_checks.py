"""The end of a monitor's check (ADR-016 §7a): the failure counter and self-pause.

Every writer of a check's outcome calls ``monitor_check_ended`` in the transaction that writes
it — ``mirror_run_status``, the could-not-start branch and admission's failed checks — and only
in the call that moves the run out of ``running``: those writers are retried, and a bump per
retry would pause a monitor after fewer real failures than five. The caller holds ``lock_user``.
A check whose approval expired also records an ``approval_expired`` event (ADR-017).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import approvals, monitor_events, monitor_schedules
from app.models.monitor import Monitor, MonitorEvent
from app.models.pipeline import PipelineRun

# D12: consecutive failed checks before a monitor pauses itself.
SELF_PAUSE_AFTER = 5


@dataclass(frozen=True)
class CheckEnded:
    # Set when this check paused its monitor: the writer pauses the Schedule after its commit.
    paused: Monitor | None = None
    # A paused_itself or approval_expired event: the writer starts its notice after commit (§8).
    notice_event_id: uuid.UUID | None = None


async def monitor_check_ended(db: AsyncSession, run: PipelineRun) -> CheckEnded:
    """+1 on failed, reset on completed, nothing on cancelled; the 5th failure pauses."""
    if run.monitor_id is None or run.status not in ("failed", "completed"):
        return CheckEnded()
    monitor = await db.scalar(select(Monitor).where(Monitor.id == run.monitor_id).with_for_update())
    if monitor is None:
        return CheckEnded()
    if run.status == "completed":
        monitor.consecutive_failures = 0
        if run.stop_reason == "approval_expired":
            event = await _approval_expired(db, monitor, run)
            return CheckEnded(notice_event_id=event.id if event else None)
        return CheckEnded()
    monitor.consecutive_failures += 1
    if monitor.consecutive_failures < SELF_PAUSE_AFTER or monitor.state != "active":
        return CheckEnded()
    now = datetime.now(UTC)
    monitor.state = "paused_itself"
    monitor.updated_at = now
    # Ends any open situation (§7b); this one stays open until the monitor resumes.
    event = await monitor_events.record(
        db,
        monitor,
        "paused_itself",
        now,
        {
            "failed_run_id": str(run.id),
            "error": run.error,
            "failures": monitor.consecutive_failures,
        },
    )
    return CheckEnded(paused=monitor, notice_event_id=event.id if event else None)


async def _approval_expired(
    db: AsyncSession, monitor: Monitor, run: PipelineRun
) -> MonitorEvent | None:
    """One per check: None when this run's is already recorded."""
    recorded = await db.scalar(
        select(MonitorEvent.id).where(
            MonitorEvent.monitor_id == monitor.id,
            MonitorEvent.kind == "approval_expired",
            MonitorEvent.detail["run_id"].astext == str(run.id),
        )
    )
    if recorded is not None:
        return None
    block = await approvals.approval_block(db, run.id)
    deadline = block.approval_deadline if block is not None else None
    return await monitor_events.record_point(
        db,
        monitor,
        "approval_expired",
        datetime.now(UTC),
        {
            "run_id": str(run.id),
            "deadline": deadline.isoformat() if deadline is not None else None,
            "run_url": approvals.run_url(run),
        },
    )


async def after_commit(temporal, ended: CheckEnded) -> None:
    """Best effort: the row already refuses every firing."""
    if ended.paused is not None:
        await monitor_schedules.pause(temporal, ended.paused)
