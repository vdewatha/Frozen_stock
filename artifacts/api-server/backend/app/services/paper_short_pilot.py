"""Exclusive-account Alpaca development paper shorts with bounded automation.

This is not an extension of the long-only stock ledger or a live execution path.
Broker snapshots and cumulative fills establish inventory, not verified all-in P&L.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import RiskRule, StockPaperAccount, StockPaperRecoveryState
from app.models.paper_short_pilot import PaperShortOrder, PaperShortPilot
from app.services.audit import write_audit_log
from app.services.paper_short_performance import observed_short_performance, recorded_performance
from app.services.exchange_sessions import session_bounds
from app.services.intraday_data import feed_status
from app.services.stock_paper_ledger import (
    ALPACA_PAPER_URL, AlpacaPaperClient, StockPaperError, StockPaperUnavailable,
    _validate_reference_price,
)

UTC = timezone.utc
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})
OPEN = frozenset({"new", "accepted", "pending_new", "partially_filled", "pending_cancel",
                  "pending_replace", "accepted_for_bidding", "stopped", "suspended",
                  "calculated", "held", "done_for_day"})


class PilotAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbols: list[str] = Field(min_length=1, max_length=4)
    max_order_notional: Decimal = Field(gt=0, le=1000, decimal_places=2)
    max_gross_notional: Decimal = Field(gt=0, le=3000, decimal_places=2)
    expires_at: datetime
    reason: str = Field(min_length=10, max_length=500)
    automated: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_bounds(self):
        if len(set(self.symbols)) != len(self.symbols) or not set(self.symbols) <= {"AAPL", "MSFT", "SPY", "QQQ"}:
            raise ValueError("Use distinct pilot symbols: AAPL, MSFT, SPY, QQQ")
        if self.max_order_notional > self.max_gross_notional:
            raise ValueError("Order cap cannot exceed gross cap")
        if self.expires_at.tzinfo is None:
            raise ValueError("Expiry must include a timezone")
        return self


class PilotOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,14}$")
    intent: Literal["open_short", "cover_short"]
    quantity: int = Field(strict=True, gt=0, le=1000)
    limit_price: Decimal = Field(gt=0, le=1000000, decimal_places=2)
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_\-]+$")
    reason: str = Field(min_length=10, max_length=500)


class ShortPilotGateway(AlpacaPaperClient):
    def clock(self) -> dict:
        return self._request("GET", "/v2/clock")

    def asset(self, symbol: str) -> dict:
        return self._request("GET", f"/v2/assets/{symbol}")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, AttributeError) as exc:
        raise StockPaperError("Broker timestamp must be timezone-aware") from exc


def _decimal(value, name: str, *, signed=False) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or (not signed and result < 0):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation) as exc:
        raise StockPaperError(f"Invalid broker {name}") from exc


def _binding(gateway, *, entry=False) -> str:
    if (settings.environment != "local" or settings.allow_live_trading
            or settings.paper_binding_source != "development" or settings.paper_workers_enabled
            or settings.active_paper_broker != "alpaca_paper"):
        raise StockPaperError("Short pilot requires exclusive local development paper mode with workers off")
    if entry and not settings.dev_paper_shorts_enabled:
        raise StockPaperError("Development paper short pilot is disabled")
    if getattr(gateway, "base_url", None) != ALPACA_PAPER_URL:
        raise StockPaperError("Only the immutable Alpaca paper endpoint is permitted")
    bound = settings.dev_paper_broker_account_id.get_secret_value().strip()
    if not bound or bound != settings.paper_broker_account_id or bound == settings.live_broker_account_id:
        raise StockPaperError("Dedicated development paper account binding is required")
    return sha256(bound.encode()).hexdigest()


def _exclusive(db: Session, account_hash: str) -> None:
    for account in db.query(StockPaperAccount).filter(StockPaperAccount.archived_at.is_(None)).all():
        if sha256(account.broker_account_id.encode()).hexdigest() == account_hash:
            raise StockPaperError("Account is owned by the long-only ledger; use an exclusive pilot database/account")


def _account(gateway, account_hash: str, *, entry=False) -> dict:
    raw = gateway.account()
    if not isinstance(raw, dict) or sha256(str(raw.get("id", "")).encode()).hexdigest() != account_hash:
        raise StockPaperError("Broker account does not match the development binding")
    if raw.get("status") != "ACTIVE" or raw.get("currency") != "USD":
        raise StockPaperError("An ACTIVE USD paper account is required")
    # Require explicit broker booleans; missing values must not silently enable orders.
    if raw.get("trading_blocked") is not False or raw.get("account_blocked") is not False:
        raise StockPaperError("Broker trading is blocked or unverified")
    if entry and (raw.get("shorting_enabled") is not True or _decimal(raw.get("equity"), "equity") < 2000):
        raise StockPaperError("Broker shorting capability or minimum equity is unavailable")
    return raw


def _audit(db, action, actor, payload):
    write_audit_log(db, event_type="development_paper_short", action=action, status="recorded",
                    message="Development paper short pilot", payload={"actor": actor, **payload})


def _orders(db):
    query = db.query(PaperShortOrder)
    if checking_id := db.info.get("short_checking_id"):
        query = query.filter(PaperShortOrder.client_order_id != checking_id)
    rows = query.order_by(PaperShortOrder.created_at).limit(5001).all()
    if len(rows) > 5000:
        raise StockPaperError("Pilot history limit reached; operator review required")
    return rows


def _observe(row: PaperShortOrder, raw: dict, now: datetime) -> None:
    side = "sell" if row.intent == "open_short" else "buy"
    intent = "sell_to_open" if side == "sell" else "buy_to_close"
    if not isinstance(raw, dict) or not raw.get("id") or any((
        raw.get("client_order_id") != row.client_order_id,
        raw.get("symbol") != row.symbol, raw.get("side") != side,
        raw.get("type") != "limit", raw.get("time_in_force") != "day",
        raw.get("order_class") not in (None, "", "simple"), bool(raw.get("legs")),
        raw.get("position_intent") not in (None, intent),
        _decimal(raw.get("qty"), "order quantity") != row.quantity,
        _decimal(raw.get("limit_price"), "limit price") != row.limit_price,
        row.broker_order_id is not None and raw.get("id") != row.broker_order_id,
    )):
        raise StockPaperError("Broker order does not match the durable short intent")
    filled = _decimal(raw.get("filled_qty"), "filled quantity")
    status = raw.get("status")
    if status not in TERMINAL | OPEN or not row.filled_quantity <= filled <= row.quantity:
        raise StockPaperError("Broker order status or cumulative fills are invalid")
    if (status == "filled" and filled != row.quantity) or (row.status in TERMINAL and status != row.status):
        raise StockPaperError("Broker order terminal state changed unexpectedly")
    average = _decimal(raw.get("filled_avg_price"), "average fill price") if filled else None
    if filled and (average is None or average <= 0):
        raise StockPaperError("Filled short order has no valid price")
    row.broker_order_id, row.status = str(raw["id"]), status
    row.filled_quantity, row.filled_avg_price = filled, average
    row.observed_at = now
    row.active_slot = None if status in TERMINAL else 1
    row.broker_evidence = {key: raw.get(key) for key in (
        "id", "client_order_id", "symbol", "side", "qty", "filled_qty", "filled_avg_price",
        "status", "type", "limit_price", "time_in_force", "position_intent", "updated_at")}


def _inventory(db, gateway, pilot, now):
    """Read a complete broker order snapshot; reject foreign orders and position drift."""
    rows = _orders(db)
    known = {r.client_order_id: r for r in rows}
    history = gateway.orders()
    if not isinstance(history, list):
        raise StockPaperError("Broker order history is invalid")
    seen = set()
    for raw in history:
        if not isinstance(raw, dict) or not raw.get("id") or raw["id"] in seen:
            raise StockPaperError("Broker order history is incomplete or duplicated")
        seen.add(raw["id"])
        row = known.get(raw.get("client_order_id"))
        if row is not None:
            if row.account_sha256 != pilot.account_sha256:
                raise StockPaperError("Pilot order belongs to another account")
            _observe(row, raw, now)
        else:
            if raw.get("status") not in TERMINAL:
                raise StockPaperError("Foreign open broker order; exclusive pilot ownership lost")
            submitted = _timestamp(raw.get("submitted_at"))
            if submitted >= _utc(pilot.authorized_at):
                raise StockPaperError("Foreign broker order after pilot authorization")
    for row in rows:
        if row.broker_order_id not in seen:
            # A missing lookup never proves a timed-out POST was not accepted.
            raw = gateway.order_by_client_id(row.client_order_id)
            if raw is not None:
                _observe(row, raw, now)
            elif row.status not in {"blocked", "rejected"}:
                raise StockPaperError("Unresolved short submission; broker lookup required, never resubmit")
    expected = {}
    for row in rows:
        expected[row.symbol] = expected.get(row.symbol, Decimal("0")) + (
            -row.filled_quantity if row.intent == "open_short" else row.filled_quantity)
    if any(q > 0 for q in expected.values()):
        raise StockPaperError("Short ledger would cross into a long position")
    positions, gross = {}, Decimal("0")
    raw_positions = gateway.positions()
    if not isinstance(raw_positions, list):
        raise StockPaperError("Broker positions response is invalid")
    for raw in raw_positions:
        if not isinstance(raw, dict):
            raise StockPaperError("Broker position is invalid")
        symbol = raw.get("symbol")
        qty = _decimal(raw.get("qty"), "signed position quantity", signed=True)
        if not symbol or symbol in positions or qty > 0 or qty != qty.to_integral_value():
            raise StockPaperError("Unexpected long, fractional, or duplicate broker position")
        positions[symbol] = qty
        value = _decimal(raw.get("market_value"), "position value", signed=True)
        if value > 0 or (qty < 0 and value == 0):
            raise StockPaperError("Short market value must be negative")
        gross += abs(value)
    if {s: q for s, q in positions.items() if q} != {s: q for s, q in expected.items() if q}:
        raise StockPaperError("Signed short inventory does not reconcile to broker fills")
    return positions, gross


def authorize_pilot(db: Session, request: PilotAuthorization, *, actor: str, gateway=None, now=None) -> dict:
    now, gateway = now or datetime.now(UTC), gateway or ShortPilotGateway()
    account_hash = _binding(gateway, entry=True)
    _exclusive(db, account_hash)
    # Serialize reauthorization with reservation, including SQLite writers.
    db.execute(update(PaperShortPilot).where(PaperShortPilot.id == 1).values(status=PaperShortPilot.status))
    window = timedelta(days=30) if request.automated else timedelta(hours=8)
    if not now < request.expires_at <= now + window:
        raise StockPaperError("Pilot authorization exceeds its paper-only validity window")
    account = _account(gateway, account_hash, entry=True)
    positions, orders = gateway.positions(), gateway.orders()
    if positions or not isinstance(positions, list) or not isinstance(orders, list) or any(
        not isinstance(o, dict) or o.get("status") not in TERMINAL for o in orders
    ):
        raise StockPaperError("Pilot authorization requires a flat account with no open broker orders")
    pilot = db.get(PaperShortPilot, 1)
    if pilot is not None:
        if pilot.account_sha256 != account_hash:
            raise StockPaperError("Pilot cannot be rebound to another account")
        _inventory(db, gateway, pilot, now)
    if any(r.active_slot is not None for r in _orders(db)):
        raise StockPaperError("Unresolved short order prevents authorization")
    equity = _decimal(account.get("equity"), "equity")
    if request.max_gross_notional > equity * Decimal("0.05"):
        raise StockPaperError("Pilot gross exposure cannot exceed five percent of equity")
    if pilot is None:
        pilot = PaperShortPilot(id=1)
        db.add(pilot)
    pilot.account_sha256, pilot.symbols = account_hash, request.symbols
    pilot.max_order_notional, pilot.max_gross_notional = request.max_order_notional, request.max_gross_notional
    pilot.baseline_equity, pilot.authorized_by, pilot.authorized_at = equity, actor, now
    pilot.expires_at, pilot.status = request.expires_at, "active"
    pilot.automated = request.automated
    _audit(db, "authorize", actor, {"account_sha256": account_hash, **request.model_dump(mode="json")})
    db.commit()
    return pilot_status(db)


def pilot_status(db: Session) -> dict:
    pilot = db.get(PaperShortPilot, 1)
    rows = _orders(db)
    from app.models import AuditLog
    latest = db.query(AuditLog).filter_by(event_type="development_paper_short", action="automation_cycle").order_by(AuditLog.id.desc()).first()
    snapshot = db.query(AuditLog).filter_by(event_type="development_paper_short", action="reconcile").order_by(AuditLog.id.desc()).first()
    observations = db.query(AuditLog).filter_by(event_type="development_paper_short", action="reconcile").order_by(AuditLog.id.desc()).limit(1000).all()
    return {"paper_only": True, "live_trading": False,
            "autonomous": bool(pilot and pilot.automated and settings.dev_paper_shorts_automation_enabled),
            "automation_last_cycle": latest.payload if latest else None,
            "performance": recorded_performance(list(reversed(observations))),
            "reconciliation": {"positions": snapshot.payload.get("positions", {}),
                "observed_at": _utc(snapshot.created_at).isoformat()} if snapshot else None,
            "costs_verified": False, "accounting_verified": False,
            "configured_enabled": settings.dev_paper_shorts_enabled,
            "entry_policy": "regular_session",
            "pilot": None if pilot is None else {"status": pilot.status, "symbols": pilot.symbols,
                "automated": pilot.automated,
                "expires_at": _utc(pilot.expires_at).isoformat(),
                "max_order_notional": str(pilot.max_order_notional),
                "max_gross_notional": str(pilot.max_gross_notional)},
            "orders": [order_status(r) for r in rows]}


def order_status(row) -> dict:
    return {"client_order_id": row.client_order_id, "symbol": row.symbol, "intent": row.intent,
            "quantity": str(row.quantity), "limit_price": str(row.limit_price), "status": row.status,
            "filled_quantity": str(row.filled_quantity),
            "filled_avg_price": str(row.filled_avg_price) if row.filled_avg_price is not None else None,
            "unresolved": row.active_slot is not None, "paper_only": True,
            "created_at": _utc(row.created_at).isoformat(),
            "observed_at": _utc(row.observed_at).isoformat() if row.observed_at else None}


def _entry_risk(db, pilot, actor, now):
    automated = db.info.get("short_automation") is True
    if automated:
        if not pilot.automated or not settings.dev_paper_shorts_automation_enabled or actor != "paper-short-scheduler":
            raise StockPaperError("Standing paper automation authority is unavailable")
    elif pilot.automated:
        raise StockPaperError("Automated pilot entries are reserved for the scheduled strategy")
    if pilot.status != "active" or now >= _utc(pilot.expires_at) or actor == pilot.authorized_by:
        raise StockPaperError("Entry requires unexpired account-specific paper authorization")
    rule = db.query(RiskRule).populate_existing().filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    if rule is None or rule.value.get("kill_switch_enabled") is not False:
        raise StockPaperError("Short entry blocked by kill switch or missing risk policy")
    recovery = db.query(StockPaperRecoveryState).populate_existing().order_by(StockPaperRecoveryState.id).first()
    if recovery and (recovery.status not in {"armed", "resumable"} or recovery.accounting_review_required):
        raise StockPaperError("Short entry blocked by recovery revalidation")


def _order_checks(db, gateway, pilot, request, actor, now):
    started = monotonic()
    entry = request.intent == "open_short"
    _binding(gateway, entry=entry)
    _exclusive(db, pilot.account_sha256)
    account = _account(gateway, pilot.account_sha256, entry=entry)
    clock = gateway.clock()
    if not isinstance(clock, dict):
        raise StockPaperError("Broker session clock is invalid")
    observed = _timestamp(clock.get("timestamp"))
    bounds = session_bounds(now.astimezone(ZoneInfo("America/New_York")).date())
    if clock.get("is_open") is not True or abs((now - observed).total_seconds()) > 5 or not bounds or not bounds[0] <= now < bounds[1]:
        raise StockPaperError("Pilot orders require a fresh broker clock during the regular session")
    market = feed_status(db, request.symbol, now=now)
    if market.get("execution_status", market.get("status")) != "ready":
        raise StockPaperError("Pilot order requires execution-eligible fresh market data")
    _validate_reference_price(db, request.symbol, request.limit_price)
    positions, gross = _inventory(db, gateway, pilot, now)
    if entry:
        _entry_risk(db, pilot, actor, now)
        if request.symbol not in pilot.symbols:
            raise StockPaperError("Symbol is not authorized for the short pilot")
        asset = gateway.asset(request.symbol)
        if not isinstance(asset, dict) or asset.get("symbol") != request.symbol or asset.get("class") != "us_equity" or asset.get("status") != "active" or any(
            asset.get(k) is not True for k in ("tradable", "shortable", "easy_to_borrow", "marginable")
        ):
            raise StockPaperError("Short entry requires an active, tradable, marginable easy-to-borrow equity")
        notional = request.limit_price * request.quantity
        # Conservative pilot buffer. Broker retains final margin/short-sale checks.
        buffered = notional * Decimal("1.05")
        equity = _decimal(account.get("equity"), "equity")
        if notional > pilot.max_order_notional or gross + buffered > min(pilot.max_gross_notional, equity * Decimal("0.05")):
            raise StockPaperError("Short order exceeds authorized exposure bounds")
        if buffered > _decimal(account.get("buying_power"), "buying power"):
            raise StockPaperError("Insufficient broker buying power for buffered short exposure")
    elif request.quantity > -positions.get(request.symbol, Decimal("0")):
        raise StockPaperError("Cover would exceed the owned short position or open a long")
    elapsed = monotonic() - started
    if elapsed > 5:
        raise StockPaperError("Paper preflight took too long; fresh evidence is required")
    if now + timedelta(seconds=elapsed) >= bounds[1]:
        raise StockPaperError("Regular market session ended during paper preflight")
    if entry:
        db.refresh(pilot)
        _entry_risk(db, pilot, actor, now)


def submit_pilot_order(db: Session, request: PilotOrderRequest, *, actor: str, gateway=None, now=None) -> dict:
    now, gateway = now or datetime.now(UTC), gateway or ShortPilotGateway()
    account_hash = _binding(gateway, entry=request.intent == "open_short")
    db.execute(update(PaperShortPilot).where(PaperShortPilot.id == 1).values(status=PaperShortPilot.status))
    pilot = db.get(PaperShortPilot, 1, populate_existing=True)
    if pilot is None or pilot.account_sha256 != account_hash:
        raise StockPaperError("No account-specific short pilot authorization")
    client_id = "dps-" + sha256(f"{account_hash}:{request.idempotency_key}".encode()).hexdigest()[:48]
    existing = db.get(PaperShortOrder, client_id)
    if existing:
        if (existing.symbol, existing.intent, existing.quantity, existing.limit_price) != (
            request.symbol, request.intent, request.quantity, request.limit_price
        ):
            raise StockPaperError("Idempotency key already belongs to a different short order")
        return order_status(existing)
    row = PaperShortOrder(client_order_id=client_id, account_sha256=account_hash, symbol=request.symbol,
                          intent=request.intent, quantity=Decimal(request.quantity), limit_price=request.limit_price,
                          filled_quantity=Decimal("0"), status="checking", active_slot=1, actor=actor, created_at=now)
    db.add(row)
    try:
        # The unique account-wide slot serializes both entry and cover, including SQLite.
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise StockPaperError("Another short order is unresolved; reconcile or cancel it first") from exc
    try:
        # Exclude this not-yet-submitted intent from the broker snapshot comparison.
        db.refresh(pilot)
        with db.no_autoflush:
            _order_checks_excluding(db, gateway, pilot, request, actor, now, client_id)
        row = db.get(PaperShortOrder, client_id)
        claimed = db.execute(update(PaperShortOrder).where(
            PaperShortOrder.client_order_id == client_id, PaperShortOrder.status == "checking",
        ).values(status="submitting"))
        if claimed.rowcount != 1:
            raise StockPaperError("Short intent was canceled before dispatch; no broker POST permitted")
        _audit(db, "submission_started", actor, {"client_order_id": client_id, "reason": request.reason})
        db.commit()
    except Exception:
        db.rollback()
        row = db.get(PaperShortOrder, client_id)
        row.status, row.active_slot = "blocked", None
        _audit(db, "preflight_blocked", actor, {"client_order_id": client_id})
        db.commit()
        raise
    payload = {"symbol": row.symbol, "qty": str(request.quantity),
               "side": "sell" if request.intent == "open_short" else "buy",
               "position_intent": "sell_to_open" if request.intent == "open_short" else "buy_to_close",
               "type": "limit", "limit_price": str(request.limit_price), "time_in_force": "day",
               "extended_hours": False, "client_order_id": client_id}
    try:
        _observe(row, gateway.submit_order(payload), now)
    except Exception as exc:
        db.rollback()
        row = db.get(PaperShortOrder, client_id)
        definite_reject = isinstance(exc, StockPaperUnavailable) and exc.status_code in {400, 401, 403, 404, 422}
        row.status, row.active_slot = ("rejected", None) if definite_reject else ("uncertain", 1)
        _audit(db, "submission_unconfirmed", actor, {"client_order_id": client_id, "status": row.status})
        db.commit()
        raise StockPaperError("Paper short submission unconfirmed; inspect and reconcile before another order") from exc
    _audit(db, "broker_observed", actor, order_status(row))
    db.commit()
    return order_status(row)


def _order_checks_excluding(db, gateway, pilot, request, actor, now, client_id):
    # Persisted checking rows have no broker order yet. Only the submitting
    # process may exclude its own row; recovery treats an abandoned one as unknown.
    original = db.info.get("short_checking_id")
    db.info["short_checking_id"] = client_id
    try:
        _order_checks(db, gateway, pilot, request, actor, now)
    finally:
        db.info["short_checking_id"] = original


def reconcile_pilot(db: Session, *, actor: str, gateway=None, now=None) -> dict:
    now, gateway = now or datetime.now(UTC), gateway or ShortPilotGateway()
    account_hash = _binding(gateway)
    pilot = db.get(PaperShortPilot, 1)
    if pilot is None or pilot.account_sha256 != account_hash:
        raise StockPaperError("No bound short pilot")
    _account(gateway, account_hash)
    positions, gross_market_value = _inventory(db, gateway, pilot, now)
    performance = observed_short_performance(_orders(db), gross_market_value)
    _audit(db, "reconcile", actor, {"positions": {s: str(q) for s, q in positions.items()},
        "observed_at": now.isoformat(), "performance": performance})
    db.commit()
    return {**pilot_status(db), "positions": {s: str(q) for s, q in positions.items()}, "observed_at": now.isoformat()}


def cancel_pilot_order(db: Session, client_id: str, *, actor: str, gateway=None, now=None) -> dict:
    gateway, now = gateway or ShortPilotGateway(), now or datetime.now(UTC)
    account_hash = _binding(gateway)
    _account(gateway, account_hash)
    row = db.get(PaperShortOrder, client_id)
    if row is None or row.account_sha256 != account_hash:
        raise StockPaperError("Unknown development short order")
    canceled = db.execute(update(PaperShortOrder).where(
        PaperShortOrder.client_order_id == client_id, PaperShortOrder.status == "checking",
    ).values(status="blocked", active_slot=None))
    if canceled.rowcount == 1:
        # The dispatcher must atomically claim checking -> submitting, so an
        # abandoned/preflight intent can be canceled without guessing about POSTs.
        _audit(db, "cancel_before_dispatch", actor, {"client_order_id": client_id})
        db.commit()
        return order_status(row)
    raw = gateway.order_by_client_id(client_id)
    if raw is None:
        raise StockPaperError("Broker lookup unresolved; cancellation cannot clear the submission slot")
    _observe(row, raw, now)
    if row.status not in TERMINAL:
        gateway.cancel_order(row.broker_order_id)
        # A cancel request is not a confirmed cancellation; keep the active slot.
    _audit(db, "cancel_requested", actor, {"client_order_id": client_id})
    db.commit()
    return order_status(row)


def pause_pilot(db: Session, *, actor: str, reason: str) -> dict:
    row = db.get(PaperShortPilot, 1)
    if row is None:
        raise StockPaperError("No short pilot authorization")
    row.status = "paused"
    _audit(db, "pause_entries", actor, {"reason": reason})
    db.commit()
    return pilot_status(db)
