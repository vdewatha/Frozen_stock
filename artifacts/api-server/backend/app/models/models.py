from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    asset_type: Mapped[str] = mapped_column(String(32), default="stock")
    sector: Mapped[Optional[str]] = mapped_column(String(128))
    industry: Mapped[Optional[str]] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (UniqueConstraint("symbol", "price_date", name="uq_market_prices_symbol_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    price_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    high: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    low: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    adjusted_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    volume: Mapped[Optional[int]] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(64), default="unknown")
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IntradayBar(Base):
    __tablename__ = "intraday_bars"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "opened_at", name="uq_intraday_bar"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="1m")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    volume: Mapped[int] = mapped_column(BigInteger)
    provider: Mapped[str] = mapped_column(String(32), default="alpaca")
    feed_class: Mapped[str] = mapped_column(String(32), default="sip")
    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (UniqueConstraint("symbol", "action_type", "ex_date", "value", name="uq_corporate_action"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action_type: Mapped[str] = mapped_column(String(32))
    ex_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    provider: Mapped[str] = mapped_column(String(32), default="alpaca")
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    source: Mapped[Optional[str]] = mapped_column(String(128))
    title: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    sentiment_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    relevance_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)


class EconomicIndicator(Base):
    __tablename__ = "economic_indicators"
    __table_args__ = (UniqueConstraint("indicator_name", "observation_date", name="uq_economic_indicator_name_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    indicator_name: Mapped[str] = mapped_column(String(128), index=True)
    observation_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    unit: Mapped[Optional[str]] = mapped_column(String(64))
    source: Mapped[Optional[str]] = mapped_column(String(128))
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketRegime(Base):
    __tablename__ = "market_regimes"

    id: Mapped[int] = mapped_column(primary_key=True)
    regime_date: Mapped[date] = mapped_column(Date, unique=True)
    spy_trend: Mapped[Optional[str]] = mapped_column(String(64))
    volatility_regime: Mapped[Optional[str]] = mapped_column(String(64))
    rate_regime: Mapped[Optional[str]] = mapped_column(String(64))
    market_regime: Mapped[Optional[str]] = mapped_column(String(64))
    features: Mapped[Optional[dict]] = mapped_column(JSON)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    strategy_type: Mapped[str] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)
    parameters: Mapped[dict] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    current_status: Mapped[str] = mapped_column(String(64), default="research")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    backtests: Mapped[list["StrategyBacktest"]] = relationship(back_populates="strategy")
    signals: Mapped[list["StrategySignal"]] = relationship(back_populates="strategy")
    paper_trades: Mapped[list["PaperTrade"]] = relationship(back_populates="strategy")


class StrategyBacktest(Base):
    __tablename__ = "strategy_backtests"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    train_start: Mapped[Optional[date]] = mapped_column(Date)
    train_end: Mapped[Optional[date]] = mapped_column(Date)
    test_start: Mapped[Optional[date]] = mapped_column(Date)
    test_end: Mapped[Optional[date]] = mapped_column(Date)
    parameters: Mapped[Optional[dict]] = mapped_column(JSON)
    total_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    annualized_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    sharpe_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    sortino_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    number_of_trades: Mapped[Optional[int]]
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    strategy: Mapped[Optional[Strategy]] = relationship(back_populates="backtests")


class StrategySignal(Base):
    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    signal_time: Mapped[datetime] = mapped_column(DateTime)
    action: Mapped[str] = mapped_column(String(16))
    probability_up: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    probability_down: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    features: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    strategy: Mapped[Optional[Strategy]] = relationship(back_populates="signals")


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16))
    entry_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    entry_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    status: Mapped[Optional[str]] = mapped_column(String(32))
    profit_loss: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    profit_loss_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    reason_entered: Mapped[Optional[str]] = mapped_column(Text)
    reason_exited: Mapped[Optional[str]] = mapped_column(Text)
    features_at_entry: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    strategy: Mapped[Optional[Strategy]] = relationship(back_populates="paper_trades")


class StrategyMemory(Base):
    __tablename__ = "strategy_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    market_regime: Mapped[Optional[str]] = mapped_column(String(64))
    symbol: Mapped[Optional[str]] = mapped_column(String(16))
    sample_size: Mapped[Optional[int]]
    avg_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    avg_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    confidence_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    last_updated: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    notes: Mapped[Optional[str]] = mapped_column(Text)


class RiskRule(Base):
    __tablename__ = "risk_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    value: Mapped[dict] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    experiment_name: Mapped[Optional[str]] = mapped_column(String(255))
    old_parameters: Mapped[Optional[dict]] = mapped_column(JSON)
    new_parameters: Mapped[Optional[dict]] = mapped_column(JSON)
    hypothesis: Mapped[Optional[str]] = mapped_column(Text)
    backtest_result_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategy_backtests.id"))
    paper_result_summary: Mapped[Optional[dict]] = mapped_column(JSON)
    decision: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CandidateDecisionJournal(Base):
    __tablename__ = "candidate_decision_journal"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"), index=True)
    strategy_type: Mapped[str] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON)
    paper_trade_id: Mapped[Optional[int]] = mapped_column(ForeignKey("paper_trades.id"), index=True)
    realized_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    realized_status: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    entity_id: Mapped[Optional[int]] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(64))
    message: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    source: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[Optional[str]] = mapped_column(Text)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    entity_id: Mapped[Optional[int]] = mapped_column(index=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ModelPrediction(Base):
    __tablename__ = "model_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    prediction_date: Mapped[date] = mapped_column(Date, index=True)
    horizon_days: Mapped[int] = mapped_column(index=True)
    probability_up: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    probability_down: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    expected_return: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    source: Mapped[str] = mapped_column(String(64))
    features: Mapped[Optional[dict]] = mapped_column(JSON)
    probabilities_by_model: Mapped[Optional[dict]] = mapped_column(JSON)
    realized_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    realized_up: Mapped[Optional[bool]] = mapped_column(Boolean)
    brier_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    is_realized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ModelValidationFold(Base):
    __tablename__ = "model_validation_folds"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    horizon_days: Mapped[int] = mapped_column(index=True)
    fold: Mapped[int]
    train_start: Mapped[date] = mapped_column(Date)
    train_end: Mapped[date] = mapped_column(Date)
    test_start: Mapped[date] = mapped_column(Date)
    test_end: Mapped[date] = mapped_column(Date)
    sample_size: Mapped[int]
    accuracy: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    brier_score: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class TradeCandidateSnapshot(Base):
    __tablename__ = "trade_candidate_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(64), default="scanner")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ScannerRefreshJob(Base):
    __tablename__ = "scanner_refresh_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(64), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ResearchModelRun(Base):
    __tablename__ = "research_model_runs"
    __table_args__ = (
        CheckConstraint("status = 'experimental'", name="ck_research_run_experimental"),
        CheckConstraint("eligible_for_trading = false", name="ck_research_run_ineligible"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="experimental")
    eligible_for_trading: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    training_metadata: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
