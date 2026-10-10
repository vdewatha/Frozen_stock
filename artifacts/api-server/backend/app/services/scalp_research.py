"""Independent 1-minute paper research; this module has no broker/order imports."""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, pstdev
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IntradayBar, ScalpResearchRun

ALLOWED_SYMBOLS = frozenset({"AAPL", "MSFT", "SPY", "QQQ"})
PROVIDER = "alpaca_iex"
FEED_CLASS = "iex"
TIMEFRAME = "1m"
ESTIMATED_SLIPPAGE_BPS = 5.0
ESTIMATED_FEE_BPS = 0.5
MAX_LOOKBACK_BARS = 390 * 5


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


def _strategy_metrics(closes: list[float], signal_fn, *, start: int = 50) -> dict:
    returns = []
    for index in range(start, len(closes) - 1):
        signal = signal_fn(closes, index)
        if signal == 0:
            continue
        gross = signal * (closes[index + 1] / closes[index] - 1.0)
        net = gross - (ESTIMATED_SLIPPAGE_BPS + ESTIMATED_FEE_BPS) / 10_000
        returns.append(net)
    return {
        "trades": len(returns),
        "wins": sum(value > 0 for value in returns),
        "win_rate": round(sum(value > 0 for value in returns) / len(returns), 4) if returns else None,
        "net_return": round(sum(returns), 6),
        "average_trade": round(mean(returns), 6) if returns else None,
        "volatility": round(pstdev(returns), 6) if len(returns) > 1 else None,
        "profitable_after_costs": bool(returns) and sum(returns) > 0,
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
    profitable = [
        name for name, result in results.items()
        if result["trades"] >= 10 and result["profitable_after_costs"]
    ]
    return {
        "status": "complete",
        "holdout_bars": len(holdout),
        "holdout_fraction": round(len(holdout) / len(closes), 4),
        "strategies": results,
        "paper_candidate": profitable[0] if len(profitable) == 1 else None,
        "candidate_reason": (
            "exactly one strategy was profitable after modeled costs on holdout"
            if len(profitable) == 1 else
            "no unique cost-positive holdout winner; remain research-only"
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
    row = db.scalar(select(ScalpResearchRun).where(ScalpResearchRun.run_id == run_id))
    if row is None:
        return {"status": "missing", "run_id": run_id}
    row.status, row.started_at = "running", _now()
    db.commit()
    try:
        metrics, snapshot = {}, {}
        for symbol in row.symbols:
            bars = _bars(db, symbol)
            snapshot[symbol] = {"bars": len(bars), "latest": bars[-1].exchange_timestamp.isoformat() if bars else None}
            if len(bars) < 60:
                metrics[symbol] = {"status": "blocked", "reason": "at least 60 trusted 1-minute bars are required"}
                continue
            closes = [float(bar.close) for bar in bars]
            metrics[symbol] = {
                "status": "complete",
                "strategies": _metrics(closes),
                "validation": _validation_metrics(closes),
            }
        row.data_snapshot, row.metrics = snapshot, metrics
        row.status = "completed" if any(item.get("status") == "complete" for item in metrics.values()) else "blocked"
    except Exception as exc:
        row.status, row.error = "failed", f"{exc.__class__.__name__}: scalp research failed"
    row.completed_at = _now()
    db.commit()
    return {"status": row.status, "run_id": row.run_id}


def project_scalp_research(row: ScalpResearchRun) -> dict:
    return {"run_id": row.run_id, "symbols": row.symbols, "timeframe": row.timeframe,
            "status": row.status, "eligible_for_trading": False, "data_snapshot": row.data_snapshot,
            "metrics": row.metrics, "assumptions": row.assumptions, "error": row.error,
            "created_at": row.created_at, "started_at": row.started_at, "completed_at": row.completed_at}
