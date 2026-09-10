"""Reproducible offline execution sensitivity; never execution permission."""
from dataclasses import replace
from decimal import Decimal
from app.services.research_simulator import SimulationConfig, simulate


def execution_stress(rows, decisions):
    """Hold signal generation fixed, perturb costs, latency and availability.

    Base fee reflects the published Kraken tier-1 taker schedule checked
    2026-09-06. Spread/slippage/participation are assumptions, not measurements.
    Each scenario starts with an independent simulated balance.
    """
    base = SimulationConfig(allocation=Decimal(".01"), fee_bps=Decimal("80"),
                            spread_bps=Decimal("10"), slippage_bps=Decimal("10"))
    scenarios = {
        "base_taker": (rows, decisions, base),
        "higher_costs": (rows, decisions, replace(base, fee_bps=Decimal("100"), slippage_bps=Decimal("20"))),
        "one_extra_bar_delay": (rows, [0] + list(decisions[:-1]), base),
        "thin_liquidity": (rows, decisions, replace(base, max_volume_participation=Decimal(".001"))),
        "intermittent_no_fills": ([dict(row, tradable=False) if index % 5 == 1 else dict(row)
                                    for index, row in enumerate(rows)], decisions, base),
    }
    results = {name: simulate(bars, signals, config) for name, (bars, signals, config) in scenarios.items()}
    return {"version": "execution-stress-v1", "mode": "offline_research", "eligible_for_trading": False,
            "fee_source": "https://www.kraken.com/features/fee-schedule", "fee_checked_on": "2026-09-06",
            "scenarios": results,
            "limitations": ["Not exchange fills or observed paper profits", "No queue position or order book reconstruction",
                            "Spread and slippage are assumed; costs must be rechecked for account tier"]}
