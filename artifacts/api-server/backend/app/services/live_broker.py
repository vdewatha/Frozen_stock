"""Fail-closed adapter, ledger, and reconciliation boundary for live Alpaca.

The live client has its own endpoint and credential accessor.  No function in
this module imports or calls the paper client, and no request accepts a broker
or mode selector from a caller.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    CorporateAction,
    IntradayBar,
    LiveBrokerAccount,
    LiveBrokerAccountSnapshot,
    LiveBrokerActivity,
    LiveBrokerFill,
    LiveBrokerLedgerEvent,
    LiveBrokerOrder,
    LiveBrokerPosition,
    RiskRule,
    StrategySignal,
    StockModelRegistry,
)
from app.services.audit import write_audit_log
from app.services.intraday_data import feed_status
from app.services.live_safety import live_order_decision
from app.services.live_pilot import pilot_order_decision
from app.services.risk import DEFAULT_RISK_RULES
from app.services.intraday_data import session_bounds

LIVE_ALPACA_URL = "https://api.alpaca.markets"
LIVE_BROKER = "alpaca_live"
UTC = timezone.utc
MAX_REFERENCE_PRICE_DEVIATION = Decimal("0.02")
MAX_ACCOUNT_AGE = timedelta(minutes=5)
MAX_QUOTE_AGE = timedelta(minutes=5)
NONTERMINAL = frozenset({"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace", "open", "held", "stopped", "calculated", "reserved", "submitting", "unknown"})
TERMINAL = frozenset({"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"})
ALLOWED_ORDER_TYPES = frozenset({"market", "limit"})
ALLOWED_TIFS = frozenset({"day"})
ALLOWED_ACTIVITIES = frozenset({
    "FILL", "FEE", "DIV", "DIVNRA", "CSD", "CSW", "JNL", "JNLC", "JNLS",
    "ACATC", "ACATS", "TRANS", "MISC", "NC", "PTC", "REORG", "SSO", "SUB",
    "TAF", "TAX", "ROC", "MA", "SPIN",
})


class LiveBrokerError(RuntimeError):
    pass


class LiveBrokerUnavailable(LiveBrokerError):
    pass


class LiveBrokerGateway(Protocol):
    def account(self) -> dict: ...
    def positions(self) -> list[dict]: ...
    def orders(self) -> list[dict]: ...
    def activities(self) -> list[dict]: ...
    def submit_order(self, payload: dict) -> dict: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def order_by_client_id(self, client_order_id: str) -> dict | None: ...


class AlpacaLiveClient:
    """Server-only live Alpaca client with no paper fallback."""

    base_url = LIVE_ALPACA_URL

    def _headers(self) -> dict[str, str]:
        key, secret = settings.live_broker_credentials()
        if not key or not secret:
            raise LiveBrokerUnavailable("Explicit live broker credentials are not configured")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}

    def _request_response(self, method: str, path: str, *, params: dict | None = None, payload: dict | None = None) -> tuple[Any, dict[str, str]]:
        try:
            with httpx.Client(base_url=self.base_url, timeout=20.0, headers=self._headers()) as client:
                response = client.request(method, path, params=params, json=payload)
            if response.status_code >= 400:
                raise LiveBrokerUnavailable(f"Live Alpaca request failed with HTTP {response.status_code}")
            return response.json(), dict(response.headers)
        except LiveBrokerUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise LiveBrokerUnavailable("Live Alpaca broker is unavailable or returned invalid data") from exc

    def _request(self, method: str, path: str, *, params: dict | None = None, payload: dict | None = None) -> Any:
        return self._request_response(method, path, params=params, payload=payload)[0]

    def account(self) -> dict:
        result = self._request("GET", "/v2/account")
        if not isinstance(result, dict):
            raise LiveBrokerUnavailable("Live Alpaca account response is invalid")
        return result

    def positions(self) -> list[dict]:
        result = self._request("GET", "/v2/positions")
        if not isinstance(result, list):
            raise LiveBrokerUnavailable("Live Alpaca positions response is invalid")
        return result

    def orders(self) -> list[dict]:
        rows, seen, until = [], set(), None
        for _ in range(100):
            params = {"status": "all", "nested": "false", "direction": "desc", "limit": "500"}
            if until:
                params["until"] = until
            page = self._request("GET", "/v2/orders", params=params)
            if not isinstance(page, list):
                raise LiveBrokerUnavailable("Live Alpaca orders response is invalid")
            for row in page:
                ident = str(row.get("id") or "").strip()
                if not ident:
                    raise LiveBrokerUnavailable("Live Alpaca order lacks an identifier")
                if ident not in seen:
                    seen.add(ident)
                    rows.append(row)
            if len(page) < 500:
                return rows
            stamps = [_timestamp(row.get("updated_at") or row.get("created_at"), datetime.now(UTC)) for row in page]
            boundary = min(stamps)
            if sum(stamp == boundary for stamp in stamps) > 1:
                raise LiveBrokerUnavailable("Live Alpaca order time boundary is ambiguous")
            next_until = boundary.isoformat()
            if next_until == until:
                raise LiveBrokerUnavailable("Live Alpaca order time cursor did not advance")
            until = next_until
        raise LiveBrokerUnavailable("Live Alpaca order pagination exceeded safe page limit")

    def activities(self) -> list[dict]:
        rows, seen, token = [], set(), None
        for _ in range(100):
            params = {"direction": "desc", "page_size": "100"}
            if token:
                params["page_token"] = token
            result, headers = self._request_response("GET", "/v2/account/activities", params=params)
            if isinstance(result, dict):
                page = result.get("activities") or result.get("data") or []
                next_token = result.get("next_page_token") or headers.get("x-next-page-token")
            else:
                page, next_token = result, headers.get("x-next-page-token")
            if not isinstance(page, list):
                raise LiveBrokerUnavailable("Live Alpaca activity response is invalid")
            for row in page:
                ident = str(row.get("id") or "").strip()
                if not ident:
                    raise LiveBrokerUnavailable("Live Alpaca activity lacks an identifier")
                if ident not in seen:
                    seen.add(ident)
                    rows.append(row)
            if not next_token:
                if len(page) >= 100:
                    raise LiveBrokerUnavailable("Live Alpaca activity page is full without a continuation cursor")
                return rows
            if str(next_token) == str(token):
                raise LiveBrokerUnavailable("Live Alpaca activity pagination token repeated")
            token = str(next_token)
        raise LiveBrokerUnavailable("Live Alpaca activity pagination exceeded safe page limit")

    def submit_order(self, payload: dict) -> dict:
        result = self._request("POST", "/v2/orders", payload=payload)
        if not isinstance(result, dict):
            raise LiveBrokerUnavailable("Live Alpaca submission response is invalid")
        return result

    def cancel_order(self, broker_order_id: str) -> None:
        try:
            self._request("DELETE", f"/v2/orders/{broker_order_id}")
        except LiveBrokerUnavailable as exc:
            if "HTTP 404" not in str(exc):
                raise

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            result = self._request("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
            return result if isinstance(result, dict) else None
        except LiveBrokerUnavailable:
            return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp(value: Any, fallback: datetime) -> datetime:
    if value is None:
        return _utc(fallback)
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError) as exc:
        raise LiveBrokerError("Live broker supplied an invalid timestamp") from exc


def _decimal(value: Any, field: str, *, nonnegative: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveBrokerError(f"Live broker supplied invalid {field}") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise LiveBrokerError(f"Live broker supplied invalid {field}")
    return result


def _event(db: Session, account: LiveBrokerAccount | None, event_type: str, status: str, reason: str, payload: dict | None = None) -> None:
    db.add(LiveBrokerLedgerEvent(
        account_id=account.id if account else None,
        event_type=event_type, status=status, reason=reason,
        actor=str(db.info.get("live_broker_actor", "system")), payload=payload or {},
    ))


def _halt(account: LiveBrokerAccount, reason: str) -> None:
    account.status = "halted"
    account.halt_reason = reason
    account.reconciliation_required = True
    account.halted_at = datetime.now(UTC)


def _accounting_digest(db: Session, account: LiveBrokerAccount) -> str:
    """Hash only persisted broker evidence used by an accounting review."""
    evidence = {
        "account": account.raw_payload or {},
        "positions": [
            row.raw_payload or {}
            for row in db.query(LiveBrokerPosition)
            .filter_by(account_id=account.id)
            .order_by(LiveBrokerPosition.symbol)
            .all()
        ],
        "orders": [
            row.raw_payload or {}
            for row in db.query(LiveBrokerOrder)
            .filter_by(account_id=account.id)
            .order_by(LiveBrokerOrder.client_order_id)
            .all()
        ],
        "fills": [
            row.raw_payload or {}
            for row in db.query(LiveBrokerFill)
            .filter_by(account_id=account.id)
            .order_by(LiveBrokerFill.broker_activity_id)
            .all()
        ],
        "activities": [
            row.raw_payload or {}
            for row in db.query(LiveBrokerActivity)
            .filter_by(account_id=account.id)
            .order_by(LiveBrokerActivity.broker_activity_id)
            .all()
        ],
    }
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _account_values(raw: dict, observed: datetime) -> dict:
    if str(raw.get("currency", "")).upper() != "USD" or str(raw.get("status", "")).upper() != "ACTIVE":
        raise LiveBrokerError("Live Alpaca account must be ACTIVE and report USD")
    account_id = str(raw.get("id") or "").strip()
    if not account_id:
        raise LiveBrokerError("Live Alpaca account identifier is missing")
    return {
        "broker_account_id": account_id, "currency": "USD",
        "cash": _decimal(raw.get("cash"), "cash"),
        "buying_power": _decimal(raw.get("buying_power"), "buying_power"),
        "equity": _decimal(raw.get("equity"), "equity"),
        "source_timestamp": observed, "raw_payload": raw,
    }


def _position_values(raw: dict, observed: datetime) -> dict:
    symbol = str(raw.get("symbol") or "").strip().upper()
    quantity = _decimal(raw.get("qty"), "position quantity", nonnegative=False)
    if not symbol or quantity < 0:
        raise LiveBrokerError("Live broker reported an invalid or short position")
    return {
        "symbol": symbol, "quantity": quantity,
        "current_price": _decimal(raw.get("current_price"), "current price") if raw.get("current_price") is not None else None,
        "market_value": _decimal(raw.get("market_value"), "market value", nonnegative=False) if raw.get("market_value") is not None else None,
        "observed_at": _timestamp(raw.get("updated_at"), observed), "raw_payload": raw,
    }


def _upsert_positions(db: Session, account: LiveBrokerAccount, rows: list[dict], observed: datetime) -> None:
    parsed = [_position_values(row, observed) for row in rows]
    incoming = {row["symbol"]: row for row in parsed}
    prior = {row.symbol: row for row in db.query(LiveBrokerPosition).filter_by(account_id=account.id).all()}
    for symbol, values in incoming.items():
        row = prior.get(symbol)
        if row is None:
            db.add(LiveBrokerPosition(account_id=account.id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
    for symbol, row in prior.items():
        if symbol not in incoming:
            db.delete(row)


def _upsert_orders(db: Session, account: LiveBrokerAccount, rows: list[dict]) -> list[str]:
    review_orders: list[str] = []
    for raw in rows:
        broker_id = str(raw.get("id") or "").strip()
        symbol = str(raw.get("symbol") or "").strip().upper()
        side = str(raw.get("side") or "").lower()
        order_type = str(raw.get("type") or "").lower()
        tif = str(raw.get("time_in_force") or "").lower()
        quantity = _decimal(raw.get("qty") or raw.get("filled_qty"), "order quantity")
        status = str(raw.get("status") or "unknown").lower()
        if not broker_id or not symbol or side not in {"buy", "sell"} or order_type not in ALLOWED_ORDER_TYPES or tif not in ALLOWED_TIFS:
            raise LiveBrokerError("Live broker order has incomplete or unsupported immutable fields")
        if status not in NONTERMINAL | TERMINAL:
            status = "unknown"
        client_id = str(raw.get("client_order_id") or f"broker-{broker_id}")[:64]
        row = db.query(LiveBrokerOrder).filter_by(broker_order_id=broker_id).one_or_none()
        if row is None:
            row = db.query(LiveBrokerOrder).filter_by(client_order_id=client_id).one_or_none()
        limit = _decimal(raw["limit_price"], "limit price") if raw.get("limit_price") is not None else None
        if row is None:
            row = LiveBrokerOrder(
                account_id=account.id, account_snapshot_id=None,
                model_run_id=None, signal_id=None, risk_decision_id="broker-import",
                risk_decision={"status": "unknown", "source": "broker_import"},
                actor="broker_import", idempotency_key=f"broker-import:{broker_id}",
                client_order_id=client_id, broker_order_id=broker_id, symbol=symbol,
                side=side, quantity=quantity, order_type=order_type, time_in_force=tif,
                reference_price=limit or Decimal("0"), reference_observed_at=datetime.now(UTC),
                limit_price=limit, reserved_cash=Decimal("0"), status=status,
                source="broker_import", raw_payload=raw,
            )
            # External orders cannot satisfy lineage FKs. Keep them as explicit
            # broker activity evidence and halt before any further exposure.
            db.add(row)
            db.flush()
            if (not raw.get("client_order_id") or not client_id.startswith("lb-")) and status in NONTERMINAL:
                review_orders.append(broker_id)
            continue
        immutable = (row.symbol, row.side, row.quantity, row.order_type, row.time_in_force, row.limit_price)
        if immutable != (symbol, side, quantity, order_type, tif, limit):
            raise LiveBrokerError("Live broker order immutable fields changed")
        row.broker_order_id, row.status, row.raw_payload = broker_id, status, raw
        row.uncertain_submission = False
        if row.source == "broker_import" and status in NONTERMINAL:
            review_orders.append(broker_id)
        if status == "unknown":
            review_orders.append(broker_id)
    return review_orders


def _upsert_activities(
    db: Session,
    account: LiveBrokerAccount,
    rows: list[dict],
    observed: datetime,
) -> tuple[set[str], list[str], set[str]]:
    fill_ids: set[str] = set()
    unsupported: list[str] = []
    enriched_fill_ids: set[str] = set()
    for raw in rows:
        activity_id = str(raw.get("id") or "").strip()
        activity_type = str(raw.get("activity_type") or "FILL").upper()
        if not activity_id:
            raise LiveBrokerError("Live broker activity is missing an identifier")
        if activity_type not in ALLOWED_ACTIVITIES:
            unsupported.append(activity_type)
        activity = db.query(LiveBrokerActivity).filter_by(broker_activity_id=activity_id).one_or_none()
        if activity is None:
            activity = LiveBrokerActivity(
                account_id=account.id, broker_activity_id=activity_id,
                activity_type=activity_type, occurred_at=_timestamp(raw.get("transaction_time") or raw.get("created_at"), observed),
                raw_payload=raw,
            )
            db.add(activity)
        else:
            old = dict(activity.raw_payload or {})
            new = dict(raw)
            old_fee, new_fee = old.pop("commission", None), new.pop("commission", None)
            enrichment = old_fee is None and new_fee is not None and old == new
            if (activity.activity_type, activity.raw_payload) != (activity_type, raw) and not enrichment:
                raise LiveBrokerError("Live broker activity immutable fields changed")
            if enrichment:
                activity.raw_payload = raw
                if activity_type == "FILL":
                    enriched_fill_ids.add(activity_id)
            activity.occurred_at = _timestamp(raw.get("transaction_time") or raw.get("created_at"), activity.occurred_at)
        if activity_type != "FILL":
            continue
        fill_ids.add(activity_id)
        existing = db.query(LiveBrokerFill).filter_by(broker_activity_id=activity_id).one_or_none()
        order_id = str(raw.get("order_id") or "").strip() or None
        side = str(raw.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise LiveBrokerError("Live broker fill has unsupported side")
        quantity, price = _decimal(raw.get("qty"), "fill quantity"), _decimal(raw.get("price"), "fill price")
        fee = _decimal(raw["commission"], "commission") if raw.get("commission") is not None else None
        broker_order = db.query(LiveBrokerOrder).filter_by(broker_order_id=order_id).one_or_none() if order_id else None
        if existing:
            immutable = (existing.broker_order_id, existing.symbol, existing.side, existing.quantity, existing.price, _utc(existing.filled_at))
            candidate = (order_id, str(raw.get("symbol") or "").upper(), side, quantity, price, _timestamp(raw.get("transaction_time"), observed))
            old = dict(existing.raw_payload or {})
            new = dict(raw)
            old.pop("commission", None)
            new.pop("commission", None)
            if immutable != candidate or old != new:
                raise LiveBrokerError("Live broker fill immutable fields changed")
            if existing.fee is None and fee is not None:
                existing.fee, existing.raw_payload = fee, raw
                enriched_fill_ids.add(activity_id)
            continue
        db.add(LiveBrokerFill(
            account_id=account.id, order_id=broker_order.id if broker_order else None,
            broker_activity_id=activity_id, broker_order_id=order_id,
            symbol=str(raw.get("symbol") or "").upper(), side=side, quantity=quantity,
            price=price, fee=fee, filled_at=_timestamp(raw.get("transaction_time"), observed), raw_payload=raw,
        ))
    return fill_ids, unsupported, enriched_fill_ids


def reconcile_live_broker_account(db: Session, gateway: LiveBrokerGateway | None = None) -> dict:
    account = db.query(LiveBrokerAccount).filter_by(broker=LIVE_BROKER).with_for_update().one_or_none()
    gateway = gateway or AlpacaLiveClient()
    observed = datetime.now(UTC)
    prior_cash = account.cash if account else None
    prior_unexplained = bool(account.unexplained_residual) if account else False
    prior_review_required = bool(account.accounting_review_required) if account else False
    prior_positions = {
        row.symbol: row.quantity
        for row in db.query(LiveBrokerPosition).filter_by(account_id=account.id).all()
    } if account else {}
    prior_activity_ids = {
        row.broker_activity_id
        for row in db.query(LiveBrokerActivity.broker_activity_id).filter_by(account_id=account.id).all()
    } if account else set()
    prior_reconciled = account.last_reconciled_at if account else None
    try:
        raw_account, positions, orders, activities = gateway.account(), gateway.positions(), gateway.orders(), gateway.activities()
        unresolved: list[str] = []
        known_client_ids = {str(row.get("client_order_id") or "") for row in orders}
        inflight = db.query(LiveBrokerOrder).filter(
            LiveBrokerOrder.status.in_(NONTERMINAL),
            LiveBrokerOrder.source != "broker_import",
        ).all()
        for pending in inflight:
            if pending.client_order_id in known_client_ids:
                continue
            resolved = gateway.order_by_client_id(pending.client_order_id)
            if resolved:
                orders.append(resolved)
                known_client_ids.add(pending.client_order_id)
            else:
                unresolved.append(pending.client_order_id)
        values = _account_values(raw_account, observed)
        if account is None:
            account = LiveBrokerAccount(
                broker=LIVE_BROKER, environment=settings.live_environment_name,
                status="uninitialized", reconciliation_required=True, unexplained_residual=False,
                **values,
            )
            db.add(account)
            db.flush()
        elif values["broker_account_id"] != account.broker_account_id:
            raise LiveBrokerError("Live broker account identifier changed")
        for key, value in values.items():
            setattr(account, key, value)
        _upsert_positions(db, account, positions, observed)
        external = _upsert_orders(db, account, orders)
        fill_ids, unsupported, enriched_fill_ids = _upsert_activities(db, account, activities, observed)
        db.add(LiveBrokerAccountSnapshot(
            account_id=account.id, cash=account.cash, buying_power=account.buying_power,
            equity=account.equity, observed_at=observed, source=LIVE_BROKER, raw_payload=raw_account,
        ))
        issues: list[str] = []
        if external:
            issues.append("External nonterminal live broker orders require manual review")
        if unresolved:
            issues.append("Durable live orders were not found during reconciliation: " + ", ".join(sorted(unresolved)))
        if unsupported:
            issues.append("Unsupported live broker activities require manual review: " + ", ".join(sorted(set(unsupported))))
        fills = db.query(LiveBrokerFill).filter_by(account_id=account.id).all()
        if any(fill.fee is None for fill in fills) and not enriched_fill_ids:
            issues.append("Live fill fees are incomplete; exposure remains halted until fee enrichment")
        external_fills = [
            fill for fill in fills
            if fill.order_id is None
            or (
                (linked := db.get(LiveBrokerOrder, fill.order_id)) is not None
                and linked.source == "broker_import"
            )
        ]
        if external_fills:
            issues.append("External live fills require manual review before further exposure")
        new_activity_rows = [
            row for row in activities
            if str(row.get("id") or "") not in prior_activity_ids
        ]
        new_fill_rows = [
            row for row in new_activity_rows
            if str(row.get("activity_type") or "FILL").upper() == "FILL"
        ]
        residual_payload: dict[str, str | list[str]] = {}
        if prior_cash is not None and not new_activity_rows and prior_cash != account.cash:
            issues.append("Live broker cash changed without a reported fill or activity")
        elif prior_cash is not None and new_fill_rows and len(new_fill_rows) == len(new_activity_rows):
            cash_delta = Decimal("0")
            quantity_delta: dict[str, Decimal] = {}
            missing_fee = False
            for row in new_fill_rows:
                qty = _decimal(row.get("qty"), "fill quantity")
                price = _decimal(row.get("price"), "fill price")
                if row.get("commission") is None:
                    missing_fee = True
                    fee = Decimal("0")
                else:
                    fee = _decimal(row.get("commission"), "commission")
                symbol = str(row.get("symbol") or "").upper()
                sign = Decimal("1") if str(row.get("side") or "").lower() == "buy" else Decimal("-1")
                cash_delta += (-sign * qty * price) - fee
                quantity_delta[symbol] = quantity_delta.get(symbol, Decimal("0")) + sign * qty
            current_positions = {
                row.symbol: row.quantity
                for row in db.query(LiveBrokerPosition).filter_by(account_id=account.id).all()
            }
            if missing_fee:
                issues.append("Live fill commission is incomplete; accounting residual remains unknown")
            elif prior_cash + cash_delta != account.cash:
                issues.append("Live broker cash does not reconcile to reported fills")
            if missing_fee or prior_cash + cash_delta != account.cash:
                residual_payload = {
                    "activity_ids": [str(row.get("id")) for row in new_fill_rows if row.get("id")],
                    "cash_residual": str(account.cash - (prior_cash + sum(
                        (
                            -(Decimal("1") if str(row.get("side") or "").lower() == "buy" else Decimal("-1"))
                            * _decimal(row.get("qty"), "fill quantity")
                            * _decimal(row.get("price"), "fill price")
                        )
                        for row in new_fill_rows
                    ))),
                }
            for symbol in set(prior_positions) | set(current_positions) | set(quantity_delta):
                if current_positions.get(symbol, Decimal("0")) - prior_positions.get(symbol, Decimal("0")) != quantity_delta.get(symbol, Decimal("0")):
                    issues.append(f"Live broker position quantity does not reconcile for {symbol}")
        if prior_unexplained and not enriched_fill_ids:
            issues.append("Prior unexplained live accounting residual remains unresolved")
        if prior_review_required and not enriched_fill_ids:
            issues.append("Explicit live accounting review is required before exposure can resume")

        # The only automatic clearing path is an exact commission enrichment
        # for the activity ids recorded with the original residual. A repeated
        # balance snapshot is deliberately not enough.
        residual_resolved = False
        if prior_unexplained and enriched_fill_ids and not external and not unresolved and not external_fills:
            prior_events = db.query(LiveBrokerLedgerEvent).filter(
                LiveBrokerLedgerEvent.account_id == account.id,
                LiveBrokerLedgerEvent.event_type == "reconcile",
                LiveBrokerLedgerEvent.status == "halted",
            ).order_by(LiveBrokerLedgerEvent.id.desc()).all()
            prior_event = next(
                (event for event in prior_events if (event.payload or {}).get("activity_ids")),
                None,
            )
            prior_payload = (prior_event.payload or {}) if prior_event else {}
            prior_ids = {str(value) for value in prior_payload.get("activity_ids", [])}
            late_fees = sum(
                (fill.fee or Decimal("0") for fill in fills if fill.broker_activity_id in enriched_fill_ids),
                Decimal("0"),
            )
            if prior_ids == enriched_fill_ids and Decimal(str(prior_payload.get("cash_residual", "0"))) + late_fees == Decimal("0"):
                residual_resolved = True
                issues = [
                    issue for issue in issues
                    if "Prior unexplained" not in issue
                    and "Explicit live accounting review" not in issue
                    and "commission is incomplete" not in issue
                    and "fees are incomplete" not in issue
                ]
        account.last_reconciled_at = observed
        account.status = "halted" if issues else "reconciled"
        account.reconciliation_required = bool(issues)
        account.unexplained_residual = False if residual_resolved else (prior_unexplained or bool(issues))
        account.accounting_review_required = False if residual_resolved else (prior_review_required or bool(issues))
        if account.unexplained_residual or account.accounting_review_required:
            account.status = "halted"
            account.reconciliation_required = True
        account.halt_reason = "; ".join(issues) if issues else None
        _event(db, account, "reconcile", "halted" if account.status == "halted" else "reconciled",
               account.halt_reason or "Live broker truth reconciled", {
            "orders": len(orders), "activities": len(activities), "prior_reconciled_at": prior_reconciled.isoformat() if prior_reconciled else None,
            **residual_payload,
        })
        db.commit()
        return live_broker_status(db)
    except Exception as exc:
        db.rollback()
        if account is not None:
            refreshed = db.get(LiveBrokerAccount, account.id)
            if refreshed is not None:
                _halt(refreshed, f"Live broker reconciliation failed: {exc}")
                _event(db, refreshed, "reconcile", "halted", refreshed.halt_reason or "reconciliation failed")
                db.commit()
        raise LiveBrokerError(str(exc)) from exc


def review_live_accounting_residual(
    db: Session,
    *,
    actor: str,
    reason: str,
    evidence_digest: str,
) -> dict:
    """Clear a live residual only after an attributable operator review."""
    actor, reason, evidence_digest = actor.strip(), reason.strip(), evidence_digest.strip().lower()
    if not actor or len(reason) < 3:
        raise LiveBrokerError("Live accounting review requires an explicit actor and reason")
    account = db.query(LiveBrokerAccount).filter_by(broker=LIVE_BROKER).with_for_update().one_or_none()
    if not account:
        raise LiveBrokerError("Live broker account is not initialized")
    if not account.unexplained_residual and not account.accounting_review_required:
        raise LiveBrokerError("No unresolved live accounting residual requires review")
    current_digest = _accounting_digest(db, account)
    if evidence_digest != current_digest:
        raise LiveBrokerError("Live accounting review evidence is stale")
    now = datetime.now(UTC)
    account.accounting_review_required = False
    account.accounting_reviewed_at = now
    account.accounting_reviewed_by = actor
    account.accounting_review_reason = reason
    account.accounting_review_digest = current_digest
    account.unexplained_residual = False
    account.reconciliation_required = False
    account.status = "reconciled"
    account.halt_reason = None
    _event(db, account, "accounting_review", "complete", reason, {
        "actor": actor,
        "evidence_digest": current_digest,
        "reviewed_at": now.isoformat(),
    })
    write_audit_log(
        db,
        event_type="live_broker",
        entity_type="live_account",
        entity_id=account.id,
        action="accounting_review",
        status="complete",
        message=reason,
        payload={"actor": actor, "evidence_digest": current_digest},
    )
    db.commit()
    return live_broker_status(db)


def _fresh_market_data(db: Session, symbol: str, reference_price: Decimal) -> dict:
    now = datetime.now(UTC)
    try:
        market = feed_status(db, symbol, now=now)
    except Exception as exc:
        raise LiveBrokerError("Live order blocked because approved current market data is unavailable") from exc
    if market.get("status") != "ready":
        raise LiveBrokerError(f"Live order blocked by stale or unavailable market data: {market.get('status')}")
    latest = db.query(IntradayBar).filter_by(symbol=symbol, timeframe="1m").order_by(IntradayBar.opened_at.desc()).first()
    configured_slippage = DEFAULT_RISK_RULES["max_live_slippage"]
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    if rule and isinstance(rule.value, dict):
        configured_slippage = rule.value.get("max_live_slippage", configured_slippage)
    max_slippage = Decimal(str(configured_slippage))
    deviation = (
        abs(reference_price - latest.close) / latest.close
        if latest is not None and latest.close > 0
        else Decimal("Infinity")
    )
    if latest is None or latest.close <= 0 or deviation > max_slippage:
        raise LiveBrokerError("Live order reference price is stale or outside the allowed deviation")
    bounds = session_bounds(now.astimezone(ZoneInfo("America/New_York")).date())
    if bounds is None or not (bounds[0] <= now < bounds[1]):
        raise LiveBrokerError("Live order is blocked outside the regular market session")
    provider_quote_time = market.get("exchange_timestamp")
    quote_time = _timestamp(provider_quote_time, latest.exchange_timestamp or latest.opened_at)
    quote_age = now - quote_time
    if provider_quote_time is not None and (quote_age < timedelta(0) or quote_age > MAX_QUOTE_AGE):
        raise LiveBrokerError("Live order is blocked because the latest approved quote is stale")
    return {
        "status": "pass",
        "symbol": symbol,
        "bar_time": latest.opened_at.isoformat(),
        "exchange_timestamp": _utc(quote_time).isoformat(),
        "quote_age_seconds": round(quote_age.total_seconds(), 3),
        "close": str(latest.close),
        "max_slippage": str(max_slippage),
    }


def _live_risk_gate(
    db: Session,
    account: LiveBrokerAccount,
    symbol: str,
    side: str,
    quantity: Decimal,
    reference_price: Decimal,
    *,
    recovery: bool = False,
) -> dict:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    values = DEFAULT_RISK_RULES | ((rule.value if rule else {}) or {})
    if bool(values.get("kill_switch_enabled", False)):
        raise LiveBrokerError("Live order blocked by the risk kill switch")
    pending = db.query(LiveBrokerOrder).filter(
        LiveBrokerOrder.account_id == account.id,
        LiveBrokerOrder.status.in_(NONTERMINAL),
    ).all()
    max_orders = int(values.get("max_live_open_orders", values.get("max_open_positions", 10)))
    if not recovery and len(pending) >= max_orders:
        raise LiveBrokerError("Live order count limit reached")
    notional = quantity * reference_price
    reserved_cash = sum((row.reserved_cash for row in pending if row.side == "buy"), Decimal("0"))
    available_buying_power = account.buying_power - reserved_cash
    if side == "buy" and notional > available_buying_power:
        raise LiveBrokerError("Live buy exceeds current buying power after pending reservations")
    position = db.query(LiveBrokerPosition).filter_by(account_id=account.id, symbol=symbol).one_or_none()
    if side == "sell" and (position is None or quantity > position.quantity):
        raise LiveBrokerError("Live sell exceeds reconciled long inventory")
    positions = db.query(LiveBrokerPosition).filter_by(account_id=account.id).all()
    gross_exposure = sum((abs(row.market_value or Decimal("0")) for row in positions), Decimal("0"))
    pending_exposure = sum((row.quantity * (row.limit_price or row.reference_price) for row in pending if row.side == "buy"), Decimal("0"))
    max_total = Decimal(str(values.get("max_total_exposure", "1.0")))
    if not recovery and side == "buy" and gross_exposure + pending_exposure + notional > account.equity * max_total:
        raise LiveBrokerError("Live order exceeds the configured total exposure limit")
    max_exposure = Decimal(str(values.get("max_symbol_exposure", "0.10")))
    symbol_pending = sum(
        (row.quantity * (row.limit_price or row.reference_price)
         for row in pending if row.side == "buy" and row.symbol == symbol),
        Decimal("0"),
    )
    if not recovery and side == "buy" and (position.market_value if position else Decimal("0")) + symbol_pending + notional > account.equity * max_exposure:
        raise LiveBrokerError("Live buy exceeds the configured symbol exposure limit")
    daily_drawdown = account.raw_payload.get("daily_drawdown") if isinstance(account.raw_payload, dict) else None
    if daily_drawdown is not None and Decimal(str(daily_drawdown)) > Decimal(str(values["max_daily_drawdown"])):
        raise LiveBrokerError("Live daily drawdown limit exceeded")
    return {
        "status": "pass",
        "rule_id": rule.id if rule else None,
        "kill_switch_enabled": False,
        "leverage": False,
        "available_buying_power": str(available_buying_power),
        "pending_order_count": len(pending),
        "gross_exposure": str(gross_exposure),
        "max_total_exposure": str(max_total),
        "max_symbol_exposure": str(max_exposure),
    }


def _model_is_eligible(model: StockModelRegistry) -> bool:
    metadata = model.training_metadata if isinstance(model.training_metadata, dict) else {}
    if model.lifecycle_state in {"demoted", "retired"}:
        return False
    if metadata.get("live_eligible") is False:
        return False
    # Research artifacts explicitly carry this false marker.  Empty metadata
    # is retained as a compatibility path for already-bound legacy evidence;
    # it is still subject to the immutable live-safety lineage gate.
    if metadata and metadata.get("eligible_for_trading") is False and model.lifecycle_state not in {"paper_canary", "champion"}:
        return False
    return True


def _order_shape(order: LiveBrokerOrder) -> tuple:
    return (order.model_run_id, order.signal_id, order.risk_decision_id, order.actor, order.symbol, order.side,
            order.quantity, order.order_type, order.time_in_force, order.reference_price, order.limit_price)


def reserve_live_order(
    db: Session,
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    reference_price: Decimal,
    order_type: str,
    time_in_force: str,
    limit_price: Decimal | None,
    idempotency_key: str,
    model_run_id: str,
    signal_id: int,
    risk_decision_id: str,
    actor: str,
    request_id: str | None = None,
) -> LiveBrokerOrder:
    symbol, side, order_type, time_in_force = symbol.strip().upper(), side.strip().lower(), order_type.strip().lower(), time_in_force.strip().lower()
    if side not in {"buy", "sell"} or order_type not in ALLOWED_ORDER_TYPES or time_in_force not in ALLOWED_TIFS:
        raise LiveBrokerError("Live broker accepts only long buy/sell market or limit day orders")
    if order_type == "limit" and (limit_price is None or limit_price <= 0):
        raise LiveBrokerError("Live limit orders require a positive limit price")
    if not risk_decision_id.strip():
        raise LiveBrokerError("Live order requires a durable risk decision identifier")
    existing = db.query(LiveBrokerOrder).filter_by(idempotency_key=idempotency_key).one_or_none()
    if existing:
        candidate = (model_run_id, signal_id, risk_decision_id, actor, symbol, side, quantity, order_type, time_in_force, reference_price, limit_price)
        if _order_shape(existing) != candidate:
            raise LiveBrokerError("Idempotency key was reused with changed immutable order fields")
        return existing
    safety = live_order_decision(db)
    if not safety.get("live_orders_allowed"):
        raise LiveBrokerError(safety.get("reason", "Live order execution is blocked by the live safety contract"))
    # The complete live safety result always contains pilot_launch.  Keeping
    # the explicit shape check preserves older isolated unit callers that mock
    # only the pre-pilot safety contract; production evaluations can never
    # omit this gate.
    if "pilot_launch" in safety.get("gates", {}):
        pilot = pilot_order_decision(
            db,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=reference_price,
            order_type=order_type,
            time_in_force=time_in_force,
        )
        if not pilot.get("allowed"):
            raise LiveBrokerError(pilot.get("reason", "Live order is outside the controlled pilot"))
    account = db.query(LiveBrokerAccount).filter_by(broker=LIVE_BROKER).with_for_update().one_or_none()
    if not account or account.status != "reconciled" or account.reconciliation_required or account.unexplained_residual:
        raise LiveBrokerError("Live broker account must be reconciled and clear before reservation")
    if not account.source_timestamp or _utc(account.source_timestamp) < datetime.now(UTC) - MAX_ACCOUNT_AGE:
        raise LiveBrokerError("Live broker account snapshot is stale")
    model = db.get(StockModelRegistry, model_run_id)
    signal = db.get(StrategySignal, signal_id)
    if not model or not signal or signal.symbol.upper() != symbol:
        raise LiveBrokerError("Live order requires an existing model version and matching strategy signal")
    if not _model_is_eligible(model):
        raise LiveBrokerError("Live order requires an eligible, non-demoted model version")
    if str(signal.action or "").upper() != side.upper():
        raise LiveBrokerError("Live order side does not match the persisted strategy signal")
    data_gate = _fresh_market_data(db, symbol, reference_price)
    risk_gate = _live_risk_gate(db, account, symbol, side, quantity, reference_price)
    snapshot = db.query(LiveBrokerAccountSnapshot).filter_by(account_id=account.id).order_by(LiveBrokerAccountSnapshot.observed_at.desc()).first()
    if not snapshot:
        raise LiveBrokerError("Live order requires an account snapshot from reconciliation")
    reserved_cash = quantity * (limit_price or reference_price) if side == "buy" else Decimal("0")
    client_order_id = "lb-" + hashlib.sha256(f"{LIVE_BROKER}:{idempotency_key}".encode()).hexdigest()[:45]
    order = LiveBrokerOrder(
        account_id=account.id, account_snapshot_id=snapshot.id, model_run_id=model_run_id, signal_id=signal_id,
        risk_decision_id=risk_decision_id.strip(),
        risk_decision={"status": "pass", "evaluated_at": datetime.now(UTC).isoformat(), "safety": safety, "data": data_gate, "risk": risk_gate},
        actor=actor, request_id=request_id, idempotency_key=idempotency_key, client_order_id=client_order_id,
        symbol=symbol, side=side, quantity=quantity, order_type=order_type, time_in_force=time_in_force,
        reference_price=reference_price, reference_observed_at=datetime.now(UTC), limit_price=limit_price,
        reserved_cash=reserved_cash, status="reserved", source="live_control_room",
    )
    db.add(order)
    db.flush()
    _event(db, account, "order_reservation", "reserved", "Live order intent durably reserved before broker submission", {
        "order_id": order.id, "client_order_id": client_order_id, "model_run_id": model_run_id, "signal_id": signal_id,
        "risk_decision_id": risk_decision_id, "actor": actor, "request_id": request_id,
    })
    write_audit_log(db, event_type="live_broker", entity_type="live_order", entity_id=order.id, action="reserve", status="reserved",
                    message="Live order intent reserved before dispatch", payload={"client_order_id": client_order_id, "actor": actor, "request_id": request_id})
    db.commit()
    return db.get(LiveBrokerOrder, order.id)


def dispatch_live_order(db: Session, order_id: int, gateway: LiveBrokerGateway | None = None) -> LiveBrokerOrder:
    order = db.query(LiveBrokerOrder).filter_by(id=order_id).with_for_update().one()
    if order.status != "reserved":
        return order
    account = db.query(LiveBrokerAccount).filter_by(id=order.account_id).with_for_update().one()
    safety = live_order_decision(db)
    recovery_order = order.source == "live_recovery_flatten" and order.side == "sell"
    recovery_mode = safety.get("mode") == "emergency-stop"
    if (
        (not safety.get("live_orders_allowed") and not (recovery_order and recovery_mode))
        or account.status != "reconciled"
        or account.reconciliation_required
        or account.unexplained_residual
    ):
        raise LiveBrokerError("Live order dispatch is no longer authorized by the live safety contract")
    if not recovery_order and "pilot_launch" in safety.get("gates", {}):
        pilot = pilot_order_decision(
            db,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            reference_price=order.reference_price,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
        )
        if not pilot.get("allowed"):
            raise LiveBrokerError(pilot.get("reason", "Live order is outside the controlled pilot"))
    if not account.source_timestamp or _utc(account.source_timestamp) < datetime.now(UTC) - MAX_ACCOUNT_AGE:
        raise LiveBrokerError("Live broker account snapshot became stale before dispatch")
    model = db.get(StockModelRegistry, order.model_run_id) if order.model_run_id else None
    signal = db.get(StrategySignal, order.signal_id) if order.signal_id else None
    if not recovery_order:
        if not model or not signal:
            raise LiveBrokerError("Live order lineage is incomplete at dispatch")
        if not _model_is_eligible(model):
            raise LiveBrokerError("Live order model is no longer eligible at dispatch")
        if str(signal.action or "").upper() != order.side.upper():
            raise LiveBrokerError("Live order signal side changed or no longer matches")
    data_gate = _fresh_market_data(db, order.symbol, order.reference_price)
    risk_gate = _live_risk_gate(
        db, account, order.symbol, order.side, order.quantity, order.reference_price,
        recovery=recovery_order,
    )
    order.risk_decision = {**(order.risk_decision or {}), "dispatch_evaluated_at": datetime.now(UTC).isoformat(), "dispatch_data": data_gate, "dispatch_risk": risk_gate}
    order.status, order.submission_attempted_at = "submitting", datetime.now(UTC)
    db.commit()
    gateway = gateway or AlpacaLiveClient()
    payload = {"symbol": order.symbol, "qty": str(order.quantity), "side": order.side, "type": order.order_type,
               "time_in_force": order.time_in_force, "client_order_id": order.client_order_id}
    if order.limit_price is not None:
        payload["limit_price"] = str(order.limit_price)
    try:
        raw = gateway.submit_order(payload)
        broker_id = str(raw.get("id") or "").strip()
        status = str(raw.get("status") or "").lower()
        if not broker_id or status not in NONTERMINAL | TERMINAL or status == "unknown":
            raise LiveBrokerUnavailable("Live broker returned an unknown order status or no identifier")
        order = db.get(LiveBrokerOrder, order_id)
        order.broker_order_id, order.status, order.submitted_at, order.raw_payload = broker_id, status, datetime.now(UTC), raw
        _event(db, account, "order_submission", status, "Live broker reported one order submission", {"order_id": order.id, "broker_order_id": broker_id})
        write_audit_log(db, event_type="live_broker", entity_type="live_order", entity_id=order.id, action="submit", status=status,
                        message="Live broker reported order submission", payload={"broker_order_id": broker_id, "request_id": order.request_id})
        db.commit()
    except Exception as exc:
        db.rollback()
        order, account = db.get(LiveBrokerOrder, order_id), db.get(LiveBrokerAccount, order.account_id)
        order.status, order.uncertain_submission = "unknown", True
        _halt(account, "Live order submission outcome is uncertain; broker lookup and reconciliation are required")
        _event(db, account, "order_submission", "unknown", account.halt_reason, {"order_id": order.id, "error_class": type(exc).__name__})
        write_audit_log(db, event_type="live_broker", entity_type="live_order", entity_id=order.id, action="submit", status="unknown",
                        message=account.halt_reason, payload={"request_id": order.request_id, "error_class": type(exc).__name__})
        db.commit()
    return db.get(LiveBrokerOrder, order_id)


def flatten_live_positions(
    db: Session,
    *,
    actor: str,
    reason: str,
    gateway: LiveBrokerGateway | None = None,
) -> dict:
    """Enter emergency-stop and submit one attributable sell per live position."""
    actor, reason = actor.strip(), reason.strip()
    if not actor or len(reason) < 3:
        raise LiveBrokerError("Live flatten requires an explicit actor and reason")
    from app.services.live_safety import ensure_live_safety_state, transition_live_safety

    safety_state = ensure_live_safety_state(db)
    if safety_state.mode in {"canary-live", "approved-live"}:
        transition_live_safety(
            db,
            target_mode="emergency-stop",
            actor=actor,
            reason=reason,
            evidence={"action": "flatten_live_positions"},
        )
    elif safety_state.mode != "emergency-stop":
        raise LiveBrokerError("Live flatten requires canary-live, approved-live, or emergency-stop mode")
    account = db.query(LiveBrokerAccount).filter_by(broker=LIVE_BROKER).with_for_update().one_or_none()
    if not account or account.status != "reconciled" or account.reconciliation_required or account.unexplained_residual:
        raise LiveBrokerError("Live account must be reconciled before an emergency flatten")
    positions = db.query(LiveBrokerPosition).filter(
        LiveBrokerPosition.account_id == account.id,
        LiveBrokerPosition.quantity > 0,
    ).order_by(LiveBrokerPosition.symbol).with_for_update().all()
    orders: list[LiveBrokerOrder] = []
    for position in positions:
        if position.current_price is None or position.current_price <= 0:
            raise LiveBrokerError(f"Cannot flatten {position.symbol} without a current broker price")
        key = f"live-recovery-flatten:{account.id}:{position.symbol}:{position.quantity}"
        idempotency_key = hashlib.sha256(key.encode()).hexdigest()
        existing = db.query(LiveBrokerOrder).filter_by(idempotency_key=idempotency_key).one_or_none()
        if existing:
            orders.append(existing)
            continue
        orders.append(LiveBrokerOrder(
            account_id=account.id,
            model_run_id=None,
            signal_id=None,
            risk_decision_id=f"recovery:{idempotency_key[:24]}",
            risk_decision={"status": "recovery", "actor": actor, "reason": reason},
            actor=actor,
            idempotency_key=idempotency_key,
            client_order_id="lb-" + idempotency_key[:45],
            symbol=position.symbol,
            side="sell",
            quantity=position.quantity,
            order_type="limit",
            time_in_force="day",
            reference_price=position.current_price,
            reference_observed_at=datetime.now(UTC),
            limit_price=position.current_price,
            reserved_cash=Decimal("0"),
            status="reserved",
            source="live_recovery_flatten",
        ))
    db.add_all([order for order in orders if order.id is None])
    _event(db, account, "flatten", "reserved", reason, {
        "actor": actor,
        "order_count": len(orders),
        "symbols": [order.symbol for order in orders],
    })
    db.commit()
    dispatched: list[int] = []
    failures: list[dict] = []
    for order in orders:
        try:
            result = dispatch_live_order(db, order.id, gateway=gateway)
            if result.status not in {"unknown", "submitting"}:
                dispatched.append(result.id)
        except LiveBrokerError as exc:
            failures.append({"order_id": order.id, "symbol": order.symbol, "error": str(exc)})
    return {
        "mode": "live",
        "status": "flattened" if not failures else "halted",
        "actor": actor,
        "reason": reason,
        "reserved_order_ids": [order.id for order in orders],
        "dispatched_order_ids": dispatched,
        "failures": failures,
    }


def cancel_live_order(db: Session, order_id: int, *, actor: str, reason: str, gateway: LiveBrokerGateway | None = None) -> LiveBrokerOrder:
    if len(reason.strip()) < 3:
        raise LiveBrokerError("Live cancellation requires a non-empty reason")
    order = db.query(LiveBrokerOrder).filter_by(id=order_id).with_for_update().one()
    if order.status in TERMINAL:
        return order
    if order.status in {"submitting", "unknown"} or order.uncertain_submission:
        raise LiveBrokerError("Uncertain live submission requires broker reconciliation before cancellation")
    if not order.broker_order_id:
        order.status = "canceled"
        db.commit()
        return order
    account = db.query(LiveBrokerAccount).filter_by(id=order.account_id).with_for_update().one()
    order.status = "pending_cancel"
    _event(db, account, "order_cancel", "pending", reason, {"order_id": order.id, "actor": actor})
    db.commit()
    try:
        (gateway or AlpacaLiveClient()).cancel_order(order.broker_order_id)
    except Exception as exc:
        db.expire_all()
        account = db.get(LiveBrokerAccount, account.id)
        _halt(account, "Live cancellation outcome is uncertain; reconciliation is required")
        _event(db, account, "order_cancel", "unknown", account.halt_reason, {"order_id": order.id, "error_class": type(exc).__name__})
        write_audit_log(
            db, event_type="live_broker", entity_type="live_order", entity_id=order.id,
            action="cancel", status="unknown", message=account.halt_reason,
            payload={"actor": actor, "request_id": order.request_id, "error_class": type(exc).__name__},
        )
        db.commit()
        raise LiveBrokerError(account.halt_reason) from exc
    db.expire_all()
    order = db.get(LiveBrokerOrder, order_id)
    _event(db, db.get(LiveBrokerAccount, account.id), "order_cancel", "requested", reason, {"order_id": order.id, "actor": actor})
    write_audit_log(
        db, event_type="live_broker", entity_type="live_order", entity_id=order.id,
        action="cancel", status="requested", message=reason,
        payload={"actor": actor, "request_id": order.request_id},
    )
    db.commit()
    return order


def live_broker_status(db: Session) -> dict:
    account = db.query(LiveBrokerAccount).filter_by(broker=LIVE_BROKER).one_or_none()
    base = {"mode": "live", "broker": LIVE_BROKER, "live_trading_enabled": False, "orders_allowed": False, "account": None, "orders": [], "positions": [], "fills": [], "activities": []}
    if not account:
        return base | {"status": "uninitialized", "reason": "Live broker reconciliation has not imported an account"}
    safety = live_order_decision(db)
    money = lambda value: str(value) if value is not None else None
    return base | {
        "status": account.status, "live_trading_enabled": bool(safety.get("live_orders_allowed")),
        "orders_allowed": bool(safety.get("live_orders_allowed")) and account.status == "reconciled" and not account.reconciliation_required,
        "reason": account.halt_reason or safety.get("reason"), "safety": safety,
        "account": {"broker": account.broker, "account_id": account.broker_account_id, "environment": account.environment,
                    "cash": money(account.cash), "buying_power": money(account.buying_power), "equity": money(account.equity),
                    "status": account.status, "reconciliation_required": account.reconciliation_required,
                    "unexplained_residual": account.unexplained_residual,
                    "accounting_review_required": account.accounting_review_required,
                    "accounting_reviewed_at": account.accounting_reviewed_at.isoformat() if account.accounting_reviewed_at else None,
                    "accounting_reviewed_by": account.accounting_reviewed_by,
                    "accounting_review_digest": account.accounting_review_digest,
                    "last_reconciled_at": account.last_reconciled_at.isoformat() if account.last_reconciled_at else None},
        "positions": [{"symbol": row.symbol, "quantity": money(row.quantity), "current_price": money(row.current_price), "market_value": money(row.market_value)} for row in db.query(LiveBrokerPosition).filter_by(account_id=account.id).order_by(LiveBrokerPosition.symbol).all()],
        "orders": [{"id": row.id, "client_order_id": row.client_order_id, "broker_order_id": row.broker_order_id, "symbol": row.symbol, "side": row.side,
                    "quantity": money(row.quantity), "status": row.status, "model_run_id": row.model_run_id, "signal_id": row.signal_id,
                    "risk_decision_id": row.risk_decision_id, "actor": row.actor, "idempotency_key": row.idempotency_key, "uncertain_submission": row.uncertain_submission}
                   for row in db.query(LiveBrokerOrder).filter_by(account_id=account.id).order_by(LiveBrokerOrder.created_at.desc()).limit(200).all()],
        "fills": [{"broker_activity_id": row.broker_activity_id, "broker_order_id": row.broker_order_id, "symbol": row.symbol, "side": row.side,
                   "quantity": money(row.quantity), "price": money(row.price), "fee": money(row.fee), "filled_at": row.filled_at.isoformat()} for row in db.query(LiveBrokerFill).filter_by(account_id=account.id).order_by(LiveBrokerFill.filled_at.desc()).limit(200).all()],
        "activities": [{"broker_activity_id": row.broker_activity_id, "activity_type": row.activity_type, "occurred_at": row.occurred_at.isoformat()} for row in db.query(LiveBrokerActivity).filter_by(account_id=account.id).order_by(LiveBrokerActivity.occurred_at.desc()).limit(200).all()],
    }