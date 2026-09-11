"""Persist owned forward-trial lots and exits."""
from alembic import op
import sqlalchemy as sa

revision = "0018_stock_trial_lots"
down_revision = "0017_forward_trial_strategy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_paper_trial_lots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trial_id", sa.String(length=36), sa.ForeignKey("stock_paper_trials.id"), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("entry_decision_id", sa.Integer(), sa.ForeignKey("stock_paper_trial_decisions.id"), nullable=False),
        sa.Column("entry_order_id", sa.Integer(), sa.ForeignKey("stock_paper_orders.id")),
        sa.Column("entry_fill_id", sa.Integer(), sa.ForeignKey("stock_paper_fills.id")),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_session", sa.String(length=32), nullable=False),
        sa.Column("planned_horizon_sessions", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("stop_fraction", sa.Numeric(12, 8), nullable=False, server_default="0.02"),
        sa.Column("exit_order_id", sa.Integer(), sa.ForeignKey("stock_paper_orders.id")),
        sa.Column("exit_status", sa.String(length=32)),
        sa.Column("exited_quantity", sa.Numeric(20, 8)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("entry_decision_id", name="uq_stock_trial_lot_decision"),
    )
    op.create_index("ix_stock_paper_trial_lots_trial_id", "stock_paper_trial_lots", ["trial_id"])
    op.create_index("ix_stock_paper_trial_lots_symbol", "stock_paper_trial_lots", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_stock_paper_trial_lots_symbol", table_name="stock_paper_trial_lots")
    op.drop_index("ix_stock_paper_trial_lots_trial_id", table_name="stock_paper_trial_lots")
    op.drop_table("stock_paper_trial_lots")