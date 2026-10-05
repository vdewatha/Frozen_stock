from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.stock_paper_ledger import StockPaperUnavailable
from tests.test_paper_probe_recovery import interrupted, recover, PREFIX


@pytest.mark.parametrize("outcome", ["canceled", "filled", "lost_ack"])
def test_cancel_confirm_then_exit_final_buy_fill(interrupted, outcome):
    broker, events = interrupted
    buy = broker.rows[PREFIX + "-buy"]
    buy.update(status="partially_filled", filled_qty="0.01")
    broker.held[0]["qty"] = "0.01"
    canceled = []
    def cancel(identifier):
        canceled.append(identifier)
        buy.update(status="filled" if outcome == "filled" else "canceled", filled_qty="0.014356789")
        broker.held[0]["qty"] = "0.014356789"
        if outcome == "lost_ack":
            raise StockPaperUnavailable("Lost cancel response")
    broker.cancel_order = cancel
    assert recover(interrupted, apply=True)["status"] == "observed_flat"
    assert canceled == [buy["id"]]
    assert len(broker.sent) == 1 and broker.sent[0]["side"] == "sell"
    assert broker.sent[0]["qty"] == "0.014356789"
    assert [e["event"] for e in events].index("before_cancel") < [e["event"] for e in events].index("before_submit", 1)


@pytest.mark.parametrize("missing", [False, True])
def test_cancel_acknowledgment_alone_never_allows_exit(interrupted, missing):
    broker, _ = interrupted
    broker.rows[PREFIX + "-buy"]["status"] = "pending_cancel"
    def cancel(_):
        if missing:
            broker.rows.clear()
    broker.cancel_order = cancel
    assert recover(interrupted, apply=True)["status"] == "buy_cancel_uncertain"
    assert not broker.sent


def test_pending_sell_cancel_uses_final_fill_not_old_snapshot(interrupted):
    broker, events = interrupted
    payload = {**events[0]["payload"], "side": "sell", "client_order_id": PREFIX + "-sell", "qty": "0.014356789"}
    payload.pop("notional")
    events.append({"event": "before_submit", "payload": payload})
    sell = {**payload, "id": "original-sell", "status": "partially_filled", "filled_qty": "0.005"}
    broker.rows[PREFIX + "-sell"] = sell
    broker.held[0]["qty"] = "0.009356789"
    def cancel(identifier):
        assert identifier == "original-sell"
        sell.update(status="canceled", filled_qty="0.01")
        broker.held[0]["qty"] = "0.004356789"
    broker.cancel_order = cancel
    assert recover(interrupted, apply=True)["status"] == "observed_flat"
    assert broker.sent[0]["qty"] == "0.004356789"
    assert broker.sent[0]["client_order_id"] == PREFIX + "-sell-r1"


def test_pending_sell_cancel_timeout_never_replaces(interrupted):
    broker, events = interrupted
    payload = {**events[0]["payload"], "side": "sell", "client_order_id": PREFIX + "-sell", "qty": "0.014356789"}
    payload.pop("notional")
    events.append({"event": "before_submit", "payload": payload})
    broker.rows[PREFIX + "-sell"] = {**payload, "id": "sell", "status": "pending_cancel", "filled_qty": "0"}
    broker.cancel_order = lambda _: None
    assert recover(interrupted, apply=True)["status"] == "sell_cancel_uncertain"
    assert not broker.sent


def test_three_exit_attempt_limit_stops_loop(interrupted):
    broker, events = interrupted
    for key in ("sell", "sell-r1", "sell-r2"):
        payload = {**events[0]["payload"], "side": "sell", "client_order_id": PREFIX + "-" + key, "qty": "0.014356789"}
        payload.pop("notional")
        events.append({"event": "before_submit", "payload": payload})
        broker.rows[payload["client_order_id"]] = {**payload, "id": key, "status": "canceled", "filled_qty": "0"}
    assert recover(interrupted, apply=True)["status"] == "exit_attempt_limit"
    assert not broker.sent


@pytest.mark.parametrize("case", ["decreased_fill", "changed_id", "replaced"])
def test_changed_cancel_evidence_never_allows_an_exit(interrupted, case):
    broker, _ = interrupted
    buy = broker.rows[PREFIX + "-buy"]
    buy["status"] = "partially_filled"
    def cancel(_):
        buy["status"] = "canceled"
        if case == "decreased_fill":
            buy["filled_qty"] = "0"
        elif case == "changed_id":
            buy["id"] = "different"
        else:
            buy["status"] = "replaced"
    broker.cancel_order = cancel
    with pytest.raises(RuntimeError):
        recover(interrupted, apply=True)
    assert not broker.sent
