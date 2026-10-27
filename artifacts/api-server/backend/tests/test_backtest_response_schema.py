from app.schemas.trading import BacktestResponse


def test_backtest_response_keeps_evaluation_honesty_fields():
    payload = {
        "symbol": "AAPL", "strategy": "s", "source": "test", "total_return": 0.01, "annualized_return": 0.02,
        "sharpe_ratio": 0.5, "sortino_ratio": 0.6, "max_drawdown": 0.1, "win_rate": 0.5, "profit_factor": 1.2,
        "number_of_trades": 3, "score": 0.1, "rejected": True, "rejection_reasons": [], "equity_curve": [],
        "trades": [], "baselines": {"cash": {"total_return": 0.0}}, "excess_return": {"vs_cash": 0.01},
        "beats_baselines": {"cash": True}, "statistics": {"bootstrap": {"status": "ok"}},
        "exit_model": "intrabar_high_low_stop_first", "cost_model": {"name": "volatility_scaled"},
    }
    dumped = BacktestResponse(**payload).model_dump()
    assert dumped["baselines"] == {"cash": {"total_return": 0.0}}
    assert dumped["beats_baselines"] == {"cash": True}
    assert dumped["statistics"]["bootstrap"]["status"] == "ok"
    # Legacy payloads without the new sections remain valid.
    legacy = {key: value for key, value in payload.items()
              if key not in {"baselines", "excess_return", "beats_baselines", "statistics", "exit_model", "cost_model"}}
    assert BacktestResponse(**legacy).baselines is None
