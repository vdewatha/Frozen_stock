from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.repair_paper_probe_precision import quantity_repair


@pytest.mark.parametrize("kind,raw", [("order", {"qty": None, "filled_qty": "0.013060394"}),
                                    ("fill", {"qty": "0.013060394"})])
def test_repair_requires_preserved_and_fresh_evidence(kind, raw):
    row = SimpleNamespace(id=1, quantity=Decimal("0.013060390"), raw_payload=raw)
    plan = quantity_repair(row, dict(raw), kind)
    assert plan["after"] == "0.013060394"
    assert row.quantity == Decimal("0.013060390")  # Planning never mutates.
    row.quantity = Decimal(plan["after"])
    assert quantity_repair(row, raw, kind) is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "0.0130603941"])
def test_invalid_quantities_rejected(value):
    raw = {"qty": value}
    row = SimpleNamespace(id=1, quantity=Decimal("0.013060390"), raw_payload=raw)
    with pytest.raises(ValueError):
        quantity_repair(row, raw, "fill")


def test_missing_or_changed_evidence_and_unexplained_values_rejected():
    raw = {"qty": "0.013060394"}
    row = SimpleNamespace(id=1, quantity=Decimal("0.013060390"), raw_payload=raw)
    for fresh in (None, {"qty": "0.013060393"}):
        with pytest.raises(ValueError):
            quantity_repair(row, fresh, "fill")
    row.quantity = Decimal("0.01306038")
    with pytest.raises(ValueError):
        quantity_repair(row, raw, "fill")
