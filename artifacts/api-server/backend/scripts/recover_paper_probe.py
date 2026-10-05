"""Inspect or recover a paper probe with confirmed cancellation and bounded exits."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import time
from uuid import UUID

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import StockPaperLedgerEvent
from app.services.stock_paper_ledger import ALPACA_PAPER_URL, AlpacaPaperClient, active_paper_account, StockPaperUnavailable
from scripts.test_alpaca_paper_roundtrip import PROBE_HALT, SYMBOLS, TERMINAL, _session_clock, validated_market_payload

EXIT_KEYS = ("sell", "sell-r1", "sell-r2")

def recover(client, run_id, account_id, events, emit, *, symbol="SPY", apply=False,
            now=None, sleep=time.sleep, clock=time.monotonic):
    if settings.allow_live_trading or client.base_url != ALPACA_PAPER_URL or symbol not in SYMBOLS:
        raise RuntimeError("Only live-disabled bounded Alpaca paper recovery is supported")
    prefix = "qa-" + UUID(run_id).hex
    account = client.account()
    if account.get("id") != account_id:
        raise RuntimeError("Paper account identity mismatch")
    intents = {}
    identifiers = {prefix + "-" + key: key for key in ("buy", *EXIT_KEYS)}
    for event in events:
        if event.get("event") != "before_submit":
            continue
        payload = event["payload"]
        side = payload.get("side")
        key = identifiers.get(payload.get("client_order_id"))
        if (key is None or key in intents or side != ("buy" if key == "buy" else "sell")
                or payload.get("symbol") != symbol
                or payload.get("type") != "market" or payload.get("time_in_force") != "day"
                or payload.get("extended_hours") is not False):
            raise RuntimeError("Journal order identity or scope is invalid")
        if side == "buy" and (payload.get("notional") != "10" or "qty" in payload):
            raise RuntimeError("Journal buy exceeds the bounded probe contract")
        intents[key] = payload
    exits = [key for key in intents if key != "buy"]
    if exits != list(EXIT_KEYS[:len(exits)]):
        raise RuntimeError("Exit journal has missing or out-of-order attempts")
    seen = {}

    def lookup(key):
        row = client.order_by_client_id(prefix + "-" + key)
        if row is not None:
            side = "buy" if key == "buy" else "sell"
            if (not row.get("id") or row.get("client_order_id") != prefix + "-" + key
                    or row.get("symbol") != symbol or row.get("side") != side or key not in intents
                    or row.get("status") == "replaced"):
                raise RuntimeError("Broker order does not match the durable journal")
            filled = qty(row)
            if key in seen and (seen[key][0] != row["id"] or filled < seen[key][1]):
                raise RuntimeError("Broker order identity or cumulative fills changed inconsistently")
            if any(other != key and value[0] == row["id"] for other, value in seen.items()):
                raise RuntimeError("Distinct client orders share a broker ID")
            seen[key] = (row["id"], filled)
            if side == "sell" and filled > Decimal(intents[key]["qty"]):
                raise RuntimeError("Exit reports more fills than requested")
        return row

    def qty(row):
        value = Decimal(row["filled_qty"])
        if not value.is_finite() or value < 0:
            raise RuntimeError("Invalid confirmed fill quantity")
        return value

    def permissions():
        current = client.account()
        if (current.get("id") != account_id or current.get("status") != "ACTIVE" or current.get("currency") != "USD"
                or any(current.get(key) is not False for key in (
                    "trading_blocked", "account_blocked", "trade_suspended_by_user"))):
            raise RuntimeError("Explicit account permissions are required for recovery")

    def no_unrelated_orders():
        owned = {prefix + "-" + key for key in intents}
        if any(row.get("status") not in TERMINAL and row.get("client_order_id") not in owned for row in client.orders()):
            raise RuntimeError("Unrelated outstanding orders prevent recovery")

    def cancel_and_confirm(key, row):
        permissions()
        emit({"event": "before_cancel", "client_order_id": prefix + "-" + key, "broker_order_id": row["id"]})
        try:
            client.cancel_order(row["id"])
        except StockPaperUnavailable:
            emit({"event": "cancel_response_uncertain", "client_order_id": prefix + "-" + key})
        deadline = clock() + 90
        while clock() < deadline:
            current = lookup(key)
            if current and current.get("status") in TERMINAL:
                emit({"event": "cancel_terminal_observed", "client_order_id": prefix + "-" + key,
                      "status": current["status"], "filled_qty": str(qty(current))})
                return current
            sleep(2)
        return None

    buy, sell = lookup("buy"), lookup("sell")
    no_unrelated_orders()
    result = {"paper_only": True, "symbol": symbol, "run_id": str(UUID(run_id)),
              "costs_verified": False, "automatic_resume": False, "buy_submitted": False}
    if buy is None:
        if client.positions() or sell or exits:
            raise RuntimeError("Inventory or sell exists without a confirmed probe buy")
        return {**result, "status": "buy_outcome_uncertain" if "buy" in intents else "not_submitted"}
    if buy.get("status") not in TERMINAL:
        if not apply:
            return {**result, "status": "buy_pending", "confirmed_buy_quantity": str(qty(buy))}
        if exits:
            raise RuntimeError("Pending buy with attempted exits requires separate review")
        buy = cancel_and_confirm("buy", buy)
        if buy is None:
            return {**result, "status": "buy_cancel_uncertain"}
    bought = qty(buy)
    sold = Decimal("0")
    for key in exits:
        requested = Decimal(intents[key]["qty"])
        if not requested.is_finite() or requested <= 0 or requested != bought-sold:
            raise RuntimeError("Sell quantity is not bound to the confirmed probe buy")
        sell = lookup(key)
        if sell is None:
            return {**result, "status": "sell_outcome_uncertain", "client_order_id": prefix + "-" + key}
        if sell.get("status") not in TERMINAL:
            if not apply:
                return {**result, "status": "sell_pending"}
            sell = cancel_and_confirm(key, sell)
            if sell is None:
                return {**result, "status": "sell_cancel_uncertain", "client_order_id": prefix + "-" + key}
        sold += qty(sell)
    positions = client.positions()
    remaining = bought-sold
    if bought == 0:
        if positions:
            raise RuntimeError("Unexpected inventory without a probe fill")
        return {**result, "status": "no_fill"}
    if remaining == 0:
        return {**result, "status": "residual_requires_review" if positions else "observed_flat", "confirmed_quantity": str(bought)}
    if (len(positions) != 1 or positions[0].get("symbol") != symbol
            or Decimal(positions[0]["qty"]) != remaining):
        raise RuntimeError("Position is not exactly attributable to the confirmed probe fill")
    result.update(status="exit_required", confirmed_quantity=str(bought), remaining_quantity=str(remaining))
    if len(exits) == len(EXIT_KEYS):
        return {**result, "status": "exit_attempt_limit"}
    if not apply:
        return result
    key = EXIT_KEYS[len(exits)]
    lookup(key)  # An unjournaled broker order is not safe to replace or adopt.
    permissions()
    no_unrelated_orders()
    _session_clock(client, now)
    payload = {"symbol": symbol, "side": "sell", "type": "market", "time_in_force": "day",
               "client_order_id": prefix + "-" + key, "extended_hours": False, "qty": str(remaining)}
    payload = validated_market_payload(payload)
    # The caller fsyncs this intent before the one permitted POST.
    emit({"event": "before_submit", "payload": payload})
    intents[key] = payload
    try:
        client.submit_order(payload)
    except StockPaperUnavailable:
        emit({"event": "submit_response_uncertain", "client_order_id": payload["client_order_id"]})
    deadline = clock() + 90
    while clock() < deadline:
        sell = lookup(key)
        if sell and sell.get("status") in TERMINAL:
            result["remaining_quantity"] = str(remaining-qty(sell))
            if qty(sell) == remaining and not client.positions():
                result["status"] = "observed_flat"
            else:
                result["status"] = "residual_requires_review"
            emit({"event": "probe_recovery_observation", **result})
            return result
        sleep(2)
    result["status"] = "sell_outcome_uncertain"
    emit({"event": "probe_recovery_observation", **result})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--symbol", choices=SYMBOLS, default="SPY")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", choices=["SIMULATED_MONEY_ONLY"])
    args = parser.parse_args()
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Only a live-disabled Alpaca paper deployment is supported")
    if args.apply and args.confirm != "SIMULATED_MONEY_ONLY":
        parser.error("Applying requires --confirm SIMULATED_MONEY_ONLY")
    path = Path("/data/reports") / f"paper-roundtrip-{args.run_id}.jsonl"
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(stream.fileno()).st_size > 1_000_000:
            raise RuntimeError("Probe journal is unexpectedly large")
        content = stream.read()
        if content and not content.endswith("\n"):
            raise RuntimeError("Incomplete journal tail requires review before recovery")
        events = [json.loads(line) for line in content.splitlines() if line.strip()]
        with SessionLocal() as db:
            account = active_paper_account(db, for_update=True)
            if account is None:
                raise RuntimeError("An existing paper ledger is required")
            if args.apply:
                reservation = db.query(StockPaperLedgerEvent).filter_by(account_id=account.id,
                    event_type="paper_probe_reservation").order_by(StockPaperLedgerEvent.id.desc()).first()
                if (not reservation or reservation.payload.get("run_id") != str(args.run_id)
                        or reservation.payload.get("symbol") != args.symbol
                        or account.status != "halted" or account.halt_reason != PROBE_HALT):
                    raise RuntimeError("The exact reserved probe halt is required for an exit")
            def emit(event):
                stream.write(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            result = recover(AlpacaPaperClient(), str(args.run_id), account.broker_account_id,
                             events, emit, symbol=args.symbol, apply=args.apply)
            print(json.dumps(result))


if __name__ == "__main__":
    main()
