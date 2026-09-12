from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Asset, PaperTrade, RiskRule, Strategy
from app.schemas.trading import (
    AssetCreate,
    AssetRead,
    AllocationReviewDryRunRequest,
    AllocationReviewRequest,
    AuditLogRead,
    BacktestRequest,
    BacktestResponse,
    BrokerOrderRequest,
    BrokerOrderResponse,
    BrokerStatusResponse,
    CandidateActivationRequest,
    CandidateActivationResponse,
    CandidateDecisionJournalRead,
    CandidateDecisionJournalRequest,
    CandidateDecisionScorecardResponse,
    CompanyOpportunityRadarResponse,
    DashboardSnapshot,
    DeploymentMonitorResponse,
    EconomicImportResponse,
    EconomicIndicatorRead,
    MacroContextSummary,
    MarketImportRequest,
    MarketImportResponse,
    MarketRegimeDetectionRequest,
    MarketRegimeRead,
    LearningWorkerBatchRequest,
    LearningWorkerLaunchResponse,
    LearningWorkerScopeRequest,
    LearningWorkerStatusResponse,
    MemoryReplayResponse,
    ModelPerformanceResponse,
    ModelPredictionRequest,
    ModelPredictionResponse,
    ModelRealizationScoreResponse,
    NewsArticleRead,
    NewsImportRequest,
    NewsImportResponse,
    NewsSentimentSummary,
    NotificationRead,
    PaperTradeReduceRequest,
    PaperTradeReduceResponse,
    PaperTradeReconcileResponse,
    PaperTradeCloseResponse,
    PaperTradeRead,
    PaperTradingRunResponse,
    PaperTradingSignalRequest,
    PersistedModelRunResponse,
    PortfolioAllocationExecuteRequest,
    PortfolioAllocationExecuteResponse,
    PortfolioAllocationResponse,
    PortfolioRiskActionRequest,
    PortfolioRiskActionResponse,
    PortfolioRiskResponse,
    PriceHistoryResponse,
    IntradayBarRead, FeedStatusResponse, IntradayImportRequest,
    ReadinessResponse,
    RiskRuleRead,
    RiskSettingsUpdateRequest,
    SafetyControlRequest,
    SafetyControlResponse,
    ScannerRefreshJobRequest,
    ScannerRefreshJobResponse,
    SignalRequest,
    SignalResponse,
    StrategyExperimentRead,
    StrategyExperimentRunRequest,
    StrategyExperimentRunResponse,
    StrategyGovernanceScorecardSnapshot,
    StrategyMemoryRead,
    StrategyGovernanceResponse,
    StrategyImprovementQueueResponse,
    StrategyReactivationQueueResponse,
    StrategyReactivationReviewRequest,
    StrategyReactivationReviewResponse,
    StrategyRead,
    TradeCandidateEvidenceResponse,
    TradeCandidateResponse,
    TradeScorecardResponse,
    WatchlistDiscoveryResponse,
    WatchlistImportRequest,
    WatchlistImportResponse,
)
from app.services.backtester import BacktestConfig, run_backtest
from app.services.broker import block_live_order, broker_status, submit_paper_order
from app.services.candidate_activation import review_candidate_activation
from app.services.candidate_evidence import candidate_evidence_drilldown
from app.services.decision_journal import decision_journal_scorecard, list_candidate_decisions, record_candidate_decision, refresh_decision_journal_outcomes, update_journal_realized_outcomes, update_strategy_memory_from_journal
from app.services.deployment_monitor import deployment_monitor_snapshot, run_deployment_monitor
from app.services.stock_monitoring import latest_stock_monitoring, run_stock_monitoring
from app.services.economic_data import import_fallback_economic_indicators, list_economic_indicators, summarize_macro_context
from app.services.experiments import list_strategy_experiments, run_strategy_experiments
from app.services.governance import evaluate_strategy_governance, latest_strategy_governance_scorecard
from app.services.learning import propose_parameter_experiments
from app.services.market_data import get_price_points, import_market_prices
from app.services.intraday_data import feed_status, ingest_intraday, preflight_intraday
from app.models import IntradayBar
from app.services.trusted_data import trusted_history, UntrustedMarketData
from app.services.market_regime import detect_and_store_market_regime, latest_market_regime, list_market_regimes
from app.services.memory_replay import memory_replay_evaluation, run_memory_replay_gate_monitor
from app.services.model_tracking import model_performance_summary, run_and_persist_model_predictions, score_realized_predictions
from app.services.news_sentiment import import_company_news, list_news_articles, summarize_news_context
from app.services.notifications import acknowledge_notification, list_notifications, resolve_notification
from app.services.opportunity_radar import company_opportunity_radar
from app.services.paper_trading import close_paper_trade, reconcile_open_paper_trades, reduce_paper_trade, run_paper_signal
from app.services.stock_paper_ledger import stock_paper_status
from app.services.portfolio_allocation import allocation_plan, allocation_review_queue, dry_run_approved_allocation_review, execute_allocation_plan, review_allocation_queue_item
from app.services.risk_actions import evaluate_portfolio_risk_actions
from app.services.portfolio_risk import portfolio_risk_snapshot
from app.services.probabilistic_model import predict_probabilities
from app.services.reactivation import reactivation_queue, review_strategy_reactivation
from app.services.readiness import readiness_snapshot
from app.services.risk import PortfolioState, StrategyState, approve_trade
from app.services.risk_settings import get_active_risk_rule, update_risk_settings
from app.services.safety_controls import disable_kill_switch, enable_kill_switch, pause_all_strategies, resume_candidate_strategies
from app.services.scanner_refresh_jobs import create_scanner_refresh_job, latest_scanner_refresh_job, run_scanner_refresh_job
from app.services.strategy_improvement import strategy_improvement_queue
from app.services.strategies.registry import STRATEGY_REGISTRY, get_strategy
from app.services.trade_candidates import get_trade_candidate_snapshot
from app.services.trade_scorecard import predictive_trade_scorecard
from app.services.watchlist_discovery import discover_watchlist_candidates, import_watchlist_candidates
from app.tasks.celery_app import celery_app
from app.tasks.jobs import strategy_learning_batch_job, strategy_learning_scope_job

router = APIRouter()
LEGACY_STOCK_EVIDENCE_QUARANTINE = (
    "Legacy simulator paper_trades/StrategyMemory are quarantined and cannot be "
    "used for stock risk, scoring, allocation, or governance."
)


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "paper_only": True, "live_trading": False}


@router.get("/auth/session")
def auth_session(request: Request) -> dict:
    """Return the service principal's role, never its credential."""
    return {"role": request.state.actor}


@router.get("/system/readiness", response_model=ReadinessResponse)
def system_readiness(db: Session = Depends(get_db)) -> dict:
    return readiness_snapshot(db)


@router.get("/system/deployment-monitor", response_model=DeploymentMonitorResponse)
def system_deployment_monitor(db: Session = Depends(get_db)) -> dict:
    return deployment_monitor_snapshot(db)


@router.post("/system/deployment-monitor/run", response_model=dict)
def run_system_deployment_monitor(db: Session = Depends(get_db)) -> dict:
    return run_deployment_monitor(db, source="manual_api")


@router.get("/system/stock-monitoring", response_model=dict)
def system_stock_monitoring(db: Session = Depends(get_db)) -> dict:
    return latest_stock_monitoring(db)


@router.post("/system/stock-monitoring/run", response_model=dict)
def run_system_stock_monitoring(db: Session = Depends(get_db)) -> dict:
    return run_stock_monitoring(db, source="manual_api")


@router.get("/trade-candidates", response_model=TradeCandidateResponse)
def trade_candidates(limit: int = 12, refresh: bool = False, db: Session = Depends(get_db)) -> dict:
    return get_trade_candidate_snapshot(db, limit=limit, refresh=refresh)


@router.get("/trade-candidates/evidence", response_model=TradeCandidateEvidenceResponse)
def trade_candidate_evidence(symbol: str, strategy: str, db: Session = Depends(get_db)) -> dict:
    try:
        return candidate_evidence_drilldown(db, symbol, strategy)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/trade-candidates/refresh-jobs", response_model=ScannerRefreshJobResponse)
def start_trade_candidate_refresh_job(
    payload: ScannerRefreshJobRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    job = create_scanner_refresh_job(db, trigger=payload.trigger, limit=payload.limit, context=payload.context)
    background_tasks.add_task(run_scanner_refresh_job, job["id"])
    return job


@router.get("/trade-candidates/refresh-jobs/latest", response_model=Optional[ScannerRefreshJobResponse])
def trade_candidate_refresh_job_status(db: Session = Depends(get_db)) -> Optional[dict]:
    return latest_scanner_refresh_job(db)


@router.get("/trade-candidates/decision-journal", response_model=list[CandidateDecisionJournalRead])
def candidate_decision_journal(
    limit: int = 25,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    update_journal_realized_outcomes(db)
    return list_candidate_decisions(db, limit=limit, symbol=symbol, strategy_type=strategy)


@router.post("/trade-candidates/decision-journal", response_model=CandidateDecisionJournalRead)
def create_candidate_decision_journal_entry(payload: CandidateDecisionJournalRequest, db: Session = Depends(get_db)) -> dict:
    try:
        row = record_candidate_decision(
            db,
            symbol=payload.symbol,
            strategy_type=payload.strategy,
            decision=payload.decision,
            status=payload.status,
            reason=payload.reason,
        )
        db.commit()
        return row
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/trade-candidates/decision-scorecard", response_model=CandidateDecisionScorecardResponse)
def candidate_decision_scorecard(limit: int = 100, db: Session = Depends(get_db)) -> dict:
    return decision_journal_scorecard(db, limit=limit)


@router.post("/trade-candidates/decision-scorecard/update-memory")
def update_decision_scorecard_memory(db: Session = Depends(get_db)) -> dict:
    return update_strategy_memory_from_journal(db)


@router.get("/trade-candidates/memory-replay", response_model=MemoryReplayResponse)
def trade_candidate_memory_replay(limit: int = 50, top_k: int = 3, db: Session = Depends(get_db)) -> dict:
    return memory_replay_evaluation(db, limit=limit, top_k=top_k, notify_gate_opens=True)


@router.post("/trade-candidates/memory-replay/monitor", response_model=dict)
def trade_candidate_memory_replay_monitor(limit: int = 60, top_k: int = 3, db: Session = Depends(get_db)) -> dict:
    return run_memory_replay_gate_monitor(db, source="manual_api", limit=limit, top_k=top_k)


@router.get("/opportunity-radar", response_model=CompanyOpportunityRadarResponse)
def opportunity_radar(limit: int = 8, refresh_news: bool = False, news_provider: str = "auto", db: Session = Depends(get_db)) -> dict:
    try:
        return company_opportunity_radar(db, limit=limit, refresh_news=refresh_news, news_provider=news_provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/trade-candidates/activation-review", response_model=CandidateActivationResponse)
def activate_trade_candidate(payload: CandidateActivationRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return review_candidate_activation(db, payload.symbol, payload.strategy, payload.decision, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/trade-scorecard", response_model=TradeScorecardResponse)
def trade_scorecard(limit: int = 50, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.get("/assets", response_model=list[AssetRead])
def list_assets(db: Session = Depends(get_db)) -> list[Asset]:
    return db.query(Asset).order_by(Asset.symbol).all()


@router.post("/assets", response_model=AssetRead)
def create_asset(payload: AssetCreate, db: Session = Depends(get_db)) -> Asset:
    symbol = payload.symbol.upper()
    existing = db.query(Asset).filter(Asset.symbol == symbol).one_or_none()
    if existing:
        return existing
    asset = Asset(symbol=symbol, name=payload.name, sector=payload.sector, industry=payload.industry)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


@router.get("/watchlist/discover", response_model=WatchlistDiscoveryResponse)
def discover_watchlist(limit: int = 10, db: Session = Depends(get_db)) -> dict:
    return discover_watchlist_candidates(db, limit=limit)


@router.post("/watchlist/import", response_model=WatchlistImportResponse)
def import_watchlist(payload: WatchlistImportRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return import_watchlist_candidates(
            db,
            symbols=payload.symbols,
            limit=payload.limit,
            period=payload.period,
            import_prices=payload.import_prices,
            import_news=payload.import_news,
            news_provider=payload.news_provider,
            refresh_candidates=payload.refresh_candidates,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/strategies", response_model=list[StrategyRead])
def list_strategies(db: Session = Depends(get_db)) -> list[Strategy]:
    return db.query(Strategy).order_by(Strategy.name).all()


@router.get("/strategies/governance-scorecard", response_model=StrategyGovernanceScorecardSnapshot)
def get_latest_strategy_governance_scorecard(db: Session = Depends(get_db)) -> dict:
    return latest_strategy_governance_scorecard(db)


@router.post("/market-data/import", response_model=MarketImportResponse)
def import_prices(payload: MarketImportRequest, db: Session = Depends(get_db)) -> dict:
    result = import_market_prices(db, payload.symbol, payload.period)
    result["decision_journal"] = refresh_decision_journal_outcomes(db, source="market_data_import", notify=True)
    result["memory_replay_gate_monitor"] = run_memory_replay_gate_monitor(db, source="market_data_import", limit=60, top_k=3)
    return result


@router.get("/market-data/{symbol}", response_model=PriceHistoryResponse)
def price_history(symbol: str, limit: int = 260, db: Session = Depends(get_db)) -> dict:
    rows, source = get_price_points(db, symbol, limit=limit)
    return {"symbol": symbol.upper(), "source": source, "rows": rows}

@router.post("/market-data/intraday/preflight")
def intraday_preflight(db: Session = Depends(get_db)):
    return preflight_intraday(db)

@router.get("/market-data/intraday/{symbol}", response_model=list[IntradayBarRead])
def intraday_history(symbol: str, limit: int = 390, db: Session = Depends(get_db)):
    return db.query(IntradayBar).filter(IntradayBar.symbol == symbol.upper()).order_by(IntradayBar.opened_at.desc()).limit(min(limit, 2000)).all()

@router.get("/market-data/intraday/{symbol}/status", response_model=FeedStatusResponse)
def intraday_feed_status(symbol: str, db: Session = Depends(get_db)):
    return feed_status(db, symbol)

@router.post("/market-data/intraday/ingest")
def ingest_intraday_route(payload: IntradayImportRequest, db: Session = Depends(get_db)):
    return ingest_intraday(db, payload.symbols)


def _research_prices(db, symbol, limit, minimum):
    try:
        return trusted_history(db, symbol, limit, minimum=minimum)
    except UntrustedMarketData as exc:
        raise HTTPException(status_code=409, detail={"status": "blocked", "symbol": symbol.strip().upper(), "reason": str(exc)}) from exc


@router.post("/signals", response_model=SignalResponse)
def generate_signal(payload: SignalRequest, db: Session = Depends(get_db)) -> SignalResponse:
    strategy_row = db.query(Strategy).filter(Strategy.strategy_type == payload.strategy).one_or_none()
    parameters = strategy_row.parameters if strategy_row else {}
    if payload.strategy not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail="Unknown strategy.")
    prices, _source = _research_prices(db, payload.symbol, 260, 60)
    signal = get_strategy(payload.strategy, parameters).generate_signal(payload.symbol.upper(), prices)
    news_context = summarize_news_context(db, payload.symbol)
    return SignalResponse(
        action=signal.action,
        probability_up=signal.probability_up,
        probability_down=signal.probability_down,
        confidence=signal.confidence,
        reason=f"{signal.reason} News context: {news_context['summary']}",
        features=signal.features | {"news_context": news_context},
    )


@router.post("/backtests", response_model=BacktestResponse)
def backtest(payload: BacktestRequest, db: Session = Depends(get_db)) -> dict:
    prices, source = _research_prices(db, payload.symbol, 320, 80)
    config = BacktestConfig(
        starting_cash=payload.starting_cash,
        risk_per_trade=payload.risk_per_trade,
        fees_bps=payload.fees_bps,
        slippage_bps=payload.slippage_bps,
    )
    strategy_row = db.query(Strategy).filter(Strategy.strategy_type == payload.strategy).one_or_none()
    parameters = strategy_row.parameters if strategy_row else {}
    result = run_backtest(payload.symbol.upper(), payload.strategy, prices, config, parameters)
    return result | {"source": source}


@router.post("/models/predict", response_model=ModelPredictionResponse)
def model_prediction(payload: ModelPredictionRequest, db: Session = Depends(get_db)) -> dict:
    prices, source = _research_prices(db, payload.symbol, 420, 140)
    result = predict_probabilities(payload.symbol.upper(), prices, source)
    return result | {"generated_at": datetime.utcnow(), "macro_context": summarize_macro_context(db)}


@router.post("/models/run", response_model=PersistedModelRunResponse)
def run_and_save_model_prediction(payload: ModelPredictionRequest, db: Session = Depends(get_db)) -> dict:
    result = run_and_persist_model_predictions(db, payload.symbol)
    return result | {"generated_at": datetime.utcnow()}


@router.post("/models/score-realized", response_model=ModelRealizationScoreResponse)
def score_model_realizations(payload: ModelPredictionRequest, db: Session = Depends(get_db)) -> dict:
    return score_realized_predictions(db, payload.symbol)


@router.get("/models/performance", response_model=ModelPerformanceResponse)
def model_performance(symbol: Optional[str] = None, limit: int = 50, db: Session = Depends(get_db)) -> dict:
    return model_performance_summary(db, symbol, min(limit, 200))


@router.post("/economic/import", response_model=EconomicImportResponse)
def import_economic_data(db: Session = Depends(get_db)) -> dict:
    return import_fallback_economic_indicators(db)


@router.get("/economic/indicators", response_model=list[EconomicIndicatorRead])
def economic_indicators(indicator_name: Optional[str] = None, limit: int = 100, db: Session = Depends(get_db)) -> list:
    return list_economic_indicators(db, indicator_name, limit)


@router.get("/economic/context", response_model=MacroContextSummary)
def economic_context(db: Session = Depends(get_db)) -> dict:
    return summarize_macro_context(db)


@router.get("/market-regimes", response_model=list[MarketRegimeRead])
def market_regimes(limit: int = 30, db: Session = Depends(get_db)) -> list[dict]:
    return list_market_regimes(db, limit)


@router.get("/market-regimes/latest", response_model=MarketRegimeRead)
def latest_regime(db: Session = Depends(get_db)) -> dict:
    regime = latest_market_regime(db)
    if not regime:
        raise HTTPException(status_code=404, detail="No market regime available.")
    return regime


@router.post("/market-regimes/detect", response_model=MarketRegimeRead)
def detect_regime(payload: MarketRegimeDetectionRequest, db: Session = Depends(get_db)) -> dict:
    return detect_and_store_market_regime(db, payload.symbol)


@router.post("/news/import", response_model=NewsImportResponse)
def import_news(payload: NewsImportRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return import_company_news(db, payload.symbol, payload.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/news/{symbol}/summary", response_model=NewsSentimentSummary)
def news_summary(symbol: str, limit: int = 10, db: Session = Depends(get_db)) -> dict:
    return summarize_news_context(db, symbol, limit=min(limit, 50))


@router.get("/news", response_model=list[NewsArticleRead])
def news_articles(symbol: Optional[str] = None, limit: int = 25, db: Session = Depends(get_db)) -> list:
    return list_news_articles(db, symbol, limit)


@router.get("/paper-trades", response_model=list[PaperTradeRead])
def list_paper_trades(db: Session = Depends(get_db)) -> list[PaperTrade]:
    return db.query(PaperTrade).order_by(PaperTrade.created_at.desc()).limit(50).all()


@router.get("/portfolio/risk", response_model=PortfolioRiskResponse)
def get_portfolio_risk(db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.get("/portfolio/allocation-plan", response_model=PortfolioAllocationResponse)
def get_portfolio_allocation_plan(limit: int = 20, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.get("/portfolio/allocation-review-queue", response_model=dict)
def get_portfolio_allocation_review_queue(limit: int = 20, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.post("/portfolio/allocation-review-queue/review", response_model=dict)
def review_portfolio_allocation_queue_item(payload: AllocationReviewRequest, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.post("/portfolio/allocation-review-queue/dry-run", response_model=dict)
def dry_run_portfolio_allocation_review_item(payload: AllocationReviewDryRunRequest, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.post("/portfolio/allocation-plan/execute", response_model=PortfolioAllocationExecuteResponse)
def run_portfolio_allocation_plan(payload: PortfolioAllocationExecuteRequest, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.post("/portfolio/risk/actions", response_model=PortfolioRiskActionResponse)
def run_portfolio_risk_actions(payload: PortfolioRiskActionRequest, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.post("/paper-trading/run-signal", response_model=PaperTradingRunResponse)
def run_paper_trading_signal(payload: PaperTradingSignalRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return run_paper_signal(db, payload.symbol, payload.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/paper-trading/reconcile", response_model=PaperTradeReconcileResponse)
def reconcile_paper_trading(db: Session = Depends(get_db)) -> dict:
    # Legacy simulator reconciliation must not mutate simulator rows or promote
    # them into decision evidence. Broker reconciliation lives at /stock-paper.
    return reconcile_open_paper_trades(db)


@router.post("/paper-trading/close/{trade_id}", response_model=PaperTradeCloseResponse)
def close_paper_trading_trade(trade_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return close_paper_trade(db, trade_id, "Manual paper close from control room.")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/paper-trading/reduce/{trade_id}", response_model=PaperTradeReduceResponse)
def reduce_paper_trading_trade(trade_id: int, payload: PaperTradeReduceRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return reduce_paper_trade(db, trade_id, payload.reduce_pct, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/strategy-memory", response_model=list[StrategyMemoryRead])
def list_strategy_memory(db: Session = Depends(get_db)) -> list:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.get("/audit-logs", response_model=list[AuditLogRead])
def list_audit_logs(
    limit: int = 50,
    event_type: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list:
    from app.models import AuditLog

    query = db.query(AuditLog)
    if event_type:
        query = query.filter(AuditLog.event_type == event_type)
    if action:
        query = query.filter(AuditLog.action == action)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    return query.order_by(AuditLog.created_at.desc()).limit(min(limit, 200)).all()


@router.get("/notifications", response_model=list[NotificationRead])
def get_notifications(
    status: Optional[str] = None,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list:
    return list_notifications(db, status=status, category=category, severity=severity, limit=limit)


@router.post("/notifications/{notification_id}/acknowledge", response_model=NotificationRead)
def acknowledge_notification_route(notification_id: int, db: Session = Depends(get_db)):
    try:
        return acknowledge_notification(db, notification_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/notifications/{notification_id}/resolve", response_model=NotificationRead)
def resolve_notification_route(notification_id: int, db: Session = Depends(get_db)):
    try:
        return resolve_notification(db, notification_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/strategies/evaluate", response_model=StrategyGovernanceResponse)
def evaluate_strategies(db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.get("/strategies/reactivation-queue", response_model=StrategyReactivationQueueResponse)
def get_reactivation_queue(db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.get("/strategies/improvement-queue", response_model=StrategyImprovementQueueResponse)
def get_strategy_improvement_queue(db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.post("/strategies/reactivation-review", response_model=StrategyReactivationReviewResponse)
def review_reactivation(payload: StrategyReactivationReviewRequest, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail=LEGACY_STOCK_EVIDENCE_QUARANTINE)


@router.post("/risk/approve")
def check_risk(signal: dict) -> dict:
    approved, reason = approve_trade(
        signal,
        PortfolioState(open_positions_count=2, open_positions_by_symbol={signal.get("symbol", ""): 0.04}),
        StrategyState(status="paper_trading_active"),
    )
    return {"approved": approved, "reason": reason}


@router.get("/risk-rules", response_model=list[RiskRuleRead])
def list_risk_rules(db: Session = Depends(get_db)) -> list[RiskRule]:
    return db.query(RiskRule).order_by(RiskRule.name).all()


@router.get("/risk/settings", response_model=RiskRuleRead)
def get_risk_settings(db: Session = Depends(get_db)) -> RiskRule:
    return get_active_risk_rule(db)


@router.patch("/risk/settings", response_model=RiskRuleRead)
def patch_risk_settings(payload: RiskSettingsUpdateRequest, db: Session = Depends(get_db)) -> RiskRule:
    updates = payload.model_dump(exclude_unset=True, exclude={"reason"})
    return update_risk_settings(db, updates, payload.reason)


@router.post("/safety/kill-switch/enable", response_model=SafetyControlResponse)
def enable_global_kill_switch(payload: SafetyControlRequest, db: Session = Depends(get_db)) -> dict:
    return enable_kill_switch(db, payload.reason)


@router.post("/safety/kill-switch/disable", response_model=SafetyControlResponse)
def disable_global_kill_switch(payload: SafetyControlRequest, db: Session = Depends(get_db)) -> dict:
    return disable_kill_switch(db, payload.reason)


@router.post("/safety/strategies/pause", response_model=SafetyControlResponse)
def pause_strategies(payload: SafetyControlRequest, db: Session = Depends(get_db)) -> dict:
    return pause_all_strategies(db, payload.reason)


@router.post("/safety/strategies/resume", response_model=SafetyControlResponse)
def resume_strategies(payload: SafetyControlRequest, db: Session = Depends(get_db)) -> dict:
    return resume_candidate_strategies(db, payload.reason)


@router.get("/broker/status", response_model=BrokerStatusResponse)
def get_broker_status() -> dict:
    return broker_status()


@router.post("/broker/paper/orders", response_model=BrokerOrderResponse)
def submit_manual_paper_order(payload: BrokerOrderRequest, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=409, detail="Manual broker orders are disabled. Use /paper-trading/run-signal for risk-gated paper execution.")


@router.post("/broker/live/orders", response_model=BrokerOrderResponse)
def block_manual_live_order(payload: BrokerOrderRequest, db: Session = Depends(get_db)) -> dict:
    result = block_live_order(db, payload.model_dump())
    db.commit()
    return result


@router.get("/experiments", response_model=list[StrategyExperimentRead])
def list_experiments(symbol: Optional[str] = None, strategy: Optional[str] = None, limit: int = 50, db: Session = Depends(get_db)) -> list:
    return list_strategy_experiments(db, symbol=symbol, strategy_slug=strategy, limit=limit)


@router.post("/experiments/run", response_model=StrategyExperimentRunResponse)
def run_experiments(payload: StrategyExperimentRunRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return run_strategy_experiments(
            db,
            symbol=payload.symbol,
            strategy_slug=payload.strategy,
            max_candidates=payload.max_candidates,
            apply_promotions=payload.apply_promotions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/learning/workers/scope", response_model=LearningWorkerLaunchResponse)
def launch_learning_scope_worker(payload: LearningWorkerScopeRequest) -> dict:
    try:
        task = strategy_learning_scope_job.apply_async(
            args=[payload.symbol.upper(), payload.strategy, payload.max_candidates],
            queue="learning",
        )
    except Exception as exc:
        return {
            "task_id": "",
            "status": "unavailable",
            "queue": "learning",
            "paper_only": True,
            "message": f"Learning worker queue unavailable: {exc.__class__.__name__}: {exc}",
        }
    return {
        "task_id": task.id,
        "status": "queued",
        "queue": "learning",
        "paper_only": True,
        "message": "Queued one symbol+strategy paper-only learning worker.",
    }


@router.post("/learning/workers/batch", response_model=LearningWorkerLaunchResponse)
def launch_learning_batch_workers(payload: LearningWorkerBatchRequest) -> dict:
    try:
        task = strategy_learning_batch_job.apply_async(
            args=[payload.limit_symbols, payload.limit_strategies, payload.max_candidates],
            queue="learning",
        )
    except Exception as exc:
        return {
            "task_id": "",
            "status": "unavailable",
            "queue": "learning",
            "paper_only": True,
            "message": f"Learning worker queue unavailable: {exc.__class__.__name__}: {exc}",
        }
    return {
        "task_id": task.id,
        "status": "queued",
        "queue": "learning",
        "paper_only": True,
        "message": "Queued a paper-only learning batch that fans out symbol+strategy scopes.",
    }


@router.get("/learning/workers/{task_id}", response_model=LearningWorkerStatusResponse)
def learning_worker_status(task_id: str) -> dict:
    try:
        result = celery_app.AsyncResult(task_id)
        ready = result.ready()
        payload = result.result if ready else None
        status = result.status
        successful = result.successful() if ready else None
    except Exception as exc:
        return {
            "task_id": task_id,
            "status": "unavailable",
            "ready": False,
            "successful": None,
            "result": {"error_type": exc.__class__.__name__, "error": str(exc)},
        }
    return {
        "task_id": task_id,
        "status": status,
        "ready": ready,
        "successful": successful,
        "result": payload if isinstance(payload, (dict, list)) else {"message": str(payload)} if payload is not None else None,
    }


@router.get("/dashboard", response_model=DashboardSnapshot)
def dashboard(db: Session = Depends(get_db)) -> DashboardSnapshot:
    strategies = db.query(Strategy).order_by(Strategy.name).all()
    risk_rule = db.query(RiskRule).first()
    try:
        stock_ledger = stock_paper_status(db)
    except KeyError:
        # Isolated dashboard unit doubles may not register stock-ledger models;
        # production database failures are deliberately not hidden.
        stock_ledger = {"status": "uninitialized", "account": None, "positions": [], "equity_snapshots": []}
    ledger_account = stock_ledger.get("account") if isinstance(stock_ledger, dict) else None
    ledger_ready = bool(
        isinstance(ledger_account, dict)
        and stock_ledger.get("status") == "reconciled"
        and ledger_account.get("equity") is not None
    )
    accounting_verified = bool(ledger_ready and ledger_account.get("accounting_verified") and stock_ledger.get("costs_known"))
    snapshots = list(reversed(stock_ledger.get("equity_snapshots") or [])) if ledger_ready else []
    equity_curve = [
        {"date": snapshot["observed_at"], "equity": float(snapshot["equity"])}
        for snapshot in snapshots
    ]
    paper_account_value = float(ledger_account["equity"]) if ledger_ready else None
    # Observation-to-observation changes are not necessarily calendar-day P/L or
    # a funding-adjusted total return. Keep both absent until full flows/costs are
    # verified by a future broker-complete accounting integration.
    daily_pl = None
    total_pl = None
    strategy_cards = [
        {
            "id": strategy.id,
            "name": strategy.name,
            "status": strategy.current_status,
            "score": None,
            "win_rate": None,
            "drawdown": None,
            "profit_factor": None,
            "last_updated": None,
            "metrics_status": "unavailable",
        }
        for strategy in strategies
    ]
    # Legacy simulator records are quarantined and never presented as trades
    # supporting stock-paper performance/governance.
    recent_trades = []
    stored_experiments = list_strategy_experiments(db, strategy_slug="moving_average_crossover", limit=3)
    proposed_experiments = propose_parameter_experiments("moving_average_crossover", {"short_window": 20, "long_window": 50})
    current_regime = latest_market_regime(db, auto_detect=False)
    kill_switch_enabled = bool(((risk_rule.value if risk_rule else {}) or {}).get("kill_switch_enabled", False))
    return DashboardSnapshot(
        paper_account_value=paper_account_value,
        daily_pl=daily_pl,
        total_pl=total_pl,
        metrics_status="reconciled" if accounting_verified else "unavailable",
        performance_note=(
            "Broker account balance is reconciled, but P/L is unavailable until complete flows and costs are verified."
            if ledger_ready else
            "Verified broker paper account is not reconciled. Legacy recorded trades below are nonqualifying evidence."
        ),
        active_strategies=sum(1 for strategy in strategies if strategy.current_status == "paper_trading_active"),
        paused_strategies=sum(1 for strategy in strategies if strategy.current_status == "paused"),
        best_strategy=None,
        worst_strategy=None,
        open_paper_trades=len(stock_ledger.get("positions") or []) if ledger_ready else 0,
        risk_state=(
            f"Kill switch {'enabled' if kill_switch_enabled else 'disabled'}. "
            f"Paper-only. Regime: {(current_regime or {}).get('market_regime', 'unclassified')}."
        ),
        equity_curve=equity_curve,
        strategies=strategy_cards,
        recent_trades=recent_trades,
        experiments=[
            {
                "experiment_name": experiment.experiment_name,
                "old_parameters": experiment.old_parameters or {},
                "new_parameters": experiment.new_parameters or {},
                "hypothesis": experiment.hypothesis or "",
                "decision": experiment.decision or "unknown",
            }
            for experiment in stored_experiments
        ] or proposed_experiments,
        risk_rules=[{"name": risk_rule.name, "value": risk_rule.value, "is_active": risk_rule.is_active}] if risk_rule else [],
        generated_at=datetime.utcnow().date(),
    )
