"""Records of the paper-only loss feedback loop.

* ``StockTradeAttribution`` is an append-only explanation of one closed paper
  trade (forward-trial lot or autotrader round trip).  A trade whose evidence
  changes later (for example broker costs become known) gets a new revision;
  earlier revisions are never rewritten.
* ``StockParameterSet`` is an immutable, versioned parameter proposal for one
  paper strategy type, with the experiment and attributed losses behind it.
* ``StockParameterApplication`` is the mutable governed state of one parameter
  set (proposed -> governed cycle -> applied/rejected).  The strategy row is
  never edited; the applied set is overlaid at paper signal time.
* ``StockMetaLabelMode`` / ``StockMetaLabelModeEvent`` hold the per-strategy
  meta-label shadow/enforce state and its append-only transition evidence.
* ``StockAllocationWeight`` is the immutable per-session performance weight
  of one strategy type used by paper sizing.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, JSON, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StockTradeAttribution(Base):
    __tablename__ = "stock_trade_attributions"
    __table_args__ = (
        Index("uq_stock_trade_attributions_source_revision", "source_key", "revision", unique=True),
        Index("ix_stock_trade_attributions_strategy_type", "strategy_type"),
        Index("ix_stock_trade_attributions_symbol", "symbol"),
        Index("ix_stock_trade_attributions_closed_at", "closed_at"),
        Index("ix_stock_trade_attributions_trial_id", "trial_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attribution_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[Optional[int]] = mapped_column(Integer)
    strategy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trial_id: Mapped[Optional[str]] = mapped_column(String(36))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_cause: Mapped[str] = mapped_column(String(32), nullable=False)
    net_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 10))
    gross_return: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False)
    net_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    costs_known: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_loss: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False)
    paper_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockParameterSet(Base):
    __tablename__ = "stock_parameter_sets"
    __table_args__ = (
        Index("uq_stock_parameter_sets_type_version", "strategy_type", "version", unique=True),
        Index("uq_stock_parameter_sets_type_sha256", "strategy_type", "parameters_sha256", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    base_parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    experiment_id: Mapped[Optional[int]] = mapped_column(Integer)
    lineage: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    paper_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockParameterApplication(Base):
    __tablename__ = "stock_parameter_applications"
    __table_args__ = (
        Index("uq_stock_parameter_applications_set", "parameter_set_id", unique=True),
        Index("ix_stock_parameter_applications_type_status", "strategy_type", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_set_id: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    cycle_id: Mapped[Optional[str]] = mapped_column(String(64))
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_reason: Mapped[Optional[str]] = mapped_column(Text)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class StockMetaLabelMode(Base):
    __tablename__ = "stock_meta_label_modes"
    __table_args__ = (Index("uq_stock_meta_label_modes_type", "strategy_type", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class StockMetaLabelModeEvent(Base):
    __tablename__ = "stock_meta_label_mode_events"
    __table_args__ = (Index("ix_stock_meta_label_mode_events_type", "strategy_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    to_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockAllocationWeight(Base):
    __tablename__ = "stock_allocation_weights"
    __table_args__ = (
        Index("uq_stock_allocation_weights_session_type", "session_date", "strategy_type", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    strategy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    paper_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
