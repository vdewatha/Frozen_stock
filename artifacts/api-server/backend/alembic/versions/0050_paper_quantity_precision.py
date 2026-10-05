"""Preserve Alpaca's nine-decimal fractional share quantities."""
from alembic import op
import sqlalchemy as sa

revision = "0050_paper_quantity_precision"
down_revision = "0049_alpaca_activity_ledger"
branch_labels = None
depends_on = None


def upgrade():
    for table, columns in {
        "stock_paper_positions": [("quantity", False)],
        "stock_paper_orders": [("quantity", False)],
        "stock_paper_fills": [("quantity", False)],
        "stock_paper_trial_lots": [("quantity", False), ("exited_quantity", True)],
    }.items():
        with op.batch_alter_table(table) as batch:
            for column, nullable in columns:
                batch.alter_column(column, existing_type=sa.Numeric(20, 8),
                                   type_=sa.Numeric(21, 9), existing_nullable=nullable)


def downgrade():
    raise RuntimeError("0050 is forward-only: reducing quantity precision would destroy evidence")
