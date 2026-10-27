from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import experiments
from app.services.experiments import _decision, multiple_testing_summary


def _result(*, score, rejected=False, trades=20, drawdown=0.10):
    return {
        "score": score,
        "rejected": rejected,
        "number_of_trades": trades,
        "max_drawdown": drawdown,
    }


def test_in_sample_improvement_requires_out_of_sample_validation():
    decision, reason = _decision(_result(score=0.20), _result(score=0.24))

    assert decision == "needs_more_data"
    assert "out-of-sample" in reason


def test_promotion_requires_validation_improvement():
    decision, reason = _decision(
        _result(score=0.20),
        _result(score=0.24),
        validation_old=_result(score=0.10),
        validation_new=_result(score=0.14),
        validation_deflated_sharpe=0.97,
        validation_trials=3,
    )

    assert decision == "promoted"
    assert "Out-of-sample" in reason


def test_failed_validation_rejects_in_sample_improvement():
    decision, reason = _decision(
        _result(score=0.20),
        _result(score=0.24),
        validation_old=_result(score=0.10),
        validation_new=_result(score=0.20, rejected=True),
    )

    assert decision == "rejected"
    assert "validation" in reason


def test_promotion_requires_deflated_sharpe_across_trials():
    common = dict(validation_old=_result(score=0.10), validation_new=_result(score=0.14))
    missing = _decision(_result(score=0.20), _result(score=0.24), **common)
    weak = _decision(_result(score=0.20), _result(score=0.24), **common,
                     validation_deflated_sharpe=0.80, validation_trials=5)

    assert missing[0] == "needs_more_data" and "deflated Sharpe" in missing[1]
    assert weak[0] == "needs_more_data" and "5 trials" in weak[1]


def _stats_result(sharpe, observations=300):
    return {"statistics": {"sharpe_per_period": sharpe, "observations": observations,
                           "skewness": 0.0, "kurtosis": 3.0}}


def test_multiple_testing_summary_deflates_best_of_many():
    best = _stats_result(0.08)
    alone = multiple_testing_summary(best, [best])
    crowd = [best] + [_stats_result(value) for value in (-0.06, 0.05, -0.02, 0.07, 0.0, -0.08, 0.03)]
    deflated = multiple_testing_summary(best, crowd)

    assert alone["trials"] == 1 and deflated["trials"] == 8
    assert deflated["deflated_sharpe_ratio"] < alone["deflated_sharpe_ratio"]


def _backtest(score, sharpe):
    return {
        "score": score, "rejected": False, "rejection_reasons": [], "number_of_trades": 40,
        "max_drawdown": 0.05, "total_return": 0.05, "annualized_return": 0.05, "sharpe_ratio": 1.0,
        "sortino_ratio": 1.0, "win_rate": 0.5, "profit_factor": 1.3, "equity_curve": [],
        "excess_return": {"vs_cash": 0.05}, "beats_baselines": {"cash": True},
        "statistics": {"sharpe_per_period": sharpe, "observations": 80, "skewness": 0.0, "kurtosis": 3.0,
                       "bootstrap": {"sharpe_ci": [-0.5, 2.0]}},
    }


def test_experiment_run_reports_trials_and_blocks_lucky_promotion():
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = SimpleNamespace(
        id=1, name="MA", parameters={"short_window": 20})
    db.get.return_value = SimpleNamespace(id=1, parameters={}, updated_at=None)
    prices = __import__("pandas").DataFrame({"date": [f"2025-01-{i % 28 + 1:02d}" for i in range(420)]})
    proposals = [{"experiment_name": f"c{i}", "new_parameters": {"short_window": i}, "hypothesis": "h"}
                 for i in range(3)]
    # Current parameters score 0.20; every candidate improves in- and out-of-sample
    # by >= 0.03, but with a modest Sharpe over a short validation tail.
    results = iter([_backtest(0.20, 0.01), _backtest(0.10, 0.01)]
                   + [_backtest(0.30, 0.05), _backtest(0.20, 0.05)] * 3)
    benchmark_calls = []

    def history(_db, symbol, *args, **kwargs):
        benchmark_calls.append(symbol)
        return prices, "test"

    with patch.object(experiments, "trusted_history", side_effect=history), \
         patch.object(experiments, "propose_parameter_experiments", return_value=proposals), \
         patch("app.services.backtester.run_backtest", side_effect=lambda *a, **k: next(results)) as backtest, \
         patch.object(experiments, "write_audit_log"), \
         patch.object(experiments, "list_strategy_experiments", return_value=[]):
        outcome = experiments.run_strategy_experiments(
            db, symbol="aapl", strategy_slug="moving_average_crossover", apply_promotions=True)

    assert benchmark_calls == ["AAPL", "SPY"]
    assert all("benchmark_prices" in call.kwargs for call in backtest.call_args_list)
    saved = [call.args[0] for call in db.add.call_args_list if hasattr(call.args[0], "decision")]
    assert len(saved) == 3
    for experiment in saved:
        summary = experiment.paper_result_summary
        assert summary["multiple_testing"]["in_sample"]["trials"] == 4
        assert summary["multiple_testing"]["validation"]["trials"] == 3
        assert experiment.decision == "needs_more_data"
        assert "deflated Sharpe" in summary["reason"]
    assert outcome["applied_parameters"] is None
