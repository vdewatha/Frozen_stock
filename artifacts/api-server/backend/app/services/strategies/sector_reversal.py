"""Sector-relative short-term reversal: buy a stock that fell hard against its own sector.

The best-documented short-horizon edge that survives in liquid large caps
after 2015 is short-term reversal measured *relative to the stock's industry
or sector*, not the raw return (Hameed & Mian 2015, "Industries and Stock
Return Reversals"; Da, Liu & Schaumburg 2014, "A Closer Look at the
Short-Term Return Reversal"; Blitz, van der Grient & Honarvar 2023,
"Reversing the Trend of Short-Term Reversal").  A sector-wide fall is
information and tends to persist; the part of a stock's fall its sector did
not share is the liquidity-driven overshoot that reverts, more strongly when
the VIX is high (Nagel 2012, "Evaporating Liquidity").  Blitz et al. report
roughly doubled gross Sharpe for industry-relative reversal but at very high
turnover, which is the main cost risk; hence the exit hysteresis below.

Rule (``DEFAULT_RULE``, the ``strategies.rules`` language):

* entry on the close: the stock is in an uptrend (close > SMA200) and its
  5-day return relative to its sector ETF is an extreme low, ``rel_z_5 < -2``
  (the 5-day relative return two standard deviations under its 60-day norm);
* exit with hysteresis: the relative RSI(2) has recovered above 70, or the
  close is back above its 5-day average; otherwise the 10-session time stop
  or the 10% catastrophic stop (``default_exit_policy``).  Entering at an
  extreme and leaving only once the relative move has fully reverted keeps
  a position through the small wiggles that a symmetric exit would trade.

The sector ETF comes from ``sector_relative`` (the static map of today's list
at runtime; the replay also assigns past S&P 500 members statistically).
Index and sector ETFs and unmapped names have no sector benchmark and never
fire.  The signal's features carry the decision bar's ``rel_z_5`` (an entry
condition), the value ``sector_ranking`` orders same-day candidates by.

``meta_label_primary.sector_reversal`` is the vectorised replica for
``gate_replay`` and the experiment engine; the admission decision lives in
``scripts/run_sector_reversal_backtest.py`` and ``seed.admit_sector_reversal``.
Paper only.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from app.services.strategies.base import ExitPolicy, StrategySignalResult, neutral_signal
from app.services.strategies.rules import RuleStrategy

SLUG = "sector_reversal"
REPORT = "reports/sector-reversal-20261107.md"
DEFAULT_RULE = {
    "entry": [{"f": "dist_sma_200", "op": ">", "v": 0}, {"f": "rel_z_5", "op": "<", "v": -2}],
    "exit": [{"f": "rel_rsi_2", "op": ">", "v": 70}, {"f": "dist_sma_5", "op": ">", "v": 0}],
}
# The pre-registered decision of scripts/run_sector_reversal_backtest.py
# (``seed.admit_sector_reversal`` creates the row as a paper candidate only when True).
# 2026-11-07 run: the default rule is ``rejected`` (+21 bps gross per trade
# against ~33 bps of modelled round-trip cost; net -12 bps [-23, -2], excess
# over drift -34 bps), so the row is created paused.
ADMITTED = False
ADMISSION = ("the default rule (close > SMA200, rel_z_5 < -2, hysteresis exit) failed the discovery gate's "
             "explore checks on the point-in-time S&P 500: net expectancy -12.2 bps [-23, -2] per trade, excess "
             "over drift -33.9 bps, -10.3 bps since 2015, -6.5 bps on the holdout; about +21 bps gross against "
             "~33 bps of modelled costs.")
SECTOR_REVERSAL_EXIT_POLICY = ExitPolicy(stop_loss_pct=0.10, take_profit_pct=0.0, max_holding_sessions=10,
                                         indicator_exit=True)


class SectorReversalStrategy(RuleStrategy):
    """``RuleStrategy`` with the sector-relative reversal rule; the row's ``parameters["rule"]`` overrides it."""

    name = "Sector Reversal"
    slug = SLUG
    default_exit_policy = SECTOR_REVERSAL_EXIT_POLICY

    def __init__(self, parameters: Optional[dict] = None, slug: str = SLUG) -> None:
        parameters = dict(parameters or {})
        parameters.setdefault("rule", DEFAULT_RULE)
        super().__init__(parameters, slug=slug)

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        from app.services.sector_relative import static_sector
        from app.services.strategies.rules import SECTOR_CLOSE_COLUMN

        if data is not None and SECTOR_CLOSE_COLUMN not in data.columns and static_sector(symbol) is None:
            return neutral_signal(f"{symbol} has no sector ETF benchmark (index or sector ETF, or unmapped).",
                                  {"rule": "sector_reversal"})
        return super().generate_signal(symbol, data)
