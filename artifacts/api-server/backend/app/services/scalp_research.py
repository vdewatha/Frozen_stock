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
        .limit(390),
    ).all()))


def _strategy_metrics(closes: list[float], signal_fn) -> dict:
    returns = []
    for index in range(50, len(closes) - 1):
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
    }


def _ema(values: list[float], period: int, end: int) -> float:
    alpha = 2 / (period + 1)
    value = values[0]
    for item in values[1:end + 1]:
        value = alpha * item + (1 - alpha) * value
    return value


def _metrics(closes: list[float]) -> dict:
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

    return {
        "ema_momentum": _strategy_metrics(closes, momentum),
        "intraday_mean_reversion": _strategy_metrics(closes, mean_reversion),
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
            metrics[symbol] = {"status": "complete", "strategies": _metrics([float(bar.close) for bar in bars])}
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
