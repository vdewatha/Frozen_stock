"""Order a strategy's same-day BUYs by how far each stock fell against its sector (``rel_z_5``).

When several symbols signal the same strategy on the same day and the book
has fewer slots than signals, the cycle takes them in its symbol order (the
execution list), first come first served.  For a strategy whose row sets
``parameters["rank_by"] = "rel_z_5"`` (written once by
``seed.apply_sector_ranking`` where ``scripts/run_sector_reversal_backtest.py``
adopted it) the BUYs are taken instead from the most negative 5-day
return relative to the stock's sector (``rules`` feature ``rel_z_5``)
upward; a BUY without a sector benchmark (an index ETF, an unmapped name)
goes after the ranked ones, in its old order.

* ``generate_cycle_signals`` calls ``rank_value`` once per symbol with a BUY
  of a ranked strategy and ``assign_ranks`` before persisting, so the
  signal's ``features["sector_rank"]`` records ``{"by", "value", "rank",
  "of"}`` (also returned as ``outcome["sector_rank"]``).
* The autotrader calls ``order_symbols`` once before its symbol loop: the
  positions the ranked symbols hold in the list are refilled in rank order,
  every other symbol keeps its place, so nothing else's priority changes.

Ranking never creates, removes or resizes a signal.  Paper only.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional

import pandas as pd

RANK_KEY = "rank_by"
RANK_FEATURE = "rel_z_5"
FEATURE_KEY = "sector_rank"
_TAIL = 400


def ranked(parameters: Optional[Mapping]) -> bool:
    return str((parameters or {}).get(RANK_KEY) or "") == RANK_FEATURE


def rank_value(symbol: str, prices: Any) -> Optional[float]:
    """``rel_z_5`` on the last bar of ``prices`` (the runtime sector ETF attached when missing); ``None`` if unknown."""
    from app.services.strategies import rules

    if not isinstance(prices, pd.DataFrame) or prices.empty or "close" not in prices:
        return None
    frame = prices.iloc[-_TAIL:].reset_index(drop=True)
    try:
        frame = rules.attach_runtime_sector(symbol, frame)
        if frame is None:
            return None
        value = float(rules.feature(frame, RANK_FEATURE).iloc[-1])
    except Exception:  # ranking is advisory; an unrankable symbol keeps its old order
        return None
    return value if math.isfinite(value) else None


def _sort_key(value: Optional[float], position: int) -> tuple:
    return (value is None, value if value is not None else 0.0, position)


def assign_ranks(buys: Iterable[tuple[str, str]], values: Mapping[str, Optional[float]]) -> dict[tuple[str, str], dict]:
    """``(symbol, strategy) -> {"by", "value", "rank", "of"}``: per strategy, most negative ``rel_z_5`` first.

    ``buys`` is in the cycle's current order, which breaks ties and orders
    the unrankable BUYs after the ranked ones.
    """
    by_strategy: dict[str, list[str]] = {}
    for symbol, strategy in buys:
        by_strategy.setdefault(strategy, []).append(symbol)
    out = {}
    for strategy, symbols in by_strategy.items():
        order = sorted(range(len(symbols)), key=lambda i: _sort_key(values.get(symbols[i]), i))
        for rank, i in enumerate(order, start=1):
            out[(symbols[i], strategy)] = {"by": RANK_FEATURE, "value": values.get(symbols[i]), "rank": rank,
                                           "of": len(symbols)}
    return out


def order_symbols(symbols: list[str], outcomes: Mapping[tuple[str, str], Any]) -> list[str]:
    """``symbols`` with the ranked BUYs' symbols refilled into their own positions in rank order.

    A symbol is ranked when any of its outcomes carries ``sector_rank``; its
    key is the best (most negative) value it was ranked with.  Every other
    symbol keeps its position.
    """
    values: dict[str, Optional[float]] = {}
    for (symbol, _strategy), outcome in outcomes.items():
        record = outcome.get(FEATURE_KEY) if isinstance(outcome, dict) else None
        if not record:
            continue
        value = record.get("value")
        current = values.get(symbol)
        if symbol not in values or (value is not None and (current is None or value < current)):
            values[symbol] = value
    if len(values) < 2:
        return list(symbols)
    positions = [i for i, symbol in enumerate(symbols) if symbol in values]
    ordered = sorted(positions, key=lambda i: _sort_key(values[symbols[i]], i))
    out = list(symbols)
    for slot, source in zip(positions, ordered):
        out[slot] = symbols[source]
    return out
