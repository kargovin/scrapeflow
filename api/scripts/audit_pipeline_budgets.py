"""Saved pipeline versions whose declared run budget no longer covers their blocks — read-only.

Budgets are checked at save; a version is immutable. E.9 raised what a Scrape block needs (5 min
queue timer, host-slot wait), so a version saved with a tight ``time_budget_seconds`` still runs,
with its blocks' ``schedule_to_close`` cut short. Versions with no declared budget are unaffected:
their budget is computed at trigger time.

Changes nothing. Usage:

    docker compose exec api uv run python -m scripts.audit_pipeline_budgets
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.pipeline import Pipeline, PipelineVersion
from app.pipelines.catalog import CATALOG, run_need_seconds
from app.pipelines.definition import DefinitionUnreadable, parse_definition
from app.settings import settings


async def audit(db: AsyncSession) -> int:
    rows = await db.execute(
        select(
            Pipeline.id,
            Pipeline.name,
            Pipeline.deleted_at,
            PipelineVersion.version,
            PipelineVersion.definition,
        )
        .join(PipelineVersion, PipelineVersion.pipeline_id == Pipeline.id)
        .order_by(Pipeline.name, PipelineVersion.version)
    )
    checked = short = 0
    for pipeline_id, name, deleted_at, version, definition in rows:
        declared = definition.get("time_budget_seconds")
        if declared is None:
            continue
        checked += 1
        try:
            parsed, _ = parse_definition(definition)
        except DefinitionUnreadable as exc:
            print(f"UNREADABLE {name} v{version} ({pipeline_id}): {exc.problems}")
            continue
        need = run_need_seconds(CATALOG[b.type].timing(b.config) for b in parsed.blocks)
        if declared < need:
            short += 1
            gone = " [deleted]" if deleted_at else ""
            print(
                f"SHORT {name} v{version}{gone} ({pipeline_id}): declared {declared} s, "
                f"blocks need {need} s"
            )
    print(f"{checked} version(s) declare a budget; {short} short.")
    return short


async def main() -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine) as db:
            await audit(db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
