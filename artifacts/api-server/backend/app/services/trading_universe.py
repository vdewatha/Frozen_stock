"""Paper execution universe, sector groups and feed-outage scope.

``settings.paper_execution_symbols`` is the single source of truth for which
symbols the scheduled paper loop may trade.  This module derives everything
else from it:

* the intraday-ingestion universe (execution symbols plus the frozen research
  core used by forward trials, scalp research and the live pilot),
* a static sector-group map for the per-sector open-position cap, and
* the rule that decides whether stale market data is a *per-symbol* problem
  (only that symbol is blocked) or a *broad outage* (everything is blocked).
"""
from __future__ import annotations

from typing import Iterable

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS as _DEFAULT_SYMBOLS, settings

# Frozen research universe. Forward trials, learning cycles, scalp research and
# the live pilot keep this exact set; it is never widened by configuration.
RESEARCH_CORE_SYMBOLS = frozenset({"AAPL", "MSFT", "QQQ", "SPY"})

# Index ETFs whose staleness means the feed (not a single name) is unhealthy.
INDEX_SENTINEL_SYMBOLS = frozenset({"SPY", "QQQ"})

# Liquid default (defined in config to avoid an import cycle).
DEFAULT_PAPER_EXECUTION_SYMBOLS = _DEFAULT_SYMBOLS

# Sector group = the SPDR sector ETF a name belongs to.  Broad index ETFs form
# their own "index" group.  Unknown symbols fall back to their own group, so an
# unmapped name is never silently pooled with unrelated exposure.
SECTOR_GROUPS: dict[str, str] = {
    "SPY": "index", "QQQ": "index", "IWM": "index", "DIA": "index",
    # Technology (XLK) incl. semiconductors
    "XLK": "XLK", "SMH": "XLK", "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AVGO": "XLK",
    "AMD": "XLK", "CRM": "XLK", "ORCL": "XLK", "ADBE": "XLK",
    # Communication services (XLC)
    "XLC": "XLC", "GOOGL": "XLC", "GOOG": "XLC", "META": "XLC", "NFLX": "XLC",
    # Consumer discretionary (XLY)
    "XLY": "XLY", "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY",
    # Consumer staples (XLP)
    "XLP": "XLP", "COST": "XLP", "PG": "XLP", "PEP": "XLP", "KO": "XLP", "WMT": "XLP",
    # Financials (XLF)
    "XLF": "XLF", "JPM": "XLF", "V": "XLF", "MA": "XLF", "BAC": "XLF",
    # Health care (XLV)
    "XLV": "XLV", "UNH": "XLV", "LLY": "XLV", "JNJ": "XLV", "ABBV": "XLV", "MRK": "XLV",
    # Energy, industrials, utilities, materials, real estate
    "XLE": "XLE", "XOM": "XLE",
    "XLI": "XLI", "XLU": "XLU", "XLB": "XLB", "XLRE": "XLRE",
}

SECTOR_NAMES: dict[str, str] = {
    "index": "Broad Market", "XLK": "Technology", "XLC": "Communication Services",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLF": "Financials",
    "XLV": "Health Care", "XLE": "Energy", "XLI": "Industrials", "XLU": "Utilities",
    "XLB": "Materials", "XLRE": "Real Estate",
}

ETF_SYMBOLS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU",
    "XLB", "XLRE", "XLC", "SMH",
})


def _normalize(values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        symbol = str(value).strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


def execution_symbols() -> list[str]:
    """Configured paper execution universe, normalized and de-duplicated."""
    return _normalize(settings.paper_execution_symbols)


def intraday_symbols() -> frozenset[str]:
    """Symbols the intraday feed may ingest and evaluate."""
    return RESEARCH_CORE_SYMBOLS | frozenset(execution_symbols())


def sector_group(symbol: str) -> str:
    symbol = str(symbol).strip().upper()
    return SECTOR_GROUPS.get(symbol, symbol)


def never_ingested_symbols(db, symbols: Iterable[str], provider: str) -> set[str]:
    """Symbols with no persisted 1m bar from ``provider`` at all.

    A newly added symbol that has never been ingested is *onboarding*, not a
    feed outage: it stays blocked by its own per-symbol gates, but it must not
    count toward the broad-outage share (otherwise expanding the universe
    would pause the whole stock path until every new name is backfilled).
    """
    from app.models import IntradayBar

    missing: set[str] = set()
    for symbol in _normalize(symbols):
        exists = (
            db.query(IntradayBar.id)
            .filter(IntradayBar.symbol == symbol, IntradayBar.provider == provider)
            .first()
        )
        if exists is None:
            missing.add(symbol)
    return missing


def classify_feed_outage(
    failed_symbols: Iterable[str],
    universe: Iterable[str],
    *,
    max_fraction: float | None = None,
    onboarding: Iterable[str] = (),
) -> dict:
    """Decide whether failed symbols are isolated or a broad outage.

    ``partial``: only the failed symbols are blocked; every other symbol keeps
    trading through its own per-symbol feed gate in ``reserve_stock_paper_order``.
    ``broad``: more than ``max_fraction`` of the universe failed, an index
    sentinel (SPY/QQQ) failed, or the universe is empty.  ``onboarding``
    symbols (never ingested) are blocked but excluded from that share; if every
    symbol is onboarding the universe is empty and the outage is broad.  A broad outage blocks
    all trading exactly as a single stale symbol used to.
    """
    onboarding_set = set(_normalize(onboarding))
    all_failed = sorted(set(_normalize(failed_symbols)))
    universe_list = [symbol for symbol in _normalize(universe) if symbol not in onboarding_set]
    failed = [symbol for symbol in all_failed if symbol not in onboarding_set]
    fraction = settings.paper_feed_broad_outage_fraction if max_fraction is None else max_fraction
    failed_fraction = (len(failed) / len(universe_list)) if universe_list else 1.0
    sentinels = sorted(set(failed) & INDEX_SENTINEL_SYMBOLS & set(universe_list))
    if not universe_list:
        scope, reason = "broad", "paper execution universe is empty"
    elif not failed:
        scope, reason = "none", None
    elif sentinels:
        scope, reason = "broad", f"index sentinel feed unavailable: {', '.join(sentinels)}"
    elif failed_fraction > fraction:
        scope, reason = "broad", (
            f"{len(failed)} of {len(universe_list)} symbols unavailable "
            f"({failed_fraction:.0%} > {fraction:.0%} broad-outage threshold)"
        )
    else:
        scope, reason = "partial", (
            f"{len(failed)} of {len(universe_list)} symbols unavailable; only those symbols are blocked"
        )
    if scope == "none" and all_failed:
        scope, reason = "partial", "only onboarding symbols without any ingested data are blocked"
    return {
        "scope": scope,
        "blocked_symbols": all_failed,
        "onboarding_symbols": sorted(onboarding_set & set(all_failed)),
        "failed_fraction": round(failed_fraction, 4),
        "broad_outage_fraction": fraction,
        "index_sentinels_failed": sentinels,
        "reason": reason,
    }
