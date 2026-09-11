from __future__ import annotations

from app.services.risk import DEFAULT_RISK_RULES


def propose_parameter_experiments(strategy_slug: str, current_parameters: dict) -> list[dict]:
    if strategy_slug == "moving_average_crossover":
        candidates = [(10, 30), (20, 50), (50, 200)]
        return [
            {
                "experiment_name": f"MA {short}/{long}",
                "old_parameters": current_parameters,
                "new_parameters": {"short_window": short, "long_window": long},
                "hypothesis": "Different trend windows may reduce whipsaw losses while preserving upside capture.",
                "decision": "queued",
            }
            for short, long in candidates
        ]
    if strategy_slug == "rsi_mean_reversion":
        return [
            {
                "experiment_name": f"RSI buy {buy} sell {sell}",
                "old_parameters": current_parameters,
                "new_parameters": {"buy_threshold": buy, "sell_threshold": sell},
                "hypothesis": "Adjusted RSI thresholds may improve entries in sideways regimes.",
                "decision": "queued",
            }
            for buy, sell in [(25, 60), (30, 70), (35, 65)]
        ]
    if strategy_slug == "macd_momentum":
        return [
            {
                "experiment_name": f"MACD {fast}/{slow}/{signal}",
                "old_parameters": current_parameters,
                "new_parameters": {"fast_span": fast, "slow_span": slow, "signal_span": signal},
                "hypothesis": "Alternative MACD spans may reduce lag or avoid noisy momentum flips.",
                "decision": "queued",
            }
            for fast, slow, signal in [(8, 21, 5), (12, 26, 9), (16, 35, 9)]
        ]
    if strategy_slug == "model_predictive_long":
        return [
            {
                "experiment_name": f"Predictive gate {prob:.2f}/{ret:.3f}",
                "old_parameters": current_parameters,
                "new_parameters": {"min_probability_up": prob, "min_expected_return": ret},
                "hypothesis": "Changing model probability and expected-return gates may improve paper entries without overriding negative evidence.",
                "decision": "queued",
            }
            for prob, ret in [(0.54, 0.002), (0.56, 0.003), (0.60, 0.004)]
        ]
    if strategy_slug == "bollinger_mean_reversion":
        return [
            {
                "experiment_name": f"Bollinger {window}/{std_dev} RSI {rsi_buy}",
                "old_parameters": current_parameters,
                "new_parameters": {"window": window, "std_dev": std_dev, "rsi_buy": rsi_buy},
                "hypothesis": "Band width and RSI confirmation changes may reduce false mean-reversion entries.",
                "decision": "queued",
            }
            for window, std_dev, rsi_buy in [(20, 2.0, 35), (30, 2.0, 32), (20, 2.5, 30)]
        ]
    if strategy_slug == "channel_breakout":
        return [
            {
                "experiment_name": f"Channel {lookback} volume {volume_window}",
                "old_parameters": current_parameters,
                "new_parameters": {"lookback": lookback, "volume_window": volume_window},
                "hypothesis": "Different breakout lookbacks and volume filters may improve trend-confirmation quality.",
                "decision": "queued",
            }
            for lookback, volume_window in [(40, 20), (55, 20), (80, 30)]
        ]
    if strategy_slug == "trend_pullback":
        return [
            {
                "experiment_name": f"Pullback EMA {fast}/{slow} RSI {rsi_pullback}",
                "old_parameters": current_parameters,
                "new_parameters": {"fast_ema": fast, "slow_ema": slow, "rsi_pullback": rsi_pullback},
                "hypothesis": "Trend and pullback thresholds may improve entries after temporary weakness in an uptrend.",
                "decision": "queued",
            }
            for fast, slow, rsi_pullback in [(15, 80, 45), (20, 100, 50), (30, 120, 48)]
        ]
    return [
        {
            "experiment_name": "Tighten risk filter",
            "old_parameters": current_parameters,
            "new_parameters": current_parameters | {"min_confidence": DEFAULT_RISK_RULES["min_confidence"] + 0.04},
            "hypothesis": "Higher confidence gating may reduce weak signals.",
            "decision": "queued",
        }
    ]
