"""Batched signal generation for the scheduled stock-paper cycle.

The cycle used to call ``create_stock_paper_signal`` once per
(symbol, strategy) pair.  Every call re-resolved the strategy row, reloaded the
same trusted daily frame and intraday observation (``feed_status`` alone scans
the whole session's bars), re-fitted the predictive model and committed.  With
8 strategies that is 8x the data loads, model fits and commits per symbol.

This module computes the same signals in one pass:

* each strategy row is resolved once per cycle (not once per symbol),
* each symbol's trusted daily frame and execution observation are loaded once
  and shared by every strategy (O(symbols) loads, not O(symbols x strategies)),
* strategy outputs are memoized on a content fingerprint of the daily frame,
  so a deterministic strategy (incl. the seeded model fit) is recomputed only
  when the frame actually changes (normally twice a day after daily import),
* every ``StrategySignal`` row and its ledger event are persisted with one
  flush and one commit.

Persisted rows, ``features`` payloads, ledger events and the returned signal
dicts are produced by the same helpers ``_create_stock_paper_signal`` uses, so
signal semantics are unchanged.  Governance is untouched: this never reserves
or dispatches; the autotrader still routes every BUY/SELL through
``reserve_stock_paper_order`` and ``dispatch_reserved_order``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd
from sqlalchemy.orm import Session

from app.services import stock_paper_ledger as ledger
from app.services.strategy_execution_policy import daily_bar_execution, gate_daily_bar_entry, symbol_scope_for

logger = logging.getLogger(__name__)
UTC = timezone.utc
_CACHE_MAX_ENTRIES = 4096
_cache: "OrderedDict[tuple, Any]" = OrderedDict()
_cache_lock = threading.Lock()


def clear_signal_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _frame_fingerprint(prices: Any) -> str | None:
    """Content hash of the daily frame; None when it cannot be fingerprinted."""
    if not isinstance(prices, pd.DataFrame):
        return None
    try:
        hashed = pd.util.hash_pandas_object(prices, index=True).to_numpy()
    except Exception:
        return None
    digest = hashlib.blake2b(hashed.tobytes(), digest_size=16)
    digest.update(",".join(map(str, prices.columns)).encode())
    return digest.hexdigest()


def _generate(strategy_type: str, parameters: dict | None, symbol: str, prices: Any,
              fingerprint: str | None, stats: dict) -> Any:
    factory = ledger.get_strategy
    key = None
    if fingerprint is not None:
        try:
            params_key = json.dumps(parameters or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            params_key = None
        if params_key is not None:
            # The factory identity keeps test doubles from sharing entries
            # with the real registry.
            key = (id(factory), strategy_type, params_key, symbol, fingerprint)
    if key is not None:
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None:
                _cache.move_to_end(key)
        if cached is not None:
            stats["strategy_cache_hits"] += 1
            return copy.deepcopy(cached)
    generated = factory(strategy_type, parameters).generate_signal(symbol, prices)
    stats["strategy_computes"] += 1
    if key is not None:
        with _cache_lock:
            _cache[key] = copy.deepcopy(generated)
            while len(_cache) > _CACHE_MAX_ENTRIES:
                _cache.popitem(last=False)
    return generated


def _is_research_only(strategy: Any) -> bool:
    # Test doubles and older rows may omit the status field; the existing
    # cycle contract treated those rows as active.
    return getattr(strategy, "current_status", "paper_trading_active") != "paper_trading_active"


def _spy_loader(db: Session):
    """Lazily load SPY's trusted daily frame once per cycle (None on failure)."""
    cache: dict[str, Any] = {}

    def load():
        if "SPY" not in cache:
            try:
                cache["SPY"] = ledger.trusted_history(db, "SPY", require_active=False)[0]
            except Exception as exc:  # regime features become missing, never fatal
                logger.info("meta-label: SPY history unavailable: %s", exc)
                cache["SPY"] = None
        return cache["SPY"]

    return load


def generate_cycle_signals(
    db: Session,
    symbols: Iterable[str],
    strategies: Iterable[Any],
    *,
    meta_label: Any = None,
    now: datetime | None = None,
) -> tuple[dict[tuple[str, str], dict | BaseException], dict]:
    """Generate and persist one signal per (symbol, strategy) in one pass.

    Returns ``(outcomes, stats)``.  ``outcomes[(symbol, strategy_type)]`` is
    either the same dict ``create_stock_paper_signal`` /
    ``create_stock_paper_research_signal`` returns, or the exception that call
    would have raised (``StockPaperError`` for untrusted data or an unknown
    strategy; any other exception for a strategy/persistence failure).

    ``meta_label`` (a ``meta_label_shadow.MetaLabelRuntime``) scores every
    BUY; the record is stored in the signal's ``features["meta_label"]`` and
    returned as ``outcome["meta_label"]``.  It never changes the signal.

    Strategies with ``daily_bar_execution`` pass through
    ``gate_daily_bar_entry`` at ``now`` (default: the current time): a BUY
    outside the early-session entry window is persisted as a HOLD, and every
    such signal records ``features["entry_timing"]``.
    """
    started = time.perf_counter()
    evaluated_at = now or datetime.now(UTC)
    symbols = [str(symbol).strip().upper() for symbol in symbols]
    strategies = list(strategies)
    stats = {
        "symbols": len(symbols),
        "strategies": len(strategies),
        "strategy_resolutions": 0,
        "daily_loads": 0,
        "intraday_loads": 0,
        "strategy_computes": 0,
        "strategy_cache_hits": 0,
        "signals_persisted": 0,
        "commits": 0,
        "out_of_scope_pairs": 0,
    }
    outcomes: dict[tuple[str, str], dict | BaseException] = {}
    meta_records: dict[tuple[str, str], dict] = {}
    load_spy = _spy_loader(db)

    # 1. Resolve each strategy's bound row once per cycle.
    bound: list[tuple[str, bool, Any | BaseException]] = []
    scopes: dict[str, frozenset[str] | None] = {}
    daily_bar: dict[str, bool] = {}
    for strategy in strategies:
        strategy_type = strategy.strategy_type
        research_only = _is_research_only(strategy)
        scope = ledger.RESEARCH_SIGNAL_STATUSES if research_only else ledger.ACTIVE_SIGNAL_STATUSES
        try:
            row: Any | BaseException = ledger._resolve_signal_strategy(db, strategy_type, scope)
        except Exception as exc:  # recorded per pair below
            row = exc
        stats["strategy_resolutions"] += 1
        bound.append((strategy_type, research_only, row))
        # Per-strategy symbol scope (e.g. RSI(2) on SPY/QQQ/IWM/DIA only):
        # out-of-scope pairs get no signal row and no outcome at all.
        scopes[strategy_type] = symbol_scope_for(row if not isinstance(row, BaseException) else strategy)
        daily_bar[strategy_type] = daily_bar_execution(row if not isinstance(row, BaseException) else strategy)
    resolved_at = time.perf_counter()

    # 2. One data load per symbol, every strategy computed on that frame.
    pending: list[tuple[str, str, bool, Any, Any, str, dict]] = []
    for symbol in symbols:
        try:
            prices, source, observation = ledger._load_signal_inputs(db, symbol)
            stats["daily_loads"] += 1
            stats["intraday_loads"] += 1
        except Exception as exc:
            stats["daily_loads"] += 1
            for strategy_type, _research_only, row in bound:
                scope = scopes.get(strategy_type)
                if scope is not None and symbol not in scope:
                    continue
                # The per-pair path resolved the strategy before loading data.
                outcomes[(symbol, strategy_type)] = row if isinstance(row, BaseException) else exc
            continue
        fingerprint = _frame_fingerprint(prices)
        symbol_start = len(pending)
        for strategy_type, research_only, row in bound:
            scope = scopes.get(strategy_type)
            if scope is not None and symbol not in scope:
                stats["out_of_scope_pairs"] += 1
                continue
            if isinstance(row, BaseException):
                outcomes[(symbol, strategy_type)] = row
                continue
            try:
                generated = _generate(strategy_type, row.parameters, symbol, prices, fingerprint, stats)
                if daily_bar.get(strategy_type):
                    # Entry timing must match the backtester's next-open fill;
                    # ``generated`` is a private copy, so the cache is unchanged.
                    generated = gate_daily_bar_entry(generated, evaluated_at, prices)
            except Exception as exc:  # one strategy must not stop the fan-out
                outcomes[(symbol, strategy_type)] = exc
                continue
            pending.append((symbol, strategy_type, research_only, row, generated, source, observation))
        if meta_label is not None and getattr(meta_label, "active", False):
            buys = [(item[1], item[4]) for item in pending[symbol_start:] if getattr(item[4], "action", None) == "BUY"]
            if buys:
                records = meta_label.evaluate_symbol(symbol, prices, load_spy(), buys)
                for strategy_type, generated in buys:
                    record = records.get(strategy_type)
                    if record is not None:
                        meta_records[(symbol, strategy_type)] = record
                        # ``generated`` is a private copy (see _generate).
                        generated.features = {**(generated.features or {}), "meta_label": record}
    computed_at = time.perf_counter()

    # 3. Persist every signal and its ledger event with a single commit.
    if pending:
        now = datetime.now(UTC)
        rows = [
            ledger._build_stock_paper_signal(row, symbol, generated, source, observation, research_only, now)
            for symbol, _strategy_type, research_only, row, generated, source, observation in pending
        ]
        try:
            db.add_all(rows)
            db.flush()
            for signal in rows:
                ledger._record_signal_generated(db, signal)
            db.commit()
            stats["commits"] += 1
        except Exception as exc:
            db.rollback()
            for symbol, strategy_type, *_rest in pending:
                outcomes[(symbol, strategy_type)] = exc
        else:
            stats["signals_persisted"] = len(rows)
            for signal, (symbol, strategy_type, research_only, _row, _gen, _src, observation) in zip(rows, pending):
                outcome = ledger._stock_paper_signal_result(signal, strategy_type, observation, research_only)
                if (symbol, strategy_type) in meta_records:
                    outcome["meta_label"] = meta_records[(symbol, strategy_type)]
                outcomes[(symbol, strategy_type)] = outcome
    finished = time.perf_counter()
    stats["timing_ms"] = {
        "resolve_strategies": round((resolved_at - started) * 1000, 1),
        "load_and_compute": round((computed_at - resolved_at) * 1000, 1),
        "persist": round((finished - computed_at) * 1000, 1),
        "total": round((finished - started) * 1000, 1),
    }
    logger.info(
        "stock paper signal batch: symbols=%s strategies=%s daily_loads=%s intraday_loads=%s "
        "computes=%s cache_hits=%s persisted=%s total_ms=%s",
        stats["symbols"], stats["strategies"], stats["daily_loads"], stats["intraday_loads"],
        stats["strategy_computes"], stats["strategy_cache_hits"], stats["signals_persisted"],
        stats["timing_ms"]["total"],
    )
    return outcomes, stats
