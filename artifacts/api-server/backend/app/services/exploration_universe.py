"""Exploration universe: the liquid point-in-time S&P 500 names, admitted for learning-sized entries only.

The bot learns from closed trades, and the 45-symbol core list under its caps
(1 per symbol, 5 per strategy, 3 per sector, 20 open) closes only a handful a
week, so every forward verdict takes months.  ``scripts/
run_liquid_universe_check.py`` measured, on the point-in-time S&P 500 replay,
which liquidity terciles trade about as well as the core list after costs
(``reports/liquid-universe-<date>.md``); this module opens the *exploration
lane* to them.

What the lane is (setting ``exploration_universe``):

* ``"core"``: nothing changes; the lane is closed.
* ``"liquid_pit"``: once a day (``run_exploration_universe``, before the open)
  today's S&P 500 single-stock members outside the core list are screened
  from the cached daily history: 20-day median dollar volume ranked into
  terciles among all members, the Corwin-Schultz high-low spread proxy under
  ``exploration_universe_max_spread``, a price of at least
  ``news_discovery_min_price`` and at most the one-share learning cap (a name
  whose one share exceeds the floor is skipped and logged).  Names in the top
  ``exploration_universe_terciles`` terciles are *admitted*.
* Of the admitted names only those where a broad-scope live strategy's daily
  replica (``meta_label_primary``) fires a BUY on the last completed bar
  become *candidates* (at most ``exploration_universe_max_candidates``, most
  liquid first): only they join the paper cycle and the intraday feed, so the
  minute-bar load stays bounded.  A lane name still held after its candidacy
  lapsed stays in the cycle for exits only, as news-found names do.
* Every lane entry is learning-sized (``paper_exploration_risk_fraction`` of
  equity with the one-share floor), placed with the exploration source,
  whatever the strategy's status.  Scheduled (full-size) entries stay on the
  core list.  Lane positions have their own slots (``exploration_max_open``)
  and never count toward, or are blocked by, the core caps; exploration
  entries also face per-entry-date caps per strategy and per sector
  (``entry_protections.ExplorationDateCaps``).

The admission pattern is the news-discovery one (``news_discovery``): state is
one append-only ``exploration_universe`` audit row per run, admitted names
become active assets (and are deactivated again when the lane drops them and
nothing is held), and ``trading_universe.intraday_symbols`` reads the cached
candidate set.  Sectors of the new names come from the correlation of their
SPY-residual daily returns with the SPDR sector ETFs' (``assign_sectors``):
$0, no new data source.

Paper only.  Nothing here places an order; the cycle and the ledger decide.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from app.core.config import settings

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT_TYPE = "exploration_universe"
ACTION = "run_exploration_universe"
SETTING_VALUES = ("core", "liquid_pit")
ASSET_SECTOR = "Exploration universe"
# Pre-registered in scripts/run_liquid_universe_check.py (fixed before the first run).
MARGIN_BPS = 5.0
DECISION_WINDOWS = ("2015+", "2020+")
SECTOR_LOOKBACK = 252
SECTOR_MIN_OBSERVATIONS = 120
SECTOR_ETFS = ("XLK", "XLC", "XLY", "XLP", "XLF", "XLV", "XLE", "XLI", "XLU", "XLB", "XLRE")
_CACHE_SECONDS = 60.0
_cache: dict = {"at": None, "symbols": frozenset(), "sectors": {}}


# -------------------------------------------------------- pre-registered rule

def tercile_qualifies(row: Mapping, baseline: Mapping, *, margin_bps: float = MARGIN_BPS,
                      max_spread: float) -> tuple[bool, str]:
    """One tercile against the core list: 2015+ and 2020+ net expectancy within ``margin_bps``, spread under the ceiling.

    ``row`` / ``baseline`` carry ``expectancy_bps`` per window (net of the
    standard cost model and the extra spread cost) and ``row["spread"]`` the
    tercile's median decision-date spread proxy.
    """
    reasons = []
    for window in DECISION_WINDOWS:
        mine = (row.get("expectancy_bps") or {}).get(window)
        core = (baseline.get("expectancy_bps") or {}).get(window)
        if mine is None or core is None:
            reasons.append(f"{window}: no trades to compare")
        elif float(mine) < float(core) - margin_bps:
            reasons.append(f"{window}: {float(mine):+.1f} bps is more than {margin_bps:g} bps below the core "
                           f"list's {float(core):+.1f}")
    spread = row.get("spread")
    if spread is None or not math.isfinite(float(spread)):
        reasons.append("no spread proxy")
    elif float(spread) >= max_spread:
        reasons.append(f"spread proxy {float(spread):.2%} is at or above the {max_spread:.2%} ceiling")
    return (not reasons), ("qualifies" if not reasons else "; ".join(reasons))


def select_universe(terciles: Mapping[int, Mapping], baseline: Mapping, *, margin_bps: float = MARGIN_BPS,
                    max_spread: float) -> dict:
    """The largest qualifying universe: the top ``k`` terciles, all of which qualify (k = 0: stay on the core list).

    Terciles are nested from the most liquid down, so a qualifying tercile 2
    under a failing tercile 1 is not admitted: the lane widens by liquidity
    only.
    """
    decisions, k, open_run = [], 0, True
    for tercile in sorted(terciles):
        ok, reason = tercile_qualifies(terciles[tercile], baseline, margin_bps=margin_bps, max_spread=max_spread)
        decisions.append({"tercile": int(tercile), "qualifies": ok, "reason": reason})
        if ok and open_run:
            k = int(tercile)
        else:
            open_run = False
    return {"terciles": k, "exploration_universe": "liquid_pit" if k > 0 else "core", "decisions": decisions,
            "margin_bps": margin_bps, "max_spread": max_spread}


# ------------------------------------------------------------------- sectors

def _returns(frame: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
    return pd.Series(close.pct_change().to_numpy(), index=pd.to_datetime(frame["date"]).dt.normalize())


def assign_sectors(frames: Mapping[str, pd.DataFrame], etfs: Mapping[str, pd.DataFrame],
                   spy: Optional[pd.DataFrame], *, lookback: int = SECTOR_LOOKBACK,
                   known: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Sector ETF per symbol: the highest correlation of SPY-residual daily returns over the last ``lookback`` bars.

    ``known`` (the static ``trading_universe.SECTOR_GROUPS``) wins; a symbol
    with too little history or no positive correlation gets its own group.
    """
    known = dict(known or {})
    out: dict[str, str] = {}
    if spy is None or not len(spy):
        return {symbol: known.get(symbol, symbol) for symbol in frames}
    market = _returns(spy)

    def residual(series: pd.Series) -> pd.Series:
        joined = pd.concat([series, market], axis=1, join="inner").dropna().tail(lookback)
        if len(joined) < SECTOR_MIN_OBSERVATIONS:
            return pd.Series(dtype=float)
        y, x = joined.iloc[:, 0], joined.iloc[:, 1]
        beta = float(np.cov(y, x, ddof=1)[0, 1] / max(np.var(x, ddof=1), 1e-12))
        return y - beta * x

    etf_residuals = {etf: residual(_returns(frame)) for etf, frame in etfs.items()
                     if frame is not None and len(frame)}
    etf_residuals = {etf: series for etf, series in etf_residuals.items() if len(series)}
    for symbol, frame in frames.items():
        if symbol in known:
            out[symbol] = known[symbol]
            continue
        mine = residual(_returns(frame)) if frame is not None and len(frame) else pd.Series(dtype=float)
        best, best_corr = symbol, 0.0
        for etf, series in sorted(etf_residuals.items()):
            joined = pd.concat([mine, series], axis=1, join="inner").dropna()
            if len(joined) < SECTOR_MIN_OBSERVATIONS:
                continue
            corr = float(np.corrcoef(joined.iloc[:, 0], joined.iloc[:, 1])[0, 1])
            if math.isfinite(corr) and corr > best_corr:
                best, best_corr = etf, corr
        out[symbol] = best
    return out


# -------------------------------------------------------------------- screen

def screen_names(members: Iterable[str], frames: Mapping[str, pd.DataFrame], *, core: Iterable[str],
                 terciles: int, max_spread: float, max_price: float, min_price: float,
                 window: int = 20) -> list[dict]:
    """The lane screen for today's members: one row per member with its liquidity, tercile and verdict.

    Terciles rank every member with a full window (core names included, so a
    tercile means the same as in the replay); core names and ETFs are then
    left out of the admitted set.
    """
    from app.services.point_in_time_universe import ALWAYS_MEMBERS, liquidity_frame

    core = {str(s).upper() for s in core}
    rows: list[dict] = []
    for symbol in sorted({str(s).upper() for s in members} - set(ALWAYS_MEMBERS)):
        frame = frames.get(symbol)
        if frame is None or len(frame) < window + 1:
            rows.append({"symbol": symbol, "admitted": False, "reason": "no daily history in the cache"})
            continue
        liquid = liquidity_frame(frame, window).iloc[-1]
        dollar, spread, price = (float(liquid["dollar_volume"]), float(liquid["spread"]), float(liquid["close"]))
        rows.append({"symbol": symbol, "dollar_volume_20d": round(dollar, 0) if math.isfinite(dollar) else None,
                     "spread_20d": round(spread, 5) if math.isfinite(spread) else None,
                     "price": round(price, 4) if math.isfinite(price) else None,
                     "as_of": str(pd.Timestamp(liquid["date"]).date()), "admitted": False, "reason": None})
    ranked = sorted((row for row in rows if row.get("dollar_volume_20d")), key=lambda row: -row["dollar_volume_20d"])
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
        row["tercile"] = int(min(3, max(1, math.ceil(3 * rank / len(ranked)))))
    for row in rows:
        if row["reason"] is not None:
            continue
        if row["symbol"] in core:
            row["reason"] = "core list"
        elif not row.get("tercile"):
            row["reason"] = "no 20-day dollar volume"
        elif row["tercile"] > terciles:
            row["reason"] = f"liquidity tercile {row['tercile']} is outside the admitted top {terciles}"
        elif row.get("spread_20d") is None or row["spread_20d"] >= max_spread:
            row["reason"] = f"spread proxy {row.get('spread_20d')} is at or above the {max_spread:.2%} ceiling"
        elif row.get("price") is None or row["price"] < min_price:
            row["reason"] = f"price {row.get('price')} is below {min_price:.2f}"
        elif row["price"] > max_price:
            row["reason"] = (f"one share ({row['price']:.2f}) exceeds the one-share learning floor "
                             f"{max_price:.2f}; skipped")
        else:
            row["admitted"] = True
    return rows


def buy_candidates(frames: Mapping[str, pd.DataFrame], strategies: Iterable[str], *,
                   spy: Optional[pd.DataFrame] = None, setups: Optional[Mapping[str, Mapping]] = None) -> dict[str, list[str]]:
    """Symbol -> the broad-scope replayable strategies whose daily replica BUYs on the last completed bar.

    Replicas are the parity-tested ``meta_label_primary`` ones (``gate_replay
    .entries``, incl. the ADX range gate); a strategy's SPY market gate from
    ``setups`` is applied on the same bar.  A frame that fails to replay is
    skipped, never raised.
    """
    from app.services import gate_replay
    from app.services import meta_label_primary as primary

    wanted = [s for s in strategies if s in gate_replay.TECHNICAL]
    setups = dict(setups or {})
    out: dict[str, list[str]] = {}
    if not wanted:
        return out
    for symbol, frame in frames.items():
        try:
            replay = primary.replay_signals(frame)
            firing = []
            for strategy in wanted:
                buy = gate_replay.entries(strategy, frame, replay)
                window = int((setups.get(strategy) or {}).get("market_gate_sma") or 0)
                if window:
                    buy = buy & gate_replay.spy_gate_on(frame, spy, window)
                if len(buy) and bool(buy[-1]):
                    firing.append(strategy)
        except Exception as exc:  # one bad frame must not stop the screen
            logger.info("exploration universe: %s replica failed: %s", symbol, exc.__class__.__name__)
            continue
        if firing:
            out[symbol] = firing
    return out


# --------------------------------------------------------------------- state

def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def enabled() -> bool:
    return str(settings.exploration_universe).strip().lower() == "liquid_pit"


def latest_state(db) -> dict:
    from app.models import AuditLog

    row = (db.query(AuditLog).filter(AuditLog.event_type == EVENT_TYPE).order_by(AuditLog.id.desc()).first())
    return dict(row.payload or {}) if row is not None else {}


def lane_universe(db, now: Optional[datetime] = None, *, state: Optional[Mapping] = None
                  ) -> tuple[dict[str, dict], set[str]]:
    """(candidate symbol -> screen row, lane symbols kept for exits only).

    Candidates expire at their ``expires_at`` (a missed refresh closes the
    lane for entries, never for exits); with the setting on ``"core"`` no
    name is a candidate and every held lane name is exits-only.
    """
    now = _aware(now or datetime.now(UTC))
    state = latest_state(db) if state is None else dict(state)
    admitted: dict[str, dict] = {}
    exits_only = set(state.get("holding") or [])
    for row in state.get("candidates") or []:
        expires = _parse(row.get("expires_at"))
        if enabled() and expires is not None and expires > now:
            admitted[row["symbol"]] = row
        else:
            exits_only.add(row["symbol"])
    return admitted, exits_only - set(admitted)


def lane_book(db, *, state: Optional[Mapping] = None) -> set[str]:
    """Every symbol the lane admitted, holds or manages: its positions count against the lane slots, not the core caps."""
    from app.services.trading_universe import execution_symbols

    state = latest_state(db) if state is None else dict(state)
    book = ({row.get("symbol") for row in state.get("candidates") or [] if row.get("symbol")}
            | set(state.get("holding") or []) | set(state.get("managed") or []))
    return book - set(execution_symbols())  # a name since added to the core list is the core's


def lane_sectors(db=None, *, state: Optional[Mapping] = None) -> dict[str, str]:
    """Lane symbol -> sector group (``assign_sectors`` at the last refresh)."""
    if state is None:
        if db is None:
            _refresh_cache()
            return dict(_cache["sectors"])
        state = latest_state(db)
    return dict(state.get("sectors") or {})


def _refresh_cache() -> None:
    now = time.monotonic()
    if _cache["at"] is not None and now - _cache["at"] < _CACHE_SECONDS:
        return
    try:
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            state = latest_state(db)
            admitted, exits_only = lane_universe(db, state=state)
        _cache["symbols"] = frozenset(admitted) | frozenset(exits_only)
        _cache["sectors"] = dict(state.get("sectors") or {})
    except Exception as exc:  # keep the last known set; never break ingestion
        logger.info("exploration universe state unavailable: %s", exc.__class__.__name__)
    _cache["at"] = now


def lane_symbols() -> frozenset[str]:
    """Candidate and still-held lane symbols, cached for the intraday feed (empty while the lane is closed and nothing is held)."""
    _refresh_cache()
    return _cache["symbols"]


def clear_cache() -> None:
    _cache.update(at=None, symbols=frozenset(), sectors={})


def sector_for(symbol: str, sectors: Optional[Mapping[str, str]] = None) -> str:
    """The sector group of a symbol: the static map first, then the lane's assignment, else the symbol itself."""
    from app.services.trading_universe import SECTOR_GROUPS

    symbol = str(symbol).strip().upper()
    if symbol in SECTOR_GROUPS:
        return SECTOR_GROUPS[symbol]
    return (sectors or {}).get(symbol, symbol)


# ----------------------------------------------------------------- daily run

def _default_loader(now: datetime) -> Callable[[str], pd.DataFrame]:
    from app.services.point_in_time_universe import cache_first_loader

    # Bars older than a day are refreshed (the candidate check needs
    # yesterday's close); the rest of the cache is read as is.
    return cache_first_loader(max_age=timedelta(hours=20), now=now)


def _strategy_setups(db) -> tuple[list[str], dict[str, dict]]:
    """Broad-scope replayable strategies the cycle runs, with their replayable gate setups."""
    from app.models import Strategy
    from app.services import gate_replay
    from app.services.strategy_execution_policy import symbol_scope_for

    rows = db.query(Strategy).filter(
        Strategy.is_active.is_(True),
        Strategy.current_status.in_({"paper_trading_active", "paper_trading_candidate", "research"}),
    ).order_by(Strategy.id).all()
    types, setups = [], {}
    for row in rows:
        if row.strategy_type in setups or not gate_replay.replayable(row.strategy_type):
            continue
        try:
            scope = symbol_scope_for(row)
        except Exception:
            scope = None
        if scope is not None:
            continue  # a fixed scope (RSI(2), IBS, trimmed rules) never reaches lane names
        types.append(row.strategy_type)
        setups[row.strategy_type] = gate_replay.setup_from_parameters(getattr(row, "parameters", None))
    return types, setups


def run_exploration_universe(db, *, now: Optional[datetime] = None, loader: Optional[Callable] = None,
                             membership=None, strategies: Optional[Iterable[str]] = None,
                             setups: Optional[Mapping[str, Mapping]] = None,
                             import_prices: Optional[Callable] = None) -> dict:
    """Refresh the lane: screen today's members, pick the BUY candidates, activate them, record one audit row."""
    from app.services import news_discovery
    from app.services import point_in_time_universe as pit
    from app.services.audit import write_audit_log
    from app.services.trading_universe import RESEARCH_CORE_SYMBOLS, SECTOR_GROUPS, execution_symbols

    now = _aware(now or datetime.now(UTC))
    previous = latest_state(db)
    if not enabled() and not (previous.get("candidates") or previous.get("holding") or previous.get("managed")):
        return {"status": "skipped", "reason": "exploration_universe is \"core\"; the lane is closed", "paper_only": True}
    managed = set(previous.get("managed") or [])
    core = set(execution_symbols()) | set(RESEARCH_CORE_SYMBOLS)
    try:  # a news-found name stays news discovery's
        news_admitted, news_exits = news_discovery.news_universe(db, now)
        core |= set(news_admitted) | set(news_exits)
    except Exception:
        db.rollback()
    status, error = "complete", None
    screen: list[dict] = []
    candidates: list[dict] = []
    sectors: dict[str, str] = dict(previous.get("sectors") or {})
    if not enabled():
        status = "disabled"
    else:
        try:
            membership = membership or pit.load_membership(download=True, now=now)
            load = loader or _default_loader(now)
            members = sorted(membership.members_on(now.date()))
            frames: dict[str, pd.DataFrame] = {}
            for symbol in members:
                try:
                    frame = load(symbol)
                except Exception:
                    continue
                if frame is not None and len(frame):
                    frames[symbol] = frame
            max_price = news_discovery._max_price(db)
            screen = screen_names(members, frames, core=core, terciles=int(settings.exploration_universe_terciles),
                                  max_spread=float(settings.exploration_universe_max_spread), max_price=max_price,
                                  min_price=float(settings.exploration_universe_min_price))
            admitted = [row for row in screen if row["admitted"]]
            if strategies is None or setups is None:
                found_types, found_setups = _strategy_setups(db)
                strategies = found_types if strategies is None else list(strategies)
                setups = found_setups if setups is None else dict(setups)
            spy = None
            try:
                spy = load("SPY")
            except Exception:
                spy = None
            firing = buy_candidates({row["symbol"]: frames[row["symbol"]] for row in admitted}, strategies,
                                    spy=spy, setups=setups)
            etfs = {}
            for etf in SECTOR_ETFS:
                try:
                    etfs[etf] = load(etf)
                except Exception:
                    etfs[etf] = None
            cap = int(settings.exploration_universe_max_candidates)
            expiry = now + timedelta(hours=int(settings.exploration_universe_admit_hours))
            chosen = [row for row in sorted(admitted, key=lambda r: r.get("rank") or 10**9) if row["symbol"] in firing]
            for row in chosen[:cap]:
                candidates.append({**row, "strategies": firing[row["symbol"]], "admitted_at": now.isoformat(),
                                   "expires_at": expiry.isoformat()})
            for row in chosen[cap:]:
                row["reason"] = f"BUY candidate beyond the {cap}-name minute-bar cap"
            sectors = assign_sectors({row["symbol"]: frames[row["symbol"]] for row in admitted}, etfs, spy,
                                     known=SECTOR_GROUPS) | {k: v for k, v in sectors.items()
                                                             if k not in {row["symbol"] for row in admitted}}
        except Exception as exc:  # keep held names for exits; no new candidates this run
            status, error = "unavailable", f"{exc.__class__.__name__}: {str(exc)[:200]}"
            candidates = []
    importer = import_prices or news_discovery._import_daily
    for row in candidates:
        symbol = row["symbol"]
        try:
            if news_discovery._activate(db, symbol, row.get("name"), sector=ASSET_SECTOR):
                managed.add(symbol)
            importer(db, symbol)
            db.commit()
        except Exception as exc:
            db.rollback()
            row["import_error"] = exc.__class__.__name__
    lane = {row["symbol"] for row in candidates}
    managed -= core
    lapsed = (lane_book(db, state=previous) | managed) - lane - core
    holding = news_discovery._held_symbols(db, lapsed)
    for symbol in sorted(lapsed - holding):
        if symbol in managed:
            from app.models import Asset

            asset = db.query(Asset).filter(Asset.symbol == symbol).one_or_none()
            if asset is not None:
                asset.is_active = False
            managed.discard(symbol)
    skipped = [row for row in screen if not row["admitted"] and row.get("reason")
               and str(row["reason"]).startswith("one share")]
    keep = lane | holding | managed
    payload = {
        "observed_at": now.isoformat(), "status": status, "error": error, "setting": str(settings.exploration_universe),
        "terciles": int(settings.exploration_universe_terciles),
        "max_spread": float(settings.exploration_universe_max_spread),
        "members": len(screen), "admitted": sorted(row["symbol"] for row in screen if row["admitted"]),
        "candidates": sorted(candidates, key=lambda row: row["symbol"]),
        "newly_admitted": sorted(lane - {row.get("symbol") for row in previous.get("candidates") or []}),
        "holding": sorted(holding), "managed": sorted(managed),
        "skipped_one_share": [{key: row.get(key) for key in ("symbol", "price", "reason")} for row in skipped],
        "sectors": {symbol: sector for symbol, sector in sorted(sectors.items()) if symbol in keep
                    or symbol in {row["symbol"] for row in screen if row["admitted"]}},
        "rejected": {reason: count for reason, count in _reason_counts(screen).items()},
        "paper_only": True,
    }
    write_audit_log(db, event_type=EVENT_TYPE, entity_type="asset", action=ACTION, status=status,
                    message=(f"exploration universe: {len(payload['admitted'])} names admitted, "
                             f"{len(candidates)} BUY candidates in the cycle, {len(holding)} held for exits"),
                    payload=payload)
    db.commit()
    clear_cache()
    return payload


def _reason_counts(rows: Iterable[Mapping]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        if row.get("admitted"):
            continue
        reason = str(row.get("reason") or "unknown")
        key = reason.split(" (")[0].split(":")[0] if not reason.startswith("liquidity tercile") else "outside the admitted terciles"
        if reason.startswith("spread proxy"):
            key = "spread above the ceiling"
        elif reason.startswith("one share"):
            key = "one share above the learning floor"
        elif reason.startswith("price"):
            key = "price below the minimum"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))
