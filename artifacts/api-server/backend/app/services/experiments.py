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


def _save_backtest(db: Session, strategy_id: int, symbol: str, parameters: dict, result: dict) -> StrategyBacktest:
    equity_curve = result.get("equity_curve") or []
    test_start = equity_curve[0]["date"] if equity_curve else None
    test_end = equity_curve[-1]["date"] if equity_curve else None
    row = StrategyBacktest(
        strategy_id=strategy_id,
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


def _decision(
    old_result: dict,
    new_result: dict,
    *,
    validation_old: dict | None = None,
    validation_new: dict | None = None,
) -> tuple[str, str]:
    delta_score = new_result["score"] - old_result["score"]
    if new_result["rejected"]:
        return "rejected", "; ".join(new_result["rejection_reasons"])
    if new_result["number_of_trades"] < max(10, int(old_result["number_of_trades"] * 0.6)):
        return "needs_more_data", "Candidate has too few trades relative to the current parameter set."
    if new_result["max_drawdown"] > old_result["max_drawdown"] + 0.02:
        return "rejected", "Candidate improved score at the cost of materially higher drawdown."
    if delta_score >= 0.03:
        if validation_old is None or validation_new is None:
            return "needs_more_data", (
                "In-sample score improved, but an untouched out-of-sample validation run "
                "is required before promotion."
            )
        validation_delta = validation_new["score"] - validation_old["score"]
        if validation_new["rejected"]:
            return "rejected", "Candidate failed the untouched out-of-sample validation run."
        if validation_delta < 0.03:
            return "needs_more_data", (
                f"Out-of-sample score delta {validation_delta:.3f} is below the promotion threshold."
            )
        return "promoted", (
            f"Out-of-sample score improved by {validation_delta:.3f} after an in-sample "
            "improvement without breaching drawdown controls."
        )
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

    # Materialize the small immutable inputs before the CPU-bound work. The
    # ORM instance is intentionally not carried through the long experiment
    # loop; expired attributes can reopen an idle PostgreSQL transaction just
    # before a later backtest insert.
    strategy_id = int(strategy.id)
    strategy_name = strategy.name
    current_parameters = dict(strategy.parameters or {})

    prices, source = trusted_history(db, symbol, 420, minimum=100)

    # Loading trusted history starts a database transaction. Backtests are
    # CPU-bound and can run longer than PostgreSQL's idle-in-transaction
    # timeout, so release that connection before computing experiments. The
    # writes below open a fresh transaction and are committed at the end.
    db.commit()

    config = BacktestConfig()
    evaluation_prices = prices
    if apply_promotions:
        split_at = max(61, int(len(prices) * 0.8))
        if split_at < len(prices) - 1:
            # Candidate selection must not inspect the rows reserved for the
            # untouched validation run. Include only the training prefix in
            # both the baseline and candidate backtests; the validation tail
            # gets its indicator warm-up separately below.
            evaluation_prices = prices.iloc[:split_at].copy()
        else:
            split_at = None
    else:
        split_at = None

    old_result = run_backtest(symbol, strategy_slug, evaluation_prices, config, current_parameters)
    old_backtest = _save_backtest(db, strategy_id, symbol, current_parameters, old_result)
    # Close the transaction before the next CPU-bound experiment. PostgreSQL
    # may terminate an idle transaction while a long backtest is running.
    db.commit()

    proposals = propose_parameter_experiments(strategy_slug, current_parameters)[: max(1, min(max_candidates, 10))]
    experiments = []
    promoted_candidates = []
    validation_old = validation_new = None
    validation_prices = None
    if split_at is not None:
        # Keep a warm-up window for indicators, but score only the untouched
        # tail after the split. This path is used only for the explicit
        # promotion workflow; research fan-out stays fast.
        validation_prices = prices.iloc[max(0, split_at - 60):].copy()
        validation_old = run_backtest(symbol, strategy_slug, validation_prices, config, current_parameters)

    for proposal in proposals:
        new_parameters = proposal["new_parameters"]
        new_result = run_backtest(symbol, strategy_slug, evaluation_prices, config, new_parameters)
        new_backtest = _save_backtest(db, strategy_id, symbol, new_parameters, new_result)
        if validation_prices is not None:
            validation_new = run_backtest(symbol, strategy_slug, validation_prices, config, new_parameters)
        decision, reason = _decision(
            old_result,
            new_result,
            validation_old=validation_old,
            validation_new=validation_new,
        )
        if not apply_promotions and decision == "promoted":
            # Research fan-out is never promotion-authorized, even if the
            # decision policy is changed or replaced in the future.
            decision = "needs_more_data"
            reason = "Research-only experiment; explicit promotion validation was not requested."
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
            "validation": ({
                "period_start": str(validation_prices.iloc[60]["date"]),
                "period_end": str(validation_prices.iloc[-1]["date"]),
                "old_score": validation_old["score"],
                "new_score": validation_new["score"],
                "score_delta": round(validation_new["score"] - validation_old["score"], 4),
                "old_rejected": validation_old["rejected"],
                "new_rejected": validation_new["rejected"],
            } if validation_prices is not None and validation_old is not None and validation_new is not None else {
                "status": "not_run",
                "reason": "Research-only experiment; no promotion validation was requested.",
            }),
            "reason": reason,
        }
        experiment = StrategyExperiment(
            strategy_id=strategy_id,
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
        # Each candidate is independent. Persist it before the next
        # backtest, which may take longer than the database idle timeout.
        db.commit()

    applied_parameters: Optional[dict] = None
    if apply_promotions and promoted_candidates:
        _score, applied_parameters, experiment_name = sorted(promoted_candidates, reverse=True, key=lambda row: row[0])[0]
        strategy = db.get(Strategy, strategy_id)
        strategy.parameters = applied_parameters
        strategy.updated_at = datetime.utcnow()
        write_audit_log(
            db,
            event_type="strategy_experiment",
            entity_type="strategy",
            entity_id=strategy.id,
            action="apply_promoted_parameters",
            status="applied",
            message=f"Applied {experiment_name} parameters to {strategy_name}.",
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
