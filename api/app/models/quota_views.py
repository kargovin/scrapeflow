"""The run-counting views (ADR-009 §3, backlog P7) — read-only Table objects.

These are database *views*, created by migration 86c780f55969 and read by
`app/core/quota.py`. They are the single definition of what each count meter counts:

quota_run_units          — one row per attempted fetch, every lane. `monthly_runs`.
quota_active_submissions — one row per submission holding a concurrency slot. `concurrent_jobs`.

They live on their own MetaData, not `Base.metadata`, on purpose: autogenerate would
otherwise emit `create_table` for each. Adding a lane means adding an arm to both views
in a new migration — nothing here changes unless a column does.
"""

from sqlalchemy import Column, DateTime, MetaData, Table, Text, Uuid

view_metadata = MetaData()

quota_run_units = Table(
    "quota_run_units",
    view_metadata,
    Column("lane", Text),
    Column("unit_id", Uuid),
    Column("submission_id", Uuid),
    Column("user_id", Uuid),
    Column("created_at", DateTime(timezone=True)),
)

quota_active_submissions = Table(
    "quota_active_submissions",
    view_metadata,
    Column("lane", Text),
    Column("submission_id", Uuid),
    Column("user_id", Uuid),
)
