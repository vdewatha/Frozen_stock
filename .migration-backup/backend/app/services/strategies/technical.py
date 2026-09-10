from __future__ import annotations

import pandas as pd

from app.services.probabilistic_model import predict_probabilities
from app.services.strategies.base import BaseStrategy, StrategySignalResult, clamp


class MovingAverageCrossoverStrategy(BaseStrategy):
    name = "Moving Average Crossover"
    slug = "moving_average_crossover"

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        short_window = int(self.parameters.get("short_window", 20))
        long_window = int(self.parameters.get("long_window", 50))
        frame = data.copy()
        frame["short_ma"] = frame["close"].rolling(short_window).mean()
        frame["long_ma"] = frame["close"].rolling(long_window).mean()
        latest = frame.iloc[-1]
        distance = (latest["short_ma"] - latest["long_ma"]) / latest["close"]
        confidence = clamp(0.5 + abs(distance) * 6)
        action = "BUY" if latest["short_ma"] > latest["long_ma"] else "SELL"
        probability_up = clamp(0.5 + distance * 5)
        return StrategySignalResult(
            action=action,
            probability_up=probability_up,
            probability_down=1 - probability_up,
            confidence=confidence,
            reason=f"{short_window}-day average is {'above' if action == 'BUY' else 'below'} the {long_window}-day average.",
            features={"short_ma": float(latest["short_ma"]), "long_ma": float(latest["long_ma"]), "distance": float(distance)},
        )


class RSIMeanReversionStrategy(BaseStrategy):
    name = "RSI Mean Reversion"
    slug = "rsi_mean_reversion"

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        buy_threshold = float(self.parameters.get("buy_threshold", 30))
        sell_threshold = float(self.parameters.get("sell_threshold", 70))
        delta = data["close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain / loss.replace(0, 1e-9)))
        latest_rsi = float(rsi.iloc[-1])
        if latest_rsi <= buy_threshold:
            action = "BUY"
            confidence = clamp((buy_threshold - latest_rsi) / buy_threshold + 0.55)
        elif latest_rsi >= sell_threshold:
            action = "SELL"
            confidence = clamp((latest_rsi - sell_threshold) / (100 - sell_threshold) + 0.55)
        else:
            action = "HOLD"
            confidence = 0.5
        probability_up = clamp((50 - latest_rsi) / 100 + 0.5)
        return StrategySignalResult(
            action=action,
            probability_up=probability_up,
            probability_down=1 - probability_up,
            confidence=confidence,
            reason=f"RSI is {latest_rsi:.1f}; thresholds are buy <= {buy_threshold:.0f}, sell >= {sell_threshold:.0f}.",
            features={"rsi": latest_rsi, "buy_threshold": buy_threshold, "sell_threshold": sell_threshold},
        )


class MACDMomentumStrategy(BaseStrategy):
    name = "MACD Momentum"
    slug = "macd_momentum"

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        fast_span = int(self.parameters.get("fast_span", 12))
        slow_span = int(self.parameters.get("slow_span", 26))
        signal_span = int(self.parameters.get("signal_span", 9))
        close = data["close"]
        macd = close.ewm(span=fast_span, adjust=False).mean() - close.ewm(span=slow_span, adjust=False).mean()
        signal = macd.ewm(span=signal_span, adjust=False).mean()
        histogram = float((macd - signal).iloc[-1])
        action = "BUY" if histogram > 0 else "SELL"
        confidence = clamp(0.55 + abs(histogram) / max(float(close.iloc[-1]), 1) * 20)
        probability_up = clamp(0.5 + histogram / max(float(close.iloc[-1]), 1) * 15)
        return StrategySignalResult(
            action=action,
            probability_up=probability_up,
            probability_down=1 - probability_up,
            confidence=confidence,
            reason=f"MACD {fast_span}/{slow_span}/{signal_span} histogram is {'positive' if histogram > 0 else 'negative'} at {histogram:.3f}.",
            features={
                "fast_span": fast_span,
                "slow_span": slow_span,
                "signal_span": signal_span,
                "macd": float(macd.iloc[-1]),
                "signal": float(signal.iloc[-1]),
                "histogram": histogram,
            },
        )


class EnsembleStrategy(BaseStrategy):
    name = "Ensemble Strategy"
    slug = "ensemble"

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        members = [
            MovingAverageCrossoverStrategy(),
            RSIMeanReversionStrategy(),
            MACDMomentumStrategy(),
        ]
        signals = [strategy.generate_signal(symbol, data) for strategy in members]
        weighted_up = sum(signal.probability_up * signal.confidence for signal in signals)
        confidence_sum = sum(signal.confidence for signal in signals) or 1
        probability_up = clamp(weighted_up / confidence_sum)
        confidence = clamp(sum(signal.confidence for signal in signals) / len(signals))
        action = "BUY" if probability_up >= 0.57 else "SELL" if probability_up <= 0.43 else "HOLD"
        return StrategySignalResult(
            action=action,
            probability_up=probability_up,
            probability_down=1 - probability_up,
            confidence=confidence,
            reason="Weighted vote across moving average, RSI, and MACD strategies.",
            features={"member_signals": [signal.__dict__ for signal in signals]},
        )


class ModelPredictiveLongStrategy(BaseStrategy):
    name = "Model Predictive Long"
    slug = "model_predictive_long"

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        min_probability = float(self.parameters.get("min_probability_up", 0.56))
        min_expected_return = float(self.parameters.get("min_expected_return", 0.003))
        model = predict_probabilities(symbol, data, "strategy_price_history")
        predictions = model.get("predictions", [])
        candidates = [
            prediction
            for prediction in predictions
            if float(prediction.get("probability_up", 0)) >= min_probability
            and float(prediction.get("expected_return", 0)) >= min_expected_return
        ]
        best = max(candidates, key=lambda item: (float(item.get("expected_return", 0)), float(item.get("probability_up", 0))), default=None)
        if not best:
            best = max(predictions, key=lambda item: float(item.get("probability_up", 0)), default={})
            probability_up = float(best.get("probability_up", 0.5))
            expected_return = float(best.get("expected_return", 0))
            return StrategySignalResult(
                action="HOLD",
                probability_up=clamp(probability_up),
                probability_down=clamp(1 - probability_up),
                confidence=0.5,
                reason="Predictive model does not meet positive expected-return entry thresholds.",
                features={
                    "best_horizon": best,
                    "thresholds": {"min_probability_up": min_probability, "min_expected_return": min_expected_return},
                    "model_warnings": model.get("warnings", []),
                    "expected_return": expected_return,
                },
            )

        probability_up = float(best.get("probability_up", 0.5))
        expected_return = float(best.get("expected_return", 0))
        confidence = clamp((probability_up * 0.75) + min(max(expected_return, 0), 0.05) * 3)
        return StrategySignalResult(
            action="BUY",
            probability_up=clamp(probability_up),
            probability_down=clamp(1 - probability_up),
            confidence=confidence,
            reason=(
                f"Predictive model finds {probability_up:.1%} up probability and "
                f"{expected_return:.2%} expected return over {best.get('horizon_days')} trading days."
            ),
            features={
                "best_horizon": best,
                "thresholds": {"min_probability_up": min_probability, "min_expected_return": min_expected_return},
                "model_warnings": model.get("warnings", []),
                "expected_return": expected_return,
            },
        )


class BollingerMeanReversionStrategy(BaseStrategy):
    name = "Bollinger Mean Reversion"
    slug = "bollinger_mean_reversion"

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        window = int(self.parameters.get("window", 20))
        std_dev = float(self.parameters.get("std_dev", 2.0))
        rsi_buy = float(self.parameters.get("rsi_buy", 35))
        frame = data.copy()
        close = frame["close"]
        middle = close.rolling(window).mean()
        std = close.rolling(window).std()
        lower = middle - std_dev * std
        upper = middle + std_dev * std
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain / loss.replace(0, 1e-9)))
        latest_close = float(close.iloc[-1])
        latest_lower = float(lower.iloc[-1])
        latest_upper = float(upper.iloc[-1])
        latest_middle = float(middle.iloc[-1])
        latest_rsi = float(rsi.iloc[-1])
        band_width = max(latest_upper - latest_lower, 1e-9)
        position_in_band = (latest_close - latest_lower) / band_width
        action = "BUY" if latest_close <= latest_lower and latest_rsi <= rsi_buy else "HOLD"
        reversion_room = max((latest_middle / latest_close - 1) if latest_close else 0, 0)
        confidence = clamp(0.52 + (1 - position_in_band) * 0.18 + max(rsi_buy - latest_rsi, 0) / 100)
        probability_up = clamp(0.5 + reversion_room * 4 + max(rsi_buy - latest_rsi, 0) / 250)
        return StrategySignalResult(
            action=action,
            probability_up=probability_up,
            probability_down=1 - probability_up,
            confidence=confidence if action == "BUY" else 0.5,
            reason=f"Price is {position_in_band:.2f} through the Bollinger band with RSI {latest_rsi:.1f}.",
            features={
                "window": window,
                "std_dev": std_dev,
                "lower_band": latest_lower,
                "middle_band": latest_middle,
                "upper_band": latest_upper,
                "rsi": latest_rsi,
                "position_in_band": position_in_band,
            },
        )


class ChannelBreakoutStrategy(BaseStrategy):
    name = "Channel Breakout"
    slug = "channel_breakout"

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        lookback = int(self.parameters.get("lookback", 55))
        volume_window = int(self.parameters.get("volume_window", 20))
        frame = data.copy()
        close = frame["close"]
        volume = frame["volume"]
        prior_high = float(close.rolling(lookback).max().shift(1).iloc[-1])
        latest_close = float(close.iloc[-1])
        avg_volume = float(volume.rolling(volume_window).mean().iloc[-1] or 0)
        latest_volume = float(volume.iloc[-1] or 0)
        breakout = latest_close > prior_high
        volume_ratio = latest_volume / avg_volume if avg_volume else 1
        breakout_strength = latest_close / prior_high - 1 if prior_high else 0
        action = "BUY" if breakout and volume_ratio >= 1.05 else "HOLD"
        confidence = clamp(0.55 + breakout_strength * 10 + max(volume_ratio - 1, 0) * 0.10)
        probability_up = clamp(0.5 + breakout_strength * 8 + max(volume_ratio - 1, 0) * 0.06)
        return StrategySignalResult(
            action=action,
            probability_up=probability_up,
            probability_down=1 - probability_up,
            confidence=confidence if action == "BUY" else 0.5,
            reason=f"Close is {'above' if breakout else 'below'} the {lookback}-day channel high; volume ratio {volume_ratio:.2f}.",
            features={
                "lookback": lookback,
                "prior_high": prior_high,
                "breakout_strength": breakout_strength,
                "volume_ratio": volume_ratio,
            },
        )


class TrendPullbackStrategy(BaseStrategy):
    name = "Trend Pullback"
    slug = "trend_pullback"

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        fast = int(self.parameters.get("fast_ema", 20))
        slow = int(self.parameters.get("slow_ema", 100))
        rsi_pullback = float(self.parameters.get("rsi_pullback", 58))
        frame = data.copy()
        close = frame["close"]
        fast_ema = close.ewm(span=fast, adjust=False).mean()
        slow_ema = close.ewm(span=slow, adjust=False).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain / loss.replace(0, 1e-9)))
        latest_close = float(close.iloc[-1])
        latest_fast = float(fast_ema.iloc[-1])
        latest_slow = float(slow_ema.iloc[-1])
        latest_rsi = float(rsi.iloc[-1])
        trend_strength = latest_fast / latest_slow - 1 if latest_slow else 0
        pullback = latest_close / latest_fast - 1 if latest_fast else 0
        action = "BUY" if trend_strength > 0 and -0.035 <= pullback <= 0.005 and 38 <= latest_rsi <= rsi_pullback else "HOLD"
        confidence = clamp(0.54 + trend_strength * 4 + max(-pullback, 0) * 3)
        probability_up = clamp(0.5 + trend_strength * 3 + max(-pullback, 0) * 2)
        return StrategySignalResult(
            action=action,
            probability_up=probability_up,
            probability_down=1 - probability_up,
            confidence=confidence if action == "BUY" else 0.5,
            reason=f"{fast}-EMA is {'above' if trend_strength > 0 else 'below'} {slow}-EMA; pullback {pullback:.2%}, RSI {latest_rsi:.1f}.",
            features={
                "fast_ema": latest_fast,
                "slow_ema": latest_slow,
                "trend_strength": trend_strength,
                "pullback": pullback,
                "rsi": latest_rsi,
                "rsi_pullback": rsi_pullback,
            },
        )
