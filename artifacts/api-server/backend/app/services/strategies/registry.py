from __future__ import annotations

from typing import Optional

from app.services.strategies.base import BaseStrategy
from app.services.strategies.short_term_reversion import IBSMeanReversionStrategy, RSI2PullbackStrategy
from app.services.strategies.technical import (
    BollingerMeanReversionStrategy,
    ChannelBreakoutStrategy,
    EnsembleStrategy,
    MACDMomentumStrategy,
    ModelPredictiveLongStrategy,
    MovingAverageCrossoverStrategy,
    RSIMeanReversionStrategy,
    TrendPullbackStrategy,
)

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    MovingAverageCrossoverStrategy.slug: MovingAverageCrossoverStrategy,
    RSIMeanReversionStrategy.slug: RSIMeanReversionStrategy,
    MACDMomentumStrategy.slug: MACDMomentumStrategy,
    EnsembleStrategy.slug: EnsembleStrategy,
    ModelPredictiveLongStrategy.slug: ModelPredictiveLongStrategy,
    BollingerMeanReversionStrategy.slug: BollingerMeanReversionStrategy,
    ChannelBreakoutStrategy.slug: ChannelBreakoutStrategy,
    TrendPullbackStrategy.slug: TrendPullbackStrategy,
    # Short-term reversion candidates: seeded as paper_trading_candidate with
    # their own exit policy and index-ETF symbol scope (see seed.py).
    RSI2PullbackStrategy.slug: RSI2PullbackStrategy,
    IBSMeanReversionStrategy.slug: IBSMeanReversionStrategy,
}

# Registered strategies that must never be seeded or traded. Empty since the
# RSI(2)/IBS candidates got paper exit handling (exit policy + indicator and
# time-stop exits in the paper cycle); kept so research-only slugs added later
# have an explicit home.
RESEARCH_ONLY_STRATEGY_SLUGS: frozenset[str] = frozenset()


def get_strategy(slug: str, parameters: Optional[dict] = None) -> BaseStrategy:
    try:
        return STRATEGY_REGISTRY[slug](parameters)
    except KeyError as exc:
        raise ValueError(f"Unknown strategy: {slug}") from exc
