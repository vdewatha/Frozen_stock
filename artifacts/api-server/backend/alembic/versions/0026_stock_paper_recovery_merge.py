"""Merge the recovery migration with the immutable readiness migration."""
from alembic import op
import sqlalchemy as sa

revision = "0026_stock_paper_recovery_merge"
down_revision = ("0025_promo_immutable", "0025_stock_paper_recovery")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both parent branches converge here. The recovery tables are created by
    # the recovery parent, including for databases that were already at the
    # legacy recovery revision.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("stock_paper_recovery_state"):
        raise RuntimeError("Recovery migration branch did not create its tables")


def downgrade() -> None:
    raise RuntimeError("0026 is forward-only; restore a verified backup to roll back")