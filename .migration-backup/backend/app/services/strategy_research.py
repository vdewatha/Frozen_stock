from __future__ import annotations


DEFAULT_RESEARCH = {
    "source_label": "Internal research note",
    "source_url": None,
    "idea": "Candidate is evaluated with local model, signal, backtest, news, and macro evidence.",
    "ideal_market": "Use only when scanner evidence, risk limits, and paper-trading readiness align.",
    "confirmation_rules": ["Positive model horizon", "Strategy signal agrees", "Backtest quality is acceptable"],
    "risk_notes": "Paper-only validation; no live capital routing is enabled.",
}


STRATEGY_RESEARCH: dict[str, dict] = {
    "moving_average_crossover": {
        "source_label": "QuantConnect indicator research: moving averages",
        "source_url": "https://www.quantconnect.com/docs/v1/research/research-on-indicators",
        "idea": "Trend-following strategy that treats shorter moving averages crossing above longer averages as evidence of improving momentum.",
        "ideal_market": "Directional markets with persistent trends and enough volatility to overcome whipsaw costs.",
        "confirmation_rules": ["Short average above long average", "BUY signal confidence above scanner threshold", "Backtest score remains positive"],
        "risk_notes": "Can lag turning points and suffer in sideways ranges; pair with model probability and drawdown limits.",
    },
    "rsi_mean_reversion": {
        "source_label": "QuantConnect Relative Strength Index documentation",
        "source_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/relative-strength-index",
        "idea": "Contrarian strategy that looks for stretched RSI readings where price may mean-revert after oversold pressure.",
        "ideal_market": "Range-bound or pullback markets where oversold conditions reverse instead of becoming breakdowns.",
        "confirmation_rules": ["RSI below buy threshold", "No severe backtest rejection", "News/macro context does not contradict the reversal"],
        "risk_notes": "Oversold can stay oversold in strong downtrends; scanner blocks weak model horizons.",
    },
    "macd_momentum": {
        "source_label": "MACD technical indicator research",
        "source_url": "https://arxiv.org/abs/2206.12282",
        "idea": "Momentum strategy that uses MACD histogram direction to detect improving trend impulse.",
        "ideal_market": "Early-to-middle trend continuation environments with improving momentum breadth.",
        "confirmation_rules": ["MACD histogram supports BUY", "Model expected return is positive", "Backtest filter avoids weak standalone MACD setups"],
        "risk_notes": "Standalone MACD can have weak win rates; this app requires model and backtest confirmation before promotion.",
    },
    "ensemble": {
        "source_label": "Internal multi-strategy ensemble",
        "source_url": None,
        "idea": "Combines votes from the technical strategy library so no single indicator dominates the paper-trade decision.",
        "ideal_market": "Mixed conditions where cross-strategy agreement is more useful than a single specialized signal.",
        "confirmation_rules": ["Multiple technical sleeves agree", "Aggregate confidence clears threshold", "Backtest and model filters agree"],
        "risk_notes": "Can dilute strong specialist signals; allocation still caps symbol and strategy exposure.",
    },
    "model_predictive_long": {
        "source_label": "Internal probabilistic expected-return model",
        "source_url": None,
        "idea": "Long-only paper strategy that promotes candidates when the learned model forecasts positive expected return.",
        "ideal_market": "Stocks with enough local history, favorable feature set, and positive forward-return probability.",
        "confirmation_rules": ["Probability-up threshold clears", "Expected return threshold clears", "Technical signal and backtest do not veto"],
        "risk_notes": "Model drift is monitored through realized prediction scorecards before increasing paper allocation.",
    },
    "bollinger_mean_reversion": {
        "source_label": "QuantConnect Bollinger Bands documentation",
        "source_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/bollinger-bands",
        "idea": "Mean-reversion strategy that watches for lower-band pressure plus oversold confirmation.",
        "ideal_market": "Volatile but range-respecting markets where band extremes tend to revert toward the middle band.",
        "confirmation_rules": ["Price near or below lower band", "RSI confirms oversold pressure", "Backtest does not reject sparse or weak trade history"],
        "risk_notes": "Band breaks can become trend breakdowns; scanner requires independent model support.",
    },
    "channel_breakout": {
        "source_label": "QuantConnect strategy library: breakout concepts",
        "source_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/strategy-library",
        "idea": "Trend-following breakout strategy that looks for price clearing a prior channel with volume support.",
        "ideal_market": "Expanding volatility markets where fresh highs can start continuation moves.",
        "confirmation_rules": ["Price breaks channel high", "Volume confirms participation", "Model horizon supports upside"],
        "risk_notes": "False breakouts are common; risk engine and backtest quality gates constrain paper entries.",
    },
    "trend_pullback": {
        "source_label": "QuantConnect indicator research: EMA trend filters",
        "source_url": "https://www.quantconnect.com/docs/v1/research/research-on-indicators",
        "idea": "Trend-continuation strategy that buys controlled pullbacks when the broader EMA trend remains intact.",
        "ideal_market": "Uptrends with temporary weakness rather than full trend reversals.",
        "confirmation_rules": ["Fast EMA remains above slow EMA", "Pullback is controlled", "Model expected return stays positive"],
        "risk_notes": "Can enter too early during trend failure; paper-only sizing and stop logic limit damage.",
    },
}


def strategy_research_for(strategy_type: str) -> dict:
    return {**DEFAULT_RESEARCH, **STRATEGY_RESEARCH.get(strategy_type, {})}
