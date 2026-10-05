from copy import deepcopy
from datetime import datetime, timezone
import fcntl
from types import SimpleNamespace

import pytest

from app.core.config import settings
from scripts import recover_paper_probe as recovery
from scripts.test_alpaca_paper_roundtrip import validated_market_payload
from tests.test_paper_roundtrip_probe import Broker, RUN

NOW = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)
PREFIX = "qa-" + RUN.replace("-", "")


@pytest.fixture
def interrupted(monkeypatch):
    monkeypatch.setattr(settings, "allow_live_trading", False)
    monkeypatch.setattr(settings, "active_paper_broker", "alpaca_paper")
    broker = Broker()
    payload = {"symbol": "SPY", "side": "buy", "type": "market", "time_in_force": "day",
               "client_order_id": PREFIX + "-buy", "extended_hours": False, "notional": "10"}
    broker.submit_order(payload)
    broker.sent.clear()
    return broker, [{"event": "before_submit", "payload": payload}]


def recover(state, apply=False):
    broker, events = state
    timer = SimpleNamespace(now=0)
    return recovery.recover(broker, RUN, "paper-test", events, events.append, apply=apply, now=NOW,
        clock=lambda: timer.now, sleep=lambda n: setattr(timer, "now", timer.now+n))


def test_default_inspection_never_submits(interrupted):
    assert recover(interrupted)["status"] == "exit_required"
    assert interrupted[0].sent == []
    assert len(interrupted[1]) == 1


@pytest.mark.parametrize("lost_ack", [False, True])
def test_exit_only_once_and_repeat_recovery_is_read_only(interrupted, lost_ack):
    broker, events = interrupted
    broker.lost_ack = lost_ack
    result = recover(interrupted, apply=True)
    assert result["status"] == "observed_flat"
    assert not result["costs_verified"] and not result["automatic_resume"]
    assert len(broker.sent) == 1 and broker.sent[0]["side"] == "sell"
    assert broker.sent[0]["qty"] == "0.014356789"
    assert events[1]["event"] == "before_submit"
    assert recover(interrupted, apply=True)["status"] == "observed_flat"
    assert len(broker.sent) == 1


def test_unknown_exit_response_never_reposts(interrupted):
    broker, events = interrupted
    broker.fill = False
    broker.lost_ack = True
    assert recover(interrupted, apply=True)["status"] == "sell_outcome_uncertain"
    assert recover(interrupted, apply=True)["status"] == "sell_outcome_uncertain"
    assert len(broker.sent) == 1


def test_unseen_buy_with_durable_intent_is_not_assumed_unsubmitted(interrupted):
    broker, _ = interrupted
    broker.rows.clear()
    broker.held.clear()
    assert recover(interrupted, apply=True)["status"] == "buy_outcome_uncertain"
    assert not broker.sent


def test_pending_buy_is_not_exited_or_repeated(interrupted):
    broker, _ = interrupted
    broker.rows[PREFIX + "-buy"]["status"] = "partially_filled"
    assert recover(interrupted)["status"] == "buy_pending"
    assert not broker.sent


@pytest.mark.parametrize("case", ["wrong_account", "wrong_symbol", "wrong_side", "position_quantity",
                                  "other_position", "other_order", "replaced", "duplicate_intent", "live", "blocked", "closed"])
def test_unsafe_recovery_does_not_submit(interrupted, monkeypatch, case):
    broker, events = interrupted
    if case == "wrong_account":
        broker.account = lambda: {"id": "other"}
    elif case in {"wrong_symbol", "wrong_side", "replaced"}:
        key, value = {"wrong_symbol": ("symbol", "AAPL"), "wrong_side": ("side", "sell"), "replaced": ("status", "replaced")}[case]
        broker.rows[PREFIX + "-buy"][key] = value
    elif case == "position_quantity":
        broker.held[0]["qty"] = "1"
    elif case == "other_position":
        broker.held.append({"symbol": "AAPL", "qty": "1"})
    elif case == "other_order":
        broker.orders = lambda: [{"status": "new", "client_order_id": "other"}]
    elif case == "duplicate_intent":
        events.append(deepcopy(events[0]))
    elif case == "live":
        monkeypatch.setattr(settings, "allow_live_trading", True)
    elif case == "blocked":
        account = broker.account()
        broker.account = lambda: {**account, "trading_blocked": True}
    elif case == "closed":
        broker.open = False
    with pytest.raises(RuntimeError):
        recover(interrupted, apply=True)
    assert not broker.sent


def test_partial_terminal_exit_closes_only_remainder_with_new_id(interrupted):
    broker, events = interrupted
    payload = {**events[0]["payload"], "side": "sell", "client_order_id": PREFIX + "-sell",
               "qty": "0.014356789"}
    payload.pop("notional")
    events.append({"event": "before_submit", "payload": payload})
    broker.rows[PREFIX + "-sell"] = {**payload, "id": "sell", "status": "canceled", "filled_qty": "0.01"}
    broker.held[0]["qty"] = "0.004356789"
    assert recover(interrupted, apply=True)["status"] == "observed_flat"
    assert len(broker.sent) == 1
    assert broker.sent[0]["client_order_id"] == PREFIX + "-sell-r1"
    assert broker.sent[0]["qty"] == "0.004356789"
    assert recover(interrupted, apply=True)["status"] == "observed_flat"
    assert len(broker.sent) == 1


def test_cli_refuses_journal_lock_contention_before_broker_access(tmp_path, monkeypatch, interrupted):
    path = tmp_path / f"paper-roundtrip-{RUN}.jsonl"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(recovery, "Path", lambda _: tmp_path)
    monkeypatch.setattr("sys.argv", ["recover", "--run-id", RUN])
    with path.open("r+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            recovery.main()


def test_official_sdk_validation_keeps_exact_wire_quantity():
    payload = {"symbol": "SPY", "side": "sell", "type": "market", "time_in_force": "day",
               "qty": "0.013060394", "client_order_id": PREFIX + "-sell", "extended_hours": False}
    assert validated_market_payload(payload)["qty"] == "0.013060394"
    with pytest.raises(ValueError):
        validated_market_payload({**payload, "notional": "10"})
