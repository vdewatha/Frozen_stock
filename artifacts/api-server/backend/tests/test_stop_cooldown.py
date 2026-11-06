"""Stop-out cooldown: no re-entry on a symbol for N sessions after it stops out."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.services.entry_protections import (
    EntryProtections,
    ExitEvent,
    ProtectionConfig,
    protection_config,
    stop_cooldown_block,
)

ET = ZoneInfo("America/New_York")


def _stop(symbol="AAPL", at=datetime(2026, 11, 2, 11, 5, tzinfo=ET), stop_out=True):
    return ExitEvent(symbol=symbol, at=at, order_key=f"{symbol}-{at.isoformat()}", stop_out=stop_out)


def test_blocks_the_stop_session_and_the_next_nine_sessions():
    config = ProtectionConfig()
    assert config.stop_cooldown_sessions == 10
    events = [_stop()]
    # Mon 2026-11-02 stop; sessions 11-03 ... 11-13 are the next nine.
    for day in (2, 3, 6, 9, 11, 13):
        block = stop_cooldown_block(events, "AAPL", datetime(2026, 11, day, 15, 0, tzinfo=ET), config)
        assert block is not None and "stopped out on 2026-11-02" in block["reason"], day
    assert stop_cooldown_block(events, "AAPL", datetime(2026, 11, 16, 9, 45, tzinfo=ET), config) is None
    assert stop_cooldown_block(events, "MSFT", datetime(2026, 11, 3, 10, 0, tzinfo=ET), config) is None
    assert stop_cooldown_block(events, "aapl", datetime(2026, 11, 3, 10, 0, tzinfo=ET), config) is not None


def test_only_stop_outs_count_and_zero_disables():
    config = ProtectionConfig()
    now = datetime(2026, 11, 3, 10, 0, tzinfo=ET)
    assert stop_cooldown_block([_stop(stop_out=False)], "AAPL", now, config) is None
    # A later stop restarts the window.
    events = [_stop(), _stop(at=datetime(2026, 11, 13, 15, 0, tzinfo=ET))]
    assert stop_cooldown_block(events, "AAPL", datetime(2026, 11, 16, 10, 0, tzinfo=ET), config) is not None
    off = protection_config({"stop_cooldown_sessions": 0})
    assert off.stop_cooldown_sessions == 0 and stop_cooldown_block(events, "AAPL", now, off) is None
    assert protection_config({"stop_cooldown_sessions": 5}).stop_cooldown_sessions == 5


def test_evaluator_names_the_block():
    protections = EntryProtections(now=datetime(2026, 11, 4, 11, 0, tzinfo=ET).astimezone(ET),
                                   config=ProtectionConfig(exit_cooldown_minutes=0), events=[_stop()])
    block = protections.check("AAPL", "macd_momentum")
    assert block is not None and "stop_cooldown" in block["reason"]
    assert protections.check("MSFT", "macd_momentum") is None


def test_backtest_decision_rule():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import run_slot_allocation_backtest as sb
    finally:
        sys.path.remove(str(scripts))

    def row(exp, dd, n):
        return {"trades": n, "expectancy_bps": exp, "max_drawdown": dd}

    base = {w: row(10.0, 0.20, 1000) for w in sb.WINDOWS}
    results = {
        "baseline": base,
        "better": {w: row(14.0, 0.21, 600) for w in sb.WINDOWS},
        "too_few": {w: row(30.0, 0.10, 400) for w in sb.WINDOWS},   # < 50% of trades
        "riskier": {w: row(40.0, 0.30, 900) for w in sb.WINDOWS},   # drawdown > 1.1x
        "mixed": {"all": row(20.0, 0.1, 900), "2015+": row(9.0, 0.1, 900), "2020+": row(20.0, 0.1, 900)},
    }
    assert sb.decide(results) == {"eligible": {"better": 4.0}, "adopted": "better"}


def test_allocation_cooldown_skips_reentry_after_a_stop():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import run_slot_allocation_backtest as sb
    finally:
        sys.path.remove(str(scripts))
    dates = pd.bdate_range("2026-01-05", periods=30).strftime("%Y-%m-%d")
    frames = {"AAPL": pd.DataFrame({"date": dates, "close": 100.0}), "MSFT": pd.DataFrame({"date": dates, "close": 100.0})}

    def trade(symbol, entry, exit_, barrier, rank):
        return {"symbol": symbol, "strategy": "macd_momentum", "entry_idx": entry, "exit_idx": exit_,
                "entry_date": dates[entry], "exit_date": dates[exit_], "barrier": barrier, "confidence": 0.6,
                "live_rank": rank, "net_return": 0.0, "decision_date": dates[entry - 1]}

    rows = pd.DataFrame([trade("AAPL", 1, 3, "stop_loss", 0), trade("AAPL", 5, 8, "vertical", 0),
                         trade("MSFT", 5, 8, "vertical", 1), trade("AAPL", 14, 16, "vertical", 0)])
    base = sb.allocate(rows, frames, {})
    assert len(base) == 4
    cooled = sb.allocate(rows, frames, {"stop_cooldown": 10})
    assert list(zip(cooled["symbol"], cooled["entry_idx"])) == [("AAPL", 1), ("MSFT", 5), ("AAPL", 14)]
