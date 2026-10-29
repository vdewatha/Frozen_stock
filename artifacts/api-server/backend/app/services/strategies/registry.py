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
    # Research-only candidates (see RESEARCH_ONLY_STRATEGY_SLUGS).
    RSI2PullbackStrategy.slug: RSI2PullbackStrategy,
    IBSMeanReversionStrategy.slug: IBSMeanReversionStrategy,
}

# Registered so they can be backtested and scored (strategy scorecard, the
# backtest API), but never seeded into the database: they have no Strategy
# row, so no active, candidate or exploration set can trade them. Their exits
# rely on ``BaseStrategy.exit_signal``, which only the backtester honours today;
# wire it into paper execution before any promotion.
RESEARCH_ONLY_STRATEGY_SLUGS: frozenset[str] = frozenset({
    RSI2PullbackStrategy.slug,
    IBSMeanReversionStrategy.slug,
})


def get_strategy(slug: str, parameters: Optional[dict] = None) -> BaseStrategy:
    try:
        return STRATEGY_REGISTRY[slug](parameters)
    except KeyError as exc:
        raise ValueError(f"Unknown strategy: {slug}") from exc
