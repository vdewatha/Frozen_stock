"""The owner's goal is profit after costs (2026-11-06); win rate is context only."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import governance, paper_trade_scorecard, strategy_scorecard


def _decide(**snapshot):
    base = {"sample_size": 25, "win_rate": 0.35, "profit_factor": 2.0, "avg_drawdown": -0.03,
            "confidence_score": 0.6}
    strategy = SimpleNamespace(id=1, name="Trend", current_status="paper_trading_active", strategy_type="trend")
    with patch.object(governance, "aggregate_strategy_memory", return_value=base | snapshot), \
            patch.object(governance, "write_audit_log"), patch.object(governance, "create_notification"):
        return governance.evaluate_single_strategy(MagicMock(), strategy)


def test_a_low_win_rate_strategy_that_makes_money_is_not_paused():
    result = _decide()
    assert result["decision"] == "hold" and "remains active" in result["reason"]


def test_losing_money_still_pauses_whatever_the_win_rate():
    assert _decide(win_rate=0.7, profit_factor=0.9)["decision"] == "pause"
    assert _decide(avg_drawdown=-0.12)["decision"] == "pause"


def test_research_goal_needs_profit_not_an_80_percent_win_rate():
    stats = {"trades": 200, "win_rate": 0.38, "expectancy_bps": 25.0, "expectancy_ci_bps": [6.0, 44.0],
             "win_rate_ci": [0.31, 0.45], "profit_factor": 1.6}
    goal = strategy_scorecard.goal_assessment(stats, min_trades=100)
    assert goal["meets_goal"] and goal["robust"] and goal["checks"]["win_rate_ge_80"] is False
    losing = stats | {"win_rate": 0.85, "expectancy_bps": -3.0, "expectancy_ci_bps": [-9.0, 2.0]}
    assert not strategy_scorecard.goal_assessment(losing, min_trades=100)["meets_goal"]


def _trade(pnl, minute):
    return {"net_pnl": pnl, "net_return": pnl / 1000, "fees": 0.0, "costs_known": True, "holding_minutes": 60,
            "exited_at": f"2026-11-06T15:{minute:02d}:00+00:00"}


def test_the_last_ten_trades_meet_the_goal_by_making_money():
    trades = [_trade(30.0, i) if i < 3 else _trade(-5.0, i) for i in range(10)]  # 3 wins of 10, +55
    goal = paper_trade_scorecard.goal_progress(trades)
    assert goal["target"] == "profit_after_costs" and goal["met_last_window"] is True
    assert "target_win_rate" not in goal
    assert paper_trade_scorecard.goal_progress([_trade(1.0, i) if i < 9 else _trade(-20.0, i)
                                                for i in range(10)])["met_last_window"] is False
