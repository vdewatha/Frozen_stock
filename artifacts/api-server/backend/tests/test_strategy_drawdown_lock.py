"""Per-strategy drawdown lock (entry protection #6) in paper and the backtester."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.models import Strategy
from app.services.backtest_statistics import realized_drawdown
from app.services.backtester import BacktestConfig, run_backtest
from app.services.entry_protections import (
    ClosedTrade,
    EntryProtections,
    ProtectionConfig,
    load_entry_protections,
    protection_config,
    strategy_drawdown_state,
    strategy_lock_end,
)
from app.services.stock_paper_ledger import _strategy_drawdown_locks
from tests.test_entry_protections import _seed_account, _seed_order, sqlite_db  # noqa: F401

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 28, 16, 0, tzinfo=UTC)  # Wed 12:00 ET
ALLOCATED = Decimal("10000")  # 5% lock -> 500
CFG = ProtectionConfig()


def _trade(pnl, hours_ago, strategy="macd_momentum", symbol="SPY"):
    closed = NOW - timedelta(hours=hours_ago)
    return ClosedTrade(symbol, strategy, closed - timedelta(hours=1), closed, Decimal(str(pnl)))


def test_realized_drawdown_is_peak_to_trough_of_cumulative_pnl():
    assert realized_drawdown([100, -300, -300, 200]) == 600
    assert realized_drawdown([Decimal("-1"), Decimal("2")]) == Decimal("1")
    assert realized_drawdown([5, 5]) == 0
    assert realized_drawdown([]) == 0


def test_lock_end_counts_nyse_sessions_after_the_close_session():
    # Wed close: rest of Wed + Thu, Fri, Mon -> unlocked from Tue 00:00 ET.
    assert strategy_lock_end(NOW, 3) == datetime(2026, 11, 3, tzinfo=ET).astimezone(UTC)
    # Thanksgiving (Thu 2026-11-26) is skipped: Wed close + 1 session (Fri).
    wednesday = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)
    assert strategy_lock_end(wednesday, 1) == datetime(2026, 11, 30, tzinfo=ET).astimezone(UTC)


def test_drawdown_over_threshold_locks_that_strategy_only():
    trades = [_trade(200, 30), _trade(-350, 20), _trade(-300, 2), _trade(-900, 3, strategy="rsi_mean_reversion")]
    protections = EntryProtections(now=NOW, config=CFG, trades=trades, allocated_notional=ALLOCATED)
    block = protections.check("QQQ", "macd_momentum")
    assert block["protection"] == "strategy_drawdown"
    assert block["status"] == "skipped"
    assert block["protection_until"] == datetime(2026, 11, 3, tzinfo=ET).astimezone(UTC).isoformat()
    assert protections.check("QQQ", "ensemble") is None
    locks = protections.summary()["strategy_locks"]
    assert [row["strategy"] for row in locks] == ["macd_momentum", "rsi_mean_reversion"]
    assert locks[0]["drawdown"] == "650.00" and locks[0]["limit"] == "500.00"


def test_below_threshold_expired_or_outside_window_does_not_lock():
    shallow = [_trade(-200, 2), _trade(-250, 1)]
    assert strategy_drawdown_state(shallow, "macd_momentum", NOW, CFG, ALLOCATED) is None
    deep = [_trade(-300, 2), _trade(-300, 1)]
    later = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    assert strategy_drawdown_state(deep, "macd_momentum", later, CFG, ALLOCATED) is None
    window = ProtectionConfig(strategy_drawdown_trades=2)
    recovered = [_trade(-900, 5), _trade(100, 2), _trade(100, 1)]
    assert strategy_drawdown_state(recovered, "macd_momentum", NOW, window, ALLOCATED) is None
    assert strategy_drawdown_state(deep, "macd_momentum", NOW, CFG, None) is None


def test_lock_is_configurable_and_disabled_by_zero():
    config = protection_config({"strategy_drawdown_lock_pct": "0", "strategy_drawdown_trades": 4})
    assert config.strategy_drawdown_lock_pct == 0 and config.strategy_drawdown_trades == 4
    deep = [_trade(-300, 2), _trade(-300, 1)]
    assert strategy_drawdown_state(deep, "macd_momentum", NOW, config, ALLOCATED) is None
    assert protection_config({}).strategy_drawdown_lock_sessions == 3


def test_loader_allocates_from_equity_and_status_lists_locks(sqlite_db):
    db = sqlite_db
    strategy = Strategy(name="MACD", strategy_type="macd_momentum", parameters={}, is_active=True,
                        current_status="paper_trading_active")
    db.add(strategy)
    db.flush()
    account = _seed_account(db, equity="100", last_equity="100")  # allocation 10 -> lock at 0.50
    now = datetime.now(UTC)
    for index in range(2):  # two round trips, each losing 3.00
        at = now - timedelta(hours=3 - index)
        _seed_order(db, account, id=40 + index * 2, side="buy", strategy_id=strategy.id, at=at)
        _seed_order(db, account, id=41 + index * 2, side="sell", strategy_id=strategy.id, at=at + timedelta(minutes=5))
    db.commit()
    protections = load_entry_protections(db, account, now=now, rules={}, type_by_id={strategy.id: "macd_momentum"})
    assert protections.allocated_notional == Decimal("10.0")
    assert [row["strategy"] for row in protections.strategy_locks()] == ["macd_momentum"]
    status = _strategy_drawdown_locks(db, account)
    assert status["status"] == "ok" and status["entries_only"] is True
    assert status["locks"][0]["drawdown"] == "6.00"


def _losing_frame(rows=400, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(-0.0005, 0.02, rows)))
    dates = pd.bdate_range("2022-01-03", periods=rows)
    return pd.DataFrame({"date": [d.date().isoformat() for d in dates], "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": np.full(rows, 2_000_000.0)})


def test_backtester_lock_is_off_by_default_and_withholds_entries_when_enabled():
    prices = _losing_frame()
    baseline = run_backtest("TEST", "moving_average_crossover", prices)
    assert "strategy_drawdown_lock" not in baseline
    locked = run_backtest("TEST", "moving_average_crossover", prices,
                          BacktestConfig(strategy_drawdown_lock_pct=0.0001, strategy_drawdown_lock_sessions=5))
    summary = locked["strategy_drawdown_lock"]
    assert summary["locked_entries"] > 0
    assert locked["number_of_trades"] < baseline["number_of_trades"]
    # Trades up to the first losing close are identical: the lock only withholds later entries.
    first_loss = next(i for i, row in enumerate(baseline["trades"]) if row["profit_loss"] < 0)
    assert locked["trades"][: first_loss + 1] == baseline["trades"][: first_loss + 1]
