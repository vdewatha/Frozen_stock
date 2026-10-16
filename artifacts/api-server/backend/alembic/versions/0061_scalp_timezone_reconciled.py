"""Reconcile the deployed scalp timestamp migration head.

Some paper deployments already recorded this revision after applying the
timezone conversion from 0060.  Keeping the revision in the repository lets
Alembic recognize those databases and continue startup without changing the
data a second time.
"""

from alembic import op


revision = "0061_scalp_timezone_reconciled"
down_revision = "0060_scalp_research_tz"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The schema change is intentionally owned by 0060. This revision only
    # reconciles the migration graph with databases that already recorded it.
    pass


def downgrade() -> None:
    raise RuntimeError("0061 is forward-only; restore a verified backup to roll back")
