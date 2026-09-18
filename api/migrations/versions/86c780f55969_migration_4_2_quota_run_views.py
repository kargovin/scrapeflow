"""migration_4_2_quota_run_views

Revision ID: 86c780f55969
Revises: 0c73753d5138
Create Date: 2026-09-18 10:12:00.000000

The run-counting views (ADR-009 §3, backlog P7). Quota counting stops naming a table:
the two count meters read one view each, and every lane that starts work is an arm of
both. A lane missing from an arm is invisible to that meter by construction — which is
exactly what happened to crawls, and what these views exist to make impossible to
repeat silently: adding a lane is a view change, not an audit of every call site.

quota_run_units          — one row per attempted fetch of one target URL (ADR-009 §8:
                           the unit is the *attempt*, created at dispatch, not the
                           result). job run = 1, batch of N = N, crawl of N pages = N.
                           `monthly_runs` counts rows.
quota_active_submissions — one row per submission currently holding a concurrency slot.
                           A job run, a batch of any size and a crawl of any size each
                           hold exactly one (owner's call, 2026-08-17). "Active" is
                           per-lane: pending/running/processing for job_runs — exactly
                           what quota.py counted before — and queued/running for crawls.

Two views, not one: the ADR describes one view with an `active` column aggregated two
ways, but a crawl holds its slot from creation and its first unit row (the seed's
crawl_pages row) is written by the coordinator at dispatch, ~2s later — or never, while
the coordinator is down. Deriving "active" from unit rows would let a queued crawl hold
nothing. Concurrency therefore reads the submission tables directly.

Hand-written: autogenerate does not see views, and must not — the Table objects in
app/models/quota_views.py live on their own MetaData for that reason.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "86c780f55969"
down_revision: str | Sequence[str] | None = "0c73753d5138"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        sa.text("""
            CREATE VIEW quota_run_units AS
                SELECT 'job'::text AS lane,
                       r.id        AS unit_id,
                       r.id        AS submission_id,
                       j.user_id   AS user_id,
                       r.created_at
                FROM job_runs r
                JOIN jobs j ON j.id = r.job_id
            UNION ALL
                SELECT 'batch'::text,
                       r.id,
                       b.id,
                       b.user_id,
                       r.created_at
                FROM job_runs r
                JOIN batch_items bi ON bi.id = r.batch_item_id
                JOIN batches b ON b.id = bi.batch_id
            UNION ALL
                SELECT 'crawl'::text,
                       p.id,
                       c.id,
                       c.user_id,
                       p.created_at
                FROM crawl_pages p
                JOIN crawls c ON c.id = p.crawl_id
        """)
    )
    op.execute(
        sa.text("""
            CREATE VIEW quota_active_submissions AS
                SELECT 'job'::text AS lane,
                       r.id        AS submission_id,
                       j.user_id   AS user_id
                FROM job_runs r
                JOIN jobs j ON j.id = r.job_id
                WHERE r.status IN ('pending', 'running', 'processing')
            UNION ALL
                SELECT DISTINCT 'batch'::text,
                       b.id,
                       b.user_id
                FROM job_runs r
                JOIN batch_items bi ON bi.id = r.batch_item_id
                JOIN batches b ON b.id = bi.batch_id
                WHERE r.status IN ('pending', 'running', 'processing')
            UNION ALL
                SELECT 'crawl'::text,
                       c.id,
                       c.user_id
                FROM crawls c
                WHERE c.status IN ('queued', 'running')
        """)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("DROP VIEW quota_active_submissions"))
    op.execute(sa.text("DROP VIEW quota_run_units"))
