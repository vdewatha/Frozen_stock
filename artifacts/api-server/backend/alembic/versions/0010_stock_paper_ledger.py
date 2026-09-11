"""stock paper broker ledger"""
from alembic import op
import sqlalchemy as sa


revision = "0010_stock_paper_ledger"
down_revision = "0009_intraday_alpaca"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("stock_paper_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("broker", sa.String(32), nullable=False),
        sa.Column("broker_account_id", sa.String(96), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("cash", sa.Numeric(20, 8), nullable=False),
        sa.Column("buying_power", sa.Numeric(20, 8), nullable=False),
        sa.Column("equity", sa.Numeric(20, 8), nullable=False),
        sa.Column("last_equity", sa.Numeric(20, 8)),
        sa.Column("status", sa.String(24), nullable=False, index=True),
        sa.Column("costs_known", sa.Boolean(), nullable=False),
        sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
        sa.Column("halt_reason", sa.Text()),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("source_timestamp", sa.DateTime(timezone=True)),
        sa.Column("halted_at", sa.DateTime(timezone=True)),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("broker", name="uq_stock_paper_account_broker"))
    op.create_table("stock_paper_positions",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("account_id", sa.Integer(), sa.ForeignKey("stock_paper_accounts.id"), nullable=False, index=True),
        sa.Column("symbol", sa.String(32), nullable=False, index=True), sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("average_entry_price", sa.Numeric(20, 8)), sa.Column("current_price", sa.Numeric(20, 8)), sa.Column("market_value", sa.Numeric(20, 8)),
        sa.Column("cost_basis", sa.Numeric(20, 8)), sa.Column("unrealized_pl", sa.Numeric(20, 8)), sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False), sa.UniqueConstraint("account_id", "symbol", name="uq_stock_paper_position"),
        sa.CheckConstraint("quantity >= 0", name="ck_stock_paper_position_no_short"))
    op.create_table("stock_paper_orders",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("account_id", sa.Integer(), sa.ForeignKey("stock_paper_accounts.id"), nullable=False, index=True),
        sa.Column("client_order_id", sa.String(64), nullable=False), sa.Column("broker_order_id", sa.String(96)), sa.Column("symbol", sa.String(32), nullable=False, index=True),
        sa.Column("side", sa.String(8), nullable=False), sa.Column("quantity", sa.Numeric(20, 8), nullable=False), sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("time_in_force", sa.String(16), nullable=False), sa.Column("limit_price", sa.Numeric(20, 8)), sa.Column("reserved_cash", sa.Numeric(20, 8), nullable=False), sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("submission_attempted_at", sa.DateTime(timezone=True)), sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("uncertain_submission", sa.Boolean(), nullable=False), sa.Column("source", sa.String(64), nullable=False), sa.Column("raw_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("client_order_id", name="uq_stock_paper_order_client_id"), sa.UniqueConstraint("broker_order_id", name="uq_stock_paper_order_broker_id"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_stock_paper_order_side"), sa.CheckConstraint("quantity > 0", name="ck_stock_paper_order_quantity"))
    op.create_table("stock_paper_fills",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("account_id", sa.Integer(), sa.ForeignKey("stock_paper_accounts.id"), nullable=False, index=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("stock_paper_orders.id"), index=True), sa.Column("broker_activity_id", sa.String(128), nullable=False),
        sa.Column("broker_order_id", sa.String(96), index=True), sa.Column("symbol", sa.String(32), nullable=False, index=True), sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False), sa.Column("price", sa.Numeric(20, 8), nullable=False), sa.Column("fee", sa.Numeric(20, 8)),
        sa.Column("cost_known", sa.Boolean(), nullable=False), sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False, index=True), sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("broker_activity_id", name="uq_stock_paper_fill_activity"), sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_stock_paper_fill_side"),
        sa.CheckConstraint("quantity > 0", name="ck_stock_paper_fill_quantity"))
    op.create_table("stock_paper_equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("account_id", sa.Integer(), sa.ForeignKey("stock_paper_accounts.id"), nullable=False, index=True),
        sa.Column("cash", sa.Numeric(20, 8), nullable=False), sa.Column("equity", sa.Numeric(20, 8), nullable=False), sa.Column("last_equity", sa.Numeric(20, 8)),
        sa.Column("buying_power", sa.Numeric(20, 8), nullable=False), sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("source", sa.String(32), nullable=False), sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "observed_at", name="uq_stock_paper_equity_time"))
    op.create_table("stock_paper_broker_activities",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("account_id", sa.Integer(), sa.ForeignKey("stock_paper_accounts.id"), nullable=False, index=True),
        sa.Column("broker_activity_id", sa.String(128), nullable=False), sa.Column("activity_type", sa.String(32), nullable=False, index=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True), sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("broker_activity_id", name="uq_stock_paper_activity_id"))
    op.create_table("stock_paper_ledger_events",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("account_id", sa.Integer(), sa.ForeignKey("stock_paper_accounts.id"), index=True),
        sa.Column("event_type", sa.String(64), nullable=False, index=True), sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("reason", sa.Text()), sa.Column("payload", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    op.drop_table("stock_paper_ledger_events")
    op.drop_table("stock_paper_broker_activities")
    op.drop_table("stock_paper_equity_snapshots")
    op.drop_table("stock_paper_fills")
    op.drop_table("stock_paper_orders")
    op.drop_table("stock_paper_positions")
    op.drop_table("stock_paper_accounts")