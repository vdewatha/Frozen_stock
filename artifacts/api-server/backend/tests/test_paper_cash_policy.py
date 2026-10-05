from copy import deepcopy

import pytest

from app.services.alpaca_activity_v2 import reconcile_baseline
from app.services.alpaca_cash_precision import diagnose
from app.services.paper_cash_policy import EXACT, CENT, apply_policy
from app.services.stock_paper_performance import calculate_observed_change
from tests.test_alpaca_cash_precision import evidence


def reports(cash="100000"):
    baseline, rows, orders = evidence()
    report = reconcile_baseline(baseline, rows, cash, {}, previously_seen_ids=set())
    diagnostic = diagnose(baseline, rows, orders, cash, {}, currency="USD", broker="alpaca_paper")
    return report, diagnostic


def test_explicit_cent_policy_preserves_exact_evidence_without_cost_authority():
    report, diagnostic = reports()
    saved = deepcopy((report, diagnostic))
    assert apply_policy(report, diagnostic, EXACT, broker="alpaca_paper", currency="USD")["status"] == "mismatch"
    result = apply_policy(report, diagnostic, CENT, broker="alpaca_paper", currency="USD")
    assert result["status"] == "matched" and result["cash_residual"] == "0"
    assert result["unrounded_cash_residual"] == "0.000078362364"
    assert result["unrounded_cash_equation_matches"] is False
    assert result["all_in_costs"] == "unknown" and result["launch_authorized"] is False
    assert (report, diagnostic) == saved
    assert apply_policy(result, diagnostic, CENT, broker="alpaca_paper", currency="USD") == result
    assert apply_policy(result, diagnostic, EXACT, broker="alpaca_paper", currency="USD")["status"] == "mismatch"


@pytest.mark.parametrize("policy,broker,currency", [("unknown", "alpaca_paper", "USD"),
    (CENT, "live", "USD"), (CENT, "tradier_sandbox", "USD"), (CENT, "alpaca_paper", "EUR")])
def test_wrong_policy_or_scope_rejected(policy, broker, currency):
    with pytest.raises(ValueError):
        apply_policy(*reports(), policy, broker=broker, currency=currency)


@pytest.mark.parametrize("change", [{"status": "unavailable"}, {"inventory_matches": False},
    {"policy": "unknown"}])
def test_missing_or_ambiguous_evidence_cannot_match(change):
    report, diagnostic = reports()
    result = apply_policy(report, {**diagnostic, **change}, CENT, broker="alpaca_paper", currency="USD")
    assert result["status"] == "mismatch"


def test_missing_cent_or_mismatched_adjustment_not_hidden():
    assert apply_policy(*reports("99999.99"), CENT, broker="alpaca_paper", currency="USD")["status"] == "mismatch"
    report, diagnostic = reports()
    diagnostic["rounding_adjustment_since_baseline"] = "0.01"
    with pytest.raises(ValueError):
        apply_policy(report, diagnostic, CENT, broker="alpaca_paper", currency="USD")


def test_observed_performance_uses_same_explicit_policy_but_remains_provisional():
    baseline, rows, orders = evidence()
    kwargs = dict(opening_equity="100000", closing_equity="100000", closing_cash="100000", closing_positions={})
    assert calculate_observed_change(baseline, rows, **kwargs)["status"] == "unavailable"
    result = calculate_observed_change(baseline, rows, orders=orders, cash_policy=CENT, **kwargs)
    assert result["status"] == "provisional" and result["net_change"] == "0"
    assert result["costs_complete"] is False and result["qualifying"] is False
