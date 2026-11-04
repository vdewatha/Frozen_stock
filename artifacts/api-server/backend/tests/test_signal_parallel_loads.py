"""Parallel read-only signal input loads: same results, real concurrency."""
import threading
import time
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.services import stock_paper_signal_batch as batch
from app.services.stock_paper_ledger import StockPaperError

SYMBOLS = ["AAPL", "MSFT", "SPY", "QQQ", "NVDA", "BAD"]


@pytest.fixture
def file_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'loads.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def fake_load(session, symbol):
    time.sleep(0.2)
    if symbol == "BAD":
        raise StockPaperError("Intraday feed has no eligible completed observation.")
    return (f"frame-{symbol}", "yfinance", {"close": 1.0, "thread": threading.current_thread().name,
                                             "session": id(session)})


def test_parallel_loads_match_and_overlap(file_db, monkeypatch):
    monkeypatch.setattr(settings, "paper_signal_load_workers", 6)
    with patch.object(batch.ledger, "_load_signal_inputs", side_effect=fake_load):
        started = time.perf_counter()
        inputs = batch._prefetch_inputs(file_db, SYMBOLS)
        elapsed = time.perf_counter() - started
    assert list(inputs) == SYMBOLS
    assert inputs["SPY"][0] == "frame-SPY"
    assert isinstance(inputs["BAD"], StockPaperError)  # a per-symbol failure stays per symbol
    assert elapsed < 0.6  # six 0.2 s loads ran concurrently, not in 1.2 s
    assert all(inputs[s][2]["thread"].startswith("signal-load") for s in SYMBOLS[:-1])
    assert len({inputs[s][2]["session"] for s in SYMBOLS[:-1]} - {id(file_db)}) >= 2  # isolated sessions


def test_one_worker_or_in_memory_db_keeps_the_sequential_path(monkeypatch):
    engine = create_engine("sqlite://")
    with Session(engine) as memory_db, patch.object(batch.ledger, "_load_signal_inputs", side_effect=fake_load):
        monkeypatch.setattr(settings, "paper_signal_load_workers", 6)
        inputs = batch._prefetch_inputs(memory_db, ["AAPL", "BAD"])
        assert inputs["AAPL"][2]["session"] == id(memory_db)
        assert isinstance(inputs["BAD"], StockPaperError)
    engine.dispose()


def test_generate_cycle_signals_reports_a_load_timing(file_db, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "paper_signal_load_workers", 4)
    strategy = SimpleNamespace(strategy_type="rsi2_pullback", current_status="paper_trading_candidate")
    with patch.object(batch.ledger, "_load_signal_inputs", side_effect=fake_load), \
         patch.object(batch.ledger, "_resolve_signal_strategy", side_effect=StockPaperError("no strategy row")):
        outcomes, stats = batch.generate_cycle_signals(file_db, ["AAPL", "BAD"], [strategy])
    assert "load" in stats["timing_ms"]
    assert all(isinstance(value, StockPaperError) for value in outcomes.values())


def test_signal_rows_and_events_are_inserted_in_batches(file_db):
    from collections import Counter
    from datetime import datetime, timezone
    from decimal import Decimal

    from sqlalchemy import event

    from app.models import Strategy, StrategySignal
    from app.models.stock_paper import StockPaperLedgerEvent

    strategy = Strategy(name="RSI(2) Pullback", strategy_type="rsi2_pullback", parameters={}, is_active=True)
    file_db.add(strategy)
    file_db.commit()
    now = datetime.now(timezone.utc)
    rows = [StrategySignal(strategy_id=strategy.id, symbol=f"S{i}", signal_time=now, action="HOLD",
                           probability_up=Decimal("0.5"), probability_down=Decimal("0.5"), confidence=Decimal("0.5"),
                           reason="r", features={"i": i}) for i in range(250)]
    statements = Counter()
    engine = file_db.get_bind()

    def count(conn, cursor, statement, params, context, executemany):
        if statement.startswith("INSERT"):
            statements[statement.split()[2]] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        batch._persist_signals(file_db, rows)
        file_db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert statements["strategy_signals"] <= 3 and statements["stock_paper_ledger_events"] <= 3
    saved = {row.id: row for row in file_db.query(StrategySignal).all()}
    assert [row.id for row in rows] == sorted(saved) and saved[rows[7].id].features == {"i": 7}
    events = file_db.query(StockPaperLedgerEvent).filter_by(event_type="signal").all()
    assert len(events) == 250 and {e.payload["signal_id"] for e in events} == set(saved)
    assert events[0].payload == {"signal_id": rows[0].id, "symbol": "S0", "strategy_id": strategy.id}


def test_a_misaligned_batch_falls_back_to_the_per_row_path(file_db):
    from datetime import datetime, timezone
    from decimal import Decimal

    from app.models import Strategy, StrategySignal

    strategy = Strategy(name="IBS", strategy_type="ibs_mean_reversion", parameters={}, is_active=True)
    file_db.add(strategy)
    file_db.commit()
    rows = [StrategySignal(strategy_id=strategy.id, symbol=s, signal_time=datetime.now(timezone.utc), action="BUY",
                           probability_up=Decimal("0.61234"), probability_down=Decimal("0.38766"),
                           confidence=Decimal("0.6196606"), reason="r", features={}) for s in ("SPY", "QQQ")]
    with patch.object(batch, "_insert_signals", side_effect=batch._BatchMismatch()):
        batch._persist_signals(file_db, rows)
    file_db.commit()
    assert {row.symbol for row in file_db.query(StrategySignal).all()} == {"SPY", "QQQ"}


def test_entry_limits_mirror_the_reservation_refusals():
    from app.services.stock_paper_autotrader import _EntryLimits

    limits = _EntryLimits({"AAPL", "SPY"}, max_open=3, exploration_lots=19, max_lots=20, min_confidence=0.58)
    strong, weak = {"confidence": "0.70"}, {"confidence": "0.40"}
    assert limits.check("AAPL", strong, exploring=False) is None  # adding to a held symbol is not a new position
    assert limits.check("QQQ", weak, exploring=True) == "Exploration signal confidence is below the minimum threshold"
    assert limits.check("QQQ", strong, exploring=True) is None
    limits.record_buy("QQQ", exploring=True)
    assert limits.check("MSFT", strong, exploring=False) == "Order exceeds the configured open-position risk limit"
    assert limits.check("AAPL", strong, exploring=True) == "Exploration lot limit reached"
    # Unknown limits never refuse here; the reservation still decides.
    open_ended = _EntryLimits(set(), 10**9, None, 10**9, None)
    assert open_ended.check("NVDA", weak, exploring=True) is None
