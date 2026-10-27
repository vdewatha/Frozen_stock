import sqlite3
from datetime import datetime, timezone

from scripts.report_paper_mistakes import (
    benchmark_comparison,
    build_report,
    load_legacy_trades,
    rank_mistakes,
    render_markdown,
    round_trips,
    strategy_for,
    summarize,
)


def _fill(order_id, symbol, side, qty, price, when):
    return {"activity_type": "FILL", "order_id": order_id, "symbol": symbol, "side": side,
            "qty": str(qty), "price": str(price), "transaction_time": when}


def _snapshot():
    orders = [
        {"id": "b1", "client_order_id": "sp-aaa", "limit_price": "100"},
        {"id": "b2", "client_order_id": "sp-bbb", "limit_price": "101"},
        {"id": "s1", "client_order_id": "sp-ccc", "limit_price": None},
        {"id": "q1", "client_order_id": "qa-1-buy", "limit_price": None},
        {"id": "q2", "client_order_id": "qa-1-sell", "limit_price": None},
    ]
    activities = [
        _fill("b1", "SPY", "buy", 10, 100.1, "2026-10-01T13:31:00Z"),   # 09:31 ET, opening window
        _fill("b2", "SPY", "buy", 10, 101, "2026-10-02T15:00:00Z"),     # stacked while long
        _fill("s1", "SPY", "sell", 20, 90, "2026-10-05T15:00:00Z"),
        _fill("q1", "AAPL", "buy", 1, 50, "2026-10-06T15:00:00Z"),
        _fill("q2", "AAPL", "sell", 1, 49.9, "2026-10-06T15:00:01Z"),
        {"activity_type": "FEE", "net_amount": "-0.02", "date": "2026-10-06"},
    ]
    return orders, activities


def test_strategy_attribution_conventions_and_override():
    assert strategy_for("sp-x") == "stock_paper_ledger_unattributed"
    assert strategy_for("qa-x-buy") == "qa_roundtrip_probe"
    assert strategy_for("lb-x") == "LIVE_BROKER_ID_ON_PAPER"
    assert strategy_for("sp-x", {"sp-x": "macd_momentum"}) == "macd_momentum"


def test_fifo_round_trips_fees_and_summary():
    orders, activities = _snapshot()
    trips = round_trips(orders, activities, {"sp-aaa": "ma", "sp-bbb": "macd"})
    assert [(t["symbol"], t["strategy"], round(t["pnl"], 2)) for t in trips] == [
        ("SPY", "ma", -101.0), ("SPY", "macd", -110.0), ("AAPL", "qa_roundtrip_probe", -0.1)]
    assert abs(trips[2]["fees"] - 0.02) < 1e-9
    summary = summarize(trips)
    assert summary["round_trips"] == 3 and summary["win_rate"] == 0
    assert abs(summary["net_pnl"] - (-211.12)) < 1e-6
    assert round(summary["entry_slippage_bps_vs_decision"], 1) == 5.0  # (100.1/100-1) and (101/101-1)


def test_mistakes_are_ranked_by_dollar_cost():
    orders, activities = _snapshot()
    mistakes = rank_mistakes(round_trips(orders, activities), stop_pct=0.03)
    names = [row["mistake"] for row in mistakes]
    assert names[0] == "losing_strategy:stock_paper_ledger_unattributed"
    beyond = next(row for row in mistakes if row["mistake"] == "oversized_loser_beyond_stop")
    # (10.09% - 3%) * 1001 + (10.89% - 3%) * 1010
    assert abs(beyond["cost"] - ((101.0 / 1001 - 0.03) * 1001 + (110 / 1010 - 0.03) * 1010)) < 1e-6
    stacked = next(row for row in mistakes if row["mistake"] == "stacked_entry_while_long")
    assert stacked["count"] == 1 and abs(stacked["cost"] - 110) < 1e-9
    assert any(row["mistake"] == "entry_in_opening_minutes" for row in mistakes)
    assert any(row["mistake"] == "churn_round_trip" for row in mistakes)
    assert [row["cost"] for row in mistakes] == sorted((row["cost"] for row in mistakes), reverse=True)


def test_benchmark_opportunity_cost():
    ts = lambda day: int(datetime(2026, 9, day, tzinfo=timezone.utc).timestamp())
    history = {"timestamp": [ts(11), ts(30)], "equity": [100000, 100100]}
    bars = [{"t": "2026-09-11T04:00:00Z", "c": 100}, {"t": "2026-09-30T04:00:00Z", "c": 102}]
    result = benchmark_comparison(history, bars)
    assert abs(result["benchmark_change_on_same_capital"] - 2000) < 1e-6
    assert abs(result["opportunity_cost"] - 1900) < 1e-6


def test_legacy_sqlite_is_read_and_report_renders(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE strategies (id INTEGER, strategy_type TEXT)")
        db.execute("CREATE TABLE paper_trades (id INTEGER, strategy_id INTEGER, symbol TEXT, side TEXT,"
                   " entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL, quantity REAL,"
                   " status TEXT, profit_loss REAL, reason_exited TEXT, features_at_entry TEXT)")
        db.execute("INSERT INTO strategies VALUES (5, 'model_predictive_long')")
        db.execute("INSERT INTO paper_trades VALUES (7, 5, 'SPY', 'BUY', '2026-06-04 01:46:53',"
                   " '2026-06-10 14:02:22', 619.86, 420.96, 6.45, 'closed', -1283.0, 'Closed by stop-loss rule.',"
                   " '{\"market_regime_features\": {\"latest_close\": 735.38}}')")
    trips = load_legacy_trades(str(path))
    assert trips[0]["strategy"] == "model_predictive_long" and trips[0]["decision_price"] == 735.38
    report = build_report(None, trips, [])
    names = {row["mistake"] for row in report["legacy_simulator"]["mistakes"]}
    assert {"oversized_loser_beyond_stop", "fill_far_from_decision_price"} <= names
    assert "Legacy local simulator" in render_markdown(report)
