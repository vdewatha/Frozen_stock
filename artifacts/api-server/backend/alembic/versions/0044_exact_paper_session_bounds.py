"""Persist exact one-session paper-run bounds."""
from alembic import op
import sqlalchemy as sa


revision = "0044_exact_paper_session_bounds"
down_revision = "0043_paper_run_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stock_paper_run_approvals",
        sa.Column("loss_limits", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "stock_paper_run_approvals",
        sa.Column("stop_authority", sa.String(length=64), nullable=False, server_default="operator_and_system"),
    )
    op.add_column(
        "stock_paper_run_approvals",
        sa.Column("pending_order_treatment", sa.String(length=32), nullable=False, server_default="cancel"),
    )
    op.add_column(
        "stock_paper_run_approvals",
        sa.Column("remaining_position_policy", sa.String(length=32), nullable=False, server_default="hold"),
    )
    op.add_column(
        "stock_paper_orders",
        sa.Column("trial_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_stock_paper_orders_trial_id", "stock_paper_orders", ["trial_id"])


def downgrade() -> None:
    raise RuntimeError("0044 is forward-only; restore a verified backup to roll back")