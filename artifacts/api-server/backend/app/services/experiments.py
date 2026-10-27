from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Strategy, StrategyBacktest, StrategyExperiment
from app.services.audit import write_audit_log
from app.services.backtest_statistics import deflated_sharpe_ratio
from app.services.learning import propose_parameter_experiments
from app.services.trusted_data import UntrustedMarketData, trusted_history

# Probability that the selected candidate's true Sharpe beats the best Sharpe
# expected from luck across all candidates tried on the same validation data.
MIN_VALIDATION_DEFLATED_SHARPE = 0.95
BENCHMARK_SYMBOL = "SPY"


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


def _sharpe_inputs(result: dict) -> tuple[float, int, float, float]:
    stats = result.get("statistics") or {}
    return (
        float(stats.get("sharpe_per_period", 0.0)),
        int(stats.get("observations", 0)),
        float(stats.get("skewness", 0.0)),
        float(stats.get("kurtosis", 3.0)),
    )


def multiple_testing_summary(selected: dict, all_results: list[dict]) -> dict:
    """Deflated Sharpe of ``selected`` given every trial run on the same data."""
    sharpe, observations, skewness, kurtosis = _sharpe_inputs(selected)
    trial_sharpes = [_sharpe_inputs(result)[0] for result in all_results]
    return deflated_sharpe_ratio(
        sharpe, observations, trials=len(all_results), trial_sharpes=trial_sharpes,
        skewness=skewness, kurtosis=kurtosis,
    )


def _comparison_metrics(result: dict) -> dict:
    return {
        "total_return": result["total_return"],
        "max_drawdown": result["max_drawdown"],
        "win_rate": result["win_rate"],
        "profit_factor": result["profit_factor"],
        "number_of_trades": result["number_of_trades"],
        "excess_return": result.get("excess_return"),
        "beats_baselines": result.get("beats_baselines"),
        "sharpe_ci": ((result.get("statistics") or {}).get("bootstrap") or {}).get("sharpe_ci"),
    }


def _benchmark_prices(db: Session, symbol: str):
    if symbol == BENCHMARK_SYMBOL:
        return None
    try:
        prices, _source = trusted_history(db, BENCHMARK_SYMBOL, 420, minimum=100)
    except UntrustedMarketData:
        return None
    return prices


def _decision(
    old_result: dict,
    new_result: dict,
    *,
    validation_old: dict | None = None,
    validation_new: dict | None = None,
    validation_deflated_sharpe: float | None = None,
    validation_trials: int | None = None,
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
        # Several candidates are validated on the same tail and the best one
        # is applied, so the tail is itself a selection. Require the
        # candidate's Sharpe to survive deflation for the number of trials.
        if validation_deflated_sharpe is None or validation_deflated_sharpe < MIN_VALIDATION_DEFLATED_SHARPE:
            shown = "unavailable" if validation_deflated_sharpe is None else f"{validation_deflated_sharpe:.3f}"
            return "needs_more_data", (
                f"Out-of-sample deflated Sharpe ratio {shown} across {validation_trials or 'unknown'} "
                f"trials is below {MIN_VALIDATION_DEFLATED_SHARPE:.2f}; the improvement may be selection luck."
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
    from app.services.backtester import BacktestConfig, run_backtest

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
    benchmark_prices = _benchmark_prices(db, symbol)

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

    old_result = run_backtest(symbol, strategy_slug, evaluation_prices, config, current_parameters,
                              benchmark_prices=benchmark_prices)
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
        validation_old = run_backtest(symbol, strategy_slug, validation_prices, config, current_parameters,
                                      benchmark_prices=benchmark_prices)

    # Run every CPU-bound backtest before persisting so multiple-testing
    # statistics can account for all trials on the same history. No database
    # transaction is open here (committed above).
    candidate_runs = []
    for proposal in proposals:
        new_parameters = proposal["new_parameters"]
        new_result = run_backtest(symbol, strategy_slug, evaluation_prices, config, new_parameters,
                                  benchmark_prices=benchmark_prices)
        candidate_validation = None
        if validation_prices is not None:
            candidate_validation = run_backtest(symbol, strategy_slug, validation_prices, config, new_parameters,
                                                benchmark_prices=benchmark_prices)
        candidate_runs.append((proposal, new_result, candidate_validation))
    in_sample_trials = [old_result] + [run[1] for run in candidate_runs]
    validation_trials = [run[2] for run in candidate_runs if run[2] is not None]

    for proposal, new_result, validation_new in candidate_runs:
        new_parameters = proposal["new_parameters"]
        new_backtest = _save_backtest(db, strategy_id, symbol, new_parameters, new_result)
        in_sample_dsr = multiple_testing_summary(new_result, in_sample_trials)
        validation_dsr = (
            multiple_testing_summary(validation_new, validation_trials) if validation_new is not None else None
        )
        decision, reason = _decision(
            old_result,
            new_result,
            validation_old=validation_old,
            validation_new=validation_new,
            validation_deflated_sharpe=validation_dsr["deflated_sharpe_ratio"] if validation_dsr else None,
            validation_trials=len(validation_trials) or None,
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
            "old_metrics": _comparison_metrics(old_result),
            "new_metrics": _comparison_metrics(new_result),
            "multiple_testing": {
                "in_sample": in_sample_dsr,
                "validation": validation_dsr,
                "minimum_validation_deflated_sharpe": MIN_VALIDATION_DEFLATED_SHARPE,
                "note": "Trials share one history; the best in-sample score is expected to be optimistic.",
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
