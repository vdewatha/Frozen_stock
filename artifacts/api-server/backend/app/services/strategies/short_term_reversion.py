"""Short-horizon mean-reversion candidates with published, high-hit-rate rules.

Both strategies are *research candidates*: they are registered so they can be
backtested and scored, but they are not seeded into the database and are not
part of any active or exploration set (see ``RESEARCH_ONLY_STRATEGY_SLUGS`` in
the registry). They are long-only, buy weakness inside a long-term uptrend and
leave via an indicator exit (``exit_signal``), a time stop and a catastrophe
stop. The stop and time stop come from ``BacktestConfig``.

Implemented from the published rule descriptions (no third-party code):

* Larry Connors & Cesar Alvarez, *Short Term Trading Strategies That Work*
  (TradingMarkets, 2008), ch. 9 "The 2-Period RSI": trade only above the
  200-day SMA, buy when RSI(2) is below 10 (5 is stronger), exit on a close
  above the 5-day SMA. Connors & Alvarez, *High Probability ETF Trading*
  (2009) uses the same frame for ETFs.
* "First up-close" exit: a common variant of the same rule set (exit on the
  first close above the prior close), e.g. Connors' "Double 7's"/RSI(2)
  discussions and Alvarez Quant Trading research notes
  (https://alvarezquanttrading.com/blog/).
* Internal Bar Strength (IBS = (close - low) / (high - low)): A. Pagonidis,
  "The IBS Effect: Mean Reversion in Equity ETFs" (NAAIM Wagner Award paper,
  2014): low IBS predicts a higher next-day return on equity index ETFs. The
  200-day trend filter and IBS > 0.8 / up-close exits are the usual
  practitioner variants (e.g. Alvarez Quant Trading posts on IBS).

Differences from the books, on purpose: signals are decided on the last
completed bar and filled at the *next open* (our execution model), not at the
signal-day close, and costs are charged by the shared backtester.

The RSI here is Wilder's RSI (exponential smoothing with alpha = 1/period), as
in the books; the repository's ``rsi`` helper is a simple-average variant used
by the 14-period strategies.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from app.services.strategies.base import (
    BaseStrategy,
    StrategySignalResult,
    clamp,
    has_valid_history,
    neutral_signal,
)

# Wilder smoothing with period <= 5 forgets its seed after a few dozen bars;
# computing on a bounded tail keeps per-bar cost O(1) without changing values
# beyond floating-point noise (0.5 ** 300 for RSI(2)).
_INDICATOR_TAIL = 400


def wilder_rsi(close: pd.Series, period: int = 2) -> pd.Series:
    """Wilder's RSI. Flat windows map to 50 and loss-free windows to 100."""
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    alpha = 1.0 / period
    average_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    average_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    values = 100 - 100 / (1 + average_gain / average_loss.where(average_loss != 0))
    values = values.mask((average_loss == 0) & (average_gain > 0), 100.0)
    values = values.mask((average_loss == 0) & (average_gain == 0), 50.0)
    return values


def internal_bar_strength(frame: pd.DataFrame) -> pd.Series:
    """IBS = (close - low) / (high - low); a zero-range bar is neutral (0.5)."""
    high = pd.to_numeric(frame["high"], errors="coerce").astype(float)
    low = pd.to_numeric(frame["low"], errors="coerce").astype(float)
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
    span = high - low
    ibs = (close - low) / span.where(span > 0)
    return ibs.where(span > 0, 0.5).clip(0.0, 1.0)


def _tail(data: pd.DataFrame, rows: int) -> pd.DataFrame:
    return data.iloc[-rows:] if len(data) > rows else data


def _has_range(data: pd.DataFrame) -> bool:
    return all(column in data.columns for column in ("high", "low"))


class RSI2PullbackStrategy(BaseStrategy):
    """Connors RSI(2) pullback: buy short-term oversold dips in an uptrend.

    Entry: close > SMA(``trend_window``) and RSI(``rsi_period``) <
    ``entry_threshold`` on the last completed bar.
    Exit (``exit_rule``): ``"sma"`` exits on a close above SMA(``exit_sma_window``)
    (Connors' rule); ``"up_close"`` exits on the first close above the prior
    close after entry. Time stop and catastrophe stop come from the backtest
    configuration.
    """

    name = "RSI(2) Pullback"
    slug = "rsi2_pullback"

    def _params(self) -> dict:
        return {
            "rsi_period": int(self.parameters.get("rsi_period", 2)),
            "entry_threshold": float(self.parameters.get("entry_threshold", 10)),
            "trend_window": int(self.parameters.get("trend_window", 200)),
            "exit_rule": str(self.parameters.get("exit_rule", "sma")),
            "exit_sma_window": int(self.parameters.get("exit_sma_window", 5)),
        }

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        params = self._params()
        required = max(params["trend_window"], params["rsi_period"] + 1)
        if not has_valid_history(data, required):
            return neutral_signal(
                f"Insufficient history for the {params['trend_window']}-day trend filter.",
                {"required_rows": required, "rows": 0 if data is None else len(data)},
            )
        close = _tail(data, max(_INDICATOR_TAIL, required))["close"].astype(float)
        trend = float(close.iloc[-params["trend_window"]:].mean())
        latest_close = float(close.iloc[-1])
        latest_rsi = float(wilder_rsi(close, params["rsi_period"]).iloc[-1])
        features = {
            "rsi": latest_rsi,
            "trend_sma": trend,
            "close": latest_close,
            "entry_threshold": params["entry_threshold"],
            "exit_rule": params["exit_rule"],
        }
        if not np.isfinite(latest_rsi):
            return neutral_signal("RSI is not defined yet.", features)
        in_uptrend = latest_close > trend
        if in_uptrend and latest_rsi < params["entry_threshold"]:
            depth = (params["entry_threshold"] - latest_rsi) / max(params["entry_threshold"], 1e-9)
            return StrategySignalResult(
                action="BUY",
                probability_up=clamp(0.55 + depth * 0.15),
                probability_down=clamp(0.45 - depth * 0.15),
                confidence=clamp(0.6 + depth * 0.3),
                reason=(f"Close above {params['trend_window']}-day SMA and RSI({params['rsi_period']}) "
                        f"{latest_rsi:.1f} < {params['entry_threshold']:.0f}."),
                features=features,
            )
        return neutral_signal(
            f"RSI({params['rsi_period']}) {latest_rsi:.1f}; uptrend={'yes' if in_uptrend else 'no'}.", features,
        )

    def exit_signal(self, symbol: str, data: pd.DataFrame, entry_index: int) -> Optional[str]:
        params = self._params()
        if data is None or len(data) < 2 or len(data) - 1 < entry_index:
            return None
        close = data["close"].astype(float)
        if params["exit_rule"] == "up_close":
            return "first_up_close" if float(close.iloc[-1]) > float(close.iloc[-2]) else None
        window = params["exit_sma_window"]
        if len(close) < window:
            return None
        sma = float(close.iloc[-window:].mean())
        return f"close_above_sma{window}" if float(close.iloc[-1]) > sma else None


class IBSMeanReversionStrategy(BaseStrategy):
    """Internal-bar-strength mean reversion inside a long-term uptrend.

    Entry: IBS < ``entry_threshold`` and close > SMA(``trend_window``) on the
    last completed bar. Exit (``exit_rule``): ``"ibs"`` exits when IBS >
    ``exit_threshold``; ``"up_close"`` exits on the first close above the prior
    close after entry. Time stop and catastrophe stop come from the backtest
    configuration.
    """

    name = "IBS Mean Reversion"
    slug = "ibs_mean_reversion"

    def _params(self) -> dict:
        return {
            "entry_threshold": float(self.parameters.get("entry_threshold", 0.2)),
            "exit_threshold": float(self.parameters.get("exit_threshold", 0.8)),
            "trend_window": int(self.parameters.get("trend_window", 200)),
            "exit_rule": str(self.parameters.get("exit_rule", "ibs")),
        }

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        params = self._params()
        required = params["trend_window"]
        if not has_valid_history(data, required) or not _has_range(data):
            return neutral_signal(
                "Insufficient history (or no high/low range) for IBS with a trend filter.",
                {"required_rows": required, "rows": 0 if data is None else len(data)},
            )
        tail = _tail(data, required)
        close = tail["close"].astype(float)
        trend = float(close.mean())
        latest_close = float(close.iloc[-1])
        latest_ibs = float(internal_bar_strength(tail.iloc[[-1]]).iloc[-1])
        features = {"ibs": latest_ibs, "trend_sma": trend, "close": latest_close,
                    "entry_threshold": params["entry_threshold"], "exit_rule": params["exit_rule"]}
        in_uptrend = latest_close > trend
        if in_uptrend and latest_ibs < params["entry_threshold"]:
            depth = (params["entry_threshold"] - latest_ibs) / max(params["entry_threshold"], 1e-9)
            return StrategySignalResult(
                action="BUY",
                probability_up=clamp(0.55 + depth * 0.1),
                probability_down=clamp(0.45 - depth * 0.1),
                confidence=clamp(0.6 + depth * 0.3),
                reason=(f"IBS {latest_ibs:.2f} < {params['entry_threshold']:.2f} with close above the "
                        f"{params['trend_window']}-day SMA."),
                features=features,
            )
        return neutral_signal(f"IBS {latest_ibs:.2f}; uptrend={'yes' if in_uptrend else 'no'}.", features)

    def exit_signal(self, symbol: str, data: pd.DataFrame, entry_index: int) -> Optional[str]:
        params = self._params()
        if data is None or len(data) < 2 or len(data) - 1 < entry_index:
            return None
        close = data["close"].astype(float)
        if params["exit_rule"] == "up_close":
            return "first_up_close" if float(close.iloc[-1]) > float(close.iloc[-2]) else None
        if not _has_range(data):
            return None
        latest_ibs = float(internal_bar_strength(data.iloc[[-1]]).iloc[-1])
        return "ibs_high" if latest_ibs > params["exit_threshold"] else None
