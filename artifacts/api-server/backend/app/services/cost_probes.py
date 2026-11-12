"""Cost probes: a few tiny paper round trips a week so the cost cells keep filling.

Once the seven broad technical strategies stop trading (the feasibility
report's step 2 moves them to shadow), the paper account would stop producing
fills and the cost calibration (``paper_cost_calibration``) and the yardstick's
measured costs would freeze.  A probe is one share (``cost_probe_quantity``,
capped at ``cost_probe_max_notional`` dollars) of a liquid ETF or large cap,
bought with a market order mid-session and sold ``cost_probe_hold_minutes``
later the same session (or at the next session's first probe tick if the
session ended first).  Each completed round trip is recorded with its
measured cost: each side's fill against the 1-minute close it was decided on,
plus the reported fees (``probe_round_trips``).

Paper only, and kept out of everything that learns from trades:

* orders carry their own source (``stock_paper_ledger.PROBE_SOURCE``) and
  client-order-id prefix (``sp-probe-``); the scorecard labels them
  ``probe``, so trade attribution, the loss diagnosis and every scoreboard
  (which read only ``scheduled`` / ``exploration`` lots) ignore them;
* they go through the existing reservation and dispatch plumbing and its
  idempotency keys (``cost-probe:{session}:{symbol}:buy`` / ``:sell``), the
  launch-admission gates, the kill switch, the reconciliation requirement and
  the risk rule's cash, symbol-exposure and open-position limits;
* a probe opens only on a symbol with no position and no open order, only
  while at least ``FREE_SLOTS`` open-position slots are free, one at a time,
  and only between ``START_WINDOW`` (10:30-14:00 ET), so it cannot crowd a
  strategy entry out of the book.

``cost_probe_per_week`` (default 3, 0 disables) spreads the week's probes over
the weekdays; ``cost_probe_symbols`` is the rotation (filtered to the paper
execution universe), least recently probed first.
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
EVENT = "cost_probe"
ACTION_BUY = "buy"
ACTION_SELL = "sell"
ACTION_ROUND_TRIP = "round_trip"
ACTION_CLOSED_ELSEWHERE = "closed_elsewhere"
START_WINDOW = (time(10, 30), time(14, 0))
SELL_BY = time(15, 30)
FREE_SLOTS = 2
LOOKBACK_DAYS = 14
# The last tick and the last tick that wanted to probe but could not, kept in
# Redis so the yardstick shows why no probe ran (on 2026-11-09 every tick was
# refused by launch admission and nothing recorded why).
LAST_TICK_KEY = "cost-probe:last-tick"
LAST_HELD_BACK_KEY = "cost-probe:last-held-back"
TICK_TTL_SECONDS = 14 * 24 * 60 * 60
ROUTINE_SKIP_PREFIXES = (
    "outside the regular session",
    "outside the probe start window",
    "next probe is planned",
    "today's probe already ran",
    "probes run on the Alpaca paper account only",
)


def probe_symbols() -> list[str]:
    """The rotation, kept to symbols the paper path can trade."""
    from app.services.trading_universe import execution_symbols

    tradable = set(execution_symbols())
    return [s.strip().upper() for s in settings.cost_probe_symbols if s.strip().upper() in tradable]


def probe_key(session: str, symbol: str, side: str) -> str:
    return f"cost-probe:{session}:{symbol.upper()}:{side}"


def probe_client_order_id(key: str) -> str:
    from app.services.stock_paper_ledger import PROBE_CLIENT_PREFIX

    return PROBE_CLIENT_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:45]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _audit(db: Session, action: str, payload: dict, message: str) -> None:
    from app.services.audit import write_audit_log

    write_audit_log(db, event_type=EVENT, entity_type="stock_paper_order", entity_id=payload.get("buy_order_id"),
                    action=action, status="complete", message=message, payload={**payload, "paper_only": True})


def _rows(db: Session, action: str) -> list:
    from app.models import AuditLog

    return db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == action).order_by(
        AuditLog.id).all()


def _fills(db: Session, order_id: Optional[int]) -> dict:
    from app.models.stock_paper import StockPaperFill

    if order_id is None:
        return {"quantity": Decimal("0"), "price": None, "fee": None, "filled_at": None}
    fills = db.query(StockPaperFill).filter_by(order_id=order_id).order_by(StockPaperFill.filled_at).all()
    quantity = sum((f.quantity for f in fills), Decimal("0"))
    if quantity <= 0:
        return {"quantity": Decimal("0"), "price": None, "fee": None, "filled_at": None}
    price = sum((f.quantity * f.price for f in fills), Decimal("0")) / quantity
    fees = [f.fee for f in fills if f.fee is not None]
    return {"quantity": quantity, "price": price, "fee": sum(fees, Decimal("0")) if fees else None,
            "filled_at": _utc(fills[0].filled_at)}


def _latest_close(db: Session, symbol: str) -> Optional[Decimal]:
    from app.models import IntradayBar
    from app.services.intraday_data import MARKET_DATA_PROVIDER

    bar = db.query(IntradayBar).filter_by(symbol=symbol, timeframe="1m", provider=MARKET_DATA_PROVIDER).order_by(
        IntradayBar.opened_at.desc()).first()
    return Decimal(str(bar.close)) if bar is not None and bar.close and bar.close > 0 else None


def probes(db: Session, *, since: Optional[datetime] = None) -> list[dict]:
    """Every probe started (newest last) with its orders, fills and whether its round trip was recorded."""
    from app.models.stock_paper import StockPaperOrder

    recorded = {row.payload.get("buy_key") for row in _rows(db, ACTION_ROUND_TRIP) if row.payload}
    closed = {row.payload.get("buy_key") for row in _rows(db, ACTION_CLOSED_ELSEWHERE) if row.payload}
    out = []
    for row in _rows(db, ACTION_BUY):
        payload = dict(row.payload or {})
        started = payload.get("started_at")
        if since is not None and started and datetime.fromisoformat(started) < since:
            continue
        buy = db.get(StockPaperOrder, payload.get("buy_order_id")) if payload.get("buy_order_id") else None
        sell = db.query(StockPaperOrder).filter_by(
            client_order_id=probe_client_order_id(payload["sell_key"])).one_or_none()
        out.append({**payload, "buy": buy, "sell": sell, "recorded": payload.get("buy_key") in recorded,
                    "closed_elsewhere": payload.get("buy_key") in closed})
    return out


def open_probe_symbols(db: Session) -> set[str]:
    """Symbols whose probe share is bought and not yet sold or recorded.

    A probe only opens on a symbol the account does not hold, so the whole
    position is the probe's: the protective-exit re-arm must leave it alone, or
    its resting take-profit commits the share and the probe's own sell is
    refused as a short (2026-11-10, QQQ).
    """
    out = set()
    for probe in probes(db):
        if probe["recorded"] or probe["closed_elsewhere"] or probe["buy"] is None:
            continue
        if probe["sell"] is not None and str(probe["sell"].status) == "filled":
            continue
        if _fills(db, probe["buy"].id)["quantity"] > 0:
            out.add(str(probe["symbol"]).upper())
    return out


def _week_start(now: datetime) -> datetime:
    local = now.astimezone(ET)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime.combine(monday, time(0, 0), ET).astimezone(UTC)


def planned_weekdays(per_week: int) -> list[int]:
    """The weekday (0 = Monday) by which the k-th probe of the week may start: spread evenly over Mon-Fri."""
    per_week = max(int(per_week), 0)
    return [min(4, (k * 5) // per_week) for k in range(per_week)] if per_week else []


def measure_round_trip(buy_order, sell_order, buy_fill: dict, sell_fill: dict) -> dict:
    """Each side against its decision close, plus fees: the probe's measured round-trip cost in bps."""
    def f(value) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    buy_ref, sell_ref = f(buy_order.limit_price), f(sell_order.limit_price)
    buy_px, sell_px = f(buy_fill["price"]), f(sell_fill["price"])
    if not all(v and v > 0 for v in (buy_ref, sell_ref, buy_px, sell_px)):
        return {"round_trip_bps": None, "reason": "a fill or decision price is missing"}
    quantity = f(min(buy_fill["quantity"], sell_fill["quantity"])) or 0.0
    buy_slip = (buy_px / buy_ref - 1) * 10_000
    sell_slip = (1 - sell_px / sell_ref) * 10_000
    fees = [v for v in (f(buy_fill["fee"]), f(sell_fill["fee"])) if v is not None]
    notional = buy_px * quantity
    fee_bps = (sum(fees) / notional * 10_000) if fees and notional > 0 else 0.0
    return {"buy_slippage_bps": round(buy_slip, 4), "sell_slippage_bps": round(sell_slip, 4),
            "fee_bps": round(fee_bps, 4), "fee_known": len(fees) == 2,
            "round_trip_bps": round(buy_slip + sell_slip + fee_bps, 4),
            "price_move_bps": round((sell_px / buy_px - 1) * 10_000, 4),
            "buy_fill": buy_px, "sell_fill": sell_px, "buy_decision": buy_ref, "sell_decision": sell_ref,
            "quantity": quantity}


def _position_quantity(db: Session, account_id: int, symbol: str) -> Decimal:
    from app.models.stock_paper import StockPaperPosition

    row = db.query(StockPaperPosition).filter_by(account_id=account_id, symbol=symbol).one_or_none()
    return row.quantity if row is not None and row.quantity else Decimal("0")


def _book_room(db: Session, account) -> tuple[bool, str]:
    """At least ``FREE_SLOTS`` open-position slots free (positions plus pending entries)."""
    from app.models import RiskRule
    from app.models.stock_paper import StockPaperOrder, StockPaperPosition
    from app.services.risk import DEFAULT_RISK_RULES
    from app.services.stock_paper_ledger import NONTERMINAL_ORDER_STATUSES

    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    rules = DEFAULT_RISK_RULES | (rule.value if rule else {})
    try:
        cap = int(rules.get("max_open_positions", 1))
    except (TypeError, ValueError):
        cap = 1
    held = {row.symbol for row in db.query(StockPaperPosition).filter(
        StockPaperPosition.account_id == account.id, StockPaperPosition.quantity > 0).all()}
    pending = {row.symbol for row in db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id, StockPaperOrder.side == "buy", StockPaperOrder.leg_role.is_(None),
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES)).all()}
    try:  # the slow book's holdings sit outside the slot caps (target_weight_book)
        from app.services.stock_paper_ledger import _book_symbols_or_empty

        outside = _book_symbols_or_empty(db)
    except ImportError:
        outside = set()
    used = len((held | pending) - outside)
    if used > cap - FREE_SLOTS:
        return False, f"{used} of {cap} open-position slots in use; a probe needs {FREE_SLOTS} free"
    return True, ""


def _eligible(db: Session, account, symbol: str) -> tuple[bool, str, Optional[Decimal]]:
    from app.models.stock_paper import StockPaperOrder
    from app.services.stock_paper_ledger import NONTERMINAL_ORDER_STATUSES

    if _position_quantity(db, account.id, symbol) > 0:
        return False, "symbol is held", None
    if db.query(StockPaperOrder).filter(StockPaperOrder.account_id == account.id, StockPaperOrder.symbol == symbol,
                                        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES)).count():
        return False, "symbol has an open order", None
    price = _latest_close(db, symbol)
    if price is None:
        return False, "no 1-minute reference bar", None
    if Decimal(int(settings.cost_probe_quantity)) * price > Decimal(str(settings.cost_probe_max_notional)):
        return False, f"one probe would cost more than ${settings.cost_probe_max_notional:g}", None
    return True, "", price


def _sell_awaits_dispatch(probe: Mapping) -> bool:
    """The probe's sell was reserved but never reached the broker (it waits for exit legs to cancel)."""
    sell = probe.get("sell")
    return sell is not None and str(sell.status) == "reserved" and not sell.broker_order_id


def _close_probe(db: Session, account, probe: dict, now: datetime) -> Optional[dict]:
    from app.services.stock_paper_ledger import (PROBE_SOURCE, StockPaperError, cancel_broker_exit_orders,
                                                 dispatch_reserved_order, live_exit_orders,
                                                 release_unsubmitted_reservation, reserve_stock_paper_order)

    buy_fill = _fills(db, probe["buy"].id if probe["buy"] else None)
    if buy_fill["quantity"] <= 0:
        return None
    symbol = probe["symbol"]
    held = _position_quantity(db, account.id, symbol)
    if held < buy_fill["quantity"]:
        _audit(db, ACTION_CLOSED_ELSEWHERE, {"buy_key": probe["buy_key"], "buy_order_id": probe["buy_order_id"],
                                             "symbol": symbol, "held": str(held)},
               f"cost probe {symbol}: position no longer holds the probe share; no round trip recorded")
        db.commit()
        return {"symbol": symbol, "status": "closed_elsewhere"}
    local = now.astimezone(ET)
    due = (now - buy_fill["filled_at"] >= timedelta(minutes=int(settings.cost_probe_hold_minutes))
           or local.time() >= SELL_BY or buy_fill["filled_at"].astimezone(ET).date() < local.date())
    if not due:
        return {"symbol": symbol, "status": "holding"}
    price = _latest_close(db, symbol)
    if price is None:
        return {"symbol": symbol, "status": "blocked", "reason": "no 1-minute reference bar"}
    # Exit legs resting on the probe's own share commit it, and the probe's
    # sell was then refused as a short on every tick: 51 times on 2026-11-11
    # for the QQQ probe of 11-10, whose share got an OCO from the exit re-arm
    # before the re-arm learned to skip probes.  When the whole position is the
    # probe's share, those legs are replaced the way the trading cycle replaces
    # them for an app-side exit.  A larger position belongs partly to a
    # strategy, whose protective legs are never touched here.
    exit_orders = live_exit_orders(db, account.id, symbol) if held == buy_fill["quantity"] else []
    extra = {"replacing_exit_order_ids": {row.id for row in exit_orders}} if exit_orders else {}
    try:
        order = reserve_stock_paper_order(db, symbol=symbol, side="sell", quantity=buy_fill["quantity"],
                                          reference_price=price, idempotency_key=probe["sell_key"],
                                          source=PROBE_SOURCE, **extra)
        if exit_orders:
            cancel = cancel_broker_exit_orders(db, exit_orders)
            if cancel["leg_filled"]:
                release_unsubmitted_reservation(
                    db, order.id, "Broker-side exit leg already filled; the probe's sell is not needed")
                _audit(db, ACTION_CLOSED_ELSEWHERE,
                       {"buy_key": probe["buy_key"], "buy_order_id": probe["buy_order_id"], "symbol": symbol,
                        "held": str(held), "closed_by": "exit_leg"},
                       f"cost probe {symbol}: a resting exit leg sold the probe share; no round trip recorded")
                db.commit()
                return {"symbol": symbol, "status": "closed_elsewhere"}
            if not cancel["clear"]:
                # The sell stays reserved; the next tick dispatches it once the broker confirms the cancel.
                return {"symbol": symbol, "status": "waiting_for_exit_cancel",
                        "cancelled_exit_order_ids": cancel["cancelled_order_ids"]}
        submitted = dispatch_reserved_order(db, order.id)
    except StockPaperError as exc:
        return {"symbol": symbol, "status": "blocked", "reason": str(exc)}
    _audit(db, ACTION_SELL, {"buy_key": probe["buy_key"], "buy_order_id": probe["buy_order_id"],
                             "sell_order_id": submitted.id, "symbol": symbol, "reference_price": str(price)},
           f"cost probe {symbol}: sell submitted ({submitted.status})")
    db.commit()
    return {"symbol": symbol, "status": "sell_submitted", "order_status": submitted.status}


def _record(db: Session, probe: dict) -> Optional[dict]:
    if probe["sell"] is None or probe["buy"] is None:
        return None
    buy_fill, sell_fill = _fills(db, probe["buy"].id), _fills(db, probe["sell"].id)
    if buy_fill["quantity"] <= 0 or sell_fill["quantity"] < buy_fill["quantity"]:
        return None
    from app.services.paper_cost_calibration import symbol_class

    measured = measure_round_trip(probe["buy"], probe["sell"], buy_fill, sell_fill)
    payload = {"buy_key": probe["buy_key"], "buy_order_id": probe["buy"].id, "sell_order_id": probe["sell"].id,
               "symbol": probe["symbol"], "symbol_class": symbol_class(probe["symbol"]),
               "session": probe.get("session"), "bought_at": buy_fill["filled_at"].isoformat(),
               "sold_at": sell_fill["filled_at"].isoformat(),
               "held_minutes": round((sell_fill["filled_at"] - buy_fill["filled_at"]).total_seconds() / 60, 1),
               **measured}
    _audit(db, ACTION_ROUND_TRIP, payload,
           f"cost probe {probe['symbol']}: round trip {measured.get('round_trip_bps')} bps")
    db.commit()
    return payload


def _open_probe(db: Session, account, now: datetime, history: list[dict]) -> dict:
    from app.services.stock_paper_ledger import (PROBE_SOURCE, StockPaperError, dispatch_reserved_order,
                                                 reserve_stock_paper_order)

    local = now.astimezone(ET)
    if not (START_WINDOW[0] <= local.time() < START_WINDOW[1]):
        return {"status": "skipped", "reason": "outside the probe start window (10:30-14:00 ET)"}
    week = [p for p in history if p.get("started_at") and datetime.fromisoformat(p["started_at"]) >= _week_start(now)]
    plan = planned_weekdays(int(settings.cost_probe_per_week))
    if len(week) >= len(plan):
        return {"status": "skipped", "reason": f"{len(week)} probes this week (target {len(plan)})"}
    if local.weekday() < plan[len(week)]:
        return {"status": "skipped", "reason": f"next probe is planned from weekday {plan[len(week)]}"}
    per_day = max(1, math.ceil(len(plan) / 5))
    today = [p for p in week if p.get("session") == local.date().isoformat()]
    if len(today) >= per_day:
        return {"status": "skipped", "reason": "today's probe already ran"}
    room, reason = _book_room(db, account)
    if not room:
        return {"status": "skipped", "reason": reason}
    last = {}
    for p in history:
        last[p["symbol"]] = p.get("started_at") or ""
    rotation = sorted(probe_symbols(), key=lambda s: (last.get(s, ""), probe_symbols().index(s)))
    skipped = []
    for symbol in rotation:
        ok, why, price = _eligible(db, account, symbol)
        if not ok:
            skipped.append({"symbol": symbol, "reason": why})
            continue
        session = local.date().isoformat()
        buy_key, sell_key = probe_key(session, symbol, "buy"), probe_key(session, symbol, "sell")
        try:
            order = reserve_stock_paper_order(db, symbol=symbol, side="buy",
                                              quantity=Decimal(int(settings.cost_probe_quantity)),
                                              reference_price=price, idempotency_key=buy_key, source=PROBE_SOURCE)
            submitted = dispatch_reserved_order(db, order.id)
        except StockPaperError as exc:
            return {"status": "blocked", "symbol": symbol, "reason": str(exc), "skipped": skipped}
        _audit(db, ACTION_BUY, {"buy_key": buy_key, "sell_key": sell_key, "buy_order_id": submitted.id,
                                "symbol": symbol, "session": session, "started_at": now.isoformat(),
                                "reference_price": str(price), "quantity": str(order.quantity)},
               f"cost probe {symbol}: buy submitted ({submitted.status})")
        db.commit()
        return {"status": "buy_submitted", "symbol": symbol, "order_status": submitted.status, "skipped": skipped}
    return {"status": "skipped", "reason": "no eligible probe symbol", "skipped": skipped}


def run_probe_cycle(db: Session, *, now: Optional[datetime] = None) -> dict:
    """One tick: record finished round trips, close a due probe, or open the week's next one (paper only)."""
    from app.services.intraday_data import session_bounds
    from app.services.stock_paper_ledger import active_paper_account, active_paper_broker_name

    now = _utc(now or datetime.now(UTC))
    base = {"paper_only": True, "live_authorized": False}
    if int(settings.cost_probe_per_week) <= 0:
        return base | {"status": "disabled"}
    if not active_paper_broker_name().startswith("alpaca"):
        return base | {"status": "skipped", "reason": "probes run on the Alpaca paper account only"}
    bounds = session_bounds(now.astimezone(ET).date())
    if bounds is None or not (bounds[0] <= now < bounds[1]):
        return base | {"status": "skipped", "reason": "outside the regular session"}
    account = active_paper_account(db)
    if account is None or account.status != "reconciled":
        return base | {"status": "skipped", "reason": "paper account is not reconciled"}
    history = probes(db, since=now - timedelta(days=LOOKBACK_DAYS))
    recorded = [r for r in (_record(db, p) for p in history if not p["recorded"] and not p["closed_elsewhere"]) if r]
    open_probes = [p for p in history if (p["sell"] is None or _sell_awaits_dispatch(p)) and not p["closed_elsewhere"]
                   and p["buy"] is not None and p["buy"].status not in {"cancelled", "rejected", "expired"}]
    if open_probes:
        result = _close_probe(db, account, open_probes[0], now) or {"status": "waiting_for_buy_fill",
                                                                     "symbol": open_probes[0]["symbol"]}
        return base | result | {"recorded": len(recorded)}
    if any(p["sell"] is not None and not p["recorded"] and not p["closed_elsewhere"] for p in history) and not recorded:
        return base | {"status": "waiting_for_sell_fill", "recorded": 0}
    return base | _open_probe(db, account, now, history) | {"recorded": len(recorded)}


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)


def held_back(result: dict) -> bool:
    """A tick that wanted to probe and could not (blocked, or skipped for a non-calendar reason)."""
    status = result.get("status")
    if status == "blocked":
        return True
    if status != "skipped":
        return False
    reason = str(result.get("reason") or "")
    if " probes this week (target " in reason:
        return False
    return not reason.startswith(ROUTINE_SKIP_PREFIXES)


def record_tick(result: dict, *, now: Optional[datetime] = None) -> None:
    """Keep the tick's outcome for probe_status; advisory, never raises."""
    import json

    stamp = _utc(now or datetime.now(UTC)).isoformat()
    payload = json.dumps({"at": stamp, **{k: v for k, v in result.items() if k not in {"paper_only", "live_authorized"}}},
                         default=str)[:4000]
    client = None
    try:
        client = _redis()
        client.set(LAST_TICK_KEY, payload, ex=TICK_TTL_SECONDS)
        if held_back(result):
            client.set(LAST_HELD_BACK_KEY, payload, ex=TICK_TTL_SECONDS)
    except Exception as exc:  # pragma: no cover - visibility only
        logger.warning("cost probes: tick not recorded: %s", exc.__class__.__name__)
    finally:
        if client is not None:
            client.close()


def recent_ticks() -> dict:
    import json

    out = {"last_tick": None, "last_held_back": None}
    client = None
    try:
        client = _redis()
        for name, key in (("last_tick", LAST_TICK_KEY), ("last_held_back", LAST_HELD_BACK_KEY)):
            raw = client.get(key)
            if raw:
                out[name] = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as exc:
        out["error"] = exc.__class__.__name__
    finally:
        if client is not None:
            client.close()
    return out


def probe_round_trips(db: Session, *, limit: Optional[int] = None) -> list[dict]:
    rows = [dict(row.payload or {}) for row in _rows(db, ACTION_ROUND_TRIP)]
    return rows[-limit:] if limit else rows


def probe_status(db: Session) -> dict:
    now = datetime.now(UTC)
    trips = probe_round_trips(db)
    week = [p for p in probes(db, since=_week_start(now))]
    values = [float(t["round_trip_bps"]) for t in trips if t.get("round_trip_bps") is not None]
    return {"per_week": int(settings.cost_probe_per_week), "quantity": int(settings.cost_probe_quantity),
            "max_notional": float(settings.cost_probe_max_notional),
            "hold_minutes": int(settings.cost_probe_hold_minutes), "symbols": probe_symbols(),
            "this_week": len(week), "round_trips": len(trips),
            "mean_round_trip_bps": round(sum(values) / len(values), 3) if values else None,
            "latest": trips[-5:], **recent_ticks(), "paper_only": True}
