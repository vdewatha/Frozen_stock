from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.stock_learning_cycle import (
    ONE_SESSION_SYMBOLS,
    paper_run_runtime_bounds,
    session_bounds,
)
from app.services.stock_paper_ledger import _approved_entry_window
from app.services.stock_training_jobs import StockTrainingError


def test_one_session_bounds_are_explicit_and_exact():
    session = date(2026, 9, 17)
    cycle = SimpleNamespace(
        symbols=ONE_SESSION_SYMBOLS,
        cutoff_date=session,
        trigger="manual",
        evidence={},
    )
    trial = SimpleNamespace(policy={
        "regular_sessions": 1,
        "paper_session_date": session.isoformat(),
        "max_order_notional": "1250",
        "max_symbol_notional": "2500",
        "max_aggregate_notional": "7500",
        "max_loss": "0.015",
        "loss_unit": "fraction_of_baseline_equity",
        "stop_authority": "operator_and_system",
        "pending_order_treatment": "cancel",
        "remaining_position_policy": "hold",
    })

    bounds = paper_run_runtime_bounds(cycle, trial)

    assert bounds["symbols"] == ONE_SESSION_SYMBOLS
    assert bounds["duration_sessions"] == 1
    assert bounds["schedule"]["timezone"] == "America/New_York"
    assert bounds["schedule"]["session_date"] == session.isoformat()
    assert bounds["schedule"]["start_at"] == session_bounds(session)[0].isoformat()
    assert bounds["schedule"]["end_at"] == session_bounds(session)[1].isoformat()
    assert bounds["exposure_limits"]["max_order_notional"] == "1250"
    assert bounds["loss_limits"]["max_loss"] == "0.015"
    assert bounds["pending_order_treatment"] == "cancel"


def test_weekend_session_has_no_regular_exchange_window():
    assert session_bounds(date(2026, 9, 19)) is None


def test_expired_approved_window_is_closed_even_without_deadline_job():
    approval = SimpleNamespace(schedule={
        "start_at": "2026-09-17T13:30:00+00:00",
        "end_at": "2026-09-17T20:00:00+00:00",
    })

    allowed, reason = _approved_entry_window(
        approval, datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)
    )

    assert allowed is False
    assert reason == "Paper run session has expired"


def test_pre_session_window_does_not_allow_entries():
    approval = SimpleNamespace(schedule={
        "start_at": "2026-09-17T13:30:00+00:00",
        "end_at": "2026-09-17T20:00:00+00:00",
    })

    allowed, reason = _approved_entry_window(
        approval, datetime(2026, 9, 17, 13, 29, tzinfo=timezone.utc)
    )

    assert allowed is False
    assert reason == "Paper run session has not started"