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
    if strategy_slug == "rsi2_pullback":
        return [
            {
                "experiment_name": f"RSI(2) entry {threshold} trend SMA {trend}",
                "old_parameters": current_parameters,
                "new_parameters": current_parameters | {"entry_threshold": threshold, "trend_filter_sma": trend},
                "hypothesis": "A deeper oversold entry or a different trend filter may avoid pullbacks that keep falling.",
                "decision": "queued",
            }
            for threshold, trend in [(5, 200), (10, 100), (15, 200)]
        ]
    if strategy_slug == "ibs_mean_reversion":
        return [
            {
                "experiment_name": f"IBS entry {entry} exit {exit_}",
                "old_parameters": current_parameters,
                "new_parameters": current_parameters | {"entry_threshold": entry, "exit_threshold": exit_},
                "hypothesis": "Stricter IBS entries or earlier IBS exits may cut losses from weak closes that do not revert.",
                "decision": "queued",
            }
            for entry, exit_ in [(0.05, 0.8), (0.1, 0.6), (0.2, 0.8)]
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


# --------------------------------------------------------------- loss focus
# Loss-driven research agenda (app.services.parameter_application): each
# proposal is tagged with the kind of change it makes so the experiments most
# relevant to the dominant cause of recent attributed losses run first.  The
# tags only order proposals; every experiment gate stays unchanged.
LOSS_CAUSE_TAGS = {
    "catastrophe_stop": ("stricter_entry", "slower_trend"),
    "indicator": ("stricter_entry",),
    "protection_lock": ("stricter_entry",),
    "marginal_signal": ("stricter_entry",),
    "time_stop": ("faster_exit", "faster_trend"),
}

_STRICTER_LOWER = {"entry_threshold", "buy_threshold", "rsi_buy", "rsi_pullback"}
_STRICTER_HIGHER = {"min_probability_up", "min_expected_return", "min_confidence", "std_dev", "lookback",
                    "trend_filter_sma"}
_TREND_WINDOWS = {"short_window", "long_window", "fast_span", "slow_span", "fast_ema", "slow_ema", "window"}
_FASTER_EXIT_LOWER = {"exit_threshold", "sell_threshold", "max_holding_sessions", "exit_sma_window"}


def proposal_change_tags(new_parameters: dict, current_parameters: dict) -> list[str]:
    """Classify a proposal's direction relative to the current parameters."""
    tags: set[str] = set()
    for key, value in (new_parameters or {}).items():
        old = (current_parameters or {}).get(key)
        if not isinstance(value, (int, float)) or not isinstance(old, (int, float)) or value == old:
            continue
        if key in _STRICTER_LOWER and value < old:
            tags.add("stricter_entry")
        if key in _STRICTER_HIGHER and value > old:
            tags.add("stricter_entry")
        if key in _TREND_WINDOWS:
            tags.add("slower_trend" if value > old else "faster_trend")
        if key in _FASTER_EXIT_LOWER and value < old:
            tags.add("faster_exit")
    return sorted(tags)


def rank_proposals_for_losses(proposals: list[dict], current_parameters: dict, focus: list[str] | None) -> list[dict]:
    """Stable re-ordering of proposals by relevance to the loss focus.

    ``focus`` lists loss causes in priority order (e.g. ``["catastrophe_stop",
    "marginal_signal"]``).  Proposals identical to the current parameters are
    dropped; with no focus the original order is kept.
    """
    useful = [item for item in proposals if (item.get("new_parameters") or {}) != (current_parameters or {})]
    if not useful:
        useful = list(proposals)
    for item in useful:
        item["change_tags"] = proposal_change_tags(item.get("new_parameters") or {}, current_parameters)
    if not focus:
        return useful
    weights: dict[str, float] = {}
    for rank, cause in enumerate(focus):
        for tag in LOSS_CAUSE_TAGS.get(cause, ()):
            weights[tag] = max(weights.get(tag, 0.0), 1.0 / (rank + 1))

    def score(item: dict) -> float:
        return sum(weights.get(tag, 0.0) for tag in item["change_tags"])

    ranked = sorted(enumerate(useful), key=lambda pair: (-score(pair[1]), pair[0]))
    out = []
    for _index, item in ranked:
        item["loss_focus_score"] = round(score(item), 6)
        out.append(item)
    return out
