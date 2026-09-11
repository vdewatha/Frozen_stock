"""Durable, broker-observed accounting records for the stock paper account.

These tables intentionally do not share the crypto execution ledger or the legacy
``paper_trades`` simulator.  Monetary values use Decimal-compatible NUMERIC columns.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StockPaperAccount(Base):
    __tablename__ = "stock_paper_accounts"
    __table_args__ = (UniqueConstraint("broker", name="uq_stock_paper_account_broker"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="alpaca_paper")
    broker_account_id: Mapped[str] = mapped_column(String(96), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    buying_power: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    last_equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="uninitialized", index=True)
    costs_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    accounting_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unexplained_residual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    halt_reason: Mapped[Optional[str]] = mapped_column(Text)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    halted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StockPaperPosition(Base):
    __tablename__ = "stock_paper_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "symbol", name="uq_stock_paper_position"),
        CheckConstraint("quantity >= 0", name="ck_stock_paper_position_no_short"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("stock_paper_accounts.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    average_entry_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    market_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    cost_basis: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    unrealized_pl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StockPaperOrder(Base):
    __tablename__ = "stock_paper_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_stock_paper_order_client_id"),
        UniqueConstraint("broker_order_id", name="uq_stock_paper_order_broker_id"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_stock_paper_order_side"),
        CheckConstraint("quantity > 0", name="ck_stock_paper_order_quantity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("stock_paper_accounts.id"), nullable=False, index=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"), index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategy_signals.id"), unique=True, index=True)
    trial_lot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_trial_lots.id"), index=True)
    evidence_id: Mapped[Optional[str]] = mapped_column(String(96))
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(96))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="market")
    time_in_force: Mapped[str] = mapped_column(String(16), nullable=False, default="day")
    limit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    reserved_cash: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    submission_attempted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    uncertain_submission: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class StockPaperFill(Base):
    __tablename__ = "stock_paper_fills"
    __table_args__ = (
        UniqueConstraint("broker_activity_id", name="uq_stock_paper_fill_activity"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_stock_paper_fill_side"),
        CheckConstraint("quantity > 0", name="ck_stock_paper_fill_quantity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("stock_paper_accounts.id"), nullable=False, index=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_orders.id"), index=True)
    broker_activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(96), index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    cost_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StockPaperEquitySnapshot(Base):
    __tablename__ = "stock_paper_equity_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "observed_at", name="uq_stock_paper_equity_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("stock_paper_accounts.id"), nullable=False, index=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    last_equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    buying_power: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="alpaca_paper")
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StockPaperBrokerActivity(Base):
    __tablename__ = "stock_paper_broker_activities"
    __table_args__ = (UniqueConstraint("broker_activity_id", name="uq_stock_paper_activity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("stock_paper_accounts.id"), nullable=False, index=True)
    broker_activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StockPaperLedgerEvent(Base):
    __tablename__ = "stock_paper_ledger_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_accounts.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StockPaperStrategyEvidence(Base):
    """Explicit admin-recorded strategy evidence; legacy StrategyMemory is excluded."""
    __tablename__ = "stock_paper_strategy_evidence"
    __table_args__ = (UniqueConstraint("strategy_id", name="uq_stock_paper_strategy_evidence_strategy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="approved")
    verified_drawdown: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    consecutive_losses: Mapped[int] = mapped_column(nullable=False, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False)


class StockPaperTrialLot(Base):
    """Immutable ownership record for a trial entry and its risk-reducing exit."""
    __tablename__ = "stock_paper_trial_lots"
    __table_args__ = (UniqueConstraint("entry_decision_id", name="uq_stock_trial_lot_decision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey("stock_paper_trials.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entry_decision_id: Mapped[int] = mapped_column(ForeignKey("stock_paper_trial_decisions.id"), nullable=False)
    entry_order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_orders.id"))
    entry_fill_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_fills.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_session: Mapped[str] = mapped_column(String(32), nullable=False)
    planned_horizon_sessions: Mapped[int] = mapped_column(nullable=False, default=5)
    stop_fraction: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False, default=Decimal("0.02"))
    exit_order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_orders.id"))
    exit_status: Mapped[Optional[str]] = mapped_column(String(32))
    exited_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    exit_reason: Mapped[Optional[str]] = mapped_column(String(24))
    exit_decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    exit_reference_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())