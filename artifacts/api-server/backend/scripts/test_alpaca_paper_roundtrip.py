"""Explicit, bounded simulated-money broker integration probe; no live authority."""
import argparse
import fcntl
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import time
from uuid import UUID
from alpaca.trading.requests import MarketOrderRequest

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import StockPaperLedgerEvent, StockPaperOrder, StockPaperPosition
from app.services.stock_paper_ledger import (
    ALPACA_PAPER_URL, AlpacaPaperClient, StockPaperUnavailable, active_paper_account,
    _event, _halt, _mark_accounting_review_required,
    PROBE_HALT,
)

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
SYMBOLS = ("AAPL", "MSFT", "QQQ", "SPY")


def validated_market_payload(payload):
    # Use the installed official SDK validator, retaining exact decimal strings
    # on the wire rather than serializing its float-valued request model.
    MarketOrderRequest(**payload)
    return payload


def reserve_probe(db, client, run_id, *, symbol="SPY", now=None):
    """Serialize with normal order reservations and commit the stop before POST."""
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Probe requires a live-disabled Alpaca paper deployment")
    run_id = str(UUID(run_id))
    account = active_paper_account(db, for_update=True)
    if (not account or account.reconciliation_required
            or account.unexplained_residual):
        raise RuntimeError("An existing reconciled paper ledger is required")
    observation = None
    if account.status != "reconciled":
        from app.services.paper_research_accounting import assess
        observation = assess(db, account)
        if observation["accounting_observation_ready"] is not True:
            raise RuntimeError("An existing reconciled or verified research ledger is required")
    if (db.query(StockPaperPosition).filter_by(account_id=account.id).count()
            or db.query(StockPaperOrder).filter(StockPaperOrder.account_id == account.id,
                (StockPaperOrder.status.notin_(TERMINAL)) | StockPaperOrder.uncertain_submission.is_(True)).count()):
        raise RuntimeError("Persisted positions or uncertain/pending orders require reconciliation")
    if db.query(StockPaperLedgerEvent).filter(
        StockPaperLedgerEvent.account_id == account.id,
        StockPaperLedgerEvent.event_type == "paper_probe_reservation",
        StockPaperLedgerEvent.payload["run_id"].as_string() == run_id,
    ).first():
        raise RuntimeError("Probe run already reserved; inspect its journal instead of restarting")
    preflight(client, account.broker_account_id, symbol=symbol, now=now)
    account_id = account.broker_account_id
    _halt(account, PROBE_HALT)
    _mark_accounting_review_required(db)
    db.info["stock_paper_actor"] = "explicit-paper-integration-probe"
    _event(db, account, "paper_probe_reservation", "reserved", PROBE_HALT,
           {"run_id": run_id, "symbol": symbol, "buy_notional_cap": "10", "paper_only": True,
            "live_authorized": False, "automatic_resume": False,
            "scope": "explicit_bounded_integration_probe", "costs_verified": False,
            "research_accounting_sha256": observation["report_sha256"] if observation else None})
    db.commit()
    return account_id


def _session_clock(client, now=None):
    session = client._request("GET", "/v2/clock")
    close = datetime.fromisoformat(session["next_close"])
    observed = datetime.fromisoformat(session["timestamp"])
    if now is None:
        now = datetime.now(timezone.utc)
        # Small inter-host skew must elapse, not be accepted as future evidence.
        ahead = (observed-now).total_seconds() if observed.tzinfo is not None else 0
        if 0 < ahead <= 1:
            time.sleep(ahead + 0.01)
            now = datetime.now(timezone.utc)
    if (now.tzinfo is None or observed.tzinfo is None or close.tzinfo is None
            or not 0 <= (now-observed).total_seconds() <= 30):
        raise RuntimeError("A fresh timezone-aware broker clock is required")
    if session.get("is_open") is not True or (close-observed).total_seconds() < 300:
        raise RuntimeError("An open regular session with five minutes remaining is required")
    return observed


def preflight(client, account_id, *, symbol="SPY", now=None):
    if client.base_url != ALPACA_PAPER_URL:
        raise RuntimeError("Only the immutable Alpaca paper endpoint is allowed")
    if symbol not in SYMBOLS:
        raise RuntimeError("Symbol is outside the bounded paper probe universe")
    account = client.account()
    asset = client._request("GET", "/v2/assets/" + symbol)
    if (account.get("id") != account_id or account.get("status") != "ACTIVE"
            or account.get("currency") != "USD"
            or any(account.get(key) is not False for key in (
                "trading_blocked", "account_blocked", "trade_suspended_by_user"))
            or any(not Decimal(account[key]).is_finite() for key in ("cash", "buying_power"))
            or Decimal(account["cash"]) < 10 or Decimal(account["buying_power"]) < 10
            or asset.get("symbol") != symbol or asset.get("status") != "active"
            or asset.get("class") != "us_equity"
            or asset.get("tradable") is not True or asset.get("fractionable") is not True):
        raise RuntimeError("Paper account, cash, asset, or regular-session preflight failed")
    if client.positions() or any(row["status"] not in TERMINAL for row in client.orders()):
        raise RuntimeError("Probe requires an empty account with no outstanding orders")
    observed = _session_clock(client, now)
    return {"paper_only": True, "symbol": symbol, "buy_notional_cap": "10",
            "broker_clock": observed.isoformat(), "ready": True}


def execute(client, run_id, account_id, emit, *, symbol="SPY", sleep=time.sleep, clock=time.monotonic, now=None):
    preflight(client, account_id, symbol=symbol, now=now)
    prefix = "qa-" + UUID(run_id).hex
    for side in ("buy", "sell"):
        if client.order_by_client_id(prefix + "-" + side) is not None:
            raise RuntimeError("Probe order ID already exists; refusing duplicate execution")

    def wait(identifier, side, seconds):
        deadline = clock() + seconds
        while clock() < deadline:
            row = client.order_by_client_id(identifier)
            if row is not None:
                if (row.get("client_order_id") != identifier or row.get("symbol") != symbol
                        or row.get("side") != side or not row.get("id")):
                    raise RuntimeError("Broker order identity mismatch")
                if row.get("status") in TERMINAL:
                    return row
            sleep(2)
        raise RuntimeError("Order outcome remains uncertain; inspect the recorded client ID, do not resubmit")

    def submit(side, quantity=None):
        if side == "buy":
            _session_clock(client, now)
        identifier = prefix + "-" + side
        payload = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day",
                   "client_order_id": identifier, "extended_hours": False}
        payload.update({"notional": "10"} if side == "buy" else {"qty": str(quantity)})
        payload = validated_market_payload(payload)
        emit({"event": "before_submit", "payload": payload})
        try:
            client.submit_order(payload)
        except StockPaperUnavailable:
            emit({"event": "submit_response_uncertain", "client_order_id": identifier})
        # Never retry POST: resolve even a lost acknowledgment by client ID.
        row = wait(identifier, side, 90)
        emit({"event": "terminal_order", **{key: row.get(key) for key in (
            "id", "client_order_id", "symbol", "side", "status", "filled_qty", "filled_avg_price", "filled_at",
        )}})
        return row

    buy = submit("buy")
    quantity = Decimal(buy["filled_qty"])
    if not quantity.is_finite() or quantity <= 0:
        raise RuntimeError("Buy had no confirmed fill; no sell submitted")
    for _ in range(15):
        positions = client.positions()
        if (len(positions) == 1 and positions[0].get("symbol") == symbol
                and Decimal(positions[0]["qty"]) == quantity):
            break
        sleep(2)
    else:
        raise RuntimeError("Position has not matched this test's confirmed fill; no sell submitted")
    sell = submit("sell", quantity)
    if Decimal(sell["filled_qty"]) != quantity:
        raise RuntimeError("Sell did not close the confirmed test quantity; manual review required")
    for _ in range(15):
        if not client.positions():
            break
        sleep(2)
    else:
        raise RuntimeError("Broker still reports a position after the sell")
    result = {"event": "roundtrip_complete", "paper_only": True, "symbol": symbol, "buy_notional_cap": "10",
              "quantity": str(quantity), "open_positions": 0, "costs_verified": False,
              "buy_order_id": buy["id"], "sell_order_id": sell["id"]}
    emit(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--confirm", required=True, choices=["SIMULATED_MONEY_ONLY"])
    parser.add_argument("--symbol", choices=SYMBOLS, default="SPY")
    args = parser.parse_args()
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Probe requires a live-disabled Alpaca paper deployment")
    path = Path("/data/reports") / f"paper-roundtrip-{args.run_id}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def emit(event):
            stream.write(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            print(json.dumps(event), flush=True)
        try:
            client = AlpacaPaperClient()
            with SessionLocal() as db:
                account_id = reserve_probe(db, client, str(args.run_id), symbol=args.symbol)
            emit({"event": "probe_reserved", "run_id": str(args.run_id), "symbol": args.symbol,
                  "paper_only": True, "automatic_resume": False})
            execute(client, str(args.run_id), account_id, emit, symbol=args.symbol)
        except Exception as exc:
            emit({"event": "probe_stopped", "error_type": type(exc).__name__, "review_required": True})
            raise


if __name__ == "__main__":
    main()
