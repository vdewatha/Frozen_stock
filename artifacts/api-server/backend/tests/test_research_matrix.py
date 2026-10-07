from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.research_simulator import simulate_research_matrix


def bars(count=4, offset=0):
    return [
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
            "open": 100 + offset,
            "high": 110 + offset,
            "low": 90 + offset,
            "close": 100 + offset,
            "volume": 1000,
            "tradable": True,
        }
        for i in range(count)
    ]


def test_matrix_covers_symbols_and_strategies_without_combining_capital():
    result = simulate_research_matrix([
        {"symbol": "spy", "strategy": "momentum", "rows": bars(), "decisions": [1, 0, 0, 0]},
        {"symbol": "AAPL", "strategy": "momentum", "rows": bars( offset=10), "decisions": [0, 0, 0, 0]},
        {"symbol": "SPY", "strategy": "mean_reversion", "rows": bars(), "decisions": [1, 0, 0, 0]},
    ])

    assert result["eligible_for_trading"] is False
    assert result["coverage"] == {
        "case_count": 3,
        "symbols": ["AAPL", "SPY"],
        "strategies": ["mean_reversion", "momentum"],
        "complete": False,
    }
    assert result["by_symbol"]["SPY"]["case_count"] == 2
    assert result["by_strategy"]["momentum"]["case_count"] == 2
    assert result["overall"]["case_count"] == 3
    assert all("cash" not in row for row in result["runs"])
    assert isinstance(result["overall"]["mean_return"], Decimal)


@pytest.mark.parametrize("cases", [[], None])
def test_matrix_requires_cases(cases):
    with pytest.raises(ValueError, match="At least one"):
        simulate_research_matrix(cases)


def test_matrix_rejects_duplicate_case():
    case = {"symbol": "SPY", "strategy": "momentum", "rows": bars(), "decisions": [0] * 4}
    with pytest.raises(ValueError, match="Duplicate"):
        simulate_research_matrix([case, case])
