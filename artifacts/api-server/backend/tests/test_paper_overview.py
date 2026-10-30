"""Overview reads never call a broker, evaluate a strategy or change the book."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.db.base import Base
from app.models import Strategy, StrategySignal, StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services.paper_overview import paper_overview

NOW = datetime(2026, 10, 30, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
def book():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = StockPaperAccount(
            broker="alpaca_paper", broker_account_id="overview-test",
            cash=Decimal("9000"), buying_power=Decimal("9000"), equity=Decimal("10000"),
            status="reconciled", costs_known=True, accounting_verified=True,
            reconciliation_required=False, last_reconciled_at=NOW, raw_payload={},
        )
        strategy = Strategy(name="RSI(2)", strategy_type="rsi2_pullback", parameters={},
                            current_status="paper_trading_active")
        db.add_all([account, strategy])
        db.commit()
        with patch("app.services.paper_overview.active_paper_account", return_value=account):
            yield db, account, strategy
    engine.dispose()


def add_holding(db, account, strategy, *, shares=10, fill_shares=None, unknown=False,
                source="test", with_fill=True):
    position = StockPaperPosition(
        account_id=account.id, symbol="SPY", quantity=Decimal(shares),
        average_entry_price=Decimal("100"), current_price=Decimal("110"),
        market_value=Decimal(shares * 110), cost_basis=Decimal(shares * 100),
        unrealized_pl=Decimal(shares * 10), observed_at=NOW, raw_payload={},
    )
    order = StockPaperOrder(
        account_id=account.id, strategy_id=None if unknown else strategy.id,
        client_order_id="overview-entry", symbol="SPY", side="buy", quantity=Decimal(shares),
        status="filled", source=source,
    )
    db.add_all([position, order])
    db.flush()
    if with_fill:
        db.add(StockPaperFill(
            account_id=account.id, order_id=order.id, broker_activity_id="overview-fill",
            symbol="SPY", side="buy", quantity=Decimal(fill_shares or shares),
            price=Decimal("100"), fee=Decimal("0"), cost_known=True, filled_at=NOW,
            raw_payload={},
        ))
    db.commit()
    return position, order


def add_fill(db, account, *, order_id, shares, activity, side="buy", symbol="SPY"):
    db.add(StockPaperFill(
        account_id=account.id, order_id=order_id, broker_activity_id=activity,
        symbol=symbol, side=side, quantity=Decimal(shares), price=Decimal("100"),
        fee=Decimal("0"), cost_known=True, filled_at=NOW, raw_payload={},
    ))
    db.commit()


def evidence_codes(result):
    return [issue["code"] for issue in result["positions"][0]["attribution_evidence"]["issues"]]


def test_exact_fill_attribution_and_broker_values(book):
    db, account, strategy = book
    add_holding(db, account, strategy)
    result = paper_overview(db, now=NOW)
    row = result["positions"][0]
    assert row["strategy"] == "RSI(2)"
    assert row["attribution"] == "attributed"
    assert Decimal(row["quantity"]) == 10
    assert Decimal(row["unrealized_pl"]) == 100
    assert row["unrealized_pl_pct"] == 10
    assert row["horizon_sessions"] is None  # No matching durable lot.
    assert result["strategies"][0]["open_positions"] == 1
    assert Decimal(result["account"]["exposure"]) == 1100
    evidence = row["attribution_evidence"]
    assert evidence["issues"] == []
    assert Decimal(evidence["unexplained_quantity"]) == 0
    assert evidence["owner_assigned_automatically"] is False
    assert "net exactly" in evidence["summary"]


@pytest.mark.parametrize("unknown,fill_shares", [(True, 10), (False, 9)])
def test_partial_or_unowned_fills_never_hide_the_broker_position(book, unknown, fill_shares):
    db, account, strategy = book
    add_holding(db, account, strategy, unknown=unknown, fill_shares=fill_shares)
    result = paper_overview(db, now=NOW)
    assert len(result["positions"]) == 1
    assert result["positions"][0]["attribution"] == "unknown"
    assert result["positions"][0]["strategy"] is None
    assert result["positions"][0]["entry_at"] is None


def test_imported_external_orders_are_explained_without_assigning_an_owner(book):
    """The production book: every fill traces to a broker_import order with no strategy."""
    db, account, strategy = book
    add_holding(db, account, strategy, unknown=True, source="broker_import")
    db.add(StrategySignal(strategy_id=strategy.id, symbol="SPY", signal_time=NOW, action="BUY",
                          reason="latest signal must not become ownership", features={}))
    db.commit()
    result = paper_overview(db, now=NOW)
    row = result["positions"][0]
    assert row["attribution"] == "unknown" and row["strategy_ids"] == []
    assert evidence_codes(result) == ["external_orders"]
    issue = row["attribution_evidence"]["issues"][0]
    assert issue["fills"] == 1 and Decimal(issue["quantity"]) == 10
    assert "outside this app" in issue["detail"]
    assert "explicit, reviewed attribution record" in issue["proposed_review"]
    assert Decimal(row["attribution_evidence"]["unexplained_quantity"]) == 0
    assert result["strategies"][0]["open_positions"] == 0


def test_missing_order_link_and_wrong_symbol_order_are_distinguished(book):
    db, account, strategy = book
    position, _ = add_holding(db, account, strategy, shares=10, with_fill=False)
    add_fill(db, account, order_id=None, shares=6, activity="orphan")
    other = StockPaperOrder(account_id=account.id, strategy_id=strategy.id, client_order_id="qqq-entry",
                            symbol="QQQ", side="buy", quantity=4, status="filled", source="test")
    db.add(other)
    db.flush()
    add_fill(db, account, order_id=other.id, shares=4, activity="wrong-symbol-link")
    result = paper_overview(db, now=NOW)
    assert evidence_codes(result) == ["missing_order_link"]
    issue = result["positions"][0]["attribution_evidence"]["issues"][0]
    assert issue["fills"] == 2 and Decimal(issue["quantity"]) == 10
    assert result["positions"][0]["attribution"] == "unknown"


def test_unowned_app_fills_including_deleted_strategy_owner(book):
    db, account, strategy = book
    _, order = add_holding(db, account, strategy, shares=10, fill_shares=7, unknown=True, source="manual_control_room")
    ghost = StockPaperOrder(account_id=account.id, strategy_id=999_999, client_order_id="ghost-entry",
                            symbol="SPY", side="buy", quantity=3, status="filled", source="scheduled_paper_signal")
    db.add(ghost)
    db.flush()
    add_fill(db, account, order_id=ghost.id, shares=3, activity="ghost-fill")
    result = paper_overview(db, now=NOW)
    assert evidence_codes(result) == ["unowned_fills"]
    issue = result["positions"][0]["attribution_evidence"]["issues"][0]
    assert issue["fills"] == 2 and Decimal(issue["quantity"]) == 10
    assert "never inference from later signals" in issue["proposed_review"]


def test_quantity_mismatch_reports_uncovered_shares(book):
    db, account, strategy = book
    add_holding(db, account, strategy, shares=10, fill_shares=9)
    result = paper_overview(db, now=NOW)
    assert evidence_codes(result) == ["quantity_mismatch"]
    evidence = result["positions"][0]["attribution_evidence"]
    assert Decimal(evidence["strategy_net_quantity"]) == 9
    assert Decimal(evidence["unexplained_quantity"]) == 1
    assert "1 shares are not covered" in evidence["issues"][0]["detail"]


def test_oversold_strategy_net_is_a_quantity_mismatch(book):
    db, account, strategy = book
    _, order = add_holding(db, account, strategy, shares=10, fill_shares=10)
    second = Strategy(name="IBS", strategy_type="ibs_mean_reversion", parameters={}, current_status="paper_trading_active")
    db.add(second)
    db.flush()
    seller = StockPaperOrder(account_id=account.id, strategy_id=second.id, client_order_id="ibs-sell",
                             symbol="SPY", side="sell", quantity=5, status="filled", source="test")
    buyer = StockPaperOrder(account_id=account.id, strategy_id=strategy.id, client_order_id="rsi-rebuy",
                            symbol="SPY", side="buy", quantity=5, status="filled", source="test")
    db.add_all([seller, buyer])
    db.flush()
    add_fill(db, account, order_id=seller.id, shares=5, side="sell", activity="ibs-sold")
    add_fill(db, account, order_id=buyer.id, shares=5, activity="rsi-rebought")
    result = paper_overview(db, now=NOW)
    assert result["positions"][0]["attribution"] == "unknown"
    assert evidence_codes(result) == ["quantity_mismatch"]
    assert "below zero" in result["positions"][0]["attribution_evidence"]["issues"][0]["detail"]


def test_holding_without_any_fill_history(book):
    db, account, strategy = book
    add_holding(db, account, strategy, with_fill=False)
    result = paper_overview(db, now=NOW)
    assert evidence_codes(result) == ["no_fill_history"]
    evidence = result["positions"][0]["attribution_evidence"]
    assert evidence["fill_count"] == 0
    assert Decimal(evidence["unexplained_quantity"]) == 10
    assert result["positions"][0]["attribution"] == "unknown"


def test_mixed_evidence_lists_every_applicable_diagnostic(book):
    db, account, strategy = book
    _, order = add_holding(db, account, strategy, shares=10, fill_shares=4)
    external = StockPaperOrder(account_id=account.id, strategy_id=None, client_order_id="broker-x",
                               symbol="SPY", side="buy", quantity=3, status="filled", source="broker_import")
    db.add(external)
    db.flush()
    add_fill(db, account, order_id=external.id, shares=3, activity="external-fill")
    add_fill(db, account, order_id=None, shares=1, activity="unlinked-fill")
    result = paper_overview(db, now=NOW)
    assert evidence_codes(result) == ["external_orders", "missing_order_link", "quantity_mismatch"]
    evidence = result["positions"][0]["attribution_evidence"]
    assert Decimal(evidence["strategy_net_quantity"]) == 4
    assert Decimal(evidence["fill_net_quantity"]) == 8
    assert Decimal(evidence["unexplained_quantity"]) == 2
    assert result["positions"][0]["unrealized_pl"] is not None  # verified account keeps P/L


def test_attribution_evidence_never_unmasks_pl_or_mutates(book):
    db, account, strategy = book
    add_holding(db, account, strategy, unknown=True, source="broker_import")
    account.accounting_verified = False
    db.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        result = paper_overview(db, now=NOW)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert result["positions"][0]["unrealized_pl"] is None
    assert evidence_codes(result) == ["external_orders"]
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert not db.new and not db.dirty and not db.deleted


def test_unverified_account_still_shows_holdings_but_never_invents_pl(book):
    db, account, strategy = book
    add_holding(db, account, strategy)
    account.accounting_verified = False
    account.status = "halted"
    account.halt_reason = "Reconciliation required"
    account.last_reconciled_at = NOW - timedelta(minutes=10)
    db.commit()
    result = paper_overview(db, now=NOW)
    assert len(result["positions"]) == 1
    assert result["positions"][0]["unrealized_pl"] is None
    assert result["positions"][0]["unrealized_pl_pct"] is None
    assert result["account"]["stale"] is True
    assert result["account"]["realized_pl"] is None
    assert result["bot"]["state"] == "Blocked"


def test_empty_is_distinct_from_uninitialized(book):
    db, _, _ = book
    empty = paper_overview(db, now=NOW)
    assert empty["positions"] == []
    assert empty["account"]["status"] == "reconciled"
    assert Decimal(empty["account"]["exposure"]) == 0
    with patch("app.services.paper_overview.active_paper_account", return_value=None):
        unavailable = paper_overview(db, now=NOW)
    assert unavailable["account"]["equity"] is None
    assert unavailable["account"]["exposure"] is None
    assert unavailable["bot"]["state"] == "Not initialized"


def test_persisted_entry_withholding_and_unknown_schedule_are_explicit(book):
    db, account, strategy = book
    db.add(StrategySignal(
        strategy_id=strategy.id, symbol="SPY", signal_time=NOW, action="HOLD",
        reason="BUY withheld", features={"entry_timing": {"withheld_action": "BUY"}},
    ))
    db.commit()
    result = paper_overview(db, now=NOW)
    assert result["strategies"][0]["last_signal"]["withheld"] is True
    assert result["strategies"][0]["next_evaluation_at"] is None
    assert result["activity"][0]["kind"] == "withheld"
    assert result["bounds"]["starts_at"] is None


def test_read_issues_no_mutating_sql_or_broker_calls(book):
    db, account, strategy = book
    add_holding(db, account, strategy)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        with patch("app.services.stock_paper_ledger.active_paper_gateway", side_effect=AssertionError("broker call")):
            paper_overview(db, now=NOW)
            paper_overview(db, now=NOW)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert statements
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert not db.new and not db.dirty and not db.deleted


def test_early_close_and_holiday_session(book):
    db, _, _ = book
    early = datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc)
    with patch("app.services.strategy_execution_policy.settings.paper_daily_bar_entry_window_end_minutes", 375):
        result = paper_overview(db, now=early)
    assert result["session"]["entry_end"].startswith("2026-11-27T18:00")
    assert result["session"]["state"] == "open"
    holiday = paper_overview(db, now=datetime(2026, 11, 26, 17, tzinfo=timezone.utc))
    assert holiday["session"]["state"] == "closed"
    assert holiday["session"]["entry_end"] is None


def test_viewer_route_is_explicitly_classified():
    from app.core.security import READ_PATHS
    assert "/stock-paper/overview" in READ_PATHS


def test_multiple_owners_and_other_account_fills(book):
    db, account, strategy = book
    _, order = add_holding(db, account, strategy, fill_shares=6)
    second = Strategy(name="IBS", strategy_type="ibs_mean_reversion", parameters={},
                      current_status="paper_trading_candidate")
    db.add(second)
    db.flush()
    order2 = StockPaperOrder(
        account_id=account.id, strategy_id=second.id, client_order_id="other-strategy",
        symbol="SPY", side="buy", quantity=Decimal(4), status="filled", source="test",
    )
    db.add(order2)
    db.flush()
    db.add(StockPaperFill(
        account_id=account.id, order_id=order2.id, broker_activity_id="second-fill",
        symbol="SPY", side="buy", quantity=Decimal(4), price=Decimal(100),
        fee=Decimal(0), cost_known=True, filled_at=NOW, raw_payload={},
    ))
    db.commit()
    result = paper_overview(db, now=NOW)
    assert result["positions"][0]["attribution"] == "multiple"
    assert result["positions"][0]["strategy_ids"] == [strategy.id, second.id]
    assert [s["open_positions"] for s in result["strategies"]] == [1, 1]
    # A second broker account must never contaminate the active book.
    other = StockPaperAccount(
        broker="tradier_sandbox", broker_account_id="other-account",
        cash=0, buying_power=0, equity=0, raw_payload={},
    )
    db.add(other)
    db.flush()
    db.add(StockPaperFill(
        account_id=other.id, order_id=None, broker_activity_id="foreign-fill",
        symbol="SPY", side="buy", quantity=1000, price=100,
        cost_known=False, filled_at=NOW, raw_payload={},
    ))
    db.commit()
    assert paper_overview(db, now=NOW)["positions"] == result["positions"]


def test_full_attribution_history_is_not_limited_to_display_tail(book):
    db, account, strategy = book
    _, order = add_holding(db, account, strategy, shares=250, fill_shares=1)
    db.add_all([StockPaperFill(
        account_id=account.id, order_id=order.id, broker_activity_id=f"extra-{i}",
        symbol="SPY", side="buy", quantity=1, price=100,
        cost_known=False, filled_at=NOW, raw_payload={},
    ) for i in range(249)])
    db.commit()
    result = paper_overview(db, now=NOW)
    assert result["positions"][0]["attribution"] == "attributed"
    assert len(result["activity"]) == 12


def test_durable_lot_exit_metadata_and_partial_sales(book):
    from app.models import StockPaperTrialLot
    db, account, strategy = book
    position, order = add_holding(db, account, strategy)
    order.stop_price = Decimal("98")
    db.add(StockPaperTrialLot(
        trial_id="isolated-test-trial", symbol="SPY", entry_decision_id=1,
        entry_order_id=order.id, quantity=10, entry_session="2026-10-29",
        planned_horizon_sessions=5, stop_fraction=Decimal("0.02"),
        exited_quantity=0,
    ))
    db.commit()
    row = paper_overview(db, now=NOW)["positions"][0]
    assert row["sessions_held"] == 1
    assert row["horizon_sessions"] == 5
    assert row["exit_intent"] == "Horizon exit · session 5"
    assert Decimal(row["stop_price"]) == 98
    assert row["entry_at"] is not None
    # The same symbol in an unrelated lot must not change the active lot.
    db.add(StockPaperTrialLot(
        trial_id="old-test-trial", symbol="SPY", entry_decision_id=2,
        quantity=99, entry_session="2020-01-01", planned_horizon_sessions=99,
        stop_fraction=Decimal("0.9"), exited_quantity=0,
    ))
    db.commit()
    assert paper_overview(db, now=NOW)["positions"][0] == row


def test_equity_curve_is_chronological_and_survives_halt(book):
    from app.models import StockPaperEquitySnapshot
    db, account, _ = book
    for i, equity in enumerate(["100", "101", "99"]):
        db.add(StockPaperEquitySnapshot(
            account_id=account.id, cash=1, equity=Decimal(equity), buying_power=1,
            observed_at=NOW - timedelta(hours=3 - i), raw_payload={},
        ))
    account.status = "halted"
    db.commit()
    curve = paper_overview(db, now=NOW)["equity_curve"]
    assert [Decimal(p["equity"]) for p in curve] == [100, 101, 99]
    assert curve[0]["observed_at"] < curve[-1]["observed_at"]
