"""Durable records for the isolated live-broker boundary.

These tables intentionally do not reuse the stock-paper account, order, or
activity tables.  A live account is a separately reconciled source of truth.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LiveBrokerAccount(Base):
    __tablename__ = "live_broker_accounts"
    __table_args__ = (UniqueConstraint("broker", name="uq_live_broker_account_broker"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="alpaca_live")
    broker_account_id: Mapped[str] = mapped_column(String(96), nullable=False)
    environment: Mapped[str] = mapped_column(String(48), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    buying_power: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="uninitialized", index=True)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unexplained_residual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    halt_reason: Mapped[Optional[str]] = mapped_column(Text)
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    halted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    accounting_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    accounting_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    accounting_reviewed_by: Mapped[Optional[str]] = mapped_column(String(128))
    accounting_review_reason: Mapped[Optional[str]] = mapped_column(Text)
    accounting_review_digest: Mapped[Optional[str]] = mapped_column(String(64))
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class LiveBrokerPosition(Base):
    __tablename__ = "live_broker_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "symbol", name="uq_live_broker_position"),
        CheckConstraint("quantity >= 0", name="ck_live_broker_position_no_short"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("live_broker_accounts.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    market_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class LiveBrokerAccountSnapshot(Base):
    __tablename__ = "live_broker_account_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "observed_at", name="uq_live_broker_snapshot_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("live_broker_accounts.id"), nullable=False, index=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    buying_power: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="alpaca_live")
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class LiveBrokerOrder(Base):
    __tablename__ = "live_broker_orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_live_broker_order_idempotency"),
        UniqueConstraint("client_order_id", name="uq_live_broker_order_client_id"),
        UniqueConstraint("broker_order_id", name="uq_live_broker_order_broker_id"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_live_broker_order_side"),
        CheckConstraint("quantity > 0", name="ck_live_broker_order_quantity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("live_broker_accounts.id"), nullable=False, index=True)
    account_snapshot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("live_broker_account_snapshots.id"), index=True)
    model_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("stock_model_registry.run_id"), index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategy_signals.id"), index=True)
    risk_decision_id: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    risk_decision: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[Optional[str]] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(96))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="market")
    time_in_force: Mapped[str] = mapped_column(String(16), nullable=False, default="day")
    reference_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    reference_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    limit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    reserved_cash: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    submission_attempted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    uncertain_submission: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="live_control_room")
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class LiveBrokerFill(Base):
    __tablename__ = "live_broker_fills"
    __table_args__ = (
        UniqueConstraint("broker_activity_id", name="uq_live_broker_fill_activity"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_live_broker_fill_side"),
        CheckConstraint("quantity > 0", name="ck_live_broker_fill_quantity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("live_broker_accounts.id"), nullable=False, index=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("live_broker_orders.id"), index=True)
    broker_activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(96), index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class LiveBrokerActivity(Base):
    __tablename__ = "live_broker_activities"
    __table_args__ = (UniqueConstraint("broker_activity_id", name="uq_live_broker_activity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("live_broker_accounts.id"), nullable=False, index=True)
    broker_activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class LiveBrokerLedgerEvent(Base):
    __tablename__ = "live_broker_ledger_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("live_broker_accounts.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())