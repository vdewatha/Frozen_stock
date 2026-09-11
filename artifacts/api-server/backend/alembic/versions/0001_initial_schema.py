"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16), nullable=False, unique=True),
        sa.Column("name", sa.String(length=255)),
        sa.Column("asset_type", sa.String(length=32), nullable=False, server_default="stock"),
        sa.Column("sector", sa.String(length=128)),
        sa.Column("industry", sa.String(length=128)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("strategy_type", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("current_status", sa.String(length=64), nullable=False, server_default="research"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "risk_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "market_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(18, 6)),
        sa.Column("high", sa.Numeric(18, 6)),
        sa.Column("low", sa.Numeric(18, 6)),
        sa.Column("close", sa.Numeric(18, 6)),
        sa.Column("adjusted_close", sa.Numeric(18, 6)),
        sa.Column("volume", sa.BigInteger()),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("imported_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("symbol", "price_date", name="uq_market_prices_symbol_date"),
    )
    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16)),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("source", sa.String(length=128)),
        sa.Column("title", sa.Text()),
        sa.Column("url", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("sentiment_score", sa.Numeric(8, 4)),
        sa.Column("relevance_score", sa.Numeric(8, 4)),
        sa.Column("raw_payload", sa.JSON()),
    )
    op.create_table(
        "economic_indicators",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("indicator_name", sa.String(length=128), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(18, 6)),
        sa.Column("unit", sa.String(length=64)),
        sa.Column("source", sa.String(length=128)),
        sa.Column("raw_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("indicator_name", "observation_date", name="uq_economic_indicator_name_date"),
    )
    op.create_index("ix_economic_indicators_indicator_name", "economic_indicators", ["indicator_name"])
    op.create_index("ix_economic_indicators_observation_date", "economic_indicators", ["observation_date"])
    op.create_table(
        "market_regimes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("regime_date", sa.Date(), nullable=False, unique=True),
        sa.Column("spy_trend", sa.String(length=64)),
        sa.Column("volatility_regime", sa.String(length=64)),
        sa.Column("rate_regime", sa.String(length=64)),
        sa.Column("market_regime", sa.String(length=64)),
        sa.Column("features", sa.JSON()),
    )
    op.create_table(
        "strategy_backtests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id")),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("train_start", sa.Date()),
        sa.Column("train_end", sa.Date()),
        sa.Column("test_start", sa.Date()),
        sa.Column("test_end", sa.Date()),
        sa.Column("parameters", sa.JSON()),
        sa.Column("total_return", sa.Numeric(12, 6)),
        sa.Column("annualized_return", sa.Numeric(12, 6)),
        sa.Column("sharpe_ratio", sa.Numeric(12, 6)),
        sa.Column("sortino_ratio", sa.Numeric(12, 6)),
        sa.Column("max_drawdown", sa.Numeric(12, 6)),
        sa.Column("win_rate", sa.Numeric(12, 6)),
        sa.Column("profit_factor", sa.Numeric(12, 6)),
        sa.Column("number_of_trades", sa.Integer()),
        sa.Column("score", sa.Numeric(12, 6)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "strategy_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id")),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("signal_time", sa.DateTime(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("probability_up", sa.Numeric(8, 4)),
        sa.Column("probability_down", sa.Numeric(8, 4)),
        sa.Column("confidence", sa.Numeric(8, 4)),
        sa.Column("reason", sa.Text()),
        sa.Column("features", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id")),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("entry_time", sa.DateTime()),
        sa.Column("exit_time", sa.DateTime()),
        sa.Column("entry_price", sa.Numeric(18, 6)),
        sa.Column("exit_price", sa.Numeric(18, 6)),
        sa.Column("quantity", sa.Numeric(18, 6)),
        sa.Column("status", sa.String(length=32)),
        sa.Column("profit_loss", sa.Numeric(18, 6)),
        sa.Column("profit_loss_pct", sa.Numeric(12, 6)),
        sa.Column("reason_entered", sa.Text()),
        sa.Column("reason_exited", sa.Text()),
        sa.Column("features_at_entry", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "strategy_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id")),
        sa.Column("market_regime", sa.String(length=64)),
        sa.Column("symbol", sa.String(length=16)),
        sa.Column("sample_size", sa.Integer()),
        sa.Column("avg_return", sa.Numeric(12, 6)),
        sa.Column("avg_drawdown", sa.Numeric(12, 6)),
        sa.Column("win_rate", sa.Numeric(12, 6)),
        sa.Column("profit_factor", sa.Numeric(12, 6)),
        sa.Column("confidence_score", sa.Numeric(12, 6)),
        sa.Column("last_updated", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("notes", sa.Text()),
    )
    op.create_table(
        "strategy_experiments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id")),
        sa.Column("experiment_name", sa.String(length=255)),
        sa.Column("old_parameters", sa.JSON()),
        sa.Column("new_parameters", sa.JSON()),
        sa.Column("hypothesis", sa.Text()),
        sa.Column("backtest_result_id", sa.Integer(), sa.ForeignKey("strategy_backtests.id")),
        sa.Column("paper_result_summary", sa.JSON()),
        sa.Column("decision", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64)),
        sa.Column("entity_id", sa.Integer()),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_entity_type", "audit_logs", ["entity_type"])
    op.create_index("ix_audit_logs_entity_id", "audit_logs", ["entity_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("entity_type", sa.String(length=64)),
        sa.Column("entity_id", sa.Integer()),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime()),
        sa.Column("resolved_at", sa.DateTime()),
    )
    op.create_index("ix_notifications_category", "notifications", ["category"])
    op.create_index("ix_notifications_severity", "notifications", ["severity"])
    op.create_index("ix_notifications_status", "notifications", ["status"])
    op.create_index("ix_notifications_source", "notifications", ["source"])
    op.create_index("ix_notifications_entity_type", "notifications", ["entity_type"])
    op.create_index("ix_notifications_entity_id", "notifications", ["entity_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_table(
        "model_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("prediction_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("probability_up", sa.Numeric(8, 4), nullable=False),
        sa.Column("probability_down", sa.Numeric(8, 4), nullable=False),
        sa.Column("expected_return", sa.Numeric(12, 6), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("features", sa.JSON()),
        sa.Column("probabilities_by_model", sa.JSON()),
        sa.Column("realized_return", sa.Numeric(12, 6)),
        sa.Column("realized_up", sa.Boolean()),
        sa.Column("brier_score", sa.Numeric(12, 6)),
        sa.Column("is_realized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_model_predictions_symbol", "model_predictions", ["symbol"])
    op.create_index("ix_model_predictions_prediction_date", "model_predictions", ["prediction_date"])
    op.create_index("ix_model_predictions_horizon_days", "model_predictions", ["horizon_days"])
    op.create_index("ix_model_predictions_created_at", "model_predictions", ["created_at"])
    op.create_table(
        "model_validation_folds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("fold", sa.Integer(), nullable=False),
        sa.Column("train_start", sa.Date(), nullable=False),
        sa.Column("train_end", sa.Date(), nullable=False),
        sa.Column("test_start", sa.Date(), nullable=False),
        sa.Column("test_end", sa.Date(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("accuracy", sa.Numeric(12, 6), nullable=False),
        sa.Column("brier_score", sa.Numeric(12, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_model_validation_folds_symbol", "model_validation_folds", ["symbol"])
    op.create_index("ix_model_validation_folds_horizon_days", "model_validation_folds", ["horizon_days"])
    op.create_index("ix_model_validation_folds_created_at", "model_validation_folds", ["created_at"])
    op.create_table(
        "trade_candidate_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="scanner"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_trade_candidate_snapshots_created_at", "trade_candidate_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_trade_candidate_snapshots_created_at", table_name="trade_candidate_snapshots")
    op.drop_table("trade_candidate_snapshots")
    op.drop_index("ix_model_validation_folds_created_at", table_name="model_validation_folds")
    op.drop_index("ix_model_validation_folds_horizon_days", table_name="model_validation_folds")
    op.drop_index("ix_model_validation_folds_symbol", table_name="model_validation_folds")
    op.drop_table("model_validation_folds")
    op.drop_index("ix_model_predictions_created_at", table_name="model_predictions")
    op.drop_index("ix_model_predictions_horizon_days", table_name="model_predictions")
    op.drop_index("ix_model_predictions_prediction_date", table_name="model_predictions")
    op.drop_index("ix_model_predictions_symbol", table_name="model_predictions")
    op.drop_table("model_predictions")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_entity_id", table_name="notifications")
    op.drop_index("ix_notifications_entity_type", table_name="notifications")
    op.drop_index("ix_notifications_source", table_name="notifications")
    op.drop_index("ix_notifications_status", table_name="notifications")
    op.drop_index("ix_notifications_severity", table_name="notifications")
    op.drop_index("ix_notifications_category", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_event_type", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("strategy_experiments")
    op.drop_table("strategy_memory")
    op.drop_table("paper_trades")
    op.drop_table("strategy_signals")
    op.drop_table("strategy_backtests")
    op.drop_table("market_regimes")
    op.drop_index("ix_economic_indicators_observation_date", table_name="economic_indicators")
    op.drop_index("ix_economic_indicators_indicator_name", table_name="economic_indicators")
    op.drop_table("economic_indicators")
    op.drop_table("news_articles")
    op.drop_table("market_prices")
    op.drop_table("risk_rules")
    op.drop_table("strategies")
    op.drop_table("assets")
