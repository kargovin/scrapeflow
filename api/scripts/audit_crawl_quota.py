"""What the last 90 days of crawls would have cost under P7's meters — read-only.

Crawls consumed no quota until P7 (backlog §1): the count meters read `job_runs` and a
crawl never wrote one. From P7 the two count meters read the run-counting views, so a
crawl of N pages is N monthly units and one concurrency slot. Neither needs grandfathering
— both recount live — but a user whose crawls already put them past `monthly_runs_limit`
this month is refused on their next submission, and every crawl still `queued` or
`running` holds a slot from the moment the release lands.

This script shows both, per user, before anyone is told. It reads the same views the
meters read, so what it prints is what the meter will say. The intended response to a
user over the line is a `user_quotas` row for *that* user, not a change to the defaults
(`PATCH /admin/users/{id}/quota`). ⚠️ On v1 a crawl never reaches a terminal state on its
own (BUG-008: nothing consumes crawl results, so the crawl stays `running`) — every crawl
listed under "active" is holding a slot until it is cancelled. That is the meter telling
the truth about a lane that never finishes; cancel the crawls, do not raise the limit.

Changes nothing. Usage (from ./docker):

    docker compose exec api uv run python scripts/audit_crawl_quota.py
    docker compose exec api uv run python scripts/audit_crawl_quota.py --days 30
"""

import argparse
import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.quota_views import quota_active_submissions, quota_run_units
from app.models.user import User
from app.models.user_quota import UserQuota
from app.settings import settings

LANES = ("job", "batch", "crawl")


async def audit(db: AsyncSession, days: int) -> None:
    since = (datetime.now(UTC) - timedelta(days=days)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    month = func.date_trunc("month", quota_run_units.c.created_at)

    # Every user who crawled in the window; everyone else is unaffected by P7.
    crawlers = set(
        await db.scalars(
            select(quota_run_units.c.user_id)
            .where(quota_run_units.c.lane == "crawl", quota_run_units.c.created_at >= since)
            .distinct()
        )
    )
    if not crawlers:
        print(f"No crawl pages since {since:%Y-%m-%d}. Nobody's numbers change.")
        return

    monthly: dict = defaultdict(lambda: defaultdict(lambda: dict.fromkeys(LANES, 0)))
    rows = await db.execute(
        select(quota_run_units.c.user_id, month, quota_run_units.c.lane, func.count())
        .where(quota_run_units.c.user_id.in_(crawlers), quota_run_units.c.created_at >= since)
        .group_by(quota_run_units.c.user_id, month, quota_run_units.c.lane)
    )
    for user_id, m, lane, n in rows:
        monthly[user_id][m][lane] = n

    active: dict = defaultdict(lambda: dict.fromkeys(LANES, 0))
    rows = await db.execute(
        select(quota_active_submissions.c.user_id, quota_active_submissions.c.lane, func.count())
        .where(quota_active_submissions.c.user_id.in_(crawlers))
        .group_by(quota_active_submissions.c.user_id, quota_active_submissions.c.lane)
    )
    for user_id, lane, n in rows:
        active[user_id][lane] = n

    users = {u.id: u for u in await db.scalars(select(User).where(User.id.in_(crawlers)))}
    quotas = {
        q.user_id: q
        for q in await db.scalars(select(UserQuota).where(UserQuota.user_id.in_(crawlers)))
    }
    this_month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    flagged = 0
    print(f"{len(crawlers)} user(s) crawled since {since:%Y-%m-%d}\n")
    for user_id in sorted(crawlers, key=lambda u: users[u].email if u in users else ""):
        q = quotas.get(user_id)
        monthly_limit = (
            q.monthly_runs_limit
            if q and q.monthly_runs_limit is not None
            else settings.default_quota_monthly_runs
        )
        concurrent_limit = (
            q.concurrent_jobs_limit
            if q and q.concurrent_jobs_limit is not None
            else settings.default_quota_concurrent_jobs
        )
        email = users[user_id].email if user_id in users else "?"
        print(f"{email}  ({user_id})")
        print(f"  monthly_runs limit {monthly_limit}   concurrent_jobs limit {concurrent_limit}")
        print(f"  {'month':<10}{'job':>6}{'batch':>7}{'crawl':>7}{'total':>7}   note")
        for m in sorted(monthly[user_id]):
            lanes = monthly[user_id][m]
            total = sum(lanes.values())
            note = ""
            if total > monthly_limit:
                note = "over limit"
                if m == this_month:
                    note += " — refused on next submission"
                    flagged += 1
            print(
                f"  {m.strftime('%Y-%m'):<10}{lanes['job']:>6}{lanes['batch']:>7}{lanes['crawl']:>7}"
                f"{total:>7}   {note}"
            )
        a = active[user_id]
        held = sum(a.values())
        note = ""
        if held >= concurrent_limit:
            note = "pool full — every new submission refused until a crawl is cancelled"
            flagged += 1
        print(
            f"  active now: job {a['job']}  batch {a['batch']}  crawl {a['crawl']}"
            f"  = {held}/{concurrent_limit} slots   {note}"
        )
        print()

    if flagged:
        print(
            f"{flagged} condition(s) would refuse a submission today. Per-user quota rows, not defaults."
        )
    else:
        print("Nobody is refused by P7 today.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90, help="look-back window (default 90)")
    args = parser.parse_args()

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine) as db:
            await audit(db, args.days)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
