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
