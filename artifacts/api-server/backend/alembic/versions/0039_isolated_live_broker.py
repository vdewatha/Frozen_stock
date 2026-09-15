"""Add the isolated live-broker ledger and account boundary."""
from alembic import op
import sqlalchemy as sa


revision = "0039_isolated_live_broker"
down_revision = "0038_stock_accuracy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_broker_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("broker", sa.String(length=32), nullable=False),
        sa.Column("broker_account_id", sa.String(length=96), nullable=False),
        sa.Column("environment", sa.String(length=48), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("cash", sa.Numeric(20, 8), nullable=False),
        sa.Column("buying_power", sa.Numeric(20, 8), nullable=False),
        sa.Column("equity", sa.Numeric(20, 8), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
        sa.Column("unexplained_residual", sa.Boolean(), nullable=False),
        sa.Column("halt_reason", sa.Text()),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("source_timestamp", sa.DateTime(timezone=True)),
        sa.Column("halted_at", sa.DateTime(timezone=True)),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("broker", name="uq_live_broker_account_broker"),
    )
    op.create_index("ix_live_broker_accounts_status", "live_broker_accounts", ["status"])

    op.create_table(
        "live_broker_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("live_broker_accounts.id"), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("current_price", sa.Numeric(20, 8)),
        sa.Column("market_value", sa.Numeric(20, 8)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "symbol", name="uq_live_broker_position"),
        sa.CheckConstraint("quantity >= 0", name="ck_live_broker_position_no_short"),
    )
    op.create_index("ix_live_broker_positions_account_id", "live_broker_positions", ["account_id"])
    op.create_index("ix_live_broker_positions_symbol", "live_broker_positions", ["symbol"])

    op.create_table(
        "live_broker_account_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("live_broker_accounts.id"), nullable=False),
        sa.Column("cash", sa.Numeric(20, 8), nullable=False),
        sa.Column("buying_power", sa.Numeric(20, 8), nullable=False),
        sa.Column("equity", sa.Numeric(20, 8), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "observed_at", name="uq_live_broker_snapshot_time"),
    )
    op.create_index("ix_live_broker_account_snapshots_account_id", "live_broker_account_snapshots", ["account_id"])
    op.create_index("ix_live_broker_account_snapshots_observed_at", "live_broker_account_snapshots", ["observed_at"])

    op.create_table(
        "live_broker_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("live_broker_accounts.id"), nullable=False),
        sa.Column("account_snapshot_id", sa.Integer(), sa.ForeignKey("live_broker_account_snapshots.id")),
        sa.Column("model_run_id", sa.String(length=64), sa.ForeignKey("stock_model_registry.run_id")),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("strategy_signals.id")),
        sa.Column("risk_decision_id", sa.String(length=96), nullable=False),
        sa.Column("risk_decision", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=128)),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("broker_order_id", sa.String(length=96)),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("time_in_force", sa.String(length=16), nullable=False),
        sa.Column("reference_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("reference_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 8)),
        sa.Column("reserved_cash", sa.Numeric(20, 8), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("submission_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("uncertain_submission", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("raw_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_live_broker_order_idempotency"),
        sa.UniqueConstraint("client_order_id", name="uq_live_broker_order_client_id"),
        sa.UniqueConstraint("broker_order_id", name="uq_live_broker_order_broker_id"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_live_broker_order_side"),
        sa.CheckConstraint("quantity > 0", name="ck_live_broker_order_quantity"),
    )
    for name, column in (
        ("account_id", "account_id"), ("account_snapshot_id", "account_snapshot_id"),
        ("model_run_id", "model_run_id"), ("signal_id", "signal_id"),
        ("risk_decision_id", "risk_decision_id"), ("symbol", "symbol"), ("status", "status"),
    ):
        op.create_index(f"ix_live_broker_orders_{name}", "live_broker_orders", [column])

    op.create_table(
        "live_broker_fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("live_broker_accounts.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("live_broker_orders.id")),
        sa.Column("broker_activity_id", sa.String(length=128), nullable=False),
        sa.Column("broker_order_id", sa.String(length=96)),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("fee", sa.Numeric(20, 8)),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("broker_activity_id", name="uq_live_broker_fill_activity"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_live_broker_fill_side"),
        sa.CheckConstraint("quantity > 0", name="ck_live_broker_fill_quantity"),
    )
    op.create_index("ix_live_broker_fills_account_id", "live_broker_fills", ["account_id"])
    op.create_index("ix_live_broker_fills_order_id", "live_broker_fills", ["order_id"])
    op.create_index("ix_live_broker_fills_broker_order_id", "live_broker_fills", ["broker_order_id"])
    op.create_index("ix_live_broker_fills_symbol", "live_broker_fills", ["symbol"])
    op.create_index("ix_live_broker_fills_filled_at", "live_broker_fills", ["filled_at"])

    op.create_table(
        "live_broker_activities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("live_broker_accounts.id"), nullable=False),
        sa.Column("broker_activity_id", sa.String(length=128), nullable=False),
        sa.Column("activity_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("broker_activity_id", name="uq_live_broker_activity_id"),
    )
    op.create_index("ix_live_broker_activities_account_id", "live_broker_activities", ["account_id"])
    op.create_index("ix_live_broker_activities_activity_type", "live_broker_activities", ["activity_type"])
    op.create_index("ix_live_broker_activities_occurred_at", "live_broker_activities", ["occurred_at"])

    op.create_table(
        "live_broker_ledger_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("live_broker_accounts.id")),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_live_broker_ledger_events_account_id", "live_broker_ledger_events", ["account_id"])
    op.create_index("ix_live_broker_ledger_events_event_type", "live_broker_ledger_events", ["event_type"])
    op.create_index("ix_live_broker_ledger_events_status", "live_broker_ledger_events", ["status"])


def downgrade() -> None:
    raise RuntimeError("0039 is forward-only; restore a verified backup to roll back")