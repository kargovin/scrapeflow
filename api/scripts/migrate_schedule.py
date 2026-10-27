"""Moves job schedules armed on v1's deleted scheduler loop to Temporal Schedules (ADR-009 §7
mechanism 3), and keeps Temporal matching the rows.

Forward, per job: pause it → confirm no v1 run of it is in flight → create its Schedule and hand
the row to v2. A job with a run in flight is left paused and reported; run again once it has
finished. There is no way back: F.6 deleted v1's loop, so a v1 schedule fires nothing — which is
also why one left behind needs moving (the admin stats count them in
``active_recurring_jobs_by_lane``).

``--reconcile`` puts every v2 schedule's Schedule as its row says and deletes ``job-schedule-*``
Schedules whose job is gone or not v2's — after a put the API could not make, or a delete it lost.

Dry-run by default; ``--apply`` writes. Usage:

    docker compose exec api uv run python -m scripts.migrate_schedule --all [--apply]
    docker compose exec api uv run python -m scripts.migrate_schedule --job <id> ... [--apply]
    docker compose exec api uv run python -m scripts.migrate_schedule --reconcile [--apply]

In production: ``kubectl -n scrapeflow exec deploy/scrapeflow-api -c api --
/app/.venv/bin/python -m scripts.migrate_schedule ...``.
"""

import argparse
import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.client import Client

from app.core import schedules
from app.models.job import Job
from app.models.job_runs import JobRun
from app.settings import settings
from app.workflows.client import connect

ACTIVE = ("pending", "running", "processing")


@dataclass
class Report:
    moved: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def print(self, verb: str, apply: bool) -> None:
        mode = "" if apply else " (dry run)"
        for line in self.skipped:
            print(f"SKIP {line}")
        for line in self.waiting:
            print(f"WAIT {line}")
        for line in self.moved:
            print(f"{verb.upper()} {line}")
        print(
            f"{len(self.moved)} {verb}, {len(self.waiting)} waiting on a run in flight, "
            f"{len(self.skipped)} skipped{mode}."
        )


async def _runs_in_flight(db: AsyncSession, job_id: uuid.UUID, lane: str) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(JobRun)
        .where(JobRun.job_id == job_id, JobRun.lane == lane, JobRun.status.in_(ACTIVE))
    )


async def _jobs(db: AsyncSession, lane: str, job_ids: list[uuid.UUID] | None) -> list[Job]:
    stmt = select(Job).where(Job.schedule_status.is_not(None)).order_by(Job.created_at)
    if job_ids:
        stmt = stmt.where(Job.id.in_(job_ids))
    else:
        stmt = stmt.where(Job.schedule_lane == lane)
    return list((await db.scalars(stmt)).all())


async def forward(
    db_factory: async_sessionmaker[AsyncSession],
    client: Client,
    job_ids: list[uuid.UUID] | None,
    apply: bool,
) -> Report:
    report = Report()
    async with db_factory() as db:
        jobs = await _jobs(db, "v1", job_ids)
    for job in jobs:
        label = f"{job.id} ({job.schedule_cron}, {job.schedule_status})"
        if job.schedule_lane != "v1":
            report.skipped.append(f"{label}: not a v1 schedule")
            continue
        if not apply:
            report.moved.append(label)
            continue
        async with db_factory() as db:
            row = await db.get(Job, job.id, with_for_update=True)
            # 1. Pause it: a v1 loop still running somewhere (an old pod) selects active rows only.
            intended = row.schedule_status
            row.schedule_status = "paused"
            await db.commit()
            # 2. A v1 dispatch already made must finish first.
            if in_flight := await _runs_in_flight(db, row.id, "v1"):
                report.waiting.append(f"{label}: {in_flight} v1 run(s); left paused")
                continue
            # 3. Arm the Schedule, then hand the row over. A tick between the two finds a v1 row
            # and is stopped at admission.
            row.schedule_status = intended
            row.schedule_lane = "v2"
            row.next_run_at = None
            await schedules.put(client, row)
            await db.commit()
        report.moved.append(label)
    return report


async def reconcile(
    db_factory: async_sessionmaker[AsyncSession], client: Client, apply: bool
) -> tuple[list[str], list[str]]:
    """(v2 rows whose Schedule was put, Schedules deleted). Puts are idempotent, so every v2
    row is put; a dry run lists them."""
    async with db_factory() as db:
        v2 = list(
            (
                await db.scalars(
                    select(Job).where(Job.schedule_lane == "v2", Job.schedule_status.is_not(None))
                )
            ).all()
        )
    wanted = {schedules.schedule_id(job.id) for job in v2}
    orphans = []
    async for listed in await client.list_schedules():
        if listed.id.startswith("job-schedule-") and listed.id not in wanted:
            orphans.append(listed.id)
    if apply:
        for job in v2:
            await schedules.put(client, job)
        for sid in orphans:
            await client.get_schedule_handle(sid).delete()
    return [str(job.id) for job in v2], orphans


async def main(argv: list[str] | None = None, connect_client: Callable = connect) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="every schedule on the source lane")
    target.add_argument("--job", type=uuid.UUID, nargs="+", help="these jobs only")
    target.add_argument("--reconcile", action="store_true", help="make Temporal match the rows")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    db_factory = async_sessionmaker(engine, expire_on_commit=False)
    client = await connect_client()
    try:
        if args.reconcile:
            put, deleted = await reconcile(db_factory, client, args.apply)
            mode = "" if args.apply else " (dry run)"
            for job_id in put:
                print(f"PUT job-schedule-{job_id}")
            for sid in deleted:
                print(f"DELETE {sid}")
            print(f"{len(put)} put, {len(deleted)} orphan(s) deleted{mode}.")
            return
        job_ids = None if args.all else args.job
        (await forward(db_factory, client, job_ids, args.apply)).print("migrated", args.apply)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
