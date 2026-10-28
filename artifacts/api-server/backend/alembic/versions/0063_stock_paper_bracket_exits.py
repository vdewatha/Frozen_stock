"""Persist broker-side bracket exits and their child legs on stock-paper orders.

Entries submitted as Alpaca bracket/OTO orders carry the planned stop-loss and
take-profit prices; each broker child leg is stored as its own order row linked
to the entry by ``parent_order_id`` so leg fills attribute to the strategy lot.
"""
from alembic import op
import sqlalchemy as sa


revision = "0063_stock_paper_bracket_exits"
down_revision = "0062_intraday_no_trade"
branch_labels = None
depends_on = None

TABLE = "stock_paper_orders"
INDEX = "ix_stock_paper_orders_parent_order_id"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns(TABLE)}
    additions = [
        sa.Column("order_class", sa.String(length=16), nullable=False, server_default="simple"),
        sa.Column("parent_order_id", sa.Integer(), nullable=True),
        sa.Column("leg_role", sa.String(length=16), nullable=True),
        sa.Column("stop_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("take_profit_price", sa.Numeric(20, 8), nullable=True),
    ]
    missing = [column for column in additions if column.name not in existing]
    if missing:
        with op.batch_alter_table(TABLE, recreate="auto") as batch_op:
            for column in missing:
                batch_op.add_column(column)
    if INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, ["parent_order_id"])


def downgrade():
    raise RuntimeError("0063 is forward-only; restore a verified backup to roll back")
