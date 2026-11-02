"""Autonomous decisions within an explicitly bounded development paper policy."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from hashlib import sha256
import math
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import IntradayBar
from app.models.paper_short_pilot import PaperShortPilot
from app.services import paper_short_pilot as pilot
from app.services.exchange_sessions import is_nyse_session, session_bounds
from app.services.intraday_data import MARKET_DATA_PROVIDER, MARKET_DATA_FEED_CLASS, collect_scheduled_intraday
from app.services.market_data import get_price_history, import_market_prices
from app.services.paper_short_simulation import ShortSimulationConfig, validated_short_history
from app.services.strategies.short_term_reversion import wilder_rsi

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
ACTOR = "paper-short-scheduler"
STRATEGY = "rsi2_short_bounce_full_session_v2"
CONFIG = ShortSimulationConfig()
SOURCE = "alpaca_iex_daily"


def previous_session(day):
    for offset in range(1, 15):
        candidate = day - timedelta(days=offset)
        if is_nyse_session(candidate):
            return candidate
    raise pilot.StockPaperError("Previous exchange session is unavailable")


def refresh_inputs(db: Session, grant: PaperShortPilot, now: datetime, *, daily=True):
    """Use existing authenticated ingestion; never fabricate bars or entitlement."""
    collect_scheduled_intraday(db, symbols=grant.symbols, now=now)
    db.commit()
    if daily:
        refresh_daily_inputs(db, grant, now)


def refresh_daily_inputs(db: Session, grant: PaperShortPilot, now: datetime):
    expected = previous_session(now.astimezone(NY).date())
    for symbol in grant.symbols:
        frame, _ = get_price_history(db, symbol, limit=260, auto_seed=False, source_filter=SOURCE)
        try:
            daily_signal(db, symbol, now)
            valid = True
        except (ValueError, pilot.StockPaperError):
            valid = False
        if not valid or frame.empty or pd.to_datetime(frame["date"], utc=True).dt.date.max() < expected:
            import_market_prices(db, symbol, period="2y", provider=SOURCE)


def daily_signal(db, symbol, now):
    frame, source = get_price_history(db, symbol, limit=260, auto_seed=False, source_filter=SOURCE)
    if source not in {SOURCE, f"database:{SOURCE}"}:
        raise pilot.StockPaperError("Short strategy requires source-pinned real daily history")
    day = now.astimezone(NY).date()
    if not frame.empty:
        days = pd.to_datetime(frame["date"], utc=True).dt.date
        if (days > day).any():
            raise pilot.StockPaperError("Short strategy history contains future sessions")
        frame = frame.loc[days < day].copy()
    data, dates, values = validated_short_history(frame, source=SOURCE, config=CONFIG, completed_before=day)
    if dates.iloc[-1].date() != previous_session(day):
        raise pilot.StockPaperError("Short strategy daily history is stale")
    close = values.close.astype(float)
    rsi, sma = float(wilder_rsi(close, 2).iloc[-1]), float(close.rolling(CONFIG.trend_window).mean().iloc[-1])
    if not math.isfinite(rsi) or not math.isfinite(sma):
        raise pilot.StockPaperError("Short strategy indicators are unavailable")
    return {"enter": bool(close.iloc[-1] < sma and rsi > CONFIG.entry_rsi),
            "cover": bool(rsi <= CONFIG.exit_rsi), "rsi2": rsi, "sma200": sma,
            "daily_session": dates.iloc[-1].date().isoformat(),
            "dataset_sha256": sha256(data.to_json(orient="split").encode()).hexdigest()}


def reference_bar(db, symbol, now):
    row = db.query(IntradayBar).filter(
        IntradayBar.symbol == symbol, IntradayBar.provider == MARKET_DATA_PROVIDER,
        IntradayBar.feed_class == MARKET_DATA_FEED_CLASS, IntradayBar.timeframe == "1m",
        IntradayBar.opened_at <= now - timedelta(minutes=1),
    ).order_by(IntradayBar.opened_at.desc()).first()
    if row is None or not timedelta(0) <= now - pilot._utc(row.opened_at) <= timedelta(minutes=5):
        raise pilot.StockPaperError("No fresh completed execution reference bar")
    if any(not Decimal(str(v)).is_finite() or Decimal(str(v)) <= 0 for v in (row.close, row.high)):
        raise pilot.StockPaperError("Invalid execution reference bar")
    return row


def _held_sessions(entry, now):
    first, last = pilot._utc(entry.created_at).astimezone(NY).date(), now.astimezone(NY).date()
    return sum(is_nyse_session(first + timedelta(days=i)) for i in range(1, (last - first).days + 1))


def _exit_reason(db, symbol, entry, now):
    bar = reference_bar(db, symbol, now)
    high = db.query(func.max(IntradayBar.high)).filter(
        IntradayBar.symbol == symbol, IntradayBar.provider == MARKET_DATA_PROVIDER,
        IntradayBar.feed_class == MARKET_DATA_FEED_CLASS, IntradayBar.timeframe == "1m",
        IntradayBar.opened_at >= entry.created_at, IntradayBar.opened_at <= now - timedelta(minutes=1),
    ).scalar()
    stop = entry.filled_avg_price * (Decimal("1") + Decimal(str(CONFIG.stop_loss_pct)))
    if max(Decimal(str(high or 0)), Decimal(str(bar.high))) >= stop:
        return "stop_loss", bar
    if _held_sessions(entry, now) >= CONFIG.max_holding_sessions:
        return "time_stop", bar
    if daily_signal(db, symbol, now)["cover"]:
        return "rsi_cover", bar
    return None, bar


def _submit(db, gateway, grant, symbol, intent, quantity, price, key, reason, now):
    request = pilot.PilotOrderRequest(symbol=symbol, intent=intent, quantity=quantity,
        limit_price=price, idempotency_key=key, reason=f"Automated {STRATEGY}: {reason}")
    previous = db.info.get("short_automation")
    db.info["short_automation"] = True
    try:
        return pilot.submit_pilot_order(db, request, actor=ACTOR, gateway=gateway, now=now)
    finally:
        db.info["short_automation"] = previous


def _record(db, result, now):
    result = {"paper_only": True, "live_trading": False, "strategy": STRATEGY,
              "entry_policy": "regular_session",
              "checked_at": now.isoformat(), **result}
    pilot._audit(db, "automation_cycle", ACTOR, result)
    db.commit()
    return result


POLICY_ACTOR = "deployment-policy"
POLICY_DAYS, POLICY_RENEW_BEFORE = 29, timedelta(days=2)


def ensure_standing_policy(db: Session, gateway, now: datetime):
    """Grant or renew the deployment-configured policy while the account is flat.

    Only a policy this function created is renewed. Manual grants are never
    promoted and an operator pause is never lifted.
    """
    grant = db.get(PaperShortPilot, 1)
    if not settings.short_pilot_auto_authorize or not settings.dev_paper_shorts_automation_enabled:
        return grant
    if grant is not None and (grant.authorized_by != POLICY_ACTOR or not grant.automated or grant.status != "active"
                              or now < pilot._utc(grant.expires_at) - POLICY_RENEW_BEFORE):
        return grant
    try:
        pilot.authorize_pilot(db, pilot.PilotAuthorization(
            symbols=settings.short_pilot_symbols,
            max_order_notional=settings.short_pilot_max_order_notional,
            max_gross_notional=settings.short_pilot_max_gross_notional,
            expires_at=now + timedelta(days=POLICY_DAYS), automated=True,
            reason="Standing short policy from deployment configuration"),
            actor=POLICY_ACTOR, gateway=gateway, now=now)
    except Exception:
        # Renewal needs a flat account and a reachable broker; covers continue
        # under the existing grant and this retries on the next cycle.
        db.rollback()
    return db.get(PaperShortPilot, 1)


def run_short_automation_cycle(db: Session, *, gateway=None, now=None, refresh=True) -> dict:
    """One deterministic cycle; production safety and broker gates remain in dispatch."""
    injected_clock = now is not None
    now, gateway = now or datetime.now(UTC), gateway or pilot.ShortPilotGateway()
    grant = ensure_standing_policy(db, gateway, now)
    if not settings.dev_paper_shorts_automation_enabled or grant is None or not grant.automated:
        return {"status": "disabled", "paper_only": True, "live_trading": False}
    try:
        account_hash = pilot._binding(gateway)
        if grant.account_sha256 != account_hash:
            raise pilot.StockPaperError("Automation account does not match its standing policy")
        pilot._exclusive(db, account_hash)
        rows = pilot._orders(db)
        # A checking intent has not crossed the atomic broker-dispatch claim.
        for row in rows:
            if row.status == "checking" and now - pilot._utc(row.created_at) >= timedelta(minutes=2):
                pilot.cancel_pilot_order(db, row.client_order_id, actor=ACTOR, gateway=gateway, now=now)
        reconciled = pilot.reconcile_pilot(db, actor=ACTOR, gateway=gateway, now=now)
        rows = pilot._orders(db)
        pending = next((r for r in rows if r.active_slot is not None), None)
        if pending:
            cancel = now - pilot._utc(pending.created_at) >= timedelta(minutes=2)
            if pending.intent == "open_short":
                db.info["short_automation"] = True
                try:
                    pilot._entry_risk(db, grant, ACTOR, now)
                    cancel = cancel or not settings.dev_paper_shorts_enabled
                except pilot.StockPaperError:
                    cancel = True
                finally:
                    db.info.pop("short_automation", None)
            if cancel:
                pilot.cancel_pilot_order(db, pending.client_order_id, actor=ACTOR, gateway=gateway, now=now)
            return _record(db, {"status": "observed", "action": "cancel_pending" if cancel else "wait_for_fill",
                               "client_order_id": pending.client_order_id}, now)
        bounds = session_bounds(now.astimezone(NY).date())
        if bounds is None or not bounds[0] <= now < bounds[1]:
            return _record(db, {"status": "observed", "action": "market_closed"}, now)
        refresh_failure = None
        if refresh:
            try:
                refresh_inputs(db, grant, now, daily=False)
            except Exception as exc:
                db.rollback()
                refresh_failure = type(exc).__name__
        if not injected_clock:
            now = datetime.now(UTC)
        positions = {s: Decimal(q) for s, q in reconciled["positions"].items() if Decimal(q) < 0}
        decisions = []
        # Exits always precede entries, including when the policy expires or pauses.
        for symbol, quantity in positions.items():
            entries = [r for r in rows if r.symbol == symbol and r.intent == "open_short" and r.filled_quantity > 0]
            entry = max(entries, key=lambda r: pilot._utc(r.created_at))
            try:
                reason, bar = _exit_reason(db, symbol, entry, now)
                covers = [r for r in rows if r.symbol == symbol and r.intent == "cover_short" and r.created_at >= entry.created_at]
                if covers and now - pilot._utc(covers[-1].created_at) < timedelta(minutes=3):
                    decisions.append({"symbol": symbol, "action": "cover_retry_cooldown"})
                    continue
                if reason:
                    price = (Decimal(str(bar.close)) * Decimal("1.005")).quantize(Decimal("0.01"), rounding=ROUND_UP)
                    result = _submit(db, gateway, grant, symbol, "cover_short", int(-quantity), price,
                        f"auto-{symbol}-{entry.client_order_id}-cover-{len(covers)}", reason, now)
                    return _record(db, {"status": "observed", "action": "cover_short", "reason": reason, "order": result}, now)
                decisions.append({"symbol": symbol, "action": "hold_short"})
            except (pilot.StockPaperError, ValueError) as exc:
                db.rollback()
                decisions.append({"symbol": symbol, "action": "cover_blocked", "reason": str(exc)})
        # Slow daily backfill must never stand in front of a known urgent exit.
        if refresh:
            try:
                refresh_daily_inputs(db, grant, now)
            except Exception as exc:
                db.rollback()
                refresh_failure = type(exc).__name__
        if not injected_clock:
            now = datetime.now(UTC)
        # Never expand risk while any held position could not be evaluated.
        if any(d["action"] == "cover_blocked" for d in decisions):
            return _record(db, {"status": "blocked", "action": "exit_evaluation_blocked", "decisions": decisions}, now)
        entry_block = (
            "entries_disabled" if not settings.dev_paper_shorts_enabled else
            "pilot_paused" if grant.status != "active" else
            "policy_expired" if now >= pilot._utc(grant.expires_at) else
            "market_closed" if not bounds[0] <= now < bounds[1] else None
        )
        if entry_block:
            for symbol in grant.symbols:
                if symbol in positions:
                    continue
                try:
                    signal = daily_signal(db, symbol, now)
                    feed = pilot.feed_status(db, symbol, now=now)
                    decisions.append({"symbol": symbol, "action": entry_block, "evidence": signal,
                        "execution_feed": feed.get("execution_status", feed.get("status", "unknown"))})
                except (ValueError, pilot.StockPaperError) as exc:
                    decisions.append({"symbol": symbol, "action": "signal_unavailable", "reason": str(exc)})
            failed = refresh_failure or any(d["action"] == "signal_unavailable" for d in decisions)
            return _record(db, {"status": "blocked" if failed else "observed", "action": "no_new_entries",
                "reason": entry_block, "decisions": decisions, "refresh_failure": refresh_failure}, now)
        day = now.astimezone(NY).date()
        for symbol in grant.symbols:
            if symbol in positions:
                continue
            attempts = [r for r in rows if r.symbol == symbol and r.intent == "open_short"
                        and pilot._utc(r.created_at).astimezone(NY).date() == day]
            if any(r.filled_quantity > 0 for r in attempts) or len(attempts) >= 3:
                decisions.append({"symbol": symbol, "action": "daily_entry_limit"})
                continue
            if attempts and now - pilot._utc(attempts[-1].created_at) < timedelta(minutes=3):
                decisions.append({"symbol": symbol, "action": "entry_retry_cooldown"})
                continue
            try:
                signal = daily_signal(db, symbol, now)
                feed = pilot.feed_status(db, symbol, now=now)
                execution_feed = feed.get("execution_status", feed.get("status", "unknown"))
                if not signal["enter"]:
                    decisions.append({"symbol": symbol, "action": "hold_flat", "evidence": signal,
                                      "execution_feed": execution_feed})
                    continue
                bar = reference_bar(db, symbol, now)
                price = (Decimal(str(bar.close)) * Decimal("0.999")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                per_symbol_budget = min(grant.max_order_notional, grant.max_gross_notional / len(grant.symbols) / Decimal("1.05"))
                quantity = int((per_symbol_budget / price).to_integral_value(rounding=ROUND_DOWN))
                if quantity <= 0:
                    decisions.append({"symbol": symbol, "action": "below_whole_share_budget"})
                    continue
                result = _submit(db, gateway, grant, symbol, "open_short", quantity, price,
                    f"auto-{symbol}-{day.isoformat()}-entry-{len(attempts)}", "completed-session RSI bounce below SMA", now)
                decisions.append({"symbol": symbol, "action": "open_short", "evidence": signal,
                                  "execution_feed": execution_feed})
                return _record(db, {"status": "observed", "action": "open_short", "evidence": signal,
                                    "order": result, "decisions": decisions}, now)
            except (pilot.StockPaperError, ValueError) as exc:
                db.rollback()
                decisions.append({"symbol": symbol, "action": "entry_blocked", "reason": str(exc)})
                if any(r.active_slot is not None for r in pilot._orders(db)):
                    break
        return _record(db, {"status": "observed", "action": "hold", "decisions": decisions,
                            "refresh_failure": refresh_failure}, now)
    except Exception as exc:
        db.rollback()
        return _record(db, {"status": "blocked", "action": "cycle_blocked", "error_type": type(exc).__name__,
            "reason": str(exc) if isinstance(exc, pilot.StockPaperError) else "Automation cycle failed safely"}, now)
