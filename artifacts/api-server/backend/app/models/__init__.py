from __future__ import annotations

from app.models.crypto_data import CryptoCandle, CollectionRun
from app.models import shadow, execution  # Register priority-package tables with Base.

from app.models.models import (
    Asset,
    AuditLog,
    CandidateDecisionJournal,
    EconomicIndicator,
    MarketPrice,
    MarketRegime,
    ModelPrediction,
    ModelValidationFold,
    NewsArticle,
    Notification,
    PaperTrade,
    RiskRule,
    ResearchModelRun,
    ScannerRefreshJob,
    Strategy,
    StrategyBacktest,
    StrategyExperiment,
    StrategyMemory,
    StrategySignal,
    TradeCandidateSnapshot,
)

__all__ = [
    "Asset",
    "AuditLog",
    "CandidateDecisionJournal",
    "EconomicIndicator",
    "MarketPrice",
    "MarketRegime",
    "ModelPrediction",
    "ModelValidationFold",
    "NewsArticle",
    "Notification",
    "PaperTrade",
    "RiskRule",
    "ResearchModelRun",
    "ScannerRefreshJob",
    "Strategy",
    "StrategyBacktest",
    "StrategyExperiment",
    "StrategyMemory",
    "StrategySignal",
    "TradeCandidateSnapshot",
]
