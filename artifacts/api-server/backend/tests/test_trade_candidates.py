from app.services.trade_candidates import _backtest_supported


def test_backtest_with_insufficient_history_is_not_actionable():
    assert not _backtest_supported({
        "rejected": True,
        "score": 0.28,
        "rejection_reasons": ["Fewer than 30 historical trades."],
    })


def test_backtest_with_quality_failure_is_not_actionable():
    assert not _backtest_supported({
        "rejected": True,
        "score": 0.42,
        "rejection_reasons": ["Profit factor below 1.1."],
    })


def test_positive_unrejected_backtest_is_actionable():
    assert _backtest_supported({"rejected": False, "score": 0.22})
