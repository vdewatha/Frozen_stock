from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pandas as pd
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import PaperTrade, RiskRule, Strategy, StrategyMemory, StrategySignal
from app.services.audit import write_audit_log
from app.services.broker import submit_paper_order
from app.services.economic_data import summarize_macro_context
from app.services.market_regime import latest_market_regime
from app.services.news_sentiment import summarize_news_context
from app.services.probabilistic_model import predict_probabilities
from app.services.readiness import readiness_snapshot
from app.services.risk import DEFAULT_RISK_RULES, PortfolioState, StrategyState, approve_trade
from app.services.trusted_data import trusted_history, trusted_intraday_observation, UntrustedMarketData, TRUSTED_SOURCES
from app.services.strategies.registry import get_strategy

DEFAULT_PAPER_EQUITY = 100_000.0
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06
MAX_HOLDING_DAYS = 20


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 6)))


def _latest_price_frame(db: Session, symbol: str, limit: int = 260) -> tuple[pd.DataFrame, str]:
    return trusted_history(db, symbol, limit, require_active=False)


def _risk_rules(db: Session) -> dict:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    return DEFAULT_RISK_RULES | (rule.value if rule else {})


def _open_positions(db: Session) -> list[PaperTrade]:
    return db.query(PaperTrade).filter(PaperTrade.status == "open").all()


def _strategy_memory_state(db: Session, strategy_id: int, symbol: str) -> StrategyState:
    current_regime = _trusted_regime(db)
    regime_name = (current_regime or {}).get("market_regime")
    memory = (
        db.query(StrategyMemory)
        .filter(StrategyMemory.strategy_id == strategy_id, StrategyMemory.symbol == symbol)
        .filter(StrategyMemory.market_regime == regime_name if regime_name else StrategyMemory.market_regime == "unclassified")
        .order_by(StrategyMemory.last_updated.desc())
        .first()
    )
    consecutive_losses = _consecutive_losses(db, strategy_id, symbol)
    drawdown = abs(float(memory.avg_drawdown or 0)) if memory else 0.0
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).one()
    return StrategyState(status=strategy.current_status, drawdown=drawdown, consecutive_losses=consecutive_losses)


def _consecutive_losses(db: Session, strategy_id: int, symbol: str) -> int:
    closed = (
        db.query(PaperTrade)
        .filter(PaperTrade.strategy_id == strategy_id, PaperTrade.symbol == symbol, PaperTrade.status == "closed")
        .order_by(PaperTrade.exit_time.desc())
        .limit(20)
        .all()
    )
    count = 0
    for trade in closed:
        if float(trade.profit_loss or 0) < 0:
            count += 1
        else:
            break
    return count


def _portfolio_state(db: Session, rules: dict) -> PortfolioState:
    open_trades = _open_positions(db)
    exposures: dict[str, float] = {}
    strategy_positions: dict[str, int] = {}
    for trade in open_trades:
        notional = float(trade.entry_price or 0) * float(trade.quantity or 0)
        exposures[trade.symbol] = exposures.get(trade.symbol, 0.0) + notional / DEFAULT_PAPER_EQUITY
        strategy_key = str(trade.strategy_id or "unassigned")
        strategy_positions[strategy_key] = strategy_positions.get(strategy_key, 0) + 1
    return PortfolioState(
        open_positions_count=len(open_trades),
        open_positions_by_symbol=exposures,
        open_positions_by_strategy=strategy_positions,
        total_equity=DEFAULT_PAPER_EQUITY,
        kill_switch_enabled=bool(rules.get("kill_switch_enabled", False)),
    )


def _position_size(price: float, rules: dict, current_symbol_exposure: float) -> float:
    max_risk = DEFAULT_PAPER_EQUITY * float(rules.get("max_risk_per_trade", 0.01))
    risk_sized_qty = max_risk / max(price * STOP_LOSS_PCT, 1)
    exposure_room = max(float(rules.get("max_symbol_exposure", 0.10)) - current_symbol_exposure, 0)
    exposure_sized_qty = DEFAULT_PAPER_EQUITY * exposure_room / max(price, 1)
    return max(0.0, min(risk_sized_qty, exposure_sized_qty))


def _predictive_trade_evidence(symbol: str, prices: pd.DataFrame, source: str) -> dict:
    model_result = predict_probabilities(symbol, prices, source)
    predictions = model_result.get("predictions", [])
    candidates = [
        prediction
        for prediction in predictions
        if float(prediction.get("probability_up", 0)) >= 0.54 and float(prediction.get("expected_return", 0)) > 0.002
    ]
    best = max(candidates, key=lambda item: (float(item.get("expected_return", 0)), float(item.get("probability_up", 0))), default=None)
    return {
        "supports_long": best is not None,
        "best_horizon": best,
        "predictions": predictions,
        "warnings": model_result.get("warnings", []),
        "latest_features": model_result.get("latest_features", {}),
        "prediction_date": model_result.get("prediction_date"),
    }


def _model_informed_signal(signal, evidence: dict) -> tuple[str, float, float, str]:
    best = evidence.get("best_horizon") or {}
    probability_up = float(best.get("probability_up", signal.probability_up))
    expected_return = float(best.get("expected_return", 0))
    if signal.action == "BUY" and evidence["supports_long"]:
        confidence = clamp_like(min(0.95, (signal.confidence * 0.55) + (probability_up * 0.35) + min(max(expected_return, 0), 0.05) * 2))
        reason = (
            f"{signal.reason} Predictive model supports a long with "
            f"{probability_up:.1%} up probability and {expected_return:.2%} expected return "
            f"over {best.get('horizon_days')} trading days."
        )
        return "BUY", probability_up, confidence, reason
    if signal.action == "BUY":
        reason = f"{signal.reason} Predictive model does not show positive expected-return support, so the paper entry is held."
        return "HOLD", min(signal.probability_up, 0.5), min(signal.confidence, 0.5), reason
    return signal.action, signal.probability_up, signal.confidence, signal.reason


def clamp_like(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _trusted_regime(db):
    regime = latest_market_regime(db, auto_detect=False)
    source = (regime or {}).get("features", {}).get("source")
    macro = (regime or {}).get("features", {}).get("macro_context") or {}
    if macro and macro.get("status") != "excluded":
        return None
    if source not in TRUSTED_SOURCES and source not in {f"database:{s}" for s in TRUSTED_SOURCES}:
        return None
    return regime


def run_paper_signal(db: Session, symbol: str, strategy_slug: str) -> dict:
    symbol = symbol.upper()
    try:
        intraday_observation = trusted_intraday_observation(db, symbol)
    except UntrustedMarketData as exc:
        return {
            "symbol": symbol, "strategy": strategy_slug, "action": "BLOCKED",
            "approved": False, "reason": str(exc), "signal_id": None,
            "paper_trade_id": None, "price": 0.0, "quantity": 0.0,
            "confidence": 0.0, "broker_order": None,
        }
    strategy_row = db.query(Strategy).filter(Strategy.strategy_type == strategy_slug).one_or_none()
    if not strategy_row:
        raise ValueError(f"Unknown strategy: {strategy_slug}")

    readiness = readiness_snapshot(db)
    readiness_payload = jsonable_encoder(readiness)
    blocked_checks = [check for check in readiness["checks"] if check["status"] != "ready"]
    if not readiness["paper_trading_allowed"]:
        reason = "System readiness blocked paper signal: " + ", ".join(check["name"] for check in blocked_checks)
        write_audit_log(
            db,
            event_type="readiness_gate",
            entity_type="strategy",
            entity_id=strategy_row.id,
            action="paper_signal_preflight",
            status="blocked",
            message=reason,
            payload={"symbol": symbol, "strategy": strategy_slug, "readiness": readiness_payload},
        )
        db.commit()
        return {
            "symbol": symbol,
            "strategy": strategy_slug,
            "action": "BLOCKED",
            "approved": False,
            "reason": reason,
            "signal_id": None,
            "paper_trade_id": None,
            "price": 0.0,
            "quantity": 0.0,
            "confidence": 0.0,
            "broker_order": None,
        }

    prices, source = trusted_history(db, symbol)
    current_regime = _trusted_regime(db)
    regime_name = (current_regime or {}).get("market_regime", "unclassified")
    # Mock news and fallback macro data are display-only, never paper-decision evidence.
    news_context = {"status": "excluded", "summary": "News context excluded from paper decisions."}
    macro_context = {"status": "excluded", "summary": "Macro context excluded from paper decisions."}
    strategy = get_strategy(strategy_slug, strategy_row.parameters)
    signal = strategy.generate_signal(symbol, prices)
    predictive_evidence = _predictive_trade_evidence(symbol, prices, source)
    model_action, model_probability_up, model_confidence, model_reason = _model_informed_signal(signal, predictive_evidence)
    price = intraday_observation["close"]
    signal_payload = {
        "symbol": symbol,
        "strategy": str(strategy_row.id),
        "action": model_action,
        "probability_up": model_probability_up,
        "probability_down": 1 - model_probability_up,
        "confidence": model_confidence,
        "features": signal.features
        | {
            "data_source": source,
            "execution_observation": intraday_observation,
            "raw_strategy_action": signal.action,
            "raw_strategy_confidence": signal.confidence,
            "confidence": model_confidence,
            "market_regime": regime_name,
            "market_regime_features": (current_regime or {}).get("features", {}),
            "news_context": news_context,
            "macro_context": macro_context,
            "predictive_evidence": predictive_evidence,
        },
    }
    signal_row = StrategySignal(
        strategy_id=strategy_row.id,
        symbol=symbol,
        signal_time=datetime.utcnow(),
        action=model_action,
        probability_up=_decimal(model_probability_up),
        probability_down=_decimal(1 - model_probability_up),
        confidence=_decimal(model_confidence),
        reason=f"{model_reason} News context: {news_context['summary']} Macro context: {macro_context['summary']}",
        features=jsonable_encoder(signal_payload["features"]),
    )
    db.add(signal_row)
    db.flush()

    rules = _risk_rules(db)
    portfolio = _portfolio_state(db, rules)
    strategy_state = _strategy_memory_state(db, strategy_row.id, symbol)
    approved, reason = approve_trade(signal_payload, portfolio, strategy_state, rules)
    write_audit_log(
        db,
        event_type="risk_decision",
        entity_type="strategy_signal",
        entity_id=signal_row.id,
        action="approve_trade",
        status="approved" if approved else "blocked",
        message=reason,
        payload={
            "symbol": symbol,
            "strategy": strategy_slug,
            "signal": signal_payload,
            "portfolio": {
                "open_positions_count": portfolio.open_positions_count,
                "open_positions_by_symbol": portfolio.open_positions_by_symbol,
                "open_positions_by_strategy": portfolio.open_positions_by_strategy,
                "kill_switch_enabled": portfolio.kill_switch_enabled,
            },
            "strategy_state": {
                "status": strategy_state.status,
                "drawdown": strategy_state.drawdown,
                "consecutive_losses": strategy_state.consecutive_losses,
            },
            "rules": rules,
        },
    )
    quantity = _position_size(price, rules, (portfolio.open_positions_by_symbol or {}).get(symbol, 0.0)) if approved else 0.0
    if approved and quantity <= 0.000001:
        approved = False
        reason = "No remaining symbol exposure capacity."
        quantity = 0.0
        write_audit_log(
            db,
            event_type="risk_decision",
            entity_type="strategy_signal",
            entity_id=signal_row.id,
            action="position_size",
            status="blocked",
            message=reason,
            payload={"price": price, "rules": rules, "symbol": symbol},
        )

    trade_id = None
    broker_order = None
    if approved and model_action == "BUY":
        trade = PaperTrade(
            strategy_id=strategy_row.id,
            symbol=symbol,
            side="BUY",
            entry_time=datetime.utcnow(),
            entry_price=_decimal(price),
            quantity=_decimal(quantity),
            status="open",
            reason_entered=f"{model_reason} News context: {news_context['summary']} Macro context: {macro_context['summary']} Risk approval: {reason}",
            features_at_entry=jsonable_encoder(signal_payload["features"]),
        )
        db.add(trade)
        db.flush()
        trade_id = trade.id
        broker_order = submit_paper_order(
            db,
            symbol=symbol,
            side="buy",
            quantity=quantity,
            source="paper_trading_signal",
            paper_trade_id=trade.id,
            signal_id=signal_row.id,
        )
        write_audit_log(
            db,
            event_type="paper_trade",
            entity_type="paper_trade",
            entity_id=trade.id,
            action="open",
            status="open",
            message="Internal paper trade opened after risk approval.",
            payload={"symbol": symbol, "strategy": strategy_slug, "price": price, "quantity": quantity},
        )
    elif approved:
        approved = False
        reason = "Short paper trades are not enabled in Version 1."

    db.commit()
    return {
        "symbol": symbol,
        "strategy": strategy_slug,
        "action": model_action,
        "approved": approved,
        "reason": reason,
        "signal_id": signal_row.id,
        "paper_trade_id": trade_id,
        "price": round(price, 4),
        "quantity": round(quantity, 6),
        "confidence": round(model_confidence, 4),
        "broker_order": broker_order,
    }


def reconcile_open_paper_trades(db: Session) -> dict:
    open_trades = _open_positions(db)
    closed_ids: list[int] = []
    blocked_ids: list[int] = []
    for trade in open_trades:
        try:
            observation = trusted_intraday_observation(db, trade.symbol)
        except UntrustedMarketData as exc:
            blocked_ids.append(trade.id)
            write_audit_log(db, event_type="market_data_gate", entity_type="paper_trade", entity_id=trade.id,
                            action="reconcile_close", status="blocked", message=str(exc), payload={"symbol": trade.symbol})
            continue
        current_price = observation["close"]
        entry_price = float(trade.entry_price or 0)
        if entry_price <= 0:
            continue

        pnl_pct = current_price / entry_price - 1
        holding_days = max((datetime.utcnow() - (trade.entry_time or datetime.utcnow())).days, 0)
        exit_reason = None
        if pnl_pct <= -STOP_LOSS_PCT:
            exit_reason = "Closed by stop-loss rule."
        elif pnl_pct >= TAKE_PROFIT_PCT:
            exit_reason = "Closed by take-profit rule."
        elif holding_days >= MAX_HOLDING_DAYS:
            exit_reason = "Closed by max holding period."

        if exit_reason:
            quantity = float(trade.quantity or 0)
            profit_loss = (current_price - entry_price) * quantity
            trade.exit_time = datetime.utcnow()
            trade.exit_price = _decimal(current_price)
            trade.status = "closed"
            trade.profit_loss = _decimal(profit_loss)
            trade.profit_loss_pct = _decimal(pnl_pct)
            trade.reason_exited = exit_reason
            closed_ids.append(trade.id)
            if trade.strategy_id:
                _update_strategy_memory(db, trade.strategy_id, trade.symbol, _trade_regime(trade))
            write_audit_log(
                db,
                event_type="paper_trade",
                entity_type="paper_trade",
                entity_id=trade.id,
                action="reconcile_close",
                status="closed",
                message=exit_reason,
                payload={
                    "symbol": trade.symbol,
                    "profit_loss": profit_loss,
                    "profit_loss_pct": pnl_pct,
                    "execution_observation": observation,
                },
            )

    db.commit()
    return {"checked": len(open_trades), "closed": len(closed_ids), "closed_trade_ids": closed_ids, "blocked_trade_ids": blocked_ids}


def close_paper_trade(db: Session, trade_id: int, reason: str = "Manual paper close.") -> dict:
    trade = db.query(PaperTrade).filter(PaperTrade.id == trade_id).one_or_none()
    if not trade:
        raise ValueError(f"Unknown paper trade: {trade_id}")
    if trade.status != "open":
        return {
            "paper_trade_id": trade.id,
            "status": trade.status or "unknown",
            "profit_loss": float(trade.profit_loss or 0),
            "profit_loss_pct": float(trade.profit_loss_pct or 0),
            "reason": trade.reason_exited or "Trade is not open.",
        }

    prices, _source = _latest_price_frame(db, trade.symbol)
    current_price = float(prices.iloc[-1]["close"])
    entry_price = float(trade.entry_price or 0)
    quantity = float(trade.quantity or 0)
    pnl_pct = current_price / entry_price - 1 if entry_price else 0
    profit_loss = (current_price - entry_price) * quantity

    trade.exit_time = datetime.utcnow()
    trade.exit_price = _decimal(current_price)
    trade.status = "closed"
    trade.profit_loss = _decimal(profit_loss)
    trade.profit_loss_pct = _decimal(pnl_pct)
    trade.reason_exited = reason
    if trade.strategy_id:
        db.flush()
        _update_strategy_memory(db, trade.strategy_id, trade.symbol, _trade_regime(trade))
    write_audit_log(
        db,
        event_type="paper_trade",
        entity_type="paper_trade",
        entity_id=trade.id,
        action="manual_close",
        status="closed",
        message=reason,
        payload={"symbol": trade.symbol, "profit_loss": profit_loss, "profit_loss_pct": pnl_pct},
    )
    db.commit()
    return {
        "paper_trade_id": trade.id,
        "status": "closed",
        "profit_loss": round(profit_loss, 4),
        "profit_loss_pct": round(pnl_pct, 6),
        "reason": reason,
    }


def reduce_paper_trade(db: Session, trade_id: int, reduce_pct: float, reason: str = "Manual paper exposure reduction.") -> dict:
    if reduce_pct <= 0 or reduce_pct > 1:
        raise ValueError("Reduction percent must be greater than 0 and at most 1.")

    trade = db.query(PaperTrade).filter(PaperTrade.id == trade_id).one_or_none()
    if not trade:
        raise ValueError(f"Unknown paper trade: {trade_id}")
    if trade.status != "open":
        return {
            "paper_trade_id": trade.id,
            "status": trade.status or "unknown",
            "reduced_quantity": 0.0,
            "remaining_quantity": float(trade.quantity or 0),
            "realized_profit_loss": float(trade.profit_loss or 0),
            "realized_profit_loss_pct": float(trade.profit_loss_pct or 0),
            "exit_price": float(trade.exit_price or 0),
            "reason": trade.reason_exited or "Trade is not open.",
            "broker_order": None,
        }

    prices, _source = _latest_price_frame(db, trade.symbol)
    current_price = float(prices.iloc[-1]["close"])
    entry_price = float(trade.entry_price or 0)
    quantity = float(trade.quantity or 0)
    if quantity <= 0:
        raise ValueError("Open paper trade has no quantity to reduce.")

    reduced_quantity = quantity * reduce_pct
    if quantity - reduced_quantity <= 0.000001:
        reduced_quantity = quantity
    remaining_quantity = max(quantity - reduced_quantity, 0.0)
    pnl_pct = current_price / entry_price - 1 if entry_price else 0
    realized_pl = (current_price - entry_price) * reduced_quantity if trade.side == "BUY" else (entry_price - current_price) * reduced_quantity
    broker_side = "sell" if trade.side == "BUY" else "buy"

    broker_order = submit_paper_order(
        db,
        symbol=trade.symbol,
        side=broker_side,
        quantity=reduced_quantity,
        source="paper_trade_manual_reduce",
        paper_trade_id=trade.id,
    )

    if remaining_quantity <= 0.000001:
        total_pl = (current_price - entry_price) * quantity if trade.side == "BUY" else (entry_price - current_price) * quantity
        trade.exit_time = datetime.utcnow()
        trade.exit_price = _decimal(current_price)
        trade.quantity = _decimal(quantity)
        trade.status = "closed"
        trade.profit_loss = _decimal(total_pl)
        trade.profit_loss_pct = _decimal(pnl_pct)
        trade.reason_exited = reason
        action = "manual_reduce_close"
        status = "closed"
        message = reason
        if trade.strategy_id:
            db.flush()
            _update_strategy_memory(db, trade.strategy_id, trade.symbol, _trade_regime(trade))
    else:
        trade.quantity = _decimal(remaining_quantity)
        reductions = list((trade.features_at_entry or {}).get("manual_reductions", []))
        reductions.append(
            {
                "reduced_at": datetime.utcnow().isoformat(),
                "reduced_quantity": round(reduced_quantity, 6),
                "remaining_quantity": round(remaining_quantity, 6),
                "price": round(current_price, 4),
                "realized_profit_loss": round(realized_pl, 4),
                "realized_profit_loss_pct": round(pnl_pct, 6),
                "reason": reason,
            }
        )
        features = dict(trade.features_at_entry or {})
        features["manual_reductions"] = reductions
        trade.features_at_entry = features
        action = "manual_reduce"
        status = "open"
        message = "Paper trade exposure reduced; remaining quantity stays open."

    write_audit_log(
        db,
        event_type="paper_trade",
        entity_type="paper_trade",
        entity_id=trade.id,
        action=action,
        status=status,
        message=message,
        payload={
            "symbol": trade.symbol,
            "side": trade.side,
            "entry_price": entry_price,
            "exit_price": current_price,
            "reduced_quantity": reduced_quantity,
            "remaining_quantity": remaining_quantity,
            "realized_profit_loss": realized_pl,
            "realized_profit_loss_pct": pnl_pct,
            "reason": reason,
            "broker_order": broker_order,
        },
    )
    db.commit()
    return {
        "paper_trade_id": trade.id,
        "status": status,
        "reduced_quantity": round(reduced_quantity, 6),
        "remaining_quantity": round(remaining_quantity, 6),
        "realized_profit_loss": round(realized_pl, 4),
        "realized_profit_loss_pct": round(pnl_pct, 6),
        "exit_price": round(current_price, 4),
        "reason": reason if status == "closed" else message,
        "broker_order": broker_order,
    }


def _trade_regime(trade: PaperTrade) -> str:
    features = trade.features_at_entry or {}
    return str(features.get("market_regime") or "unclassified")


def _update_strategy_memory(db: Session, strategy_id: int, symbol: str, market_regime: str = "unclassified") -> None:
    closed = (
        db.query(PaperTrade)
        .filter(PaperTrade.strategy_id == strategy_id, PaperTrade.symbol == symbol, PaperTrade.status == "closed")
        .order_by(PaperTrade.exit_time.desc())
        .all()
    )
    closed = [trade for trade in closed if _trade_regime(trade) == market_regime]
    if not closed:
        return

    returns = [float(trade.profit_loss_pct or 0) for trade in closed]
    wins = [trade for trade in closed if float(trade.profit_loss or 0) > 0]
    losses = [trade for trade in closed if float(trade.profit_loss or 0) <= 0]
    gross_profit = sum(float(trade.profit_loss or 0) for trade in wins)
    gross_loss = abs(sum(float(trade.profit_loss or 0) for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (3.0 if gross_profit else 0.0)
    win_rate = len(wins) / len(closed)
    avg_return = sum(returns) / len(returns)
    avg_drawdown = min(returns)
    confidence_score = max(0.0, min(1.0, 0.5 + avg_return * 5 + (profit_factor - 1) * 0.1))

    memory = (
        db.query(StrategyMemory)
        .filter(StrategyMemory.strategy_id == strategy_id, StrategyMemory.symbol == symbol, StrategyMemory.market_regime == market_regime)
        .one_or_none()
    )
    if not memory:
        memory = StrategyMemory(strategy_id=strategy_id, symbol=symbol, market_regime=market_regime)
        db.add(memory)
    memory.sample_size = len(closed)
    memory.avg_return = _decimal(avg_return)
    memory.avg_drawdown = _decimal(avg_drawdown)
    memory.win_rate = _decimal(win_rate)
    memory.profit_factor = _decimal(profit_factor)
    memory.confidence_score = _decimal(confidence_score)
    memory.last_updated = datetime.utcnow()
    memory.notes = f"Updated from internal paper-trading reconciliation for {market_regime}."


def update_all_strategy_memory(db: Session) -> int:
    pairs = db.query(PaperTrade.strategy_id, PaperTrade.symbol).filter(PaperTrade.status == "closed").distinct().all()
    updated = 0
    for strategy_id, symbol in pairs:
        if strategy_id:
            regimes = {
                _trade_regime(trade)
                for trade in db.query(PaperTrade)
                .filter(PaperTrade.strategy_id == strategy_id, PaperTrade.symbol == symbol, PaperTrade.status == "closed")
                .all()
            }
            for regime in regimes:
                _update_strategy_memory(db, strategy_id, symbol, regime)
                updated += 1
    db.commit()
    return updated
