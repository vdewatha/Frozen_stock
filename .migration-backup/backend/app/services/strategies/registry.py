from __future__ import annotations

from typing import Optional

from app.services.strategies.base import BaseStrategy
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
}


def get_strategy(slug: str, parameters: Optional[dict] = None) -> BaseStrategy:
    try:
        return STRATEGY_REGISTRY[slug](parameters)
    except KeyError as exc:
        raise ValueError(f"Unknown strategy: {slug}") from exc
