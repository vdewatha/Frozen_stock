"""Defensive sleeve (step 3): the frozen designs and rule, the zero-capital Design A shadow and its re-decision.

Synthetic prices and a fake fetcher only; nothing reaches yfinance or a broker.
"""
import tempfile
from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import AuditLog, Notification, Strategy
from app.models.stock_paper import StockPaperOrder
from app.services import defensive_sleeve as ds
from app.services import defensive_sleeve_rule as rule
from app.services import sleeve_guardrail
from app.services import target_weight_book as twb

UTC = timezone.utc


@pytest.fixture()
def db():
    tmp = tempfile.TemporaryDirectory()
    engine = create_engine("sqlite:///" + str(Path(tmp.name) / "defensive.sqlite"))
    Base.metadata.create_all(engine)
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "defensive_shadow_enabled", True))
        stack.enter_context(patch.object(settings, "passive_arm_equity_weight", 0.20))
        stack.enter_context(patch.object(settings, "passive_arm_equity_symbol", "VOO"))
        stack.enter_context(patch.object(settings, "passive_arm_tbill_symbol", "SGOV"))
        stack.enter_context(patch.object(settings, "target_weight_rebalance_weekday", 1))
        with Session(engine) as session:
            yield session
    tmp.cleanup()


def closes(end="2026-11-13", sessions=700, seed=7, spy_crash=None):
    """Synthetic adjusted closes for the design universe and the passive arm's ETFs (business days)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=sessions)
    symbols = list(rule.SYMBOLS) + ["VOO", "SGOV"]
    drift = {s: 0.0003 for s in symbols} | {"BIL": 0.00012, "SGOV": 0.00012}
    vol = {s: 0.01 for s in symbols} | {"BIL": 0.0002, "SGOV": 0.0002}
    data = {s: 100 * np.cumprod(1 + drift[s] + vol[s] * rng.standard_normal(sessions)) for s in symbols}
    frame = pd.DataFrame(data, index=idx)
    if spy_crash is not None:
        after = frame.index > pd.Timestamp(spy_crash)
        frame.loc[after, "SPY"] = frame.loc[after, "SPY"] * 0.7
    return frame


def fetcher_for(frame):
    calls = []

    def fetch(symbols, start):
        calls.append((tuple(symbols), start))
        return frame.loc[frame.index >= pd.Timestamp(start)]

    fetch.calls = calls
    return fetch


# ------------------------------------------------------------- frozen rule

def test_design_a_weights_follow_the_note():
    px = closes()
    w = rule.design_a_weights(px)
    assert set(w) == set(rule.SYMBOLS)
    assert abs(sum(w.values()) - 1) < 1e-9 and min(w.values()) >= -1e-12
    # Inverse-vol weight times the signal fraction (0, 1/4, ... 1): a risky weight is never above its iv share.
    rets = px[list(rule.RISKY8)].iloc[-64:].pct_change().iloc[1:]
    iv = (1 / rets.std()) / (1 / rets.std()).sum()
    for symbol in rule.RISKY8:
        frac = w[symbol] / iv[symbol]
        assert min(abs(frac - k / 4) for k in range(5)) < 1e-9
    assert rule.design_a_weights(px.iloc[:253]) is None


def test_design_b_weights_follow_the_note():
    px = closes()
    w = rule.design_b_weights(px)
    assert set(w) == set(rule.GTAA5) | {rule.CASH}
    for symbol in rule.GTAA5:
        assert 0 <= w[symbol] <= 0.2 + 1e-12
        assert min(abs(w[symbol] / 0.2 - k / 7) for k in range(8)) < 1e-9
    assert abs(sum(w.values()) - 1) < 1e-9
    assert rule.design_b_weights(px.iloc[:252]) is None


def test_slice_rule_needs_a_higher_sharpe_and_at_most_two_extra_points_of_drawdown():
    base = {"sharpe": 0.68, "max_drawdown": -0.071}
    assert rule.slice_passes(base, {"sharpe": 0.69, "max_drawdown": -0.090})
    assert not rule.slice_passes(base, {"sharpe": 0.69, "max_drawdown": -0.092})
    assert not rule.slice_passes(base, {"sharpe": 0.68, "max_drawdown": -0.05})


def test_evaluate_reports_every_cell_and_requires_all_of_them():
    out = rule.evaluate(closes(end="2016-06-30", sessions=900), designs=[rule.DESIGN_A])
    cells = out["designs"][rule.DESIGN_A]["cells"]
    assert set(cells) == {f"{c:g}bps|{w}" for c in rule.COSTS_BPS for w in rule.WINDOW_STARTS}
    assert out["designs"][rule.DESIGN_A]["passes"] == all(c["passes"] for c in cells.values())
    for cell in cells.values():
        assert set(cell["slices"]) == {"0.10", "0.20", "0.30"}


def test_recorded_decision_shadows_design_a_and_funds_nothing():
    assert rule.DECISION["funded"] == []
    assert rule.DECISION["shadow"] == rule.DESIGN_A
    for design in (rule.DESIGN_A, rule.DESIGN_B):
        body = rule.DESIGN_DECISIONS[design]
        assert body["passes"] is False
        assert body["cells"]["11bps|2015+"]["passes"] is False
    assert "20% of equity" in rule.RULE and "36 months" in rule.RULE and "20%" in rule.RULE


def test_redecision_trigger_is_36_months_or_a_spy_drawdown_deeper_than_20_percent():
    assert ds.REDECIDE_ON == date(2029, 11, 7)
    assert ds.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert ds.trigger_reason(date(2027, 1, 4), -0.19) is None
    assert "SPY drawdown" in ds.trigger_reason(date(2027, 1, 4), -0.21)
    assert "36 months" in ds.trigger_reason(date(2029, 11, 7), None)


# ------------------------------------------------------------ the shadow

def test_admission_is_a_zero_capital_sleeve_that_fences_nothing(db):
    before = twb.book_symbols(db)
    assert ds.admit_shadow(db) == ds.SHADOW_STATUS
    assert ds.admit_shadow(db) is None
    row = ds.shadow_row(db)
    assert row.parameters["sleeve_cadence"] == "monthly" and row.parameters["capital_fraction"] == 0
    assert "target_weights" not in row.parameters
    assert sleeve_guardrail.is_monthly_sleeve(row)
    assert twb.book_symbols(db) == before
    active, shadow = twb.sleeve_allocations(db)
    assert all(s["id"] != row.id for s in active)
    assert any(s["id"] == row.id and s["share"] == 0 for s in shadow)
    state = ds.redecision_state(db)
    assert state["state"] == "scheduled" and state["due_on"] == "2029-11-07"


def test_shadow_records_tranched_fills_and_daily_pnl_without_orders(db):
    ds.admit_shadow(db)
    frame = closes()
    fetch = fetcher_for(frame)
    first = ds.run_shadow(db, now=datetime(2026, 11, 9, 23, 0, tzinfo=UTC), fetcher=fetch)
    assert first["started"] == "2026-11-09" and first["recorded"] == []
    second = ds.run_shadow(db, now=datetime(2026, 11, 13, 23, 0, tzinfo=UTC), fetcher=fetch)
    assert second["recorded"] == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    # 2026-11-10 is the second Tuesday: tranche 2 rebalances on its signal from the 11-09 close.
    assert second["tranches"] == [2]
    tranche = db.query(AuditLog).filter_by(event_type=ds.EVENT, action=ds.TRANCHE_ACTION).one().payload
    assert tranche["signal_session"] == "2026-11-09" and tranche["fill_session"] == "2026-11-10"
    expected = rule.design_a_weights(frame.loc[:"2026-11-09", list(rule.SYMBOLS)])
    assert tranche["target_weights"] == pytest.approx({k: round(v, 6) for k, v in expected.items()})
    assert tranche["cost"] == pytest.approx(tranche["turnover"] * 11 / 1e4 * tranche["tranche_value"], abs=1e-3)
    assert db.query(StockPaperOrder).count() == 0
    rows = sleeve_guardrail.sleeve_daily_returns(db, ds.shadow_row(db))
    assert [r["session"] for r in rows] == second["recorded"]
    assert all(r["passive_return"] is not None for r in rows)
    # Passive leg modeled from VOO/SGOV at the arm's weights when the book has no row for the session.
    day, prev = pd.Timestamp("2026-11-12"), pd.Timestamp("2026-11-11")
    modeled = 0.2 * (frame.at[day, "VOO"] / frame.at[prev, "VOO"] - 1) + 0.8 * (frame.at[day, "SGOV"] / frame.at[prev, "SGOV"] - 1)
    assert rows[2]["passive_return"] == pytest.approx(modeled, abs=1e-9)
    # Idempotent: nothing new on a rerun.
    assert ds.run_shadow(db, now=datetime(2026, 11, 13, 23, 30, tzinfo=UTC), fetcher=fetch)["recorded"] == []


def test_shadow_prefers_the_books_passive_arm_return(db):
    from app.services.audit import write_audit_log

    ds.admit_shadow(db)
    fetch = fetcher_for(closes())
    ds.run_shadow(db, now=datetime(2026, 11, 9, 23, 0, tzinfo=UTC), fetcher=fetch)
    write_audit_log(db, event_type="target_weight_book", entity_type="target_weight_book", action="sleeve_daily_pnl",
                    status="recorded", message="book", payload={"session": "2026-11-10", "sleeves": {},
                                                                "passive_arm": {"strategy_id": 1, "return": 0.00123}})
    ds.run_shadow(db, now=datetime(2026, 11, 10, 23, 0, tzinfo=UTC), fetcher=fetch)
    payload = db.query(AuditLog).filter_by(event_type=ds.PNL_EVENT, action=ds.PNL_ACTION).one().payload
    assert payload["passive_return"] == pytest.approx(0.00123)
    assert payload["passive_source"] == "target_weight_book"


def test_spy_drawdown_fires_the_redecision_once_and_records_a_verdict(db):
    ds.admit_shadow(db)
    frame = closes(spy_crash="2026-11-11")
    fetch = fetcher_for(frame)
    ds.run_shadow(db, now=datetime(2026, 11, 9, 23, 0, tzinfo=UTC), fetcher=fetch)
    result = ds.run_shadow(db, now=datetime(2026, 11, 13, 23, 0, tzinfo=UTC), fetcher=fetch)
    assert result["redecision"]["state"] == "decided"
    fired = db.query(AuditLog).filter_by(event_type=ds.EVENT, action=ds.FIRED_ACTION).one().payload
    assert "SPY drawdown" in fired["reason"]
    verdict = db.query(AuditLog).filter_by(event_type=ds.EVENT, action=ds.VERDICT_ACTION).one().payload
    assert set(verdict["passes"]) == {rule.DESIGN_A, rule.DESIGN_B}
    assert db.query(Notification).filter_by(category="defensive_sleeve").count() == 2
    again = ds.check_redecision(db, now=datetime(2026, 11, 16, 23, 0, tzinfo=UTC), through=date(2026, 11, 16),
                                closes=frame, fetcher=fetch)
    assert again == {"state": "decided"}
    assert db.query(AuditLog).filter_by(event_type=ds.EVENT, action=ds.FIRED_ACTION).count() == 1
    assert ds.redecision_state(db)["state"] == "decided"


def test_redecision_waits_while_spy_is_within_20_percent(db):
    ds.admit_shadow(db)
    fetch = fetcher_for(closes())
    ds.run_shadow(db, now=datetime(2026, 11, 9, 23, 0, tzinfo=UTC), fetcher=fetch)
    out = ds.run_shadow(db, now=datetime(2026, 11, 13, 23, 0, tzinfo=UTC), fetcher=fetch)
    assert out["redecision"]["state"] == "scheduled"
    assert db.query(AuditLog).filter_by(event_type=ds.EVENT, action=ds.FIRED_ACTION).count() == 0


def test_view_is_a_field_on_the_target_weight_book_endpoint(db):
    from app.api.stock_paper import target_weight_book

    ds.admit_shadow(db)
    fetch = fetcher_for(closes())
    ds.run_shadow(db, now=datetime(2026, 11, 9, 23, 0, tzinfo=UTC), fetcher=fetch)
    ds.run_shadow(db, now=datetime(2026, 11, 12, 23, 0, tzinfo=UTC), fetcher=fetch)
    result = target_weight_book(db)
    view = result["defensive_sleeve"]
    assert view["decision"]["funded"] == [] and view["decision"]["passes"] == {rule.DESIGN_A: False, rule.DESIGN_B: False}
    assert view["shadow"]["sessions"] == 3 and view["shadow"]["zero_capital"] is True
    assert view["redecision"]["state"] == "scheduled" and view["redecision"]["due_on"] == "2029-11-07"


def test_disabled_or_missing_shadow_does_nothing(db):
    fetch = fetcher_for(closes())
    assert ds.run_shadow(db, now=datetime(2026, 11, 13, 23, 0, tzinfo=UTC), fetcher=fetch)["status"] == "skipped"
    ds.admit_shadow(db)
    with patch.object(settings, "defensive_shadow_enabled", False):
        assert ds.run_shadow(db, now=datetime(2026, 11, 13, 23, 0, tzinfo=UTC), fetcher=fetch)["status"] == "skipped"
    assert fetch.calls == []


def test_resume_all_cannot_turn_the_shadow_into_a_trading_row(db):
    ds.admit_shadow(db)
    row = ds.shadow_row(db)
    row.current_status = "paper_trading_candidate"
    db.flush()
    ds.run_shadow(db, now=datetime(2026, 11, 9, 23, 0, tzinfo=UTC), fetcher=fetcher_for(closes()))
    assert ds.shadow_row(db).current_status == ds.SHADOW_STATUS
