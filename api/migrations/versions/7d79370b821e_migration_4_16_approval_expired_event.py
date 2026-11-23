"""migration_4_16_approval_expired_event

Revision ID: 7d79370b821e
Revises: f2c328bcbfcc
Create Date: 2026-11-23 18:36:16.943497

O.0 (ADR-017): `monitor_events.kind` gains `approval_expired`. No quota view reads the table.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d79370b821e"
down_revision: str | Sequence[str] | None = "f2c328bcbfcc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "'skipped_monthly_runs', 'skipped_check_in_progress', 'skipped_pipelines_disabled', "
    "'waiting_for_slot', 'gave_up_waiting', 'paused_itself'"
)


def _kind_check(values: str) -> None:
    op.drop_constraint("ck_monitor_events_kind", "monitor_events", type_="check")
    op.create_check_constraint("ck_monitor_events_kind", "monitor_events", f"kind IN ({values})")


def upgrade() -> None:
    """Upgrade schema."""
    _kind_check(f"{KINDS}, 'approval_expired'")


def downgrade() -> None:
    """Downgrade schema."""
    # ⚠️ Lossy: the events go, and their notice rows with them (ON DELETE CASCADE).
    op.execute("DELETE FROM monitor_events WHERE kind = 'approval_expired'")
    _kind_check(KINDS)
