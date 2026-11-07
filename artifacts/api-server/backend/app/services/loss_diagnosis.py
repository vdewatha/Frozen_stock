"""Why did a paper trade lose, and is the same failure repeating?

Trade attribution records *what* happened (exit cause, regime, slippage).
This module answers the question the learning loop needs: what was wrong.

Every closed trade gets one primary cause in one of four families:

* ``missed`` - something the strategy did not account for:
  ``missed_earnings`` (a report landed inside the hold),
  ``missed_market_move`` (SPY fell over the hold and the stock fell with it),
  ``missed_sector_move`` (its sector ETF fell and the stock fell with it).
* ``news`` - ``news_wrong``: the entry used a positive news read and the stock
  then fell on its own.
* ``strategy`` - ``signal_wrong``: the stock fell on its own, well beyond the
  market and its sector; the entry signal itself was wrong.
* ``noise`` - a loss smaller than ``NOISE_RETURN``.

``unknown_context`` means the benchmark prices were missing, so the cause
cannot be told apart.  Contributing flags are recorded next to the cause:
``crowded_symbol`` (another lot held the same symbol), ``clustered_stops``
(several stops filled the same session), ``exit_untested`` (the exits were
not the ones the strategy was backtested with), ``market_down``,
``macro_event_in_hold`` (a Fed decision, CPI or jobs report was released
during the hold), ``high_vix_entry`` (VIX at or above 25 at entry) and
``weak_breadth_entry`` (under 40% of S&P 500 members above their 200-day
average at entry).

Three flags judge the exit rather than the entry, from the attribution's
``post_exit`` path (exit price to the planned horizon) and the hold's best
excursion: ``premature_stop`` (stopped out, then the price got back to the
entry price before the horizon), ``gave_back_gains`` (the trade closed below
half of its best open gain, which was at least 1%) and ``held_too_long`` (the
time stop closed a trade whose best gain had reached the target, or 3% when
the strategy has no target, and that gain was never taken).

Wins are labelled too: ``market_tailwind`` when the market rose more than the
stock (the gain was drift, not edge), else ``edge``.

``repeating_failures`` turns per-trade causes into lessons: a cause that keeps
coming back for one strategy within its recent trades.  Lessons are proposals
for the research loop (``auto_apply: False``); nothing here changes a
strategy or an order.  Everything is hindsight evaluation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

UTC = timezone.utc
DIAGNOSIS_VERSION = "loss-diagnosis-v2"
# A benchmark that moved this much over the hold, with the stock within this
# much of it, explains the trade.
MOVE_RETURN = 0.01
NOISE_RETURN = 0.005
CLUSTERED_STOPS = 3
EXIT_TOLERANCE = 0.005
FAMILIES = {
    "missed_earnings": "missed", "missed_market_move": "missed", "missed_sector_move": "missed",
    "news_wrong": "news", "signal_wrong": "strategy", "noise": "noise", "unknown_context": "unknown",
    "market_tailwind": "win", "edge": "win",
}
LESSONS = {
    "missed_earnings": "An earnings report landed inside the hold; the strategy does not price event risk.",
    "missed_market_move": "The whole market fell and took the position with it; the entry ignored market direction.",
    "missed_sector_move": "The sector fell and took the position with it; the entry ignored sector direction.",
    "news_wrong": "The news read at entry was positive but the stock fell on its own; the news signal misled.",
    "signal_wrong": "The stock fell on its own, beyond the market and its sector; the entry signal was wrong.",
    "noise": "A small loss inside normal day-to-day movement.",
    "unknown_context": "Benchmark prices were missing; the cause cannot be separated from the market.",
}
PROPOSALS = {
    "missed_earnings": "Test blacking out entries whenever a report falls inside the full planned hold.",
    "missed_market_move": "Test a market gate: no new longs while SPY is falling (e.g. below its 10-day average) "
                          "and a cap on open longs moving together.",
    "missed_sector_move": "Test a sector gate: no new longs while the sector ETF is below its short average.",
    "news_wrong": "Re-score these headlines with a better sentiment model and check the source before trusting it.",
    "signal_wrong": "The weekly signal autopsy compares the entry features of these losses with this strategy's "
                    "wins (rsi_2, rsi_14, dist_sma_200, dist_sma_50, atr_pct_14, gap, vol_ratio_20_100, ibs, "
                    "bb_pctb_20, down_streak, dow, entry VIX and breadth); a feature that separates becomes a "
                    "scope candidate the experiment engine replays, and a scope that passes the rule narrows the "
                    "entries. Retire the strategy if no feature ever separates.",
}
CONTRIBUTING_PROPOSALS = {
    "crowded_symbol": "Several lots held one symbol: test one position per symbol across strategies.",
    "clustered_stops": "Stops fired together: positions were one bet. Test a cap on correlated entries per day.",
    "exit_untested": "The exits were not the backtested ones, so the result does not test the strategy.",
    "macro_event_in_hold": "A Fed, CPI or jobs release landed inside these holds: test a longer announcement "
                           "blackout for this strategy.",
    "high_vix_entry": "These losses were opened with the VIX at 25 or more: backtest half size or no entries "
                      "above a VIX level.",
    "weak_breadth_entry": "These losses were opened while most stocks were below their 200-day average: "
                          "backtest a breadth gate for this strategy.",
    "premature_stop": "Stopped out, then the price got back to the entry before the planned horizon: test a "
                      "wider stop for this strategy.",
    "gave_back_gains": "These trades gave back more than half of their best open gain: test a shorter time stop "
                       "or a profit-protection exit.",
    "held_too_long": "The time stop closed trades whose best gain had passed the target: test a shorter time "
                     "stop or a target that is taken.",
}
HIGH_VIX_ENTRY = 25.0
WEAK_BREADTH_ENTRY = 40.0
# Exit flags: the smallest open gain worth protecting, the share of it that
# counts as given back, and the target assumed for a strategy with none.
GAVE_BACK_MIN_MFE = 0.01
GAVE_BACK_SHARE = 0.5
HELD_TOO_LONG_MFE = 0.03
STOP_CAUSES = {"catastrophe_stop"}
TIME_STOP_CAUSES = {"time_stop"}


def _ret(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 6)


def diagnose(*, gross_return: float, is_loss: bool, benchmark_return: Optional[float] = None,
             sector_symbol: Optional[str] = None, sector_return: Optional[float] = None,
             earnings_in_hold: Optional[bool] = None, news_used: bool = False,
             news_sentiment: Optional[float] = None, concurrent_same_symbol: int = 0,
             same_session_stops: int = 0, exit_tested: Optional[bool] = None,
             macro_events_in_hold: Optional[list[str]] = None, entry_vix: Optional[float] = None,
             entry_breadth: Optional[float] = None, exit_cause: Optional[str] = None,
             post_exit: Optional[dict] = None, hold_mfe: Optional[float] = None,
             take_profit_pct: Optional[float] = None) -> dict:
    """One trade's cause and contributing flags.  Pure and deterministic.

    ``exit_cause`` is the attribution vocabulary (``catastrophe_stop``,
    ``time_stop``, ...); ``post_exit`` the attribution's exit-to-horizon path
    (``mfe`` from the exit price); ``hold_mfe`` the hold's best excursion from
    the entry price; ``take_profit_pct`` the strategy's declared target.
    """
    r = float(gross_return)
    excess_market = r - benchmark_return if benchmark_return is not None else None
    sector_differs = sector_symbol not in {None, "SPY"} and sector_return is not None
    excess_sector = r - sector_return if sector_differs else None
    contributing = []
    if concurrent_same_symbol > 0:
        contributing.append("crowded_symbol")
    if same_session_stops >= CLUSTERED_STOPS:
        contributing.append("clustered_stops")
    if exit_tested is False:
        contributing.append("exit_untested")
    if benchmark_return is not None and benchmark_return <= -MOVE_RETURN:
        contributing.append("market_down")
    if macro_events_in_hold:
        contributing.append("macro_event_in_hold")
    if entry_vix is not None and entry_vix >= HIGH_VIX_ENTRY:
        contributing.append("high_vix_entry")
    if entry_breadth is not None and entry_breadth < WEAK_BREADTH_ENTRY:
        contributing.append("weak_breadth_entry")
    contributing.extend(exit_flags(gross_return=r, exit_cause=exit_cause, post_exit=post_exit, hold_mfe=hold_mfe,
                                   take_profit_pct=take_profit_pct))

    if not is_loss:
        cause = "market_tailwind" if (benchmark_return is not None and benchmark_return >= MOVE_RETURN
                                      and excess_market is not None and excess_market <= 0) else "edge"
    elif earnings_in_hold:
        cause = "missed_earnings"
    elif r > -NOISE_RETURN:
        cause = "noise"
    elif benchmark_return is None:
        cause = "unknown_context"
    elif benchmark_return <= -MOVE_RETURN and excess_market > -MOVE_RETURN:
        cause = "missed_market_move"
    elif sector_differs and sector_return <= -MOVE_RETURN and excess_sector > -MOVE_RETURN:
        cause = "missed_sector_move"
    elif news_used and (news_sentiment or 0) > 0:
        cause = "news_wrong"
    else:
        cause = "signal_wrong"
    return {
        "version": DIAGNOSIS_VERSION, "cause": cause, "family": FAMILIES[cause],
        "contributing": contributing, "lesson": LESSONS.get(cause),
        "gross_return": _ret(r), "benchmark": "SPY", "benchmark_return": _ret(benchmark_return),
        "excess_vs_market": _ret(excess_market), "sector_symbol": sector_symbol if sector_differs else None,
        "sector_return": _ret(sector_return) if sector_differs else None, "excess_vs_sector": _ret(excess_sector),
        "earnings_in_hold": earnings_in_hold, "news_used": news_used, "news_sentiment": news_sentiment,
        "concurrent_same_symbol": concurrent_same_symbol, "same_session_stops": same_session_stops,
        "exit_tested": exit_tested, "macro_events_in_hold": macro_events_in_hold,
        "entry_vix": entry_vix, "entry_breadth": entry_breadth, "exit_cause": exit_cause,
        "post_exit_return": _ret((post_exit or {}).get("return")), "post_exit_mfe": _ret((post_exit or {}).get("mfe")),
        "hold_mfe": _ret(hold_mfe), "take_profit_pct": take_profit_pct, "hindsight_only": True,
    }


def exit_flags(*, gross_return: float, exit_cause: Optional[str], post_exit: Optional[dict] = None,
               hold_mfe: Optional[float] = None, take_profit_pct: Optional[float] = None) -> list[str]:
    """The exit-judging flags for one trade (see the module docstring).  Pure."""
    r = float(gross_return)
    flags = []
    post_mfe = (post_exit or {}).get("mfe")
    if exit_cause in STOP_CAUSES and r < 0 and post_mfe is not None and r > -1:
        # Back to the entry price from the exit price needs 1 / (1 + r) - 1.
        if float(post_mfe) >= 1 / (1 + r) - 1 - 1e-9:
            flags.append("premature_stop")
    if hold_mfe is not None and hold_mfe >= GAVE_BACK_MIN_MFE and r < (1 - GAVE_BACK_SHARE) * hold_mfe:
        flags.append("gave_back_gains")
    if exit_cause in TIME_STOP_CAUSES and hold_mfe is not None:
        target = float(take_profit_pct) if take_profit_pct else HELD_TOO_LONG_MFE
        if hold_mfe >= target and r < target:
            flags.append("held_too_long")
    return flags


def news_from_features(features: Optional[dict]) -> tuple[bool, Optional[float]]:
    """Whether a signal used news, and the sentiment it read (None when unknown)."""
    if not isinstance(features, dict):
        return False, None
    keys = [key for key in features if "news" in str(key).lower() or "sentiment" in str(key).lower()]
    if not keys:
        return False, None
    for key in keys:
        value = features[key]
        if isinstance(value, dict):
            value = value.get("sentiment_score", value.get("sentiment", value.get("score")))
        try:
            return True, float(value)
        except (TypeError, ValueError):
            continue
    return True, None


def exit_tested(policy: Any, *, entry_price: Optional[float], stop_price: Optional[float],
                take_profit_price: Optional[float]) -> Optional[bool]:
    """Did the entry's broker legs match the strategy's declared (backtested) exits?

    ``None`` when the entry carried no legs to compare.  A strategy with no
    declared exit policy has no tested exits, so its trades are ``False``.
    """
    if not entry_price or entry_price <= 0 or (stop_price is None and take_profit_price is None):
        return None
    if policy is None:
        return False
    if policy.stop_loss_pct is not None and stop_price is not None:
        if abs((1 - stop_price / entry_price) - float(policy.stop_loss_pct)) > EXIT_TOLERANCE:
            return False
    if policy.take_profit_pct is not None:
        planned = (take_profit_price / entry_price - 1) if take_profit_price else 0.0
        if abs(planned - float(policy.take_profit_pct)) > EXIT_TOLERANCE:
            return False
    return True


def earnings_between(symbol: str, start: datetime, end: datetime,
                     lookup: Optional[Callable[[str], Any]]) -> Optional[bool]:
    """A report time inside ``(start, end]``; ``None`` when the history is unavailable."""
    from app.services.trading_universe import ETF_SYMBOLS

    if symbol.upper() in ETF_SYMBOLS:
        return False
    if lookup is None:
        return None
    events = lookup(symbol)
    if events is None:
        return None
    if getattr(events, "empty", True):
        return False
    import pandas as pd

    stamps = pd.to_datetime(events["reported_at"], utc=True)
    return bool(((stamps > pd.Timestamp(start)) & (stamps <= pd.Timestamp(end))).any())


class PriceLookup:
    """Close at or just before a moment: 1-minute bars first, then daily closes."""

    def __init__(self, db) -> None:
        self.db = db
        self.cache: dict = {}

    def at(self, symbol: str, moment: datetime) -> Optional[float]:
        from sqlalchemy import select

        from app.models import IntradayBar, MarketPrice

        moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
        key = (symbol, moment.replace(second=0, microsecond=0))
        if key in self.cache:
            return self.cache[key]
        close = self.db.scalar(
            select(IntradayBar.close).where(IntradayBar.symbol == symbol, IntradayBar.timeframe == "1m",
                                            IntradayBar.opened_at <= moment,
                                            IntradayBar.opened_at >= moment - timedelta(days=4))
            .order_by(IntradayBar.opened_at.desc()).limit(1))
        if close is None:
            close = self.db.scalar(
                select(MarketPrice.close).where(MarketPrice.symbol == symbol,
                                                MarketPrice.price_date <= moment.date(),
                                                MarketPrice.price_date >= moment.date() - timedelta(days=6))
                .order_by(MarketPrice.price_date.desc()).limit(1))
        value = float(close) if close is not None and float(close) > 0 else None
        self.cache[key] = value
        return value

    def window_return(self, symbol: str, start: datetime, end: datetime) -> Optional[float]:
        first, last = self.at(symbol, start), self.at(symbol, end)
        return last / first - 1 if first and last else None


def _detail(row) -> dict:
    return (getattr(row, "detail", None) or {}) if not isinstance(row, dict) else row


def repeating_failures(rows: Iterable, *, window: int = 20, min_count: int = 3,
                       min_share: float = 0.4) -> list[dict]:
    """Causes that keep coming back among each strategy's last ``window`` diagnosed trades.

    ``rows`` are attribution rows (or their ``detail`` dicts with
    ``strategy_type``, ``symbol``, ``closed_at``).  A cause repeats when it is
    the primary cause of at least ``min_count`` losses and ``min_share`` of
    the strategy's losses in the window.  The account-wide ``"all"`` entry
    catches a failure spread across strategies (a market move hits them all).
    """
    by_strategy: dict[str, list] = defaultdict(list)
    for row in rows:
        detail = _detail(row)
        diagnosis = detail.get("diagnosis")
        if not diagnosis:
            continue
        strategy = getattr(row, "strategy_type", None) or detail.get("strategy_type") or "unattributed"
        symbol = getattr(row, "symbol", None) or detail.get("symbol")
        closed = getattr(row, "closed_at", None) or detail.get("closed_at")
        closed = closed.isoformat() if isinstance(closed, datetime) else str(closed)
        item = (closed, symbol, diagnosis)
        by_strategy[strategy].append(item)
        by_strategy["all"].append(item)
    out = []
    for strategy, items in by_strategy.items():
        recent = sorted(items, key=lambda item: item[0])[-(window * (3 if strategy == "all" else 1)):]
        losses = [item for item in recent if FAMILIES.get(item[2]["cause"]) not in {"win", None}]
        causes = Counter(item[2]["cause"] for item in losses)
        flags = Counter(flag for item in losses for flag in item[2].get("contributing") or [])
        for cause, count in causes.most_common():
            if cause in {"noise", "unknown_context"} or count < min_count or count / len(losses) < min_share:
                continue
            members = [item for item in losses if item[2]["cause"] == cause]
            out.append({
                "strategy_type": strategy, "cause": cause, "family": FAMILIES[cause], "count": count,
                "losses_in_window": len(losses), "trades_in_window": len(recent),
                "share_of_losses": round(count / len(losses), 4),
                "symbols": sorted({item[1] for item in members if item[1]}),
                "first_seen": members[0][0], "last_seen": members[-1][0],
                "lesson": LESSONS[cause], "proposal": PROPOSALS.get(cause),
                "contributing": {flag: n for flag, n in flags.most_common() if flag in CONTRIBUTING_PROPOSALS},
                "auto_apply": False, "requires_fresh_holdout_and_forward_trial": True,
            })
    out.sort(key=lambda item: (item["strategy_type"] != "all", -item["count"], item["strategy_type"], item["cause"]))
    return out


def cause_breakdown(rows: Iterable) -> dict:
    """Counts of primary causes and contributing flags over diagnosed rows."""
    causes, families, flags = Counter(), Counter(), Counter()
    diagnosed = 0
    for row in rows:
        diagnosis = _detail(row).get("diagnosis")
        if not diagnosis:
            continue
        diagnosed += 1
        causes[diagnosis["cause"]] += 1
        families[diagnosis["family"]] += 1
        flags.update(diagnosis.get("contributing") or [])
    return {"diagnosed": diagnosed, "by_cause": dict(causes.most_common()),
            "by_family": dict(families.most_common()), "contributing": dict(flags.most_common())}
