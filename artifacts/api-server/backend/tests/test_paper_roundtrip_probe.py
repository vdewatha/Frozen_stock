from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.stock_paper_ledger import ALPACA_PAPER_URL, StockPaperUnavailable
from scripts.test_alpaca_paper_roundtrip import execute

RUN = "16fa684a-185c-4b27-844b-b867e7023cc1"


class Broker:
    base_url = ALPACA_PAPER_URL

    def __init__(self):
        self.sent = []
        self.rows = {}
        self.held = []
        self.open = True
        self.lost_ack = False
        self.fill = True
        self.symbol = "SPY"

    def account(self):
        return {"id": "paper-test", "status": "ACTIVE", "trading_blocked": False,
                "currency": "USD", "account_blocked": False, "trade_suspended_by_user": False,
                "cash": "1000", "buying_power": "1000"}

    def _request(self, method, path):
        assert method == "GET"
        if path == "/v2/clock":
            return {"is_open": self.open, "timestamp": "2026-09-29T15:00:00Z", "next_close": "2026-09-29T20:00:00Z"}
        assert path == "/v2/assets/" + self.symbol
        return {"tradable": True, "fractionable": True, "symbol": self.symbol, "status": "active", "class": "us_equity"}

    def positions(self):
        return deepcopy(self.held)

    def orders(self):
        return []

    def order_by_client_id(self, identifier):
        return self.rows.get(identifier)

    def submit_order(self, payload):
        self.sent.append(payload)
        if self.fill:
            self.rows[payload["client_order_id"]] = {**payload, "id": payload["client_order_id"], "status": "filled",
                                                    "filled_qty": payload.get("qty", "0.014356789"), "filled_avg_price": "696.534"}
            self.held = [{"symbol": self.symbol, "qty": "0.014356789"}] if payload["side"] == "buy" else []
        if self.lost_ack:
            raise StockPaperUnavailable("Lost response")
        return self.rows.get(payload["client_order_id"])


def run(broker, *, now=None):
    clock = SimpleNamespace(now=0)
    events = []
    result = execute(broker, RUN, "paper-test", events.append,
                     symbol=broker.symbol, now=now or datetime(2026, 9, 29, 15, tzinfo=timezone.utc),
                     sleep=lambda seconds: setattr(clock, "now", clock.now + seconds), clock=lambda: clock.now)
    return result, events


@pytest.mark.parametrize("lost_ack", [False, True])
def test_roundtrip_never_reposts_and_sells_only_confirmed_quantity(lost_ack):
    broker = Broker()
    broker.lost_ack = lost_ack
    result, events = run(broker)
    assert len(broker.sent) == 2
    assert broker.sent[0]["notional"] == "10"
    assert broker.sent[1]["qty"] == "0.014356789"
    assert "notional" not in broker.sent[1]
    assert result["open_positions"] == 0 and result["costs_verified"] is False
    assert events[0]["event"] == "before_submit"


@pytest.mark.parametrize("problem", ["live_url", "closed", "existing_position", "wrong_account", "existing_id"])
def test_preflight_cannot_touch_an_unqualified_account(problem):
    broker = Broker()
    if problem == "live_url":
        broker.base_url = "https://api.alpaca.markets"
    elif problem == "closed":
        broker.open = False
    elif problem == "existing_position":
        broker.held = [{"symbol": "SPY", "qty": "1"}]
    elif problem == "wrong_account":
        broker.account = lambda: {"id": "other"}
    else:
        broker.rows["qa-" + RUN.replace("-", "") + "-buy"] = {"id": "existing"}
    with pytest.raises(RuntimeError):
        run(broker)
    assert broker.sent == []


def test_unresolved_buy_never_submits_a_second_buy_or_a_sell():
    broker = Broker()
    broker.fill = False
    with pytest.raises(RuntimeError, match="uncertain"):
        run(broker)
    assert len(broker.sent) == 1


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "QQQ", "SPY"])
def test_bounded_symbol_roundtrips_preserve_cap_and_identity(symbol):
    broker = Broker()
    broker.symbol = symbol
    result, _ = run(broker)
    assert result["symbol"] == symbol
    assert all(row["symbol"] == symbol for row in broker.sent)
    assert broker.sent[0]["notional"] == "10"


@pytest.mark.parametrize("field,value", [("account_blocked", True), ("trade_suspended_by_user", True),
                                         ("account_blocked", None), ("currency", "EUR")])
def test_explicit_account_permissions_required(field, value):
    broker = Broker()
    account = broker.account()
    broker.account = lambda: {**account, field: value}
    with pytest.raises(RuntimeError):
        run(broker)
    assert broker.sent == []


@pytest.mark.parametrize("now", [datetime(2026, 9, 29, 15, 1, tzinfo=timezone.utc),
                                 datetime(2026, 9, 29, 14, 59, tzinfo=timezone.utc),
                                 datetime(2026, 9, 29, 15)])
def test_stale_future_or_naive_clock_cannot_place_orders(now):
    broker = Broker()
    with pytest.raises(RuntimeError, match="clock"):
        run(broker, now=now)
    assert broker.sent == []


@pytest.mark.parametrize("ahead", [0.021, 1, 1.001, 60])
def test_real_clock_waits_out_only_bounded_positive_skew(monkeypatch, ahead):
    import scripts.test_alpaca_paper_roundtrip as probe
    broker_time = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)
    state = {"now": broker_time-timedelta(seconds=ahead), "waits": []}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"]
    def sleep(seconds):
        state["waits"].append(seconds)
        state["now"] += timedelta(seconds=seconds)
    monkeypatch.setattr(probe, "datetime", Clock)
    monkeypatch.setattr(probe.time, "sleep", sleep)
    if ahead <= 1:
        assert probe._session_clock(Broker()) == broker_time
        assert state["now"] >= broker_time
        assert state["waits"] == [pytest.approx(ahead + .01)]
    else:
        with pytest.raises(RuntimeError, match="clock"):
            probe._session_clock(Broker())
        assert not state["waits"]


def test_wait_does_not_excuse_a_clock_that_stays_behind(monkeypatch):
    import scripts.test_alpaca_paper_roundtrip as probe
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 29, 14, 59, 59, 990000, tzinfo=timezone.utc)
    monkeypatch.setattr(probe, "datetime", Clock)
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="clock"):
        probe._session_clock(Broker())


def test_wrong_order_side_stops_without_followup_order():
    broker = Broker()
    submit = broker.submit_order
    def changed(payload):
        row = submit(payload)
        row["side"] = "sell"
        return row
    broker.submit_order = changed
    with pytest.raises(RuntimeError, match="identity"):
        run(broker)
    assert len(broker.sent) == 1


def test_session_closing_during_preflight_prevents_buy():
    broker = Broker()
    lookup = broker.order_by_client_id
    def close_during_lookup(identifier):
        broker.open = False
        return lookup(identifier)
    broker.order_by_client_id = close_during_lookup
    with pytest.raises(RuntimeError, match="open regular session"):
        run(broker)
    assert broker.sent == []


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_balances_rejected(value):
    broker = Broker()
    account = broker.account()
    broker.account = lambda: {**account, "cash": value}
    with pytest.raises(RuntimeError):
        run(broker)
    assert broker.sent == []
