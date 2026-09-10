from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Strategy, StrategyBacktest, StrategyExperiment
from app.services.audit import write_audit_log
from app.services.backtester import BacktestConfig, run_backtest
from app.services.learning import propose_parameter_experiments
from app.services.trusted_data import trusted_history


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 6)))


def _save_backtest(db: Session, strategy: Strategy, symbol: str, parameters: dict, result: dict) -> StrategyBacktest:
    equity_curve = result.get("equity_curve") or []
    test_start = equity_curve[0]["date"] if equity_curve else None
    test_end = equity_curve[-1]["date"] if equity_curve else None
    row = StrategyBacktest(
        strategy_id=strategy.id,
        symbol=symbol,
        test_start=datetime.strptime(test_start, "%Y-%m-%d").date() if test_start else None,
        test_end=datetime.strptime(test_end, "%Y-%m-%d").date() if test_end else None,
        parameters=parameters,
        total_return=_decimal(result["total_return"]),
        annualized_return=_decimal(result["annualized_return"]),
        sharpe_ratio=_decimal(result["sharpe_ratio"]),
        sortino_ratio=_decimal(result["sortino_ratio"]),
        max_drawdown=_decimal(result["max_drawdown"]),
        win_rate=_decimal(result["win_rate"]),
        profit_factor=_decimal(result["profit_factor"]),
        number_of_trades=int(result["number_of_trades"]),
        score=_decimal(result["score"]),
    )
    db.add(row)
    db.flush()
    return row


def _decision(old_result: dict, new_result: dict) -> tuple[str, str]:
    delta_score = new_result["score"] - old_result["score"]
    if new_result["rejected"]:
        return "rejected", "; ".join(new_result["rejection_reasons"])
    if new_result["number_of_trades"] < max(10, int(old_result["number_of_trades"] * 0.6)):
        return "needs_more_data", "Candidate has too few trades relative to the current parameter set."
    if new_result["max_drawdown"] > old_result["max_drawdown"] + 0.02:
        return "rejected", "Candidate improved score at the cost of materially higher drawdown."
    if delta_score >= 0.03:
        return "promoted", f"Candidate score improved by {delta_score:.3f} without breaching drawdown controls."
    return "needs_more_data", f"Candidate score delta {delta_score:.3f} is below the promotion threshold."


def run_strategy_experiments(
    db: Session,
    *,
    symbol: str,
    strategy_slug: str,
    max_candidates: int = 3,
    apply_promotions: bool = False,
) -> dict:
    symbol = symbol.strip().upper()
    strategy = db.query(Strategy).filter(Strategy.strategy_type == strategy_slug).one_or_none()
    if not strategy:
        raise ValueError(f"Unknown strategy: {strategy_slug}")

    prices, source = trusted_history(db, symbol, 420, minimum=100)

    current_parameters = strategy.parameters or {}
    config = BacktestConfig()
    old_result = run_backtest(symbol, strategy_slug, prices, config, current_parameters)
    old_backtest = _save_backtest(db, strategy, symbol, current_parameters, old_result)

    proposals = propose_parameter_experiments(strategy_slug, current_parameters)[: max(1, min(max_candidates, 10))]
    experiments = []
    promoted_candidates = []

    for proposal in proposals:
        new_parameters = proposal["new_parameters"]
        new_result = run_backtest(symbol, strategy_slug, prices, config, new_parameters)
        new_backtest = _save_backtest(db, strategy, symbol, new_parameters, new_result)
        decision, reason = _decision(old_result, new_result)
        if decision == "promoted":
            promoted_candidates.append((new_result["score"], new_parameters, proposal["experiment_name"]))

        summary = {
            "symbol": symbol,
            "source": source,
            "old_backtest_id": old_backtest.id,
            "candidate_backtest_id": new_backtest.id,
            "old_score": old_result["score"],
            "new_score": new_result["score"],
            "delta_score": round(new_result["score"] - old_result["score"], 4),
            "old_metrics": {
                "total_return": old_result["total_return"],
                "max_drawdown": old_result["max_drawdown"],
                "win_rate": old_result["win_rate"],
                "profit_factor": old_result["profit_factor"],
                "number_of_trades": old_result["number_of_trades"],
            },
            "new_metrics": {
                "total_return": new_result["total_return"],
                "max_drawdown": new_result["max_drawdown"],
                "win_rate": new_result["win_rate"],
                "profit_factor": new_result["profit_factor"],
                "number_of_trades": new_result["number_of_trades"],
            },
            "reason": reason,
        }
        experiment = StrategyExperiment(
            strategy_id=strategy.id,
            experiment_name=proposal["experiment_name"],
            old_parameters=current_parameters,
            new_parameters=new_parameters,
            hypothesis=proposal["hypothesis"],
            backtest_result_id=new_backtest.id,
            paper_result_summary=summary,
            decision=decision,
        )
        db.add(experiment)
        db.flush()
        write_audit_log(
            db,
            event_type="strategy_experiment",
            entity_type="strategy_experiment",
            entity_id=experiment.id,
            action="run_parameter_comparison",
            status=decision,
            message=f"{proposal['experiment_name']}: {reason}",
            payload=summary,
        )
        experiments.append(experiment)

    applied_parameters: Optional[dict] = None
    if apply_promotions and promoted_candidates:
        _score, applied_parameters, experiment_name = sorted(promoted_candidates, reverse=True, key=lambda row: row[0])[0]
        strategy.parameters = applied_parameters
        strategy.updated_at = datetime.utcnow()
        write_audit_log(
            db,
            event_type="strategy_experiment",
            entity_type="strategy",
            entity_id=strategy.id,
            action="apply_promoted_parameters",
            status="applied",
            message=f"Applied {experiment_name} parameters to {strategy.name}.",
            payload={"old_parameters": current_parameters, "new_parameters": applied_parameters},
        )

    db.commit()
    return {
        "symbol": symbol,
        "strategy": strategy_slug,
        "source": source,
        "baseline_backtest_id": old_backtest.id,
        "baseline_score": old_result["score"],
        "applied_parameters": applied_parameters,
        "experiments": list_strategy_experiments(db, symbol=symbol, strategy_slug=strategy_slug, limit=len(experiments)),
    }


def list_strategy_experiments(
    db: Session,
    *,
    symbol: Optional[str] = None,
    strategy_slug: Optional[str] = None,
    limit: int = 50,
) -> list[StrategyExperiment]:
    query = db.query(StrategyExperiment).outerjoin(Strategy, StrategyExperiment.strategy_id == Strategy.id)
    if strategy_slug:
        query = query.filter(Strategy.strategy_type == strategy_slug)
    if symbol:
        query = query.filter(StrategyExperiment.paper_result_summary["symbol"].as_string() == symbol.strip().upper())
    return query.order_by(StrategyExperiment.created_at.desc(), StrategyExperiment.id.desc()).limit(min(limit, 200)).all()
