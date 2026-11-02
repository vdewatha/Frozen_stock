import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.services.exchange_sessions import is_nyse_session
from app.services.paper_short_simulation import ShortSimulationConfig, simulate_short_history


def history(prices=None):
    prices = prices if prices is not None else [200, 190, 180, 170, 160, 150, 140, 130, 120, 110, 100, 104, 108, 112, 99, 97, 95, 94]
    dates = [d for d in pd.date_range("2025-01-02", periods=40) if is_nyse_session(d.date())][:len(prices)]
    return pd.DataFrame({"date": dates, "open": prices, "close": prices,
                         "high": np.array(prices) + .1, "low": np.array(prices) - .1})


def config(**kwargs):
    return ShortSimulationConfig(**({"trend_window": 10, "entry_rsi": 55.0, "stop_loss_pct": .1} | kwargs))


def run(frame=None, **kwargs):
    return simulate_short_history(history() if frame is None else frame, symbol="SPY",
                                  source="alpaca_iex_daily", config=config(**kwargs))


def test_short_profit_and_accounting():
    report = run()
    assert report["number_of_trades"] == 1
    trade = report["trades"][0]
    assert trade["exit_price"] < trade["entry_price"]
    assert trade["net_pnl"] > 0
    assert trade["quantity"] == int(trade["quantity"])
    assert report["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in report["trades"]))
    assert report["equity_curve"][-1]["equity"] == report["ending_equity"]
    assert report["broker_orders_submitted"] == 0
    assert report["execution_eligible"] is False
    assert report["costs_verified"] is False


def test_next_open_and_no_lookahead():
    frame = history()
    first = run(frame)["trades"][0]
    entry = first["entry_index"]
    assert first["entry_price"] == pytest.approx(frame.open.iloc[entry] * .9995)
    frame["close"] = frame["close"].astype(float)
    frame.loc[entry:, "close"] = frame.loc[entry:, "open"] + .05
    assert run(frame)["trades"][0]["entry_date"] == first["entry_date"]


def test_gap_stop_loses_money_and_uses_open():
    frame = history()
    entry = run(frame)["trades"][0]["entry_index"]
    frame.loc[entry + 1:, ["open", "high", "low", "close"]] = [150, 151, 149, 150]
    trade = run(frame)["trades"][0]
    assert trade["exit_reason"] == "stop_gap"
    assert trade["exit_price"] == pytest.approx(150 * 1.0005)
    assert trade["net_pnl"] < 0


def test_entry_session_stop():
    frame = history()
    entry = run(frame)["trades"][0]["entry_index"]
    frame.loc[entry, "high"] = 150
    trade = run(frame)["trades"][0]
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_date"] == trade["entry_date"]


def test_costs_and_calendar_borrow():
    report = run()
    trade = report["trades"][0]
    days = (pd.Timestamp(trade["exit_date"]) - pd.Timestamp(trade["entry_date"])).days
    assert trade["borrow_cost"] == pytest.approx(trade["entry_price"] * trade["quantity"] * .03 * days / 365)
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["entry_fee"] - trade["exit_fee"] - trade["borrow_cost"])


def test_does_not_short_in_uptrend():
    assert run(history(list(range(100, 118))))["number_of_trades"] == 0


@pytest.mark.parametrize("mutation", ["duplicate", "unsorted", "missing", "nan", "bad_range", "zero", "short"])
def test_rejects_bad_history(mutation):
    frame = history()
    if mutation == "duplicate":
        frame.loc[1, "date"] = frame.loc[0, "date"]
    elif mutation == "unsorted":
        frame = frame.iloc[::-1]
    elif mutation == "missing":
        frame = frame.drop(index=4)
    elif mutation == "nan":
        frame.loc[5, "open"] = float("nan")
    elif mutation == "bad_range":
        frame.loc[5, "high"] = 1
    elif mutation == "zero":
        frame.loc[5, "close"] = 0
    else:
        frame = frame.iloc[:5]
    with pytest.raises(ValueError):
        run(frame)


def test_rejects_synthetic():
    with pytest.raises(ValueError, match="real historical"):
        simulate_short_history(history(), symbol="SPY", source="synthetic_fallback")


def test_rejects_duplicate_columns_and_boolean_prices():
    frame = history()
    with pytest.raises(ValueError, match="duplicate columns"):
        run(pd.concat([frame, frame[["close"]]], axis=1))
    frame = frame.astype({key: object for key in ("open", "high", "low", "close")})
    frame.loc[5, ["open", "high", "low", "close"]] = True
    with pytest.raises(ValueError, match="Boolean"):
        run(frame)


def test_rejects_mixed_provenance():
    frame = history()
    frame["source"] = "alpaca_iex_daily"
    frame.loc[3, "source"] = "synthetic_fallback"
    with pytest.raises(ValueError, match="consistent source"):
        run(frame)


@pytest.mark.parametrize("kwargs", [{"fee_bps": float("nan")}, {"allocation": .5}, {"stop_loss_pct": 0.0}, {"live": True}])
def test_config_fails_closed(kwargs):
    with pytest.raises(ValidationError):
        config(**kwargs)


def test_reproducible_fingerprint():
    assert run()["run_id"] == run()["run_id"]
    assert run(fee_bps=2.0)["run_id"] != run()["run_id"]


def test_research_permission():
    from app.core.security import required_role
    assert required_role("POST", "/stock-paper/short-simulation") == "researcher"


def test_forced_cover_and_time_stop():
    frame = history()
    entry = run(frame)["trades"][0]["entry_index"]
    shortened = frame.iloc[:entry + 1]
    assert run(shortened)["trades"][0]["exit_reason"] == "end_of_history"
    assert run(frame, max_holding_sessions=1)["trades"][0]["exit_reason"] == "time_stop"


def test_small_account_cannot_open_fractional_short():
    assert run(starting_equity=1.0)["trades"] == []


def test_http_endpoint_uses_database_only():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.api.stock_paper import router
    from app.db.session import get_db
    from app.models import MarketPrice

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    MarketPrice.__table__.create(engine)
    with Session(engine) as db:
        for row in history().to_dict("records"):
            db.add(MarketPrice(symbol="SPY", price_date=row["date"].date(), source="alpaca_iex_daily",
                               **{k: row[k] for k in ("open", "high", "low", "close")}))
        db.commit()
        api = FastAPI()
        api.include_router(router, prefix="/api")
        api.dependency_overrides[get_db] = lambda: db
        with TestClient(api) as client:
            payload = {"symbol": "SPY", "config": config().model_dump()}
            response = client.post("/api/stock-paper/short-simulation", json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["number_of_trades"] == 1
            assert response.json()["broker_orders_submitted"] == 0
            assert client.post("/api/stock-paper/short-simulation", json=payload | {"source": "synthetic_fallback"}).status_code == 422
            assert client.post("/api/stock-paper/short-simulation", json=payload | {"symbol": "AAPL"}).status_code == 422
            assert client.post("/api/stock-paper/short-simulation", json=payload | {"live": True}).status_code == 422
    engine.dispose()
