"""Paper-account import, reconciliation, and safe order lifecycle.

Dispatch is explicitly operator-triggered after a durable reservation; it commits
before one POST and turns an ambiguous outcome into a halt rather than a retry.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CorporateAction, IntradayBar, RiskRule, Strategy, StrategySignal, StockPaperRecoveryState
from app.models.stock_paper import (
    StockPaperAccount, StockPaperBrokerActivity, StockPaperEquitySnapshot,
    StockPaperFill, StockPaperLedgerEvent, StockPaperOrder, StockPaperPosition, StockPaperStrategyEvidence,
)
from app.services.intraday_data import MARKET_DATA_PROVIDER, feed_status, session_bounds
from app.services.risk import DEFAULT_RISK_RULES, PortfolioState, StrategyState, approve_trade
from app.services.strategies.registry import get_strategy
from app.services.trusted_data import UntrustedMarketData, trusted_history, trusted_intraday_observation

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
TRADIER_SANDBOX_URL = "https://sandbox.tradier.com/v1"
LEGACY_BROKER = "alpaca_paper"
BROKER = LEGACY_BROKER
UTC = timezone.utc
UNKNOWN_COSTS_REASON = "Broker-reported commissions/spread/slippage are incomplete; costs are unknown."
ACCOUNTING_RESIDUAL_REVIEW_REASON = "Prior unexplained cash or position residual requires manual accounting review"
RECONCILIATION_OVERLAP = timedelta(minutes=10)
NONTERMINAL_ORDER_STATUSES = frozenset({"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace", "open", "held", "stopped", "calculated", "reserved", "submitting", "unknown"})
ALLOWED_ORDER_SOURCES = frozenset({"manual_control_room", "manual_close", "manual_reduce", "recovery_flatten", "broker_import"})
MAX_REFERENCE_PRICE_DEVIATION = Decimal("0.02")
MAX_BROKER_SNAPSHOT_AGE = timedelta(minutes=5)
TRADIER_PAPER_EVIDENCE = {
    "complete": False,
    "status": "incomplete",
    "orders_scope": "current_session",
    "transaction_history_scope": "unavailable_in_sandbox",
    "costs_scope": "unknown_without_complete_activity_history",
    "restart_recovery": "not_provable_from_sandbox_history",
}


class StockPaperError(RuntimeError):
    pass


class StockPaperUnavailable(StockPaperError):
    pass


class AlpacaPaperGateway(Protocol):
    def account(self) -> dict: ...
    def positions(self) -> list[dict]: ...
    def orders(self, after: datetime | None = None) -> list[dict]: ...
    def fills(self, after: datetime | None = None) -> list[dict]: ...
    def submit_order(self, payload: dict) -> dict: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def order_by_client_id(self, client_order_id: str) -> dict | None: ...


class AlpacaPaperClient:
    """Server-only Alpaca client. The trading base URL is deliberately immutable."""

    base_url = ALPACA_PAPER_URL

    def _headers(self) -> dict[str, str]:
        key, secret = settings.paper_broker_credentials()
        if not key or not secret:
            raise StockPaperUnavailable("Alpaca paper credentials are not configured")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}

    def _request_response(self, method: str, path: str, *, params: dict | None = None, payload: dict | None = None) -> tuple[Any, dict[str, str]]:
        try:
            with httpx.Client(base_url=self.base_url, timeout=20.0, headers=self._headers()) as client:
                response = client.request(method, path, params=params, json=payload)
            if response.status_code >= 400:
                raise StockPaperUnavailable(f"Alpaca paper request failed with HTTP {response.status_code}")
            return response.json(), dict(response.headers)
        except StockPaperUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise StockPaperUnavailable("Alpaca paper broker is unavailable or returned invalid data") from exc

    def _request(self, method: str, path: str, *, params: dict | None = None, payload: dict | None = None) -> Any:
        return self._request_response(method, path, params=params, payload=payload)[0]

    def account(self) -> dict:
        return self._request("GET", "/v2/account")

    def positions(self) -> list[dict]:
        result = self._request("GET", "/v2/positions")
        if not isinstance(result, list):
            raise StockPaperUnavailable("Alpaca paper positions response is invalid")
        return result

    def _activity_pages(self, params: dict[str, str]) -> list[dict]:
        """Backfill activities with Alpaca's last-activity-ID cursor.

        Account activities do not return a continuation token.  When a full
        page is returned, the ID of its last activity is sent as the next
        ``page_token``.  This is intentionally separate from order paging:
        orders are a bare list and use their documented ``until`` timestamp
        cursor instead.
        """
        rows, seen, token = [], set(), None
        for _ in range(100):  # explicit bounded failure rather than silently truncating evidence
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            result, _headers = self._request_response(
                "GET", "/v2/account/activities", params=page_params
            )
            if isinstance(result, dict):
                page = result.get("activities") or result.get("data") or []
            else:
                page = result
            if not isinstance(page, list):
                raise StockPaperUnavailable("Alpaca paper paginated response is invalid")
            page_ids = []
            new_ids = []
            for row in page:
                if not isinstance(row, dict):
                    raise StockPaperUnavailable("Alpaca paper activity page contains an invalid record")
                ident = str(row.get("id") or "")
                if not ident:
                    raise StockPaperUnavailable("Alpaca paper activity lacks an identifier")
                page_ids.append(ident)
                if ident not in seen:
                    rows.append(row)
                    seen.add(ident)
                    new_ids.append(ident)
            declared_size = int(params.get("page_size") or 0)
            if len(page) < declared_size:
                return rows
            if not page_ids:
                raise StockPaperUnavailable(
                    "Alpaca activity page is full but has no continuation cursor"
                )
            next_token = page_ids[-1]
            if next_token == token:
                raise StockPaperUnavailable(
                    "Alpaca paper activity pagination cursor did not advance"
                )
            if not new_ids:
                raise StockPaperUnavailable(
                    "Alpaca paper activity page repeated without new evidence"
                )
            token = next_token
        raise StockPaperUnavailable("Alpaca paper pagination exceeded safe page limit")

    def orders(self, after: datetime | None = None) -> list[dict]:
        """Alpaca orders are a bare list; page with its documented `until` cursor."""
        params = {"status": "all", "nested": "false", "direction": "desc", "limit": "500"}
        if after:
            params["after"] = _utc(after - RECONCILIATION_OVERLAP).isoformat()
        rows, seen, until = [], set(), None
        for _ in range(100):
            page_params = dict(params)
            if until:
                page_params["until"] = until
            page = self._request("GET", "/v2/orders", params=page_params)
            if not isinstance(page, list):
                raise StockPaperUnavailable("Alpaca paper orders response is invalid")
            for row in page:
                ident = str(row.get("id") or "")
                if not ident:
                    raise StockPaperUnavailable("Alpaca paper order lacks an identifier")
                if ident not in seen:
                    seen.add(ident)
                    rows.append(row)
            if len(page) < 500:
                return rows
            timestamps = [_timestamp(row.get("updated_at") or row.get("created_at"), fallback=datetime.now(UTC)) for row in page]
            boundary = min(timestamps)
            # A timestamp cursor cannot safely split a full same-time boundary:
            # advancing could skip rows and retaining it can loop forever.
            if sum(stamp == boundary for stamp in timestamps) > 1:
                raise StockPaperUnavailable("Alpaca order time boundary is ambiguous; refusing incomplete import")
            next_until = boundary.isoformat()
            if next_until == until:
                raise StockPaperUnavailable("Alpaca order time cursor did not advance")
            until = next_until
        raise StockPaperUnavailable("Alpaca order pagination exceeded safe page limit")

    def fills(self, after: datetime | None = None) -> list[dict]:
        # Fetch all reported account activities for the audit trail.  Fill rows
        # are extracted below; non-fill activity remains durable evidence rather
        # than being silently discarded.
        params: dict[str, str] = {"direction": "desc", "page_size": "100"}
        # Activities are fully backfilled each reconciliation. This prevents a
        # delayed broker activity from being lost behind a short lookback window.
        return self._activity_pages(params)

    def submit_order(self, payload: dict) -> dict:
        # Kept out of all routes; callers must use dispatch_reserved_order.
        return self._request("POST", "/v2/orders", payload=payload)

    def cancel_order(self, broker_order_id: str) -> None:
        try:
            self._request("DELETE", f"/v2/orders/{broker_order_id}")
        except StockPaperUnavailable as exc:
            if "HTTP 404" in str(exc):
                return
            raise

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            return self._request("GET", f"/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        except StockPaperUnavailable:
            return None


class TradierPaperClient:
    """Read/write adapter for the Tradier *sandbox* paper account.

    Tradier's sandbox order endpoint is deliberately the only trading endpoint
    this client can address.  The sandbox order list is current-session-only and
    Tradier does not expose a complete historical activity cursor, so the
    adapter marks every snapshot as incomplete.  The ledger consequently halts
    rather than treating a partial broker view as accounting truth.
    """

    evidence_complete = False

    def __init__(self) -> None:
        configured = str(getattr(settings, "tradier_sandbox_url", "") or TRADIER_SANDBOX_URL).rstrip("/")
        if configured != TRADIER_SANDBOX_URL:
            raise StockPaperUnavailable("Tradier paper broker URL must be the immutable sandbox endpoint")
        self.base_url = TRADIER_SANDBOX_URL
        self.account_id = str(getattr(settings, "tradier_account_id", "") or "").strip()
        if not self.account_id:
            raise StockPaperUnavailable("Tradier sandbox account ID is not configured")

    def _token(self) -> str:
        value = getattr(settings, "tradier_api_key", "")
        return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")

    def _headers(self) -> dict[str, str]:
        token = self._token()
        if not token:
            raise StockPaperUnavailable("Tradier sandbox credentials are not configured")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _request_response(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        form: dict | None = None,
    ) -> tuple[Any, dict[str, str]]:
        try:
            with httpx.Client(base_url=self.base_url, timeout=20.0, headers=self._headers()) as client:
                response = client.request(method, path, params=params, data=form)
            if response.status_code >= 400:
                raise StockPaperUnavailable(
                    f"Tradier sandbox request failed with HTTP {response.status_code}"
                )
            return response.json(), dict(response.headers)
        except StockPaperUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise StockPaperUnavailable(
                "Tradier sandbox broker is unavailable or returned invalid data"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        form: dict | None = None,
    ) -> Any:
        return self._request_response(method, path, params=params, form=form)[0]

    @staticmethod
    def _one_or_many(value: Any) -> list[dict]:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list) and all(isinstance(row, dict) for row in value):
            return value
        raise StockPaperUnavailable("Tradier sandbox returned an invalid collection")

    def account(self) -> dict:
        response = self._request("GET", f"/accounts/{self.account_id}/balances")
        balances = response.get("balances") if isinstance(response, dict) else None
        if not isinstance(balances, dict):
            raise StockPaperUnavailable("Tradier sandbox balances response is invalid")
        reported_id = str(balances.get("account_number") or self.account_id).strip()
        if reported_id != self.account_id:
            raise StockPaperUnavailable("Tradier sandbox account identifier changed")
        cash = balances.get("cash") if isinstance(balances.get("cash"), dict) else {}
        margin = balances.get("margin") if isinstance(balances.get("margin"), dict) else {}
        equity = balances.get("total_equity")
        if equity is None:
            equity = balances.get("equity")
        buying_power = margin.get("stock_buying_power")
        if buying_power is None:
            buying_power = balances.get("buying_power")
        return {
            "id": reported_id,
            "currency": "USD",
            "status": "ACTIVE",
            "cash": cash.get("cash_available", balances.get("cash_available")),
            "buying_power": buying_power,
            "equity": equity,
            "last_equity": equity,
            "updated_at": balances.get("updated_at"),
            "tradier_raw": response,
        }

    def positions(self) -> list[dict]:
        response = self._request("GET", f"/accounts/{self.account_id}/positions")
        container = response.get("positions") if isinstance(response, dict) else None
        rows = self._one_or_many(container.get("position") if isinstance(container, dict) else None)
        normalized = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            quantity = row.get("quantity")
            if not symbol or quantity is None:
                raise StockPaperUnavailable("Tradier sandbox position is missing immutable fields")
            qty = Decimal(str(quantity))
            cost_basis = row.get("cost_basis")
            average = None
            if cost_basis is not None and qty:
                average = str(Decimal(str(cost_basis)) / qty)
            normalized.append({
                "symbol": symbol,
                "qty": str(quantity),
                "avg_entry_price": average,
                "cost_basis": cost_basis,
                "updated_at": row.get("date_acquired"),
                "tradier_raw": row,
            })
        return normalized

    @staticmethod
    def _normalize_order(row: dict) -> dict:
        broker_id = str(row.get("id") or "").strip()
        if not broker_id:
            raise StockPaperUnavailable("Tradier sandbox order is missing an identifier")
        order = {
            "id": broker_id,
            "client_order_id": str(row.get("tag") or row.get("client_order_id") or f"broker-{broker_id}")[:64],
            "symbol": str(row.get("symbol") or "").strip().upper(),
            "side": str(row.get("side") or "").lower(),
            "qty": row.get("quantity") if row.get("quantity") is not None else row.get("qty"),
            "filled_qty": row.get("exec_quantity"),
            "type": str(row.get("type") or "").lower(),
            "time_in_force": str(row.get("duration") or row.get("time_in_force") or "").lower(),
            "limit_price": row.get("price") if row.get("price") is not None else row.get("limit_price"),
            "status": str(row.get("status") or "").lower(),
            "created_at": row.get("create_date") or row.get("created_at"),
            "updated_at": row.get("transaction_date") or row.get("updated_at"),
            "tradier_raw": row,
        }
        if order["status"] == "ok":
            order["status"] = "accepted"
        return order

    def orders(self, after: datetime | None = None) -> list[dict]:
        response = self._request("GET", f"/accounts/{self.account_id}/orders")
        container = response.get("orders") if isinstance(response, dict) else None
        rows = self._one_or_many(container.get("order") if isinstance(container, dict) else None)
        # The normalized current-session rows are useful for read-only lookup,
        # but are never sufficient for ledger reconciliation.
        return [self._normalize_order(row) for row in rows]

    def order_details(self, broker_order_id: str) -> dict:
        response = self._request("GET", f"/accounts/{self.account_id}/orders/{broker_order_id}")
        raw = response.get("order") if isinstance(response, dict) else None
        if not isinstance(raw, dict):
            raise StockPaperUnavailable("Tradier sandbox order detail response is invalid")
        normalized = self._normalize_order(raw)
        executions = raw.get("exec") or raw.get("executions") or []
        normalized["executions"] = self._one_or_many(executions)
        return normalized

    def fills(self, after: datetime | None = None) -> list[dict]:
        # Tradier has no complete historical account-activity pagination.  We
        # import only explicit execution rows, never exec_quantity/avg_fill_price
        # summaries, and the incomplete-evidence marker still halts snapshots.
        fills = []
        for order in self.orders(after):
            if order.get("status") not in {"filled", "partially_filled"}:
                continue
            detailed = self.order_details(order["id"])
            for index, execution in enumerate(detailed.get("executions", [])):
                if not isinstance(execution, dict):
                    raise StockPaperUnavailable("Tradier sandbox execution row is invalid")
                quantity = execution.get("quantity")
                price = execution.get("price")
                if quantity is None or price is None:
                    raise StockPaperUnavailable("Tradier sandbox execution lacks quantity or price")
                execution_id = str(
                    execution.get("id")
                    or f"{order['id']}:{execution.get('execution_date') or execution.get('date') or index}"
                )
                fills.append({
                    "id": execution_id,
                    "activity_type": "FILL",
                    "order_id": order["id"],
                    "symbol": order["symbol"],
                    "side": order["side"],
                    "qty": quantity,
                    "price": price,
                    "commission": execution.get("commission"),
                    "transaction_time": execution.get("execution_date") or execution.get("date"),
                    "tradier_raw": execution,
                })
        return fills

    def submit_order(self, payload: dict) -> dict:
        allowed = {"symbol", "side", "type", "quantity", "duration", "price", "stop", "tag"}
        form = {}
        for key, value in payload.items():
            if key == "qty":
                key = "quantity"
            if key == "time_in_force":
                key = "duration"
            if key == "limit_price":
                key = "price"
            if key == "client_order_id":
                key = "tag"
            if key in allowed and value is not None:
                form[key] = str(value)
        response = self._request("POST", f"/accounts/{self.account_id}/orders", form=form)
        raw = response.get("order") if isinstance(response, dict) else None
        if not isinstance(raw, dict):
            raise StockPaperUnavailable("Tradier sandbox order submission response is invalid")
        order_id = str(raw.get("id") or "").strip()
        if not order_id:
            raise StockPaperUnavailable("Tradier sandbox accepted an order without an identifier")
        return {
            "id": order_id,
            "status": "accepted" if str(raw.get("status") or "ok").lower() == "ok" else str(raw.get("status")).lower(),
            "client_order_id": form.get("tag"),
            "tradier_raw": response,
        }

    def cancel_order(self, broker_order_id: str) -> None:
        response = self._request("DELETE", f"/accounts/{self.account_id}/orders/{broker_order_id}")
        raw = response.get("order") if isinstance(response, dict) else None
        if isinstance(raw, dict) and str(raw.get("status") or "").lower() not in {"ok", "pending_cancel"}:
            raise StockPaperUnavailable("Tradier sandbox did not acknowledge order cancellation")

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        for row in self.orders():
            if row.get("client_order_id") == client_order_id:
                return row
        return None


def active_paper_broker_name() -> str:
    name = str(getattr(settings, "active_paper_broker", "") or LEGACY_BROKER).strip()
    if name not in {LEGACY_BROKER, "tradier_sandbox"}:
        raise StockPaperError(f"Unsupported active paper broker: {name}")
    return name


def active_paper_account(db: Session, *, for_update: bool = False) -> StockPaperAccount | None:
    query = db.query(StockPaperAccount).filter_by(broker=active_paper_broker_name())
    return query.with_for_update().one_or_none() if for_update else query.one_or_none()


def active_paper_gateway() -> AlpacaPaperGateway:
    return TradierPaperClient() if active_paper_broker_name() == "tradier_sandbox" else AlpacaPaperClient()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp(value: Any, *, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)
    except (TypeError, ValueError) as exc:
        raise StockPaperError("Broker supplied an invalid timestamp") from exc


def _decimal(value: Any, field: str, *, nonnegative: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise StockPaperError(f"Broker supplied invalid {field}") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise StockPaperError(f"Broker supplied invalid {field}")
    return result


def _event(db: Session, account: StockPaperAccount | None, event_type: str, status: str, reason: str | None, payload: dict | None = None) -> None:
    db.add(StockPaperLedgerEvent(account_id=account.id if account else None, event_type=event_type, status=status, reason=reason,
           actor=str(db.info.get("stock_paper_actor", "system")), payload=payload))


def _event_once(
    db: Session,
    account: StockPaperAccount | None,
    event_type: str,
    status: str,
    reason: str | None,
    payload: dict | None = None,
) -> None:
    """Do not append the same reconciliation notice for repeated broker evidence."""
    query = db.query(StockPaperLedgerEvent).filter(
        StockPaperLedgerEvent.account_id == (account.id if account else None),
        StockPaperLedgerEvent.event_type == event_type,
        StockPaperLedgerEvent.status == status,
        StockPaperLedgerEvent.reason == reason,
    )
    if any((row.payload or {}) == (payload or {}) for row in query.all()):
        return
    _event(db, account, event_type, status, reason, payload)


def _activity_classification(raw: dict) -> str:
    """Classify provider activity without treating it as strategy performance."""
    activity_type = str(raw.get("activity_type") or "FILL").upper()
    if activity_type == "FILL":
        return "fill"
    if activity_type in {"DIV", "DIVNRA"}:
        return "dividend"
    if activity_type in {"SPLIT", "REORG", "ROC", "SPIN"}:
        return "corporate_action"
    if activity_type in {"FEE", "TAF", "TAX"}:
        return "fee"
    if activity_type in {"TRANS", "ACATC", "ACATS", "CSD", "CSW", "DEPOSIT", "WITHDRAWAL"}:
        return "transfer_or_cash_movement"
    if activity_type in {"JNL", "JNLC", "JNLS", "MISC", "NC", "PTC", "MA", "SUB", "SSO"}:
        return "broker_adjustment"
    return "unsupported"


def _halt(account: StockPaperAccount, reason: str, *, reconciliation_required: bool = True) -> None:
    account.status = "halted"
    account.halt_reason = reason
    account.reconciliation_required = reconciliation_required
    account.halted_at = datetime.now(UTC)


def _mark_accounting_review_required(db: Session) -> None:
    state = db.get(StockPaperRecoveryState, 1)
    if state is None:
        state = StockPaperRecoveryState(id=1, status="armed", flatten_policy="none", updated_by="stock_reconciler")
        db.add(state)
    state.accounting_review_required = True
    state.accounting_reviewed_at = None
    state.accounting_reviewed_by = None
    state.accounting_review_reason = None
    state.accounting_review_digest = None
    state.updated_by = "stock_reconciler"


def _account_values(raw: dict, observed: datetime) -> dict:
    if not isinstance(raw, dict) or str(raw.get("currency", "")).upper() != "USD":
        raise StockPaperError("Paper broker account must report USD")
    if str(raw.get("status", "")).upper() != "ACTIVE":
        raise StockPaperError("Paper broker account is not ACTIVE")
    account_id = str(raw.get("id") or "").strip()
    if not account_id:
        raise StockPaperError("Paper broker account identifier is missing")
    return {
        "broker_account_id": account_id,
        "currency": "USD",
        "cash": _decimal(raw.get("cash"), "cash"),
        "buying_power": _decimal(raw.get("buying_power"), "buying_power"),
        "equity": _decimal(raw.get("equity"), "equity"),
        "last_equity": _decimal(raw.get("last_equity"), "last_equity"),
        # This is when the app observed broker truth, not an account creation time.
        "source_timestamp": observed,
        "raw_payload": raw,
    }


def _position_values(raw: dict, observed: datetime) -> dict:
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        raise StockPaperError("Broker position is missing a symbol")
    quantity = _decimal(raw.get("qty"), "position quantity", nonnegative=False)
    if quantity < 0:
        raise StockPaperError(f"Short position reported for {symbol}; shorts are not permitted")
    return {
        "symbol": symbol, "quantity": quantity,
        "average_entry_price": _decimal(raw.get("avg_entry_price"), "average entry price") if raw.get("avg_entry_price") is not None else None,
        "current_price": _decimal(raw.get("current_price"), "current price") if raw.get("current_price") is not None else None,
        "market_value": _decimal(raw.get("market_value"), "market value") if raw.get("market_value") is not None else None,
        "cost_basis": _decimal(raw.get("cost_basis"), "cost basis") if raw.get("cost_basis") is not None else None,
        "unrealized_pl": _decimal(raw.get("unrealized_pl"), "unrealized P/L", nonnegative=False) if raw.get("unrealized_pl") is not None else None,
        "observed_at": _timestamp(raw.get("updated_at"), fallback=observed), "raw_payload": raw,
    }


def _snapshot(gateway: AlpacaPaperGateway, after: datetime | None = None) -> tuple[dict, list[dict], list[dict], list[dict], datetime]:
    observed = datetime.now(UTC)
    # Alpaca's `after` filter is based on creation/update evidence rather than
    # a terminal-state watermark. A prior order can fill or cancel days later,
    # so reconciliation imports the bounded, fail-closed complete order history.
    account, positions, orders, fills = gateway.account(), gateway.positions(), gateway.orders(None), gateway.fills(None)
    if getattr(gateway, "evidence_complete", True) is not True:
        raise StockPaperUnavailable(
            "Tradier sandbox order/activity evidence is current-session-only; "
            "complete historical reconciliation is unavailable"
        )
    if not all(isinstance(value, list) for value in (positions, orders, fills)):
        raise StockPaperError("Broker returned an invalid collection")
    return account, positions, orders, fills, observed


def _activity_timestamp(raw: dict, fallback: datetime) -> datetime:
    """Use broker-provided activity time before the reconciliation observation time."""
    return _timestamp(raw.get("transaction_time") or raw.get("created_at"), fallback=fallback)


def _upsert_orders(db: Session, account: StockPaperAccount, rows: list[dict]) -> None:
    for raw in rows:
        broker_order_id = str(raw.get("id") or "").strip()
        if not broker_order_id:
            raise StockPaperError("Broker order is missing an identifier")
        side = str(raw.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise StockPaperError("Broker order has unsupported side")
        symbol = str(raw.get("symbol") or "").strip().upper()
        quantity = _decimal(raw.get("qty") or raw.get("filled_qty"), "order quantity")
        order_type = str(raw.get("type") or "").lower()
        tif = str(raw.get("time_in_force") or "").lower()
        if not symbol or order_type not in {"market", "limit", "stop", "stop_limit", "trailing_stop"} or not tif:
            raise StockPaperError("Broker order has incomplete immutable fields")
        limit_price = _decimal(raw["limit_price"], "limit price") if raw.get("limit_price") is not None else None
        imported_client_id = str(raw.get("client_order_id") or f"broker-{broker_order_id}")[:64]
        order = db.query(StockPaperOrder).filter(StockPaperOrder.broker_order_id == broker_order_id).one_or_none()
        if order is None:
            order = db.query(StockPaperOrder).filter(StockPaperOrder.client_order_id == imported_client_id).one_or_none()
        if order is None:
            order = StockPaperOrder(account_id=account.id, client_order_id=imported_client_id, broker_order_id=broker_order_id,
                                    symbol=symbol, side=side, quantity=quantity, order_type=order_type, time_in_force=tif,
                                    limit_price=limit_price,
                                    reserved_cash=Decimal("0"), status=str(raw.get("status") or "unknown"), source="broker_import")
            db.add(order)
        else:
            immutable = (order.symbol, order.side, order.quantity, order.order_type, order.time_in_force, order.limit_price)
            if immutable != (symbol, side, quantity, order_type, tif, limit_price):
                raise StockPaperError("Broker order immutable fields changed; refusing corrupted evidence")
            order.broker_order_id = broker_order_id
            order.status = str(raw.get("status") or "unknown")
            order.uncertain_submission = False
            order.raw_payload = raw
        order.raw_payload = raw


def _upsert_fills(db: Session, account: StockPaperAccount, rows: list[dict], observed: datetime) -> set[str]:
    enriched_activity_ids: set[str] = set()
    for raw in rows:
        activity_id = str(raw.get("id") or "").strip()
        if not activity_id:
            raise StockPaperError("Broker activity is missing an identifier")
        activity = db.query(StockPaperBrokerActivity).filter_by(broker_activity_id=activity_id).one_or_none()
        if not activity:
            activity = StockPaperBrokerActivity(account_id=account.id, broker_activity_id=activity_id,
                activity_type=str(raw.get("activity_type") or "FILL"), occurred_at=_activity_timestamp(raw, observed), raw_payload=raw)
            db.add(activity)
        else:
            activity_type = str(raw.get("activity_type") or "FILL")
            previous_payload = dict(activity.raw_payload or {})
            incoming_payload = dict(raw)
            previous_commission = previous_payload.pop("commission", None)
            incoming_commission = incoming_payload.pop("commission", None)
            commission_enrichment = (
                activity_type.upper() == "FILL"
                and previous_commission is None
                and incoming_commission is not None
                and previous_payload == incoming_payload
            )
            if (activity.activity_type, activity.raw_payload) != (activity_type, raw) and not commission_enrichment:
                raise StockPaperError("Broker activity immutable fields changed; refusing corrupted evidence")
            if commission_enrichment:
                activity.raw_payload = raw
            # occurred_at is derived metadata. Correct an old observation-time
            # fallback once the broker supplies a stable created_at value, while
            # keeping the immutable payload comparison fail-closed.
            activity.occurred_at = _activity_timestamp(raw, activity.occurred_at)
        if str(raw.get("activity_type") or "FILL").upper() != "FILL":
            continue
        existing_fill = db.query(StockPaperFill).filter_by(broker_activity_id=activity_id).one_or_none()
        if existing_fill:
            candidate = (
                str(raw.get("order_id") or "").strip() or None, str(raw.get("symbol") or "").strip().upper(),
                str(raw.get("side") or "").lower(), _decimal(raw.get("qty"), "fill quantity"),
                _decimal(raw.get("price"), "fill price"),
                _decimal(raw["commission"], "commission") if raw.get("commission") is not None else None,
                _activity_timestamp(raw, fallback=observed),
            )
            existing_payload = dict(existing_fill.raw_payload or {})
            candidate_payload = dict(raw)
            existing_commission = existing_payload.pop("commission", None)
            candidate_commission = candidate_payload.pop("commission", None)
            immutable_match = (
                existing_fill.broker_order_id, existing_fill.symbol, existing_fill.side, existing_fill.quantity,
                existing_fill.price, _utc(existing_fill.filled_at)
            ) == (
                candidate[0], candidate[1], candidate[2], candidate[3], candidate[4], candidate[6]
            )
            is_cost_enrichment = (
                existing_fill.fee is None
                and candidate[5] is not None
                and immutable_match
                and existing_commission is None
                and existing_payload == candidate_payload
            )
            if is_cost_enrichment:
                existing_fill.fee = candidate[5]
                existing_fill.cost_known = True
                existing_fill.raw_payload = raw
                enriched_activity_ids.add(activity_id)
                continue
            if (existing_fill.broker_order_id, existing_fill.symbol, existing_fill.side, existing_fill.quantity,
                    existing_fill.price, existing_fill.fee, _utc(existing_fill.filled_at)) != candidate:
                raise StockPaperError("Broker fill immutable fields changed; refusing corrupted evidence")
            continue
        order_id = str(raw.get("order_id") or "").strip() or None
        order = db.query(StockPaperOrder).filter_by(broker_order_id=order_id).one_or_none() if order_id else None
        side = str(raw.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise StockPaperError("Broker fill has unsupported side")
        fee = _decimal(raw["commission"], "commission") if raw.get("commission") is not None else None
        db.add(StockPaperFill(account_id=account.id, order_id=order.id if order else None, broker_activity_id=activity_id,
            broker_order_id=order_id, symbol=str(raw.get("symbol") or "").upper(), side=side,
            quantity=_decimal(raw.get("qty"), "fill quantity"), price=_decimal(raw.get("price"), "fill price"),
            fee=fee, cost_known=fee is not None, filled_at=_activity_timestamp(raw, fallback=observed), raw_payload=raw))
    return enriched_activity_ids


def _sync_positions(db: Session, account: StockPaperAccount, broker_rows: list[dict], observed: datetime, *, detect_drift: bool) -> list[str]:
    parsed = [_position_values(row, observed) for row in broker_rows]
    incoming = {row["symbol"]: row for row in parsed}
    prior = {row.symbol: row for row in db.query(StockPaperPosition).filter_by(account_id=account.id).all()}
    drift = []
    if detect_drift:
        for symbol in sorted(set(prior) | set(incoming)):
            before, after = prior.get(symbol), incoming.get(symbol)
            if before is None or after is None or before.quantity != after["quantity"]:
                drift.append(symbol)
    for symbol, values in incoming.items():
        row = prior.get(symbol)
        if row is None:
            db.add(StockPaperPosition(account_id=account.id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
    for symbol, row in prior.items():
        if symbol not in incoming:
            db.delete(row)
    gross = sum((item["market_value"] or Decimal("0")) for item in parsed)
    if account.equity > 0 and gross > account.equity:
        drift.append("LEVERAGE")
    return drift


def _record_snapshot(db: Session, account: StockPaperAccount, observed: datetime) -> None:
    # Each reconciliation is a distinct observation even if the broker account
    # payload has not changed; never use account-created/updated timestamps here.
    db.add(StockPaperEquitySnapshot(account_id=account.id, cash=account.cash, equity=account.equity, last_equity=account.last_equity,
           buying_power=account.buying_power, observed_at=observed, source=account.broker, raw_payload=account.raw_payload))


def _equation_failure(raw_activities: list[dict], previous_cash: Decimal, current_cash: Decimal,
                      previous_positions: dict[str, Decimal], current_positions: list[dict],
                      since: datetime | None, allowed_late_activity_ids: set[str] | None = None) -> str | None:
    """Verify cash/inventory deltas only for fully reported, supported activity."""
    unsupported = sorted({str(row.get("activity_type") or "FILL").upper() for row in raw_activities
                          if str(row.get("activity_type") or "FILL").upper() != "FILL"})
    if unsupported:
        return f"Unsupported broker cash/position activities require review: {', '.join(unsupported)}"
    new_rows = []
    allowed_late_activity_ids = allowed_late_activity_ids or set()
    for row in raw_activities:
        if not (row.get("transaction_time") or row.get("created_at")):
            return "Broker fill lacks a stable broker timestamp"
        filled_at = _activity_timestamp(row, fallback=datetime.now(UTC))
        if since and filled_at <= _utc(since) and str(row.get("id") or "") not in allowed_late_activity_ids:
            return "Late broker fill predates the reconciliation watermark; full accounting reconstruction is required"
        new_rows.append(row)
    if not new_rows:
        if previous_cash != current_cash:
            return "Broker cash changed without a supported reported activity"
        current = {row["symbol"]: row["quantity"] for row in current_positions}
        if current != previous_positions:
            return "Broker positions changed without a supported reported activity"
        return None
    costs_unknown = any(row.get("commission") is None for row in new_rows)
    cash_delta = Decimal("0")
    qty_delta: dict[str, Decimal] = {}
    for row in new_rows:
        qty, price = _decimal(row.get("qty"), "fill quantity"), _decimal(row.get("price"), "fill price")
        fee = _decimal(row.get("commission"), "commission") if row.get("commission") is not None else Decimal("0")
        side, symbol = str(row.get("side") or "").lower(), str(row.get("symbol") or "").upper()
        if side not in {"buy", "sell"} or not symbol:
            return "Broker fill has incomplete equation fields"
        sign = Decimal("1") if side == "buy" else Decimal("-1")
        cash_delta += (-sign * qty * price) - fee
        qty_delta[symbol] = qty_delta.get(symbol, Decimal("0")) + sign * qty
    current = {row["symbol"]: row["quantity"] for row in current_positions}
    for symbol in set(previous_positions) | set(current) | set(qty_delta):
        if current.get(symbol, Decimal("0")) - previous_positions.get(symbol, Decimal("0")) != qty_delta.get(symbol, Decimal("0")):
            return f"Broker position quantity does not reconcile for {symbol}"
    if costs_unknown:
        # This residual is deliberately not labeled as a fee or treated as
        # zero: the broker did not provide the cost evidence needed to do either.
        if previous_cash + cash_delta != current_cash:
            return "Broker cash has an unverified residual because fill commissions are incomplete"
        return None
    if previous_cash + cash_delta != current_cash:
        return "Broker cash does not reconcile to reported fills"
    return None


def _enriched_fill_resolves_residual(
    db: Session,
    account: StockPaperAccount,
    enriched_activity_ids: set[str],
    prior_cash: Decimal,
    current_cash: Decimal,
    prior_positions: dict[str, Decimal],
    current_positions: list[dict],
) -> bool:
    """Accept only a late fee correction that matches the recorded residual."""
    if not enriched_activity_ids or prior_cash != current_cash:
        return False
    current_quantities = {row["symbol"]: row["quantity"] for row in current_positions}
    if current_quantities != prior_positions:
        return False
    prior_residual = None
    prior_ids: set[str] = set()
    for event in (
        db.query(StockPaperLedgerEvent)
        .filter_by(account_id=account.id, event_type="reconcile", status="halted")
        .order_by(StockPaperLedgerEvent.id.desc())
        .all()
    ):
        payload = event.payload or {}
        candidate_ids = {str(value) for value in payload.get("activity_ids", [])}
        if payload.get("cash_residual") is not None and candidate_ids:
            prior_residual = Decimal(str(payload["cash_residual"]))
            prior_ids = candidate_ids
            break
    if prior_residual is None or prior_ids != enriched_activity_ids:
        return False
    fees = sum(
        (fill.fee or Decimal("0") for fill in db.query(StockPaperFill).filter(
            StockPaperFill.account_id == account.id,
            StockPaperFill.broker_activity_id.in_(enriched_activity_ids),
        ).all()),
        Decimal("0"),
    )
    return prior_residual + fees == Decimal("0")


def _strategy_owned_position_counts(db: Session, account: StockPaperAccount,
                                    positions: list[StockPaperPosition]) -> dict[str, int]:
    """Count physical long positions from net, attributable stock-paper fills."""
    current = {row.symbol: row.quantity for row in positions if row.quantity > 0}
    if not current:
        return {}
    fills = db.query(StockPaperFill).filter_by(account_id=account.id).all()
    order_ids = {row.order_id for row in fills if row.order_id is not None}
    orders = {
        row.id: row for row in db.query(StockPaperOrder).filter(StockPaperOrder.id.in_(order_ids)).all()
    } if order_ids else {}
    quantities: dict[tuple[int, str], Decimal] = {}
    unknown_symbols: set[str] = set()
    for fill in fills:
        if fill.symbol not in current:
            continue
        order = orders.get(fill.order_id)
        if not order or order.strategy_id is None:
            unknown_symbols.add(fill.symbol)
            continue
        sign = Decimal("1") if fill.side == "buy" else Decimal("-1")
        key = (order.strategy_id, fill.symbol)
        quantities[key] = quantities.get(key, Decimal("0")) + sign * fill.quantity
    for symbol, quantity in current.items():
        owned = sum((net for (_, candidate) , net in quantities.items() if candidate == symbol), Decimal("0"))
        if symbol in unknown_symbols or owned != quantity:
            raise StockPaperError(
                f"Current {symbol} position has unknown strategy attribution; new buys are blocked"
            )
    counts: dict[str, int] = {}
    for (strategy_id, _), quantity in quantities.items():
        if quantity > 0:
            key = str(strategy_id)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _validate_reference_price(db: Session, symbol: str, price: Decimal) -> None:
    latest = (
        db.query(IntradayBar)
        .filter_by(
            symbol=symbol,
            timeframe="1m",
            provider=MARKET_DATA_PROVIDER,
        )
        .order_by(IntradayBar.opened_at.desc())
        .first()
    )
    if latest is None or latest.close <= 0:
        raise StockPaperError("Stock paper order has no persisted reference bar")
    deviation = abs(price - latest.close) / latest.close
    if deviation > MAX_REFERENCE_PRICE_DEVIATION:
        raise StockPaperError("Order reference price exceeds allowed deviation from the current completed bar")


def initialize_stock_paper_account(db: Session, gateway: AlpacaPaperGateway | None = None) -> dict:
    """Explicitly import the observed sandbox account once; never reset it."""
    broker_name = active_paper_broker_name()
    if db.query(StockPaperAccount).filter_by(broker=broker_name).one_or_none():
        raise StockPaperError("Stock paper account is already initialized; use reconciliation and never reset it")
    gateway = gateway or active_paper_gateway()
    raw_account, raw_positions, raw_orders, raw_fills, observed = _snapshot(gateway)
    values = _account_values(raw_account, observed)
    account = StockPaperAccount(broker=broker_name, status="reconciled", costs_known=False, accounting_verified=False, reconciliation_required=False,
                                last_reconciled_at=observed, **values)
    db.add(account)
    db.flush()
    _sync_positions(db, account, raw_positions, observed, detect_drift=False)
    _upsert_orders(db, account, raw_orders)
    _upsert_fills(db, account, raw_fills, observed)
    _record_snapshot(db, account, observed)
    review_activity_types = sorted({
        str(row.get("activity_type") or "FILL").upper()
        for row in raw_fills
        if str(row.get("activity_type") or "FILL").upper() != "FILL"
    })
    external_outstanding = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id, StockPaperOrder.source == "broker_import",
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).count()
    if external_outstanding or review_activity_types:
        reasons = []
        if external_outstanding:
            reasons.append("Externally submitted nonterminal broker order requires manual review")
        if review_activity_types:
            reasons.append(
                "Initial broker activity requires accounting review: "
                + ", ".join(review_activity_types)
            )
            account.unexplained_residual = True
            _mark_accounting_review_required(db)
        _halt(account, "; ".join(reasons))
        _event(
            db,
            account,
            "initialize",
            "halted",
            account.halt_reason,
            {
                "external_outstanding_orders": external_outstanding,
                "activity_types": review_activity_types,
                "activity_classifications": {
                    activity_type: _activity_classification({"activity_type": activity_type})
                    for activity_type in review_activity_types
                },
            },
        )
    else:
        _event(db, account, "initialize", "reconciled", UNKNOWN_COSTS_REASON,
               {"shorting_capability_ignored": True, "leverage_capability_ignored": True})
    db.commit()
    return stock_paper_status(db)


def reconcile_stock_paper_account(db: Session, gateway: AlpacaPaperGateway | None = None) -> dict:
    """Reconcile the broker account and preserve the automatic-recovery contract.

    When automatic recovery runs, its exact response mapping is nested under
    ``automatic_recovery``. API callers can therefore use the same ``status``
    and optional ``reason`` fields as callers of
    ``attempt_automatic_stock_recovery``.
    """
    account = active_paper_account(db, for_update=True)
    if account is None:
        raise StockPaperError("Stock paper account is not initialized")
    gateway = gateway or active_paper_gateway()
    automatic_recovery = None
    try:
        prior_cash = account.cash
        prior_positions = {row.symbol: row.quantity for row in db.query(StockPaperPosition).filter_by(account_id=account.id).all()}
        prior_accounting_verified = account.accounting_verified
        previous_reconciled_at = account.last_reconciled_at
        prior_activity_ids = {
            row.broker_activity_id for row in db.query(StockPaperBrokerActivity.broker_activity_id)
            .filter_by(account_id=account.id).all()
        }
        raw_account, raw_positions, raw_orders, raw_fills, observed = _snapshot(gateway, account.last_reconciled_at)
        # Poll every client-owned nonterminal order by immutable client id.
        # Full history catches old order transitions; the lookup is the
        # fail-closed evidence path if a provider page cannot show one.
        nonterminal = db.query(StockPaperOrder).filter(
            StockPaperOrder.account_id == account.id,
            StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
            StockPaperOrder.source != "broker_import",
        ).all()
        known_broker_ids = {str(row.get("id") or "") for row in raw_orders}
        unresolved_nonterminal: list[str] = []
        for order in nonterminal:
            if order.status == "reserved" and order.submission_attempted_at is None and not order.broker_order_id:
                # Local reservations cannot exist at the broker before dispatch.
                continue
            resolved = gateway.order_by_client_id(order.client_order_id)
            if resolved and str(resolved.get("id") or "") not in known_broker_ids:
                raw_orders.append(resolved)
                known_broker_ids.add(str(resolved.get("id") or ""))
            elif not resolved and (not order.broker_order_id or order.broker_order_id not in known_broker_ids):
                unresolved_nonterminal.append(order.client_order_id)
        values = _account_values(raw_account, observed)
        configured_id = (
            getattr(settings, "tradier_account_id", "")
            if active_paper_broker_name() == "tradier_sandbox"
            else getattr(settings, "paper_broker_account_id", "")
        )
        configured_id = str(configured_id or "").strip()
        if configured_id and values["broker_account_id"] != configured_id:
            raise StockPaperError("Paper broker account does not match the configured account binding")
        if values["broker_account_id"] != account.broker_account_id:
            raise StockPaperError("Broker account identifier changed; refusing to merge accounts")
        for key, value in values.items():
            setattr(account, key, value)
        _upsert_orders(db, account, raw_orders)
        enriched_activity_ids = _upsert_fills(db, account, raw_fills, observed)
        position_values = [_position_values(row, observed) for row in raw_positions]
        new_activities = [
            row for row in raw_fills
            if str(row.get("id") or "") not in prior_activity_ids
            or str(row.get("id") or "") in enriched_activity_ids
        ]
        residual_resolved = _enriched_fill_resolves_residual(
            db,
            account,
            enriched_activity_ids,
            prior_cash,
            account.cash,
            prior_positions,
            position_values,
        )
        equation_error = None if residual_resolved else _equation_failure(
            new_activities,
            prior_cash,
            account.cash,
            prior_positions,
            position_values,
            previous_reconciled_at,
            enriched_activity_ids,
        )
        # A quantity delta backed by imported fills is explained, not drift.
        drift = _sync_positions(db, account, raw_positions, observed, detect_drift=False)
        outstanding = db.query(StockPaperOrder).filter(
            StockPaperOrder.account_id == account.id,
            StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
        ).all()
        external_outstanding = [row.broker_order_id for row in outstanding if row.source == "broker_import"]
        actions = db.query(CorporateAction).filter(
            CorporateAction.ex_date >= _utc(account.last_reconciled_at or observed).date(),
            CorporateAction.symbol.in_([str(row.get("symbol", "")).upper() for row in raw_positions]),
        ).count()
        automatic_review_candidate = bool(
            account.unexplained_residual
            and enriched_activity_ids
            and equation_error is None
            and not unresolved_nonterminal
            and not external_outstanding
            and not outstanding
            and not actions
            and not drift
            and all(
                str(row.get("activity_type") or "FILL").upper() == "FILL"
                and (row.get("transaction_time") or row.get("created_at"))
                for row in raw_fills
            )
            and all(fill.fee is not None and fill.cost_known for fill in db.query(StockPaperFill).filter_by(account_id=account.id).all())
        )
        account.last_reconciled_at = observed
        fills_complete = all(
            fill.fee is not None and fill.cost_known
            for fill in db.query(StockPaperFill).filter_by(account_id=account.id).all()
        )
        accounting_evidence_complete = bool(
            prior_accounting_verified
            and fills_complete
            and equation_error is None
            and not unresolved_nonterminal
            and not external_outstanding
            and not actions
            and not drift
        )
        # Preserve a qualifying accounting proof only while fresh broker
        # evidence remains fully supported. Initial imports and ordinary
        # reconciliations stay non-qualifying until that proof exists.
        account.costs_known = accounting_evidence_complete
        account.accounting_verified = accounting_evidence_complete
        if any(row.get("commission") is None for row in new_activities):
            _event(db, account, "accounting_residual", "costs_unknown",
                   "Reported fill commissions are incomplete; P/L remains unavailable",
                   {"activity_ids": [str(row.get("id")) for row in new_activities if row.get("commission") is None]})
        if equation_error:
            # A later identical snapshot cannot explain an already-observed
            # cash/inventory discrepancy. Persist that fact instead of letting
            # a second reconcile silently establish the discrepant value as a
            # new baseline.
            account.unexplained_residual = True
            _mark_accounting_review_required(db)
            _halt(account, equation_error)
            residual_payload = {"activity_ids": [str(row.get("id")) for row in new_activities]}
            activity_types = sorted({
                str(row.get("activity_type") or "FILL").upper()
                for row in new_activities
            })
            if activity_types:
                residual_payload["activity_types"] = activity_types
            fill_activities = [
                row for row in new_activities
                if str(row.get("activity_type") or "FILL").upper() == "FILL"
            ]
            if fill_activities:
                cash_delta_without_fee = Decimal("0")
                for row in fill_activities:
                    sign = Decimal("1") if str(row.get("side") or "").lower() == "buy" else Decimal("-1")
                    cash_delta_without_fee += -sign * _decimal(row.get("qty"), "fill quantity") * _decimal(row.get("price"), "fill price")
                residual_payload["cash_residual"] = str(account.cash - (prior_cash + cash_delta_without_fee))
            _event_once(db, account, "reconcile", "halted", equation_error, residual_payload)
        elif unresolved_nonterminal:
            _halt(account, "Nonterminal client order has no broker lookup result")
            _event(db, account, "reconcile", "halted", account.halt_reason, {"unresolved_client_order_ids": unresolved_nonterminal})
        elif external_outstanding:
            _halt(account, "Externally submitted nonterminal broker order requires manual review")
            _event(db, account, "reconcile", "halted", account.halt_reason,
                   {"external_broker_order_ids": external_outstanding, "outstanding_order_count": len(outstanding)})
        elif actions:
            _halt(account, "Corporate action requires manual accounting review")
            _event(db, account, "reconcile", "halted", account.halt_reason, {"corporate_actions": actions})
        elif drift:
            _halt(account, "Broker position drift detected; reconciliation review is required")
            _event(db, account, "reconcile", "drift", account.halt_reason, {"symbols": drift})
        elif account.unexplained_residual:
            _halt(account, ACCOUNTING_RESIDUAL_REVIEW_REASON)
            _mark_accounting_review_required(db)
            prior_halt = db.query(StockPaperLedgerEvent).filter_by(
                account_id=account.id,
                event_type="reconcile",
                status="halted",
            ).first()
            if new_activities or prior_halt is None:
                _event_once(
                    db,
                    account,
                    "reconcile",
                    "halted",
                    ACCOUNTING_RESIDUAL_REVIEW_REASON,
                )
        else:
            account.reconciliation_required = False
            if account.status != "halted":
                account.status, account.halt_reason = "reconciled", None
            _event(db, account, "reconcile", "reconciled", UNKNOWN_COSTS_REASON)
        _record_snapshot(db, account, observed)
        db.commit()
        recovery_state = db.get(StockPaperRecoveryState, 1)
        if (
            account.unexplained_residual
            or automatic_review_candidate
            or (
                recovery_state is not None
                and recovery_state.accounting_reviewed_by == "stock_recovery_automation"
            )
        ):
            from app.services.stock_recovery import attempt_automatic_stock_recovery
            automatic_recovery = attempt_automatic_stock_recovery(
                db,
                candidate=automatic_review_candidate,
                evidence={
                    "enriched_activity_ids": sorted(enriched_activity_ids),
                    "observed_at": observed.isoformat(),
                },
            )
    except (StockPaperError, StockPaperUnavailable) as exc:
        db.rollback()
        account = active_paper_account(db, for_update=True)
        if account is None:
            raise StockPaperError("Stock paper account is not initialized")
        _halt(account, str(exc))
        _event(db, account, "reconcile", "unavailable", str(exc))
        db.commit()
    result = stock_paper_status(db)
    if automatic_recovery is not None:
        # Keep the service response intact; callers depend on parity with the
        # direct automatic-recovery entry point.
        result["automatic_recovery"] = automatic_recovery
    return result


def halt_stock_paper_account(db: Session, reason: str) -> dict:
    account = active_paper_account(db, for_update=True)
    if account is None:
        raise StockPaperError("Stock paper account is not initialized")
    reason = reason.strip()
    if not reason:
        raise StockPaperError("A non-empty halt reason is required")
    _halt(account, reason)
    _event(db, account, "manual_halt", "halted", reason)
    db.commit()
    return stock_paper_status(db)


def resume_stock_paper_account(db: Session, *, reason: str) -> dict:
    from app.services.stock_recovery import resume_stock_paper_after_revalidation
    return resume_stock_paper_after_revalidation(
        db,
        actor=str(db.info.get("stock_paper_actor", "operator")),
        reason=reason,
    )


def _validate_signal_buy(db: Session, account: StockPaperAccount, symbol: str, signal_id: int | None,
                         quantity: Decimal, reference_price: Decimal, rules: dict, now: datetime) -> tuple[StrategySignal, StockPaperStrategyEvidence]:
    if signal_id is None:
        raise StockPaperError("Buy requires a persisted stock-paper signal_id")
    signal = db.query(StrategySignal).filter_by(id=signal_id).with_for_update().one_or_none()
    if not signal or signal.symbol != symbol or signal.action != "BUY":
        raise StockPaperError("Signal is missing or does not match a BUY for this symbol")
    if not signal.strategy_id or not signal.signal_time or _utc(signal.signal_time) < now - timedelta(minutes=5):
        raise StockPaperError("Signal is stale or has no bound strategy")
    strategy = db.get(Strategy, signal.strategy_id)
    if not strategy or not strategy.is_active or strategy.current_status != "paper_trading_active":
        raise StockPaperError("Signal strategy is not active for paper trading")
    evidence = db.query(StockPaperStrategyEvidence).filter_by(strategy_id=strategy.id, status="approved").one_or_none()
    if not evidence or _utc(evidence.expires_at) < now:
        raise StockPaperError("Validated non-legacy strategy performance evidence is required for a buy")
    positions = db.query(StockPaperPosition).filter_by(account_id=account.id).all()
    strategy_counts = _strategy_owned_position_counts(db, account, positions)
    pending = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id, StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES)
    ).all()
    # A dispatch recheck sees this same reservation as pending; risk evaluates
    # pre-trade state, so exclude the signal-bound order itself.
    pending = [row for row in pending if row.signal_id != signal_id]
    exposure = {row.symbol: float((row.market_value or Decimal("0")) / account.equity) for row in positions if account.equity > 0}
    for row in pending:
        if row.strategy_id:
            strategy_counts[str(row.strategy_id)] = strategy_counts.get(str(row.strategy_id), 0) + 1
    daily_drawdown = float(max(Decimal("0"), (account.last_equity or account.equity) - account.equity) / (account.last_equity or account.equity))
    approved, reason = approve_trade(
        {"symbol": symbol, "strategy": str(strategy.id), "action": signal.action, "confidence": float(signal.confidence or 0)},
        PortfolioState(daily_drawdown=daily_drawdown, open_positions_count=len(positions) + len(pending),
                       open_positions_by_symbol=exposure, open_positions_by_strategy=strategy_counts,
                       total_equity=float(account.equity), kill_switch_enabled=bool(rules.get("kill_switch_enabled"))),
        StrategyState(status=strategy.current_status, drawdown=float(evidence.verified_drawdown),
                      consecutive_losses=evidence.consecutive_losses),
        rules,
    )
    if not approved:
        raise StockPaperError(f"Deterministic signal risk policy blocked buy: {reason}")
    if account.equity <= 0 or (quantity * reference_price / account.equity) > Decimal(str(rules["max_risk_per_trade"])):
        raise StockPaperError("Risk-per-trade limit exceeded")
    return signal, evidence


def create_stock_paper_signal(db: Session, symbol: str, strategy_slug: str) -> dict:
    """Persist a real generated signal only; it creates no reservation or order."""
    symbol = symbol.strip().upper()
    strategy = db.query(Strategy).filter_by(strategy_type=strategy_slug).one_or_none()
    if not strategy:
        raise StockPaperError(f"Unknown strategy: {strategy_slug}")
    try:
        prices, source = trusted_history(db, symbol, require_active=False)
        observation = trusted_intraday_observation(db, symbol)
    except UntrustedMarketData as exc:
        raise StockPaperError(str(exc)) from exc
    generated = get_strategy(strategy_slug, strategy.parameters).generate_signal(symbol, prices)
    now = datetime.now(UTC)
    observation_payload = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in observation.items()
    }
    signal = StrategySignal(strategy_id=strategy.id, symbol=symbol, signal_time=now, action=generated.action,
        probability_up=Decimal(str(generated.probability_up)), probability_down=Decimal(str(generated.probability_down)),
        confidence=Decimal(str(generated.confidence)), reason=generated.reason,
        features={**generated.features, "source": source, "execution_observation": observation_payload, "stock_paper": True})
    db.add(signal)
    db.flush()
    _event(db, None, "signal", "generated", None, {"signal_id": signal.id, "symbol": symbol, "strategy_id": strategy.id})
    db.commit()
    return {"mode": "paper", "action": "signal_generated", "signal_id": signal.id, "symbol": symbol,
            "strategy": strategy_slug, "signal_action": signal.action, "confidence": str(signal.confidence),
            "reference_price": str(observation["close"]), "execution_created": False}


def reserve_stock_paper_order(db: Session, *, symbol: str, side: str, quantity: Decimal, reference_price: Decimal,
                              idempotency_key: str, source: str, signal_id: int | None = None) -> StockPaperOrder:
    """Commit a serialized reservation before any broker POST."""
    account = active_paper_account(db, for_update=True)
    side, symbol = side.lower(), symbol.strip().upper()
    client_order_id = "sp-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:45]
    existing = db.query(StockPaperOrder).filter_by(client_order_id=client_order_id).one_or_none()
    if existing:
        if (existing.symbol, existing.side, existing.quantity, existing.limit_price) != (symbol, side, quantity, reference_price):
            raise StockPaperError("Idempotency key was already used for a different order")
        return existing
    if (
        not account
        or account.status != "reconciled"
        or account.reconciliation_required
        or account.unexplained_residual
    ):
        raise StockPaperError("Stock paper exposure is blocked until reconciled")
    if side not in {"buy", "sell"} or quantity <= 0 or reference_price <= 0:
        raise StockPaperError("Only positive long buy/sell paper orders are permitted")
    now = datetime.now(UTC)
    if not account.source_timestamp or _utc(account.source_timestamp) < now - MAX_BROKER_SNAPSHOT_AGE:
        raise StockPaperError("Stock paper account snapshot is stale; reconcile before reserving")
    bounds = session_bounds(now.astimezone(ZoneInfo("America/New_York")).date())
    if bounds is None or not (bounds[0] <= now < bounds[1]):
        raise StockPaperError("Stock paper orders are blocked outside the regular market session")
    try:
        market = feed_status(db, symbol, now=now)
    except ValueError as exc:
        raise StockPaperError("Stock paper order has no approved fresh market-data symbol") from exc
    if market["status"] != "ready":
        raise StockPaperError(f"Stock paper order blocked by stale/unavailable market data: {market['status']}")
    _validate_reference_price(db, symbol, reference_price)
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    rules = rule.value if rule else {}
    if bool(rules.get("kill_switch_enabled", False)):
        raise StockPaperError("Stock paper order blocked by kill switch")
    if source not in ALLOWED_ORDER_SOURCES:
        raise StockPaperError("Stock paper order source is not an approved execution source")
    signal = evidence = None
    if side == "buy":
        signal, evidence = _validate_signal_buy(db, account, symbol, signal_id, quantity, reference_price, DEFAULT_RISK_RULES | rules, now)
    if side == "sell":
        position = db.query(StockPaperPosition).filter_by(account_id=account.id, symbol=symbol).one_or_none()
        already_reserved = db.scalar(select(func.coalesce(func.sum(StockPaperOrder.quantity), 0)).where(
            StockPaperOrder.account_id == account.id, StockPaperOrder.symbol == symbol, StockPaperOrder.side == "sell",
            StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES)
        ))
        if not position or quantity + Decimal(str(already_reserved)) > position.quantity:
            raise StockPaperError("Sell would create a short position")
        reserved_cash = Decimal("0")
    else:
        reserved = db.scalar(select(func.coalesce(func.sum(StockPaperOrder.reserved_cash), 0)).where(
            StockPaperOrder.account_id == account.id, StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES)
        ))
        reserved_cash = quantity * reference_price
        if reserved_cash > account.cash - Decimal(str(reserved)):
            raise StockPaperError("Order would use unavailable cash or leverage")
        current = db.query(StockPaperPosition).filter_by(account_id=account.id, symbol=symbol).one_or_none()
        existing_notional = (current.market_value if current and current.market_value else Decimal("0"))
        pending_symbol_notional = db.scalar(select(func.coalesce(func.sum(StockPaperOrder.reserved_cash), 0)).where(
            StockPaperOrder.account_id == account.id, StockPaperOrder.symbol == symbol,
            StockPaperOrder.side == "buy", StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES)
        ))
        max_symbol_exposure = Decimal(str(rules.get("max_symbol_exposure", "0.10")))
        if existing_notional + Decimal(str(pending_symbol_notional)) + reserved_cash > account.equity * max_symbol_exposure:
            raise StockPaperError("Order exceeds the configured symbol risk limit")
        open_count = db.query(StockPaperPosition).filter(
            StockPaperPosition.account_id == account.id, StockPaperPosition.quantity > 0
        ).count()
        if current is None and open_count >= int(rules.get("max_open_positions", 1)):
            raise StockPaperError("Order exceeds the configured open-position risk limit")
    order = StockPaperOrder(account_id=account.id, client_order_id=client_order_id, symbol=symbol, side=side, quantity=quantity,
        strategy_id=signal.strategy_id if signal else None, signal_id=signal.id if signal else None,
        evidence_id=evidence.evidence_id if evidence else None, order_type="limit", time_in_force="day",
        limit_price=reference_price, reserved_cash=reserved_cash, status="reserved", source=source)
    db.add(order)
    _event(db, account, "order_reservation", "reserved", None, {"client_order_id": client_order_id})
    try:
        db.commit()  # durable reservation intentionally precedes any network submission
    except IntegrityError:
        db.rollback()
        existing = db.query(StockPaperOrder).filter_by(client_order_id=client_order_id).one_or_none()
        if existing and (existing.symbol, existing.side, existing.quantity, existing.limit_price) == (symbol, side, quantity, reference_price):
            return existing
        raise StockPaperError("Concurrent order reservation conflicted; no broker request was made")
    return order


def reserve_full_close(db: Session, symbol: str, idempotency_key: str) -> StockPaperOrder:
    account = active_paper_account(db)
    position = db.query(StockPaperPosition).filter_by(account_id=account.id if account else None, symbol=symbol.strip().upper()).one_or_none()
    if not position or position.quantity <= 0 or not position.current_price or position.current_price <= 0:
        raise StockPaperError("No reconciled long position with a current broker price is available to close")
    return reserve_stock_paper_order(db, symbol=position.symbol, side="sell", quantity=position.quantity,
                                     reference_price=position.current_price, idempotency_key=idempotency_key, source="manual_close")


def reserve_position_reduction(db: Session, symbol: str, reduce_pct: Decimal, idempotency_key: str) -> StockPaperOrder:
    if reduce_pct <= 0 or reduce_pct > 1:
        raise StockPaperError("Reduction percent must be greater than zero and at most one")
    account = active_paper_account(db)
    position = db.query(StockPaperPosition).filter_by(account_id=account.id if account else None, symbol=symbol.strip().upper()).one_or_none()
    if not position or position.quantity <= 0 or not position.current_price or position.current_price <= 0:
        raise StockPaperError("No reconciled long position with a current broker price is available to reduce")
    quantity = (position.quantity * reduce_pct).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    if quantity <= 0:
        raise StockPaperError("Reduction quantity rounds to zero and is rejected")
    return reserve_stock_paper_order(db, symbol=position.symbol, side="sell", quantity=quantity,
                                     reference_price=position.current_price, idempotency_key=idempotency_key, source="manual_reduce")


def dispatch_reserved_order(db: Session, order_id: int, gateway: AlpacaPaperGateway | None = None) -> StockPaperOrder:
    """Exactly-once submission: exceptions become unknown + halt; never retry POST."""
    order = db.query(StockPaperOrder).filter_by(id=order_id).with_for_update().one()
    if order.status != "reserved":
        return order
    account = db.query(StockPaperAccount).filter_by(id=order.account_id).with_for_update().one()
    recovery_flatten = order.source == "recovery_flatten" and order.side == "sell"
    if (
        (not recovery_flatten and account.status != "reconciled")
        or account.reconciliation_required
        or account.unexplained_residual
    ):
        raise StockPaperError("Reserved order cannot dispatch until stock paper account is reconciled")
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    if not recovery_flatten and bool((rule.value if rule else {}).get("kill_switch_enabled", False)):
        raise StockPaperError("Reserved order blocked by kill switch")
    now = datetime.now(UTC)
    if not account.source_timestamp or _utc(account.source_timestamp) < now - MAX_BROKER_SNAPSHOT_AGE:
        raise StockPaperError("Reserved order blocked by stale broker account snapshot")
    bounds = session_bounds(now.astimezone(ZoneInfo("America/New_York")).date())
    if bounds is None or not (bounds[0] <= now < bounds[1]):
        raise StockPaperError("Reserved order blocked outside the regular market session")
    try:
        market = feed_status(db, order.symbol, now=now)
    except ValueError as exc:
        raise StockPaperError("Reserved order has no approved fresh market-data symbol") from exc
    if market["status"] != "ready":
        raise StockPaperError(f"Reserved order blocked by stale/unavailable market data: {market['status']}")
    _validate_reference_price(db, order.symbol, order.limit_price)
    pending = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.id != order.id,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).all()
    position = db.query(StockPaperPosition).filter_by(account_id=account.id, symbol=order.symbol).one_or_none()
    if order.side == "sell":
        reserved_quantity = sum((row.quantity for row in pending if row.side == "sell" and row.symbol == order.symbol), Decimal("0"))
        if position is None or order.quantity + reserved_quantity > position.quantity:
            raise StockPaperError("Reserved sell exceeds current inventory or competing reservations")
    if order.side == "buy":
        notional = order.quantity * order.limit_price
        reserved_cash = sum((row.reserved_cash for row in pending if row.side == "buy"), Decimal("0"))
        if notional + reserved_cash > account.cash:
            raise StockPaperError("Reserved buy exceeds current cash; leverage is forbidden")
        symbol_reserved = sum((row.reserved_cash for row in pending if row.side == "buy" and row.symbol == order.symbol), Decimal("0"))
        limit = Decimal(str((DEFAULT_RISK_RULES | (rule.value if rule else {}))["max_symbol_exposure"]))
        if (position.market_value if position else Decimal("0")) + symbol_reserved + notional > account.equity * limit:
            raise StockPaperError("Reserved buy exceeds current symbol exposure limit")
        _validate_signal_buy(db, account, order.symbol, order.signal_id, order.quantity, order.limit_price,
                             DEFAULT_RISK_RULES | (rule.value if rule else {}), now)
    elif not recovery_flatten and order.source not in ALLOWED_ORDER_SOURCES:
        raise StockPaperError("Reserved order source is not approved")
    account_id = account.id
    order.status, order.submission_attempted_at = "submitting", datetime.now(UTC)
    db.commit()
    try:
        raw = (gateway or active_paper_gateway()).submit_order({"symbol": order.symbol, "qty": str(order.quantity), "side": order.side,
            "type": order.order_type, "time_in_force": order.time_in_force, "limit_price": str(order.limit_price),
            "client_order_id": order.client_order_id})
        broker_id = str(raw.get("id") or "").strip()
        if not broker_id:
            raise StockPaperUnavailable("Broker accepted an order without an identifier")
        order = db.get(StockPaperOrder, order_id)
        order.broker_order_id, order.status, order.submitted_at, order.raw_payload = broker_id, str(raw.get("status") or "accepted"), datetime.now(UTC), raw
        _event(db, db.get(StockPaperAccount, account_id), "order_submission", order.status,
               "Broker reported sandbox order submission", {"client_order_id": order.client_order_id, "broker_order_id": broker_id})
        db.commit()
    except Exception:
        db.rollback()
        order, account = db.get(StockPaperOrder, order_id), db.get(StockPaperAccount, account_id)
        order.status, order.uncertain_submission = "unknown", True
        _halt(account, "Order submission outcome is uncertain; read-only broker lookup/reconciliation required")
        _event(db, account, "order_submission", "unknown", account.halt_reason, {"client_order_id": order.client_order_id})
        db.commit()
    return db.get(StockPaperOrder, order_id)


def stock_paper_status(db: Session) -> dict:
    broker = active_paper_broker_name()
    account = active_paper_account(db)
    base = {
        "mode": "paper",
        "broker": broker,
        "broker_evidence": (
            {"provider": broker, **TRADIER_PAPER_EVIDENCE}
            if broker == "tradier_sandbox"
            else {"provider": broker, "complete": None, "status": "provider_specific"}
        ),
        "legacy_nonqualifying": True,
        "costs_known": False,
        "positions": [],
        "orders": [],
        "fills": [],
        "equity_snapshots": [],
    }
    if not account:
        return base | {"status": "uninitialized", "reason": "Explicit admin initialization has not imported the broker paper account", "account": None}
    money = lambda value: str(value) if value is not None else None
    positions = db.query(StockPaperPosition).filter_by(account_id=account.id).order_by(StockPaperPosition.symbol).all()
    orders = db.query(StockPaperOrder).filter_by(account_id=account.id).order_by(StockPaperOrder.created_at.desc()).limit(200).all()
    fills = db.query(StockPaperFill).filter_by(account_id=account.id).order_by(StockPaperFill.filled_at.desc()).limit(200).all()
    snapshots = db.query(StockPaperEquitySnapshot).filter_by(account_id=account.id).order_by(StockPaperEquitySnapshot.observed_at.desc()).limit(365).all()
    return base | {
        "status": account.status, "reason": account.halt_reason or (UNKNOWN_COSTS_REASON if not account.costs_known else "Reconciled broker paper account"),
        "costs_known": account.costs_known,
        "account": {"broker": account.broker, "account_id": account.broker_account_id, "currency": account.currency, "cash": money(account.cash),
            "buying_power": money(account.buying_power), "equity": money(account.equity), "last_equity": money(account.last_equity),
            "initialized_at": account.initialized_at.isoformat(), "last_reconciled_at": account.last_reconciled_at.isoformat() if account.last_reconciled_at else None,
            "source_timestamp": account.source_timestamp.isoformat() if account.source_timestamp else None,
            "reconciliation_required": account.reconciliation_required, "accounting_verified": account.accounting_verified,
            "unexplained_residual": account.unexplained_residual,
            "halt_reason": account.halt_reason},
        "positions": [{"symbol": row.symbol, "quantity": money(row.quantity), "average_entry_price": money(row.average_entry_price), "current_price": money(row.current_price),
                       "market_value": money(row.market_value),
                       "cost_basis": money(row.cost_basis) if account.accounting_verified and account.costs_known else None,
                       "unrealized_pl": money(row.unrealized_pl) if account.accounting_verified and account.costs_known else None,
                       "observed_at": row.observed_at.isoformat()} for row in positions],
        "orders": [{"id": row.id, "client_order_id": row.client_order_id, "broker_order_id": row.broker_order_id, "symbol": row.symbol, "side": row.side, "quantity": money(row.quantity),
                    "order_type": row.order_type, "limit_price": money(row.limit_price), "status": row.status,
                    "reserved_cash": money(row.reserved_cash), "signal_id": row.signal_id,
                    "strategy_id": row.strategy_id, "evidence_id": row.evidence_id,
                    "uncertain_submission": row.uncertain_submission,
                    "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None} for row in orders],
        "fills": [{"broker_activity_id": row.broker_activity_id, "broker_order_id": row.broker_order_id, "symbol": row.symbol, "side": row.side, "quantity": money(row.quantity),
                   "price": money(row.price), "fee": money(row.fee), "cost_known": row.cost_known, "filled_at": row.filled_at.isoformat()} for row in fills],
        "equity_snapshots": [{"cash": money(row.cash), "equity": money(row.equity), "last_equity": money(row.last_equity), "buying_power": money(row.buying_power),
                              "observed_at": row.observed_at.isoformat()} for row in snapshots],
    }