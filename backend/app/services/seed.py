from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Asset, RiskRule, Strategy
from app.services.risk import DEFAULT_RISK_RULES


def seed_defaults(db: Session) -> None:
    if not db.query(Asset).first():
        db.add_all(
            [
                Asset(symbol="SPY", name="SPDR S&P 500 ETF Trust", sector="Broad Market"),
                Asset(symbol="QQQ", name="Invesco QQQ Trust", sector="Technology"),
                Asset(symbol="AAPL", name="Apple Inc.", sector="Technology"),
                Asset(symbol="MSFT", name="Microsoft Corporation", sector="Technology"),
            ]
        )
    if not db.query(Strategy).first():
        db.add_all(
            [
                Strategy(
                    name="Moving Average Crossover",
                    strategy_type="moving_average_crossover",
                    description="Trend-following strategy using short and long moving average distance.",
                    parameters={"short_window": 20, "long_window": 50},
                    current_status="paper_trading_active",
                ),
                Strategy(
                    name="RSI Mean Reversion",
                    strategy_type="rsi_mean_reversion",
                    description="Contrarian strategy looking for oversold and overbought RSI conditions.",
                    parameters={"buy_threshold": 30, "sell_threshold": 70},
                    current_status="research",
                ),
                Strategy(
                    name="MACD Momentum",
                    strategy_type="macd_momentum",
                    description="Momentum strategy based on MACD histogram direction.",
                    parameters={},
                    current_status="paper_trading_candidate",
                ),
                Strategy(
                    name="Ensemble Strategy",
                    strategy_type="ensemble",
                    description="Confidence-weighted vote across the technical strategy library.",
                    parameters={},
                    current_status="research",
                ),
                Strategy(
                    name="Model Predictive Long",
                    strategy_type="model_predictive_long",
                    description="Long-only paper strategy driven by probabilistic expected-return evidence.",
                    parameters={"min_probability_up": 0.56, "min_expected_return": 0.003},
                    current_status="paper_trading_candidate",
                ),
                Strategy(
                    name="Bollinger Mean Reversion",
                    strategy_type="bollinger_mean_reversion",
                    description="Mean-reversion strategy that looks for lower Bollinger Band touches with oversold RSI confirmation.",
                    parameters={"window": 20, "std_dev": 2.0, "rsi_buy": 35},
                    current_status="research",
                ),
                Strategy(
                    name="Channel Breakout",
                    strategy_type="channel_breakout",
                    description="Trend-following breakout strategy using prior channel highs and volume confirmation.",
                    parameters={"lookback": 55, "volume_window": 20},
                    current_status="research",
                ),
                Strategy(
                    name="Trend Pullback",
                    strategy_type="trend_pullback",
                    description="Trend-continuation strategy that buys controlled pullbacks inside an EMA uptrend.",
                    parameters={"fast_ema": 20, "slow_ema": 100},
                    current_status="research",
                ),
            ]
        )
    if not db.query(RiskRule).first():
        db.add(RiskRule(name="default_paper_risk", value=DEFAULT_RISK_RULES, is_active=True))
    db.commit()
