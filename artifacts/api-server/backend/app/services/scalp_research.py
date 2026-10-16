"""Independent 1-minute paper research; this module has no broker/order imports."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from statistics import mean, pstdev
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import IntradayBar, ScalpResearchRun
from app.services.exchange_sessions import is_nyse_session, session_bounds
from app.services.research_simulator import SimulationConfig, simulate

ALLOWED_SYMBOLS = frozenset({"AAPL", "MSFT", "SPY", "QQQ"})
PROVIDER = "alpaca_iex"
FEED_CLASS = "iex"
TIMEFRAME = "1m"
ESTIMATED_SLIPPAGE_BPS = 5.0
ESTIMATED_FEE_BPS = 0.5
MAX_LOOKBACK_BARS = 390 * 5
STALE_QUEUED_AFTER = timedelta(minutes=30)
# The Celery task has a 55 second hard limit. A ten-minute bound is long
# enough for a slow database transaction, but short enough that one crashed
# worker cannot permanently block the scheduled research lane.
STALE_RUNNING_AFTER = timedelta(minutes=10)
MAX_INTRADAY_GAP = timedelta(minutes=5)
SCALP_LATE_DATA_ALLOWANCE = timedelta(minutes=2)
FEED_VERIFICATION_MAX_AGE = timedelta(minutes=5)
NY = ZoneInfo("America/New_York")


def _now():
    return datetime.now(timezone.utc)


def _bars(db: Session, symbol: str):
    return list(reversed(db.scalars(
        select(IntradayBar)
        .where(
            IntradayBar.symbol == symbol,
            IntradayBar.timeframe == TIMEFRAME,
            IntradayBar.provider == PROVIDER,
            IntradayBar.feed_class == FEED_CLASS,
        )
        .order_by(IntradayBar.opened_at.desc())
        .limit(MAX_LOOKBACK_BARS),
    ).all()))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _expected_research_session(observed: datetime):
    """Return the NYSE session that should be the newest research session."""
    local = observed.astimezone(NY)
    candidate = local.date()
    bounds = session_bounds(candidate)
    if bounds and local >= bounds[0].astimezone(NY):
        return candidate
    for _ in range(10):
        candidate -= timedelta(days=1)
        if session_bounds(candidate):
            return candidate
    return None


def _session_quality(bars: list[IntradayBar], *, now: datetime) -> dict:
    """Reject stale or gapped bars before they can produce scalp labels."""
    if not bars:
        return {"status": "blocked", "reason": "no trusted intraday bars", "missing_intervals": []}
    if any(
        bar.provider != PROVIDER or bar.feed_class != FEED_CLASS or bar.timeframe != TIMEFRAME
        for bar in bars
    ):
        return {
            "status": "blocked",
            "reason": "intraday provenance is not authenticated Alpaca IEX 1m data",
            "missing_intervals": [],
        }
    timestamps = [_aware(bar.exchange_timestamp) for bar in bars]
    latest = timestamps[-1]
    latest_day = latest.astimezone(NY).date()
    bounds = session_bounds(latest_day)
    expected_day = _expected_research_session(now)
    if not bounds or latest < bounds[0] or latest >= bounds[1]:
        return {"status": "blocked", "reason": "latest observation is outside an NYSE regular session", "missing_intervals": []}
    if expected_day != latest_day:
        return {"status": "blocked", "reason": "latest trusted session is stale", "missing_intervals": []}
    observed = now.astimezone(timezone.utc)
    expected_end = bounds[1]
    if expected_day == observed.astimezone(NY).date() and observed < bounds[1] + SCALP_LATE_DATA_ALLOWANCE:
        expected_end = min(
            bounds[1],
            (observed - SCALP_LATE_DATA_ALLOWANCE).replace(second=0, microsecond=0) + timedelta(minutes=1),
        )
    expected = []
    cursor = bounds[0]
    while cursor < expected_end:
        expected.append(cursor)
        cursor += timedelta(minutes=1)
    present = {stamp for stamp in timestamps if bounds[0] <= stamp < bounds[1]}
    missing = [stamp.isoformat() for stamp in expected if stamp not in present]
    if missing:
        return {
            "status": "blocked",
            "reason": "trusted intraday session is incomplete; missing minutes remain unknown",
            "missing_intervals": missing[:120],
            "missing_count": len(missing),
            "session_day": latest_day.isoformat(),
            "latest": latest.isoformat(),
        }
    # The evaluation window may include several regular sessions. Require
    # complete coverage for every session represented by the lookback, not
    # just the newest one, so an older hole cannot improve a backtest result.
    first_day = timestamps[0].astimezone(NY).date()
    session_coverage = []
    cursor_day = first_day
    while cursor_day <= latest_day:
        day_bounds = session_bounds(cursor_day)
        if day_bounds and is_nyse_session(cursor_day):
            day_present = {
                stamp for stamp in timestamps
                if day_bounds[0] <= stamp < day_bounds[1]
            }
            day_expected_end = expected_end if cursor_day == latest_day else day_bounds[1]
            day_expected = int((day_expected_end - day_bounds[0]).total_seconds() // 60)
            missing_count = max(0, day_expected - len(day_present))
            session_coverage.append({
                "session_day": cursor_day.isoformat(),
                "expected_bars": day_expected,
                "observed_bars": len(day_present),
                "missing_count": missing_count,
            })
        cursor_day += timedelta(days=1)
    incomplete_sessions = [item for item in session_coverage if item["missing_count"]]
    if incomplete_sessions:
        return {
            "status": "blocked",
            "reason": "trusted intraday lookback contains an incomplete regular session",
            "missing_intervals": [],
            "incomplete_sessions": incomplete_sessions,
            "session_day": latest_day.isoformat(),
            "latest": latest.isoformat(),
        }
    latest_session_bars = [
        bar for bar in bars
        if bounds[0] <= _aware(bar.exchange_timestamp) < bounds[1]
    ]
    verified_at = [
        _aware(bar.last_verified_at)
        for bar in latest_session_bars
        if bar.last_verified_at is not None
    ]
    if len(verified_at) != len(latest_session_bars):
        return {
            "status": "blocked",
            "reason": "latest trusted session lacks authenticated feed verification",
            "missing_intervals": [],
            "session_day": latest_day.isoformat(),
            "latest": latest.isoformat(),
        }
    latest_verification = max(verified_at)
    verification_age = now.astimezone(timezone.utc) - latest_verification
    if verification_age < timedelta(0) or verification_age > FEED_VERIFICATION_MAX_AGE:
        return {
            "status": "blocked",
            "reason": "authenticated feed verification is stale",
            "missing_intervals": [],
            "verification_age_seconds": int(verification_age.total_seconds()),
            "session_day": latest_day.isoformat(),
            "latest": latest.isoformat(),
        }
    return {
        "status": "ready",
        "reason": None,
        "missing_intervals": [],
        "missing_count": 0,
        "session_day": latest_day.isoformat(),
        "latest": latest.isoformat(),
        "expected_bars": len(expected),
        "session_coverage": session_coverage,
        "authenticated_verification_at": latest_verification.isoformat(),
    }


def _bar_lineage(bars: list[IntradayBar], *, now: datetime | None = None) -> dict:
    """Describe and fingerprint the exact trusted bars used by a run.

    Overnight/weekend gaps are expected. A large gap inside one regular NYSE
    session is not silently treated as continuous history, because that can
    make a scalp backtest look better than the executable feed.
    """
    canonical = []
    intraday_gaps = []
    for bar in bars:
        opened_at = _aware(bar.opened_at)
        exchange_at = _aware(bar.exchange_timestamp)
        canonical.append({
            "opened_at": opened_at.isoformat(),
            "exchange_timestamp": exchange_at.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": int(bar.volume),
            "provider": bar.provider,
            "feed_class": bar.feed_class,
        })

    for previous, current in zip(bars, bars[1:]):
        previous_at = _aware(previous.exchange_timestamp)
        current_at = _aware(current.exchange_timestamp)
        bounds = session_bounds(previous_at.astimezone(NY).date())
        if not bounds:
            continue
        start, end = bounds
        if start <= previous_at <= end and start <= current_at <= end:
            gap = current_at - previous_at
            if gap > MAX_INTRADAY_GAP:
                intraday_gaps.append({
                    "from": previous_at.isoformat(),
                    "to": current_at.isoformat(),
                    "gap_seconds": int(gap.total_seconds()),
                })

    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    data_quality = _session_quality(bars, now=(now or _now()))
    return {
        "bar_count": len(bars),
        "observed_start": canonical[0]["exchange_timestamp"] if canonical else None,
        "observed_end": canonical[-1]["exchange_timestamp"] if canonical else None,
        "intraday_gap_count": len(intraday_gaps),
        "intraday_gaps": intraday_gaps[:20],
        "bars_sha256": hashlib.sha256(encoded).hexdigest(),
        "provider": PROVIDER,
        "feed_class": FEED_CLASS,
        "timeframe": TIMEFRAME,
        "synthetic": False,
        "execution_eligible": False,
        "data_quality": data_quality,
    }


def _strategy_metrics(closes: list[float], signal_fn, *, start: int = 50) -> dict:
    returns = []
    gross_returns = []
    for index in range(start, len(closes) - 1):
        signal = signal_fn(closes, index)
        if signal == 0:
            continue
        gross = signal * (closes[index + 1] / closes[index] - 1.0)
        net = gross - (ESTIMATED_SLIPPAGE_BPS + ESTIMATED_FEE_BPS) / 10_000
        gross_returns.append(gross)
        returns.append(net)
    gross_total = sum(gross_returns)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    sensitivity = {
        f"{bps:g}bps": round(gross_total - len(gross_returns) * bps / 10_000, 6)
        for bps in (5.5, 10.0, 20.0)
    }
    modeled_positive = bool(returns) and sum(returns) > 0
    return {
        "trades": len(returns),
        "wins": sum(value > 0 for value in returns),
        "win_rate": round(sum(value > 0 for value in returns) / len(returns), 4) if returns else None,
        "net_return": round(sum(returns), 6),
        "average_trade": round(mean(returns), 6) if returns else None,
        "volatility": round(pstdev(returns), 6) if len(returns) > 1 else None,
        "max_drawdown": round(max_drawdown, 6),
        # This is deliberately not called observed profitability: the
        # simulator has no quote-level spread, latency, or partial-fill data.
        "execution_model": "next_close_single_bar_with_modeled_costs",
        "modeled_cost_sensitivity": sensitivity,
        "profitable_under_modeled_costs": modeled_positive,
        # Keep the legacy field for existing consumers while making the
        # stronger semantic field available to new promotion logic.
        "profitable_after_costs": modeled_positive,
    }


def _ema(values: list[float], period: int, end: int) -> float:
    alpha = 2 / (period + 1)
    value = values[0]
    for item in values[1:end + 1]:
        value = alpha * item + (1 - alpha) * value
    return value


def _rsi(values: list[float], end: int, period: int = 14) -> float:
    changes = [values[index] - values[index - 1] for index in range(1, end + 1)]
    window = changes[-period:]
    if len(window) < period:
        return 50.0
    gains = sum(change for change in window if change > 0) / period
    losses = sum(-change for change in window if change < 0) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def _strategy_signals():
    """Small, independent hypotheses; none of these functions can submit orders."""

    def momentum(values, index):
        return 1 if _ema(values, 20, index) > _ema(values, 50, index) else -1

    def mean_reversion(values, index):
        window = values[index - 20:index]
        band = pstdev(window) if len(window) > 1 else 0
        if band and values[index] < mean(window) - band:
            return 1
        if band and values[index] > mean(window) + band:
            return -1
        return 0

    def rsi_reversion(values, index):
        value = _rsi(values, index)
        if value <= 30:
            return 1
        if value >= 70:
            return -1
        return 0

    def range_breakout(values, index):
        window = values[index - 20:index]
        if not window:
            return 0
        if values[index] > max(window):
            return 1
        if values[index] < min(window):
            return -1
        return 0

    def short_term_reversal(values, index):
        window = values[index - 5:index]
        if len(window) < 5 or window[0] == 0:
            return 0
        move = values[index] / window[0] - 1.0
        if move <= -0.0025:
            return 1
        if move >= 0.0025:
            return -1
        return 0

    return {
        "ema_momentum": momentum,
        "intraday_mean_reversion": mean_reversion,
        "rsi_reversion": rsi_reversion,
        "range_breakout": range_breakout,
        "short_term_reversal": short_term_reversal,
    }


def _metrics(closes: list[float]) -> dict:
    return {name: _strategy_metrics(closes, signal) for name, signal in _strategy_signals().items()}


def _long_only_shadow_simulation(bars: list[IntradayBar], signal_fn) -> dict:
    """Run a conservative execution simulation without broker authority.

    Scalp hypotheses can emit short signals, while the shared simulator is
    intentionally long/flat only. The adapter explicitly omits short signals
    instead of treating them as sells, so its result is a conservative shadow
    comparison rather than a claim of executable short performance.
    """
    closes = [float(bar.close) for bar in bars]
    rows = [
        {
            "timestamp": _aware(bar.exchange_timestamp),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "tradable": True,
        }
        for bar in bars
    ]
    decisions = [
        1 if index >= 50 and signal_fn(closes, index) == 1 else 0
        for index in range(len(closes))
    ]
    result = simulate(rows, decisions, SimulationConfig())
    return {
        "mode": result["mode"],
        "execution_model": "next_open_volume_limited_long_flat_shadow",
        "eligible_for_trading": False,
        "profitability_proven": False,
        "short_signals_omitted": True,
        "long_entry_signals": sum(decisions),
        "fill_count": result["fill_count"],
        "completed_trades": result["completed_trades"],
        "total_return": str(result["total_return"]),
        "max_drawdown": str(result["max_drawdown"]),
        "total_costs": str(result["total_costs"]),
        "partial_fill_count": sum(1 for fill in result["fills"] if fill["partial"]),
    }


def _validation_metrics(closes: list[float]) -> dict:
    """Evaluate untouched trailing data so in-sample winners cannot self-promote."""
    if len(closes) < 120:
        return {"status": "insufficient_data", "holdout_bars": 0, "strategies": {}, "paper_candidate": None}
    split = max(60, int(len(closes) * 0.7))
    holdout = closes[split:]
    results = {
        name: _strategy_metrics(holdout, signal)
        for name, signal in _strategy_signals().items()
    }
    # Evaluate several later, non-overlapping windows as a simple
    # walk-forward consistency check. A single favorable holdout is not
    # enough evidence for a scalp strategy whose regime can change quickly.
    fold_size = max(60, len(closes) // 5)
    walk_forward = []
    for start in range(fold_size, len(closes), fold_size):
        fold = closes[start:start + fold_size]
        if len(fold) < 60:
            break
        walk_forward.append({
            "start_index": start,
            "bars": len(fold),
            "strategies": {
                name: _strategy_metrics(fold, signal)
                for name, signal in _strategy_signals().items()
            },
        })
        if len(walk_forward) == 3:
            break
    profitable = [
        name for name, result in results.items()
        if result["trades"] >= 10
        and result["profitable_under_modeled_costs"]
        and walk_forward
        and all(
            fold["strategies"][name]["trades"] >= 10
            and fold["strategies"][name]["profitable_under_modeled_costs"]
            for fold in walk_forward
            for name in [name]
        )
    ]
    return {
        "status": "complete",
        "holdout_bars": len(holdout),
        "holdout_fraction": round(len(holdout) / len(closes), 4),
        "strategies": results,
        "walk_forward": walk_forward,
        "paper_candidate": profitable[0] if len(profitable) == 1 else None,
        "candidate_reason": (
            "exactly one strategy was positive under modeled costs on holdout"
            if len(profitable) == 1 else
            "no unique modeled-cost-positive holdout winner; remain research-only"
        ),
    }


def create_scalp_research_run(db: Session, *, symbols: list[str], actor: str) -> ScalpResearchRun:
    normalized = sorted({symbol.strip().upper() for symbol in symbols})
    if not normalized or not set(normalized) <= ALLOWED_SYMBOLS:
        raise ValueError("Scalp research supports only AAPL, MSFT, SPY, and QQQ")
    row = ScalpResearchRun(
        run_id=str(uuid4()), requested_by=actor or "unknown", symbols=normalized,
        timeframe=TIMEFRAME, data_snapshot={}, metrics={}, assumptions={
            "provider": PROVIDER, "feed_class": FEED_CLASS,
            "estimated_slippage_bps": ESTIMATED_SLIPPAGE_BPS,
            "estimated_fee_bps": ESTIMATED_FEE_BPS,
            "max_lookback_bars": MAX_LOOKBACK_BARS,
            "paper_only": True, "eligible_for_trading": False,
        },
    )
    db.add(row)
    db.flush()
    return row


def run_scalp_research(db: Session, run_id: str) -> dict:
    claimed_at = _now()
    claimed = db.execute(
        update(ScalpResearchRun)
        .where(ScalpResearchRun.run_id == run_id, ScalpResearchRun.status == "queued")
        .values(status="running", started_at=claimed_at)
    ).rowcount
    db.commit()
    row = db.scalar(select(ScalpResearchRun).where(ScalpResearchRun.run_id == run_id))
    if row is None:
        return {"status": "missing", "run_id": run_id}
    if not claimed:
        return {"status": row.status, "run_id": run_id}
    try:
        metrics, snapshot = {}, {}
        for symbol in row.symbols:
            bars = _bars(db, symbol)
            lineage = _bar_lineage(bars, now=claimed_at)
            snapshot[symbol] = lineage
            if len(bars) < 60:
                metrics[symbol] = {"status": "blocked", "reason": "at least 60 trusted 1-minute bars are required"}
                continue
            if lineage["data_quality"]["status"] != "ready" or lineage["intraday_gap_count"]:
                metrics[symbol] = {
                    "status": "blocked",
                    "reason": lineage["data_quality"]["reason"] or "trusted intraday history contains a material in-session gap",
                    "lineage": lineage,
                }
                continue
            closes = [float(bar.close) for bar in bars]
            validation = _validation_metrics(closes)
            strategies = _strategy_signals()
            metrics[symbol] = {
                "status": "complete" if validation["status"] == "complete" else "blocked",
                "strategies": _metrics(closes),
                "validation": validation,
                "shadow_simulations": {
                    name: _long_only_shadow_simulation(bars, signal)
                    for name, signal in strategies.items()
                },
            }
        row.data_snapshot, row.metrics = snapshot, metrics
        row.status = "completed" if any(item.get("status") == "complete" for item in metrics.values()) else "blocked"
    except Exception as exc:
        row.status, row.error = "failed", f"{exc.__class__.__name__}: scalp research failed"
    row.completed_at = _now()
    db.commit()
    return {"status": row.status, "run_id": row.run_id}


def expire_stale_scalp_research_runs(db: Session) -> dict:
    """Close orphaned queue/worker records without replaying unknown work."""
    now = _now()
    cutoff = now - STALE_QUEUED_AFTER
    running_cutoff = now - STALE_RUNNING_AFTER
    rows = db.scalars(
        select(ScalpResearchRun).where(
            (
                (ScalpResearchRun.status == "queued") & (ScalpResearchRun.created_at < cutoff)
            )
            | (
                (ScalpResearchRun.status == "running")
                & (ScalpResearchRun.started_at.is_not(None))
                & (ScalpResearchRun.started_at < running_cutoff)
            ),
        )
    ).all()
    for row in rows:
        row.status = "blocked"
        row.error = (
            "queued research task expired before a worker claimed it"
            if row.started_at is None
            else "running research task exceeded its worker liveness bound"
        )
        row.completed_at = now
    db.commit()
    return {
        "status": "complete",
        "expired_run_ids": [row.run_id for row in rows],
        "expired_running_count": sum(1 for row in rows if row.started_at is not None),
        "paper_only": True,
    }


def project_scalp_research(row: ScalpResearchRun) -> dict:
    return {"run_id": row.run_id, "symbols": row.symbols, "timeframe": row.timeframe,
            "status": row.status, "eligible_for_trading": False, "data_snapshot": row.data_snapshot,
            "metrics": row.metrics, "assumptions": row.assumptions, "error": row.error,
            "created_at": row.created_at, "started_at": row.started_at, "completed_at": row.completed_at}
